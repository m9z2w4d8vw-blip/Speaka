/*
 * Hooks.xm - Speaka inside bluetoothd: makes the iPhone a Bluetooth speaker (an A2DP Sink) for the PC.
 *
 * At launch it checks that this is the one bluetoothd build it was written for (BluetoothdBuild.h): the image's
 * UUID, then the first four instructions at every hook address, read with a fault-safe reader. If the code differs
 * anywhere, nothing is hooked. A mismatch is a log line, never a crash.
 *
 * Then it hooks, by offset:
 *   - always: five pass-through observers (endpoint registration, advertising, SetConfigInd, accept-SBC and
 *     publish), which forward all eight argument registers untouched and log the first calls;
 *   - once Start has armed a session (Session.c): the endpoint-config getter, to give the table a seventh slot;
 *     native registration, to register the Sink (SEID 7) after iOS's six Sources; the SDP record add, to publish the
 *     Audio Sink record (0x110B); AVDTP signal receive and transmit, to follow the PC's stream (StreamControl.c); the
 *     media handler, to copy the PC's SBC packets into the shared ring for the audio helper (AudioRelay.c); and the
 *     idle check, so bluetoothd doesn't suspend a stream the helper is playing.
 * Without a session, bluetoothd behaves as it does without the tweak. (Two diagnostic observers also install if
 * their preferences are set by hand: SpeakaSDPProbe and SpeakaV80ConfigLookupObserver. Nothing sets them now.)
 *
 * Hook addresses are function starts, the pacibsp itself: on arm64e a hook one instruction later double-signs the
 * return address, and the first call crashes. BluetoothdBuild.h was generated from the binary for that reason.
 *
 * AVDTP_RegisterMultipoint's own debug line prints its arguments in a different order from the registers: a0 is the
 * media type, a1 the endpoint type, a2 a pointer to numCaps 16-byte capability records, a3 numCaps, a4 the count,
 * and a5 points at a ONE-BYTE assigned SEID. Never reuse a Source's output pointer for the Sink, or read 8 bytes there.
 *
 * How each part was found: docs/BLUETOOTHD.md. What changed when: docs/CHANGELOG.md.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <time.h>
#include "tweak/Tweak.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <substrate.h>

#include "tweak/BluetoothdBuild.h"
#include "shared/SpeakaShared.h"
#include "shared/AACConfig.h"
#include <pthread.h>
#include <ptrauth.h>

#define TWEAK_BUILD_TAG "v8.8-continuous-sbc-ring"
/* v8.9 keeps the v8.8 build label, as v8.8.1 and v8.8.2 did; this separate tag identifies the one-tap
 * Start/Stop build in the status block (diag.v89.build) and the log. */
#define SESSION_BUILD_TAG "v9.6.0-one-tap-output-select"

/* AVDTP constants, used for native Source registration diagnostics */
#define AVDTP_TSEP_SRC 0u
#define AVDTP_TSEP_SNK 1u
#define AVDTP_MEDIA_AUDIO 0u

/* ------------------------------------------------------------------------------------------------ helpers */

/* All eight integer argument registers, returning x0. Anything the real function takes in x0..x7 is passed through
 * unchanged; floating-point and stack arguments are untouched because this wrapper never writes them. */
typedef uint64_t (*hook_fn_t)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);

static volatile unsigned g_calls[HOOK_COUNT];
static hook_fn_t         g_orig[HOOK_COUNT];

/* Read memory without being able to crash: returns 0 if the address is not readable. */
static int safe_peek(uint64_t addr, void *out, size_t n)
{
    if (addr < 0x1000ull || addr > 0x0000FFFFFFFFFFFFull) return 0;
    vm_size_t got = 0;
    kern_return_t kr = vm_read_overwrite(mach_task_self(), (vm_address_t)addr, (vm_size_t)n, (vm_address_t)out, &got);
    return kr == KERN_SUCCESS && got == (vm_size_t)n;
}

static int image_uuid(const struct mach_header_64 *mh, uint8_t out[16])
{
    if (!mh || mh->magic != MH_MAGIC_64 || mh->ncmds > 4096u) return 0;
    const uint8_t *p = (const uint8_t *)(mh + 1);
    for (uint32_t i = 0; i < mh->ncmds; i++) {
        const struct load_command *lc = (const struct load_command *)p;
        if (lc->cmdsize < sizeof(struct load_command)) return 0;       /* corrupt header: stop */
        if (lc->cmd == LC_UUID) {
            memcpy(out, ((const struct uuid_command *)lc)->uuid, 16);
            return 1;
        }
        p += lc->cmdsize;
    }
    return 0;
}

/* The daemon is the one image of type MH_EXECUTE. Its index is not guaranteed to be 0 when libraries are injected. */
static int find_main_image(uint32_t *index_out, const struct mach_header_64 **mh_out)
{
    uint32_t n = _dyld_image_count();
    for (uint32_t i = 0; i < n; i++) {
        const struct mach_header_64 *mh = (const struct mach_header_64 *)_dyld_get_image_header(i);
        if (mh && mh->magic == MH_MAGIC_64 && mh->filetype == MH_EXECUTE) {
            *index_out = i;
            *mh_out = mh;
            return 1;
        }
    }
    return 0;
}

static void uuid_string(const uint8_t u[16], char *out, size_t cap)
{
    snprintf(out, cap, "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-%02X%02X%02X%02X%02X%02X",
             u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7], u[8], u[9], u[10], u[11], u[12], u[13], u[14], u[15]);
}

static const char *tail_of(const char *s, size_t keep)
{
    if (!s) return "?";
    size_t n = strlen(s);
    return n > keep ? s + (n - keep) : s;
}

/* Log the first loaded images (index, address, type, UUID, path) so it is never a guess which one is which. */
static void log_images(void)
{
    uint32_t n = _dyld_image_count();
    SpeakaLogNow("loaded images: %u (listing the first %u)", n, n < 8u ? n : 8u);
    for (uint32_t i = 0; i < n && i < 8u; i++) {
        const struct mach_header_64 *mh = (const struct mach_header_64 *)_dyld_get_image_header(i);
        uint8_t u[16];
        char us[40] = "-";
        if (image_uuid(mh, u)) uuid_string(u, us, sizeof us);
        SpeakaLogLine("image[%u] %p type=%u uuid=%s %s", i, (const void *)mh, mh ? (unsigned)mh->filetype : 0u, us,
                tail_of(_dyld_get_image_name(i), 70));
    }
}

static void note_call(int idx, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
{
    unsigned n = __sync_add_and_fetch(&g_calls[idx], 1u);
    if (n <= 25u || (n % 200u) == 0u) {
        SpeakaLogLine("%s call #%u  x0..x5 = %#llx %#llx %#llx %#llx %#llx %#llx", HOOK_TARGETS[idx].name, n,
                (unsigned long long)a0, (unsigned long long)a1, (unsigned long long)a2,
                (unsigned long long)a3, (unsigned long long)a4, (unsigned long long)a5);
        char key[48];
        snprintf(key, sizeof key, "seen.%s", HOOK_TARGETS[idx].name);
        SpeakaSetStatus(key, "%u call(s)", n);
        SpeakaSetStatus("last_event", "%s #%u", HOOK_TARGETS[idx].name, n);
    }
}

/* Decode the copied 16-byte records from this particular firmware. Do not treat
 * these byte positions as an ABI or use them to construct new endpoints. */
static void log_caps(const uint8_t *buf, size_t len)
{
    avdtp_caps_info_t c;
    if (!avdtp_decode_caps(buf, len, &c)) {
        SpeakaLogLine("CAPS invalid or truncated, len=%zu; no decoding attempted", len);
        return;
    }
    SpeakaLogLine("CAPS records=%u transport=%u codec=%u delay=%u other=%u fingerprint=%08x",
            c.total_records, c.transport_records, c.codec_records, c.delay_records,
            c.unknown_records, c.fingerprint);
    if (c.codec_present) {
        SpeakaLogLine("CAPS codec=%s (code=0x%02x at record+2) media=%u specific_len=%u",
                avdtp_codec_name(c.codec_type), (unsigned)c.codec_type,
                (unsigned)c.media_type, (unsigned)c.codec_data_len);
        SpeakaSetStatus("diag.last_caps", "%s code=%02x len=%u hash=%08x",
                      avdtp_codec_name(c.codec_type), (unsigned)c.codec_type,
                      (unsigned)c.codec_data_len, c.fingerprint);
    } else {
        SpeakaLogLine("CAPS no codec record found");
    }
    SpeakaLogHexdump("caps", buf, len);
}

static uint64_t mach_elapsed_us(uint64_t start, uint64_t end)
{
    if (end < start) return 0;
    mach_timebase_info_data_t tb = {0, 0};
    (void)mach_timebase_info(&tb);
    if (!tb.denom) return 0;
    return ((end - start) * (uint64_t)tb.numer / (uint64_t)tb.denom) / 1000u;
}

static void note_return(int idx, uint64_t r)
{
    if (g_calls[idx] <= 25u)
        SpeakaLogLine("%s returned %#llx", HOOK_TARGETS[idx].name, (unsigned long long)r);
}

/* ------------------------------------------------------------------------------------------ pass-through hooks */

/* The four non-registration hooks remain pass-through. First 25 invocations include
 * timing and return codes; later calls are sampled to avoid stressing bluetoothd.
 * Return values are raw because these private ABIs are not yet characterized. */
#define PASSTHROUGH_OBSERVER(FN, IDX)                                                                            \
    static uint64_t FN(uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3,                                \
                       uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7)                                \
    {                                                                                                     \
        note_call(IDX, a0, a1, a2, a3, a4, a5);                                                           \
        unsigned n = g_calls[IDX];                                                                         \
        uint64_t started = (n <= 25u) ? mach_absolute_time() : 0u;                                       \
        uint64_t r = g_orig[IDX] ? g_orig[IDX](a0, a1, a2, a3, a4, a5, a6, a7) : 0;                       \
        note_return(IDX, r);                                                                              \
        if (started) {                                                                                   \
            uint64_t us = mach_elapsed_us(started, mach_absolute_time());                                 \
            SpeakaLogLine("TRACE %s #%u return=%#llx duration_us=%llu (private ABI; raw return)",              \
                    HOOK_TARGETS[IDX].name, n, (unsigned long long)r, (unsigned long long)us);            \
            char key[40];                                                                                 \
            snprintf(key, sizeof key, "diag.ret.%s", HOOK_TARGETS[IDX].name);                          \
            SpeakaSetStatus(key, "call=%u ret=%#llx duration_us=%llu", n,                                 \
                          (unsigned long long)r, (unsigned long long)us);                                 \
        }                                                                                                 \
        return r;                                                                                         \
    }

PASSTHROUGH_OBSERVER(hook_advertise,     HOOK_ADVERTISE)
PASSTHROUGH_OBSERVER(hook_setconfigind_observe, HOOK_SETCONFIGIND)
PASSTHROUGH_OBSERVER(hook_accept_sbc,    HOOK_ACCEPT_SBC)
PASSTHROUGH_OBSERVER(hook_publish,       HOOK_PUBLISH)

/* AVDTP_RegisterMultipoint: a0=media type, a1=TSEP, a2=caps pointer,
 * a3=number of 16-byte capability records, a4=count, a5=SEID output BYTE.
 *
 * The arm64e daemon stores the assigned SEID with `strb w8, [x19]` at
 * image+0x0ceee8. Do NOT treat a5 as a uint64_t output. An injected Sink
 * must never reuse Apple's Source SEID pointer.
 *
 * v5 safety: observe without extra registrations. Six native Source records
 * were previously observed, exhausting the six-entry endpoint table.
 * Adding even one Sink requires verifying a safe capacity expansion or
 * an explicit trade-off that removes one Source endpoint.
 */
static volatile unsigned g_native_source_success = 0;
/* Trial requires BOTH separate one-shot preferences. Never retry automatically. */
static int g_sink_armed = 0;
/* v9.3 AAC experiment: when on, SEID 7 advertises AAC-LC capabilities instead of SBC.
 * Does NOT register a second endpoint; just swaps the codec bytes in the one existing Sink SEP.
 * Fallback: if AAC negotiation fails (or Windows keeps sending SBC), set SpeakaAACSink=NO and restart BT. */
static int g_aac_sink_mode = 0;
/* The Sink's codec bytes live in a tail after its three 16-byte capability records: 4 for SBC, 6 for AAC-LC.
 * v9.8.84 sized the tail for SBC only (4 bytes), so AAC mode wrote 2 bytes past the allocation; this fixes it. */
#define SINK_CODEC_TAIL_BYTES 8u
typedef char sink_codec_tail_fits_aac[(AAC_CSI_BYTES <= SINK_CODEC_TAIL_BYTES) ? 1 : -1];
typedef char sink_codec_tail_fits_sbc[(4u <= SINK_CODEC_TAIL_BYTES) ? 1 : -1];
static volatile unsigned g_sink_attempted = 0;
static SinkCapabilities g_sink_caps;
static volatile unsigned g_sink_caps_ready = 0;
static volatile unsigned g_sink_accepted = 0;
static volatile uint32_t g_sink_sdp_handle = 0;
static volatile unsigned g_sink_sdp_registered = 0;
static hook_fn_t g_signal_rx_orig = NULL;
static volatile unsigned g_signal_rx_calls = 0;
static volatile unsigned g_signal_rx_hook_ok = 0;
static volatile unsigned g_sdp_hook_ok = 0;
static hook_fn_t g_signal_tx_orig = NULL;
static volatile unsigned g_signal_tx_calls = 0;
static volatile unsigned g_signal_tx_hook_ok = 0;

/* v8.7: one-shot raw media-entry argument counter only.  No channel
 * structures, packet pointers, RTP parsing, or media forwarding. */
static hook_fn_t g_media_orig = NULL;
static volatile unsigned g_media_hook_ok = 0;
static volatile unsigned g_media_calls = 0;
static volatile unsigned long long g_media_bytes = 0;

typedef struct {
    volatile unsigned sequence;   /* published last, with release ordering */
    volatile unsigned cid;        /* raw w0 from L2CAP media-handler entry */
    volatile unsigned bytes;      /* raw w2 from handler entry */
} MediaHistory;
static MediaHistory g_media_recent[8];

/* Existing v8.5 TX/RX signaling correlation remains unchanged. */
static volatile unsigned g_start_pending = 0;
static volatile unsigned g_start_label = 0;
static volatile uint64_t g_start_signal_cid = 0;
static volatile unsigned g_sink_stream_started = 0;
static volatile uint64_t g_sink_stream_signal_cid = 0;
static volatile uint64_t g_sink_media_cid = 0;
/* v8.9: the PC's AVDTP signaling channel, learned from its SET_CONFIGURATION for our Sink SEID 7. Once known,
 * only that channel can start or stop the forwarded stream, so signaling with other devices (for example
 * earbuds now used as the audio output) can never toggle it, even if their SEID happens to match. */
static volatile uint64_t g_pc_signal_cid = 0;


/* SetConfigInd x2 is the native stream handle; x3/x4 are local/remote
 * SEIDs. Learn the handle here, never by calling the private CID lookup on RX. */
static unsigned g_relay_ready, g_sink_stream_handle;
static uint64_t g_sink_owner;
static uint64_t g_media_callbacks[27];
static StreamControl g_stream_control;
static pthread_mutex_t g_stream_control_lock = PTHREAD_MUTEX_INITIALIZER;
/* The codec the PC chose in its SET_CONFIGURATION for our Sink (note_pc_configuration), read by SetConfigInd. */
static struct { unsigned valid, codec; uint8_t info[16]; size_t info_len; } g_pc_config;
static uint64_t hook_setconfigind(uint64_t a0,uint64_t a1,uint64_t a2,uint64_t a3,
    uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7)
{
    if (g_relay_ready && g_sink_armed && a3 == 7u && a2 > 0u && a2 <= 255u && a4 > 0u && a4 < 63u) {
        pthread_mutex_lock(&g_stream_control_lock);
        AudioRelaySetActive(0);
        __atomic_store_n(&g_sink_owner,a0,__ATOMIC_RELEASE);
        __atomic_store_n(&g_sink_stream_handle,(unsigned)a2,__ATOMIC_RELEASE);
        memset(&g_stream_control,0,sizeof g_stream_control);
        g_stream_control.local_seid=7;g_stream_control.remote_seid=(unsigned)a4;
        /* Tell the helper the codec the PC chose, read from its SET_CONFIGURATION (note_pc_configuration). An AAC
         * configuration goes with its 6 Codec Specific bytes; any other non-SBC codec is published as it is, so the
         * helper refuses it rather than decoding it as SBC. If the PDU wasn't seen: SBC, the only codec SEID 7 offers. */
        unsigned codec=g_pc_config.valid?g_pc_config.codec:A2DP_CODEC_SBC;
        if(codec==A2DP_CODEC_AAC && g_pc_config.info_len==AAC_CSI_BYTES)
            AudioRelaySetCodec(A2DP_CODEC_AAC,g_pc_config.info,AAC_CSI_BYTES);
        else if(codec!=A2DP_CODEC_SBC)
            AudioRelaySetCodec(codec,g_pc_config.info,g_pc_config.info_len);
        else
            AudioRelaySetCodec(A2DP_CODEC_SBC,NULL,0);
        unsigned seen=g_pc_config.valid;
        pthread_mutex_unlock(&g_stream_control_lock);
        SpeakaLogNow("V88_CONFIG handle=%u local_seid=7 remote_seid=%u codec=%s%s",(unsigned)a2,(unsigned)a4,
                     avdtp_codec_name((uint8_t)codec),seen?"":" (assumed: SET_CONFIGURATION not seen)");
        /* v9.6: the PC's side of the speaker's stream, for the app's connection line (diag.v95.peer). */
        SpeakaSetStatus("diag.v95.peer","configured: the PC set up the speaker's stream");
    }
    return hook_setconfigind_observe(a0,a1,a2,a3,a4,a5,a6,a7);
}
static void note_signal(unsigned direction,uint64_t cid,const uint8_t *p,size_t size)
{
    if (!g_relay_ready || !g_sink_accepted) return;
    uint64_t sink_cid=__atomic_load_n(&g_pc_signal_cid,__ATOMIC_ACQUIRE);
    if (sink_cid && cid!=sink_cid) return;
    pthread_mutex_lock(&g_stream_control_lock);
    int change=stream_control_signal(&g_stream_control,direction,cid,p,size);
    if(change>=0) {
        AudioRelaySetActive(change);
        /* v9.1: by= names who sent the command this line belongs to. A command (msg=0) is its sender's; a response
         * answers the other side's command. In the 23:33 loop every START and SUSPEND was by=phone: Apple's own A2DP
         * code driving the PC's stream as if the iPhone were the source. Logging only; nothing is blocked. */
        SpeakaLogNow("V882_CONTROL direction=%s CID=%#llx tx=%u msg=%u sig=%u active=%d by=%s",
            direction?"TX":"RX",(unsigned long long)cid,p[0]>>4,p[0]&3u,p[1]&63u,change,
            ((direction==1u)==((p[0]&3u)==0u))?"phone":"pc");
        /* v9.6: a START accepted streams; SUSPEND is a pause, and the PC is still connected; CLOSE or ABORT ends it.
         * (A link that just drops sends none of these, so the last one stays until bluetoothd restarts.) */
        SpeakaSetStatus("diag.v95.peer","%s",change==1?"streaming":(p[1]&63u)==9u?"paused":"closed");
    }
    pthread_mutex_unlock(&g_stream_control_lock);
}

/* v8.8.2: the verified native idle check assumes mediaserverd owns playback.
 * x0 is the same owner as SetConfigInd x0; x1 is its x2 map key/handle (the
 * lookup at +0x3747F0 is identical in both paths). No object fields are read.
 * Only this idle check is skipped for a healthy, explicitly started helper
 * session. Explicit Suspend/Close/Abort and other connections remain native. */
#define IDLE_CHECK_ENTRY 0x00376034ul
static const struct { uintptr_t offset; uint32_t words[4]; } IDLE_CHECK_GATES[] = {
    {0x376034u,{0xd503237fu,0xd10183ffu,0xa9025ff8u,0xa90357f6u}},
    {0x376068u,{0xaa1503e0u,0xaa1403e1u,0x97fff9e0u,0xaa0003f3u}},
    {0x37607cu,{0x91060260u,0xb942ca76u,0x9400e58bu,0x79001fe0u}},
    {0x3760d8u,{0xaa1303e0u,0x940874afu,0x370004e0u,0xf94002f6u}},
    {0x376138u,{0x91003be0u,0x52800021u,0xd2800002u,0x97f56f29u}},
    {0x37aeccu,{0xaa1a03e0u,0xaa1903e1u,0x97ffe647u,0xaa0003f5u}},
    {0x593398u,{0x394c0000u,0xd65f03c0u,0xaa0103e2u,0xaa0003e1u}}
};
typedef void (*idle_check_fn_t)(uint64_t,uint64_t);
static idle_check_fn_t g_idle_orig;
static unsigned g_idle_kept;
static void hook_idle_check(uint64_t owner,uint64_t key)
{
    unsigned handle=__atomic_load_n(&g_sink_stream_handle,__ATOMIC_ACQUIRE);
    int sink=g_relay_ready && g_sink_armed && g_sink_accepted && handle &&
        owner==__atomic_load_n(&g_sink_owner,__ATOMIC_ACQUIRE) && key==handle;
    if(sink && AudioRelaySinkHealthy()) {
        unsigned kept=__sync_add_and_fetch(&g_idle_kept,1u);
        SpeakaSetStatus("diag.v882.idle_guard","v8.8.2 kept=%u last_handle=%u; healthy helper replaced native idle decision",kept,handle);
        SpeakaLogNow("V882_IDLE_GUARD action=keep handle=%u reason=live_helper; native mediaserverd-only idle check skipped",handle);
        return;
    }
    if(sink) SpeakaLogNow("V882_IDLE_GUARD action=pass handle=%u reason=inactive_or_unhealthy",handle);
    if(g_idle_orig) g_idle_orig(owner,key);
}
static void install_idle_guard(uintptr_t base,int uuid_ok)
{
    if(!g_relay_ready || !g_sink_armed || !uuid_ok) return;
    for(unsigned i=0;i<sizeof IDLE_CHECK_GATES/sizeof IDLE_CHECK_GATES[0];i++) {
        uint32_t words[4];
        if(!safe_peek(base+IDLE_CHECK_GATES[i].offset,words,sizeof words) ||
           memcmp(words,IDLE_CHECK_GATES[i].words,sizeof words)) {
            SpeakaLogNow("V882_IDLE_GUARD refused: signature mismatch at +%#lx",(unsigned long)IDLE_CHECK_GATES[i].offset);
            SpeakaSetStatus("diag.v882.idle_guard","REFUSED: exact-image idle-check gate failed");return;
        }
    }
    void *trampoline=NULL;
    MSHookFunction((void *)(base+IDLE_CHECK_ENTRY),(void *)hook_idle_check,&trampoline);
    if(!trampoline) {
        SpeakaSetStatus("diag.v882.idle_guard","FAILED: no idle-check trampoline");return;
    }
    g_idle_orig=(idle_check_fn_t)trampoline;
    SpeakaSetStatus("diag.v882.idle_guard","v8.8.2 installed: configured Sink only, requires fresh media + helper rendering");
    SpeakaLogNow("V882_IDLE_GUARD installed at +0x376034; one-shot, owner/handle scoped, health lease required");
}

/* Existing SDP insertion routine, shared with the v8.3 post-registration path. */
typedef uint64_t (*sdp_add_record_fn_t)(uint64_t, uint64_t);
static sdp_add_record_fn_t g_sdp_orig = NULL;

/* v8.3: the higher-level audio table is separate from the AVDTP SEP table.
 * One-shot hook is installed at the exact verified registration-loop entry.
 * It calls the native loop first, THEN expands the audio table to seven.
 */
#define SOURCE_REGISTRATION_ENTRY 0x003b4128ul
static const uint32_t SOURCE_REGISTRATION_SIG[4] = {
    0xd503237fu, 0xd10103ffu, 0xa90157f6u, 0xa9024ff4u
};
typedef uint64_t (*source_registration_fn_t)(uint64_t);
static source_registration_fn_t g_source_reg_orig = NULL;
static volatile unsigned g_source_reg_hook_ok = 0;
static volatile unsigned g_audio_table_extended = 0;
static void *g_leaked_original_table = NULL; /* intentionally retained; see docs/BLUETOOTHD.md, section 10 */


/* Optional one-shot Sink SEP capacity experiment. Unlike v4, this does not
 * consume slots before Apple's six native Sources. Disabled by default;
 * the OFF legacy flag is a hard prerequisite. This never publishes SDP. */


/* ONLY address- and signature-validated reads; NEVER written or hooked. */
static uintptr_t g_image_base = 0;
static int g_exact_build = 0;
static volatile unsigned g_table_snapshots = 0;

static int table_signature_matches(void)
{
    /* Each exact instruction is drawn from the user's verified bluetoothd binary.
       The table layout is invalid for any different firmware build. */
    struct sig_at { uintptr_t offset; uint32_t word; } s[] = {
        {0x0cede4u, 0xf9445b28u}, /* ldr x8, [x25,#0x8b0]: AVDTP global */
        {0x0cede8u, 0x3941911au}, /* ldrb w26,[x8,#0x64]: SEID cursor */
        {0x0cee08u, 0x3941810au}, /* ldrb w10,[x8,#0x60]: slot capacity */
        {0x0cee14u, 0x52800d00u}, /* mov w0,#0x68 on exhausted search */
        {0x0cee54u, 0x385fb36eu}, /* ldurb w14,[x27,#-5]: slot marker */
        {0x0cee60u, 0x9100a37bu}, /* add x27,x27,#0x28: slot stride */
    };
    if (!g_exact_build || !g_image_base) return 0;
    for (size_t i = 0; i < sizeof s / sizeof s[0]; ++i) {
        uint32_t word = 0;
        if (!safe_peek((uint64_t)(g_image_base + s[i].offset), &word, sizeof word) ||
            word != s[i].word) return 0;
    }
    return 1;
}

static void snapshot_table_capacity(void)
{
    /* Called once, only after Apple's six Source registrations returned zero.
       No private stack calls, writes, sockets, locks or allocations. */
    if (!__sync_bool_compare_and_swap(&g_table_snapshots, 0u, 1u)) return;
    if (!table_signature_matches()) {
        SpeakaSetStatus("diag.v78_table", "UNAVAILABLE: UUID or signature mismatch");
        SpeakaLogNow("V78_TABLE SKIPPED: exact binary UUID/signatures are required");
        return;
    }
    uint64_t state = 0, slot_ptr = 0;
    uint8_t count = 0, next = 0;
    /* ADRP x25,#0x976000; LDR x8,[x25,#0x8b0] (not an exported API). */
    if (!safe_peek((uint64_t)(g_image_base + 0x9768b0u), &state, sizeof state) ||
        !safe_peek(state + 0x0u, &slot_ptr, sizeof slot_ptr) ||
        !safe_peek(state + 0x60u, &count, sizeof count) ||
        !safe_peek(state + 0x64u, &next, sizeof next) ||
        !count || count > SINK_TABLE_MAX_SLOTS) {
        SpeakaSetStatus("diag.v78_table", "UNREADABLE: table metadata missing or unsafe");
        SpeakaLogNow("V78_TABLE UNREADABLE: capacity metadata was not readable");
        return;
    }
    uint8_t slots[SINK_TABLE_MAX_SLOTS * SINK_TABLE_SLOT_BYTES];
    const size_t len = (size_t)count * SINK_TABLE_SLOT_BYTES;
    SinkTableCapacity capacity;
    if (!safe_peek(slot_ptr, slots, len) ||
        sink_analyze_table(slots, len, count, &capacity) != 0) {
        SpeakaSetStatus("diag.v78_table", "UNREADABLE: slot bytes inaccessible");
        SpeakaLogNow("V78_TABLE UNREADABLE: slot bytes inaccessible");
        return;
    }
    SpeakaSetStatus("diag.v78_table", "READ-ONLY slots=%u occupied=%u free=%u nextSEID=%u duplicates=%u",
                  capacity.total, capacity.occupied, capacity.vacant,
                  (unsigned)next, capacity.duplicate_seids);
    SpeakaSetStatus("diag.capacity", "verified endpoint table: slots=%u occupied=%u free=%u (read-only)",
                  capacity.total, capacity.occupied, capacity.vacant);
    SpeakaLogNow("V78_TABLE RESULT slots=%u occupied=%u free=%u nextSEID=%u duplicates=%u; no endpoint inserted",
               capacity.total, capacity.occupied, capacity.vacant,
               (unsigned)next, capacity.duplicate_seids);
    for (unsigned i = 0; i < capacity.total; i++)
        SpeakaLogLine("V78_TABLE slot[%u] SEID=%u", i, (unsigned)capacity.seids[i]);
}




/* v7.9: Exact-image static instructions identify the constructor which
 * obtains AVDTP configuration block #3, then allocates three independent
 * arrays of config byte [0]*40, [1]*144, and [2]*64 bytes. We ONLY read
 * the registered configuration pointer and the already-live context.
 * There are NO allocator hooks, calls into private init, or changes to counts.
 */
static int g_config_hook_for_audit(void);
static volatile unsigned g_allocation_snapshots = 0;
/* Captured before the optional getter hook changes its prologue. */
static int g_initial_signature_verified = 0;
static int allocator_signatures_match(void)
{
    if (!g_exact_build || !g_image_base) return 0;
    struct sig_at { uintptr_t offset; uint32_t word; } s[] = {
        {0x0ce3f8u, 0x52800060u}, /* mov w0,#3: AVDTP configuration slot */
        {0x0ce3fcu, 0x94049ec9u}, /* bl config getter */
        {0x0ce408u, 0x52800d00u}, /* mov w0,#0x68: context allocation */
        {0x0ce41cu, 0x394002e8u}, /* ldrb w8,[x23]: endpoint count */
        {0x0ce424u, 0xd37df100u}, /* lsl x0,x8,#3 (after x8=5*count) */
        {0x0ce428u, 0x97ff6f1du}, /* bl allocator: endpoint bytes */
        {0x0ce434u, 0xf9000018u}, /* str x24,[x0]: endpoint pointer */
        {0x0ce470u, 0xd37ced00u}, /* lsl x0,x8,#4: secondary bytes */
        {0x0ce4d4u, 0xd37ae500u}, /* lsl x0,x8,#6: tertiary bytes */
        {0x0ce4f4u, 0x39018008u}, /* strb w8,[x0,#0x60]: live slot count */
        {0x1f5f20u, 0xd503237fu}, /* PAC-protected configuration lookup */
        {0x1f5f48u, 0xf8735a80u}, /* ldr x0,[x20,w19,uxtw #3] */
    };
    for (size_t i = 0; i < sizeof s / sizeof s[0]; ++i) {
        uint32_t word = 0;
        /* ElleKit/Substrate rewrites the getter prologue. Verify its ORIGINAL
         * four words before installing the hook; do not re-read a patched
         * instruction and mistake it for a different iOS build. All other
         * initializer/lookup instructions are still checked live. */
        if (s[i].offset == 0x1f5f20u && g_config_hook_for_audit()) {
            if (!g_initial_signature_verified) return 0;
            continue;
        }
        if (!safe_peek((uint64_t)(g_image_base + s[i].offset), &word, sizeof word) ||
            word != s[i].word) return 0;
    }
    return 1;
}
static void snapshot_allocation(void)
{
    if (!__sync_bool_compare_and_swap(&g_allocation_snapshots, 0u, 1u)) return;
    if (!allocator_signatures_match()) {
        SpeakaSetStatus("diag.v79_allocator", "UNAVAILABLE: exact UUID or instruction signatures differ");
        SpeakaLogNow("V79_ALLOC SKIPPED: cannot verify exact initializer and lookup instructions");
        return;
    }
    /* The config getter uses pointer table image+0x984a48 and index 3.
       We READ pointer table entry image+0x984a60, never call the getter. */
    uint64_t config_ptr = 0, context_ptr = 0;
    uint8_t config[3] = {0}, live[3] = {0};
    uint64_t tables[3] = {0};
    if (!safe_peek((uint64_t)(g_image_base + 0x984a60u), &config_ptr, sizeof config_ptr) ||
        !config_ptr || !safe_peek(config_ptr, config, sizeof config) ||
        !safe_peek((uint64_t)(g_image_base + 0x9768b0u), &context_ptr, sizeof context_ptr) ||
        !context_ptr || !safe_peek(context_ptr, tables, sizeof tables) ||
        !safe_peek(context_ptr + 0x60u, live, sizeof live)) {
        SpeakaSetStatus("diag.v79_allocator", "UNREADABLE: config pointer or live allocation metadata");
        SpeakaLogNow("V79_ALLOC UNREADABLE: config pointer or live allocation metadata");
        return;
    }
    unsigned present = 0;
    for (unsigned i = 0; i < 3; i++) if (tables[i]) present |= (1u << i);
    SinkAllocation result;
    int rc = sink_analyze_allocation(config, live, present, &result);
    if (rc != 0) {
        SpeakaSetStatus("diag.v79_allocator", "INVALID: config/context counts are out of bounds (code=%d)", rc);
        SpeakaLogNow("V79_ALLOC INVALID: bounded geometry rejected rc=%d", rc);
        return;
    }
    SpeakaSetStatus("diag.v79_allocator",
        "READ-ONLY config=(%u,%u,%u) live=(%u,%u,%u) request_bytes=(%u,%u,%u) match=%s missing_mask=%u",
        result.configured[0], result.configured[1], result.configured[2],
        result.observed[0], result.observed[1], result.observed[2],
        result.bytes[0], result.bytes[1], result.bytes[2],
        result.config_matches_context ? "yes" : "NO", result.missing_table_mask);
    SpeakaLogNow("V79_ALLOC RESULT config=(%u,%u,%u) live=(%u,%u,%u) request_bytes=(%u,%u,%u) match=%s missing_mask=%u; READ ONLY",
        result.configured[0], result.configured[1], result.configured[2],
        result.observed[0], result.observed[1], result.observed[2],
        result.bytes[0], result.bytes[1], result.bytes[2],
        result.config_matches_context ? "yes" : "NO", result.missing_table_mask);
    SpeakaLogNow("V79_ALLOC NOTE: constructor uses config[0]*40, config[1]*144, config[2]*64; byte counts are allocation REQUESTS, not measured heap extents");
}

/* v8.0/v8.1: Optional config getter observer or one-shot shadow override.
 * AVDTP init calls this exact function (image+0x1f5f20) with index 3 to
 * obtain the three count bytes BEFORE allocating the three AVDTP arrays.
 * In ordinary observation mode no count or pointer changes. In explicitly
 * armed one-shot mode, the first validated index-3 call returns a static
 * 32-byte copy with only the first count byte changed from 6 to 7. */
#define CONFIG_GETTER_ENTRY 0x001f5f20u
static const uint32_t CONFIG_GETTER_SIG[4] = {
    0xd503237fu, 0xa9be4ff4u, 0xa9017bfdu, 0x910043fdu
};
static hook_fn_t g_config_get_orig = NULL;
static volatile unsigned g_config_get_calls = 0;
static volatile unsigned g_config_index3_calls = 0;
static int g_config_hook_enabled = 0;
static int g_seven_armed = 0;
static volatile unsigned g_seven_applied = 0;
static volatile unsigned g_config_shadow_returned = 0;
/* Original object at exact-image __TEXT,__const +0x774010 has a bounded
 * 32-byte configuration record. This shadow has static process lifetime. */
static uint8_t g_config_shadow[SINK_CONFIG_COPY_BYTES] __attribute__((aligned(16)));
static int g_config_hook_for_audit(void) { return g_config_hook_enabled; }

/* Consumed before installing ANY private hook. Missing seven-slot arm fails closed. */
static int consume_sink_arm(void)
{
    CFPropertyListRef v = CFPreferencesCopyAppValue(CFSTR("SpeakaV82DecisiveOnce"),
                                                   CFSTR(SPEAKA_PREFS_DOMAIN));
    int requested = v && CFGetTypeID(v) == CFBooleanGetTypeID() &&
                    CFBooleanGetValue((CFBooleanRef)v);
    if (v) CFRelease(v);
    if (!requested) return 0;
    CFPreferencesSetAppValue(CFSTR("SpeakaV82DecisiveOnce"), kCFBooleanFalse,
                             CFSTR(SPEAKA_PREFS_DOMAIN));
    Boolean saved = CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    v = CFPreferencesCopyAppValue(CFSTR("SpeakaV82DecisiveOnce"), CFSTR(SPEAKA_PREFS_DOMAIN));
    int clear = v && CFGetTypeID(v) == CFBooleanGetTypeID() && !CFBooleanGetValue((CFBooleanRef)v);
    if (v) CFRelease(v);
    SpeakaSetStatus("diag.v82.arm", saved && clear ? "one-shot CONSUMED" : "REFUSED: cannot verify disarm");
    return saved && clear;
}


/* A request is CONSUMED before the hook can run. Do not retry automatically
 * on daemon relaunch; fail closed if the preference cannot be cleared. */
static int consume_seven_slots_arm(void)
{
    CFPropertyListRef initial = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE),
                                                          CFSTR(SPEAKA_PREFS_DOMAIN));
    int requested = initial && CFGetTypeID(initial) == CFBooleanGetTypeID() &&
                    CFBooleanGetValue((CFBooleanRef)initial);
    if (initial) CFRelease(initial);
    if (!requested) return 0;
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE), kCFBooleanFalse,
                             CFSTR(SPEAKA_PREFS_DOMAIN));
    Boolean synced = CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    CFPropertyListRef check = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE),
                                                        CFSTR(SPEAKA_PREFS_DOMAIN));
    int disarmed = check && CFGetTypeID(check) == CFBooleanGetTypeID() &&
                   !CFBooleanGetValue((CFBooleanRef)check);
    if (check) CFRelease(check);
    if (!synced || !disarmed) {
        SpeakaSetStatus("diag.v81_seven_slots", "REFUSED: cannot persist one-shot disarm");
        SpeakaLogNow("V81_CFG REFUSED: did not verify persistent one-shot disarm");
        return 0;
    }
    SpeakaLogNow("V81_CFG ARM CONSUMED: preference set OFF before any count changes");
    SpeakaSetStatus("diag.v81_seven_slots", "ARMED for next matching index-3 lookup (one shot)");
    return 1;
}

/* This function is the ONLY path returning a changed config pointer.
 * Refuse whenever the exact original source/data state is not expected. */
static uint64_t try_seven_slots(uint64_t original, unsigned hit)
{
    if (!g_seven_armed || hit != 1u ||
        !__sync_bool_compare_and_swap(&g_seven_applied, 0u, 1u)) return original;
    uint8_t source[SINK_CONFIG_COPY_BYTES] = {0};
    uint64_t table_original = 0;
    int good = g_initial_signature_verified && g_exact_build &&
        safe_peek((uint64_t)(g_image_base + 0x984a60u), &table_original, sizeof table_original) &&
        table_original == original &&
        safe_peek(original, source, sizeof source) &&
        sink_prepare_config_shadow(source, sizeof source, g_config_shadow, 1) == 0;
    if (!good) {
        SpeakaSetStatus("diag.v81_seven_slots", "REFUSED: config pointer/signature/count not exact");
        SpeakaLogNow("V81_CFG REFUSED index=3: preflight mismatch; ORIGINAL pointer returned");
        return original;
    }
    g_config_shadow_returned = 1u;
    SpeakaSetStatus("diag.v81_seven_slots", "OVERRIDE APPLIED once: config shadow=(7,3,3); verify live slots");
    SpeakaLogNow("V81_CFG SHADOW index=3 original=(6,3,3) returned=(7,3,3) pointer=STATIC; awaiting live audit");
    return (uint64_t)(uintptr_t)g_config_shadow;
}

static uint64_t hook_get_config(uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3,
                                         uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7)
{
    /* Always call the original first; pass it through unless v8.1 was explicitly armed. */
    uint64_t value = g_config_get_orig ?
        g_config_get_orig(a0,a1,a2,a3,a4,a5,a6,a7) : 0;
    __sync_add_and_fetch(&g_config_get_calls, 1u);
    if ((uint32_t)a0 != 3u) return value;
    unsigned hit = __sync_add_and_fetch(&g_config_index3_calls, 1u);
    if (hit > 4u) return value;
    uint8_t cfg[3] = {0};
    uint64_t in_table = 0;
    int read_ok = value && safe_peek(value, cfg, sizeof cfg);
    int table_ok = safe_peek((uint64_t)(g_image_base + 0x984a60u), &in_table, sizeof in_table);
    SpeakaLogNow("V80_CFG_GET index=3 call=%u result=%#llx readable=%s bytes=(%u,%u,%u) matches_table=%s; original getter result (trial may subsequently return a shadow)",
               hit, (unsigned long long)value, read_ok ? "yes" : "no",
               (unsigned)cfg[0], (unsigned)cfg[1], (unsigned)cfg[2],
               (table_ok && value == in_table) ? "yes" : "no");
    SpeakaSetStatus("diag.v80_config_lookup",
                  "OBSERVED index=3 calls=%u bytes=(%u,%u,%u) getter_matches_table=%s; original read-only",
                  hit,(unsigned)cfg[0],(unsigned)cfg[1],(unsigned)cfg[2],
                  (table_ok && value == in_table) ? "yes" : "no");
    if (g_seven_armed) return try_seven_slots(value, hit);
    return value;
}

static int config_observer_requested(void)
{
    CFPropertyListRef v = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_CONFIG_OBSERVER),
                                                   CFSTR(SPEAKA_PREFS_DOMAIN));
    int enabled = v && CFGetTypeID(v) == CFBooleanGetTypeID() &&
                  CFBooleanGetValue((CFBooleanRef)v);
    if (v) CFRelease(v);
    return enabled;
}

static void install_config_hook(uintptr_t base, int uuid_ok)
{
    /* Disarm the one-shot setting even if later gates refuse installation. */
    g_seven_armed = consume_seven_slots_arm();
    int decisive_requested = consume_sink_arm();
    g_sink_armed = decisive_requested && g_seven_armed && !SpeakaExperimentsEnabled();
    /* AAC sink experiment: read once and log; never changes within a session. */
    {
        CFPropertyListRef aac_pref = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_AAC_SINK), CFSTR(SPEAKA_PREFS_DOMAIN));
        g_aac_sink_mode = (aac_pref && CFGetTypeID(aac_pref) == CFBooleanGetTypeID() &&
                           CFBooleanGetValue((CFBooleanRef)aac_pref)) ? 1 : 0;
        if (aac_pref) CFRelease(aac_pref);
        SpeakaLogNow("V93 CODEC_MODE %s", g_aac_sink_mode ? "AAC-sink (SEID 7 will advertise AAC-LC)" : "SBC-sink (default)");
    }
    SpeakaSetStatus("diag.v82.trial", g_sink_armed ?
        "ARMED: one-shot seven slots + one Sink SEP + local SDP candidate + signaling RX" :
        (decisive_requested ? "REFUSED: seven-slot arm absent or legacy ON" : "OFF (default)"));
    if (decisive_requested && !g_sink_armed) SpeakaLogNow("V82 REFUSED: both independent one-shot arms required; legacy OFF");
    if (!config_observer_requested() && !g_seven_armed) {
        SpeakaSetStatus("diag.v80_config_lookup", "OFF (default), no extra hook");
        SpeakaLogNow("V80_CFG_GET observer OFF; no config getter hook installed");
        return;
    }
    if (!uuid_ok || !g_orig[HOOK_REGISTER] || !g_orig[HOOK_ADVERTISE]) {
        SpeakaSetStatus("diag.v80_config_lookup", "REFUSED: exact UUID and baseline hooks required");
        return;
    }
    /* Must be checked BEFORE MSHookFunction rewrites the getter prologue. */
    g_initial_signature_verified = allocator_signatures_match();
    if (!g_initial_signature_verified) {
        SpeakaSetStatus("diag.v80_config_lookup", "REFUSED: initializer/getter static signature mismatch");
        SpeakaSetStatus("diag.v81_seven_slots", "REFUSED: initializer/getter static signature mismatch");
        SpeakaLogNow("V81_CFG REFUSED: binary preflight signature mismatch");
        return;
    }
    uint32_t observed[4] = {0};
    uintptr_t target = base + CONFIG_GETTER_ENTRY;
    if (!safe_peek(target, observed, sizeof observed) ||
        memcmp(observed, CONFIG_GETTER_SIG, sizeof observed) != 0) {
        SpeakaSetStatus("diag.v80_config_lookup", "REFUSED: config getter signature mismatch");
        SpeakaLogNow("V80_CFG_GET refused: getter instruction signature mismatch");
        return;
    }
    void *trampoline = NULL;
    MSHookFunction((void *)target, (void *)hook_get_config, &trampoline);
    if (!trampoline) {
        SpeakaSetStatus("diag.v80_config_lookup", "FAILED: no trampoline, switch OFF");
        SpeakaLogNow("V80_CFG_GET FAILED: hook engine returned no trampoline");
        return;
    }
    g_config_get_orig = (hook_fn_t)trampoline;
    g_config_hook_enabled = 1;
    SpeakaSetStatus("diag.v80_config_lookup", "ARMED: pass-through getter observer, awaiting index=3");
    SpeakaLogNow("V80_CFG_GET installed at image+0x1f5f20; one-shot seven=%s; otherwise original return unchanged", g_seven_armed ? "ARMED" : "OFF");
}

/* The codec the PC chose for our Sink, from its SET_CONFIGURATION (AVDTP 8.9, parsed by StreamControl.c): protocol
 * bytes the RX hook already copies, no bluetoothd structure. Read by hook_setconfigind for the helper. */
static void note_pc_configuration(const uint8_t *p,size_t length)
{
    unsigned seid=0,media=0,codec=0;
    uint8_t info[16]={0};
    size_t info_len=0;
    int ok=avdtp_setconfig_media_codec(p,length,&seid,&media,&codec,info,sizeof info,&info_len);
    pthread_mutex_lock(&g_stream_control_lock);
    memset(&g_pc_config,0,sizeof g_pc_config);
    if(ok && media==0u) {                                   /* media type 0: audio */
        g_pc_config.valid=1;
        g_pc_config.codec=codec;
        memcpy(g_pc_config.info,info,info_len);
        g_pc_config.info_len=info_len;
    }
    pthread_mutex_unlock(&g_stream_control_lock);
    char hex[64]="";
    log_hex(info,ok?info_len:0,hex,sizeof hex);
    SpeakaLogNow("V93_CONFIG_RX seid=%u ok=%d media=%u codec=%s info=[%s]",seid,ok,media,
                 ok?avdtp_codec_name((uint8_t)codec):"none",hex);
}

/* Verified exact-image AVDTP RX signaling dispatcher; CID, packet ptr, length.
 * These logs are inbound SIGNALING, not RTP media or TX response receipts. */
#define SIGNAL_RX_DISPATCH 0x000d3b08ul
static const uint32_t SIGNAL_RX_SIG[4] = {0xd503237fu,0xd10203ffu,0xa90557f6u,0xa9064ff4u};
static uint64_t hook_receive_signal(uint64_t cid, uint64_t ptr, uint64_t length,
                                       uint64_t a3,uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7)
{
    uint32_t n = __sync_add_and_fetch(&g_signal_rx_calls, 1);
    uint8_t control[1024];
    if(length>=2u && length<=sizeof control && safe_peek(ptr,control,(size_t)length)) {
        if(length>=4u && (control[0]&3u)==0u && ((control[0]>>2)&3u)==0u &&
           (control[1]&63u)==3u && (control[2]>>2)==7u) {
            __atomic_store_n(&g_pc_signal_cid,cid,__ATOMIC_RELEASE);
            note_pc_configuration(control,(size_t)length);
        }
        note_signal(0,cid,control,(size_t)length);
    }
    if (length >= 2u && length <= 1024u) {
        uint8_t h[2];
        if (safe_peek(ptr, h, 2)) {
            unsigned tx=(h[0]>>4)&15u, packet=(h[0]>>2)&3u, msg=h[0]&3u;
            unsigned id=h[1]&63u;
            /* Correlate the Sink's accepted START to a separate media CID.
             * This is read-only bookkeeping on the existing RX observer. */
            if (g_sink_armed && msg == 0u && id == 7u && length == 3u) {
                uint8_t seid_byte = 0;
                if (safe_peek(ptr + 2u, &seid_byte, 1u) && seid_byte == 0x1cu) {
                    g_start_signal_cid = cid;
                    g_start_label = tx;
                    g_start_pending = 1u;
                }
            }
            if (n < 2000u) {
                SpeakaLogNow("V82 RX_SIGNAL #%u CID=%#llx tx=%u pkt=%u msg=%u sig=%u len=%llu",
                    n,(unsigned long long)cid,tx,packet,msg,id,(unsigned long long)length);
                uint8_t bytes[32];
                for (uint64_t off=0; off<length; off+=sizeof bytes) {
                    size_t take = (size_t)(length-off < sizeof bytes ? length-off : sizeof bytes);
                    if (!safe_peek(ptr+off,bytes,take)) {
                        SpeakaLogNow("V82 RX_SIGNAL #%u read-failed at +%llu",n,(unsigned long long)off);break;
                    }
                    char hex[sizeof bytes*3+1]; size_t p=0;
                    for (size_t i=0;i<take;i++) p+=(size_t)snprintf(hex+p,sizeof hex-p,"%02x%s",bytes[i],i+1==take?"":" ");
                    SpeakaLogLine("V82 RX_RAW #%u +%llu %s", n,(unsigned long long)off,hex);
                }
            }
        }
    } else if (n < 2000u) SpeakaLogNow("V82 RX_SIGNAL #%u unexpected length=%llu",n,(unsigned long long)length);
    return g_signal_rx_orig ? g_signal_rx_orig(cid,ptr,length,a3,a4,a5,a6,a7) : 0;
}
static void install_signal_rx(uintptr_t base,int uuid_ok)
{
    if (!g_sink_armed || !uuid_ok) return;
    uintptr_t target=base+SIGNAL_RX_DISPATCH; uint32_t words[4]={0};
    if (!safe_peek(target,words,sizeof words) || memcmp(words,SIGNAL_RX_SIG,sizeof words)!=0) {
        SpeakaSetStatus("diag.v82.rx","REFUSED: exact dispatcher signature mismatch");return;
    }
    void *trampoline=NULL;
    MSHookFunction((void*)target,(void*)hook_receive_signal,&trampoline);
    if (!trampoline) { SpeakaSetStatus("diag.v82.rx","FAILED: no trampoline");return; }
    g_signal_rx_orig=(hook_fn_t)trampoline;
    g_signal_rx_hook_ok=1;
    SpeakaSetStatus("diag.v82.rx","installed, incoming AVDTP SIGNALING only; no TX/media hook");
    SpeakaLogNow("V82 RX dispatcher hooked at image+0xD3B08; incoming signaling only");
}

/* AVDTP SIGNALING TX, never media: verified image+0xDA414.
 * Caller +0xDA36C constructs an AVDTP response PDU and tail-branches here;
 * arguments x0=CID, x1=encoded AVDTP PDU pointer, x2=byte length.
 * The underlying routine queues the packet for L2CAP transmission; returning
 * zero is NOT evidence of over-the-air delivery or remote acceptance.
 * Experimental hook is strictly pass-through, bounded and one-shot-gated. */
#define SIGNAL_TX_QUEUE 0x000da414ul
static const uint32_t SIGNAL_TX_SIG[4] = {0xd503237fu, 0xd10103ffu, 0xa90157f6u, 0xa9024ff4u};
static uint64_t hook_transmit_signal(uint64_t cid, uint64_t packet, uint64_t length,
                                         uint64_t a3, uint64_t a4, uint64_t a5,
                                         uint64_t a6, uint64_t a7)
{
    unsigned n = __sync_add_and_fetch(&g_signal_tx_calls, 1u);
    int sampled = n <= 128u;
    unsigned tx = 0, ptype = 0, mtype = 0, sig = 0;
    uint8_t bytes[96];
    size_t take = 0;
    if (sampled && length >= 2u && length <= 1024u) {
        take = (size_t)(length < sizeof bytes ? length : sizeof bytes);
        if (safe_peek(packet, bytes, take)) {
            tx = (unsigned)((bytes[0] >> 4) & 15u);
            ptype = (unsigned)((bytes[0] >> 2) & 3u);
            mtype = (unsigned)(bytes[0] & 3u);
            sig = (unsigned)(bytes[1] & 63u);
            SpeakaLogNow("V82 TX_SIGNAL #%u CID=%#llx tx=%u pkt=%u msg=%u sig=%u len=%llu",
                       n, (unsigned long long)cid, tx, ptype, mtype, sig,
                       (unsigned long long)length);
            if (mtype == 3u && take >= 3u)
                SpeakaLogNow("V82 TX_REJECT #%u sig=%u last_error_byte=0x%02x (inspect complete raw PDU)",
                           n, sig, (unsigned)bytes[take-1u]);
            for (size_t off = 0; off < take; off += 32u) {
                size_t count = take-off < 32u ? take-off : 32u;
                char hex[32u*3u+1u]; size_t p = 0;
                for (size_t i = 0; i < count; ++i)
                    p += (size_t)snprintf(hex+p, sizeof hex-p, "%02x%s", bytes[off+i],
                                          i+1u == count ? "" : " ");
                SpeakaLogNow("V82 TX_RAW #%u +%u %s", n, (unsigned)off, hex);
            }
            if ((uint64_t)take < length)
                SpeakaLogNow("V82 TX_RAW #%u truncated at %u of %llu bytes", n,
                           (unsigned)take, (unsigned long long)length);
        } else SpeakaLogNow("V82 TX_SIGNAL #%u unreadable packet pointer=%#llx",
                          n, (unsigned long long)packet);
    } else if (sampled) {
        SpeakaLogNow("V82 TX_SIGNAL #%u length outside safe capture range: %llu",
                   n, (unsigned long long)length);
    }
    /* The native TX routine may take ownership of packet. Capture before it;
     * apply the transition only after a successful queue result. */
    uint8_t control[1024];
    int control_ok=length>=2u && length<=sizeof control && safe_peek(packet,control,(size_t)length);
    uint64_t ret = g_signal_tx_orig
      ? g_signal_tx_orig(cid, packet, length, a3, a4, a5, a6, a7) : 0xFFFFFFFFu;
    if(ret==0u && control_ok) note_signal(1,cid,control,(size_t)length);
    if (g_sink_armed && g_sink_accepted && g_start_pending &&
        ret == 0u && mtype == 2u && sig == 7u && tx == g_start_label &&
        cid == g_start_signal_cid) {
        g_sink_stream_signal_cid = cid;
        g_sink_stream_started = 1u;
        g_start_pending = 0u;
        SpeakaLogNow("V86 SINK_START_ACCEPT_QUEUED sig_cid=%#llx seid=7 tx=%u; media CID not yet known",
                   (unsigned long long)cid, tx);
    }
    if (sampled) {
        SpeakaLogNow("V82 TX_QUEUE_RESULT #%u ret=%#llx (not proof of air delivery)",
                   n, (unsigned long long)ret);
        SpeakaSetStatus("diag.v82.tx", "TX capture active: %u queued attempts, last CID=%#llx signal=%u msg=%u ret=%#llx",
                      n, (unsigned long long)cid, sig, mtype, (unsigned long long)ret);
    }
    return ret;
}
static void install_signal_tx(uintptr_t base, int uuid_ok)
{
    if (!g_sink_armed || !uuid_ok) return;
    uintptr_t target = base + SIGNAL_TX_QUEUE;
    uint32_t words[4] = {0};
    if (!safe_peek(target, words, sizeof words) ||
        memcmp(words, SIGNAL_TX_SIG, sizeof words) != 0) {
        SpeakaSetStatus("diag.v82.tx", "REFUSED: exact AVDTP TX queue signature mismatch");
        SpeakaLogNow("V82 TX refused: image+0xDA414 signature mismatch");
        return;
    }
    void *trampoline = NULL;
    MSHookFunction((void *)target, (void *)hook_transmit_signal, &trampoline);
    if (!trampoline) {
        SpeakaSetStatus("diag.v82.tx", "FAILED: no TX trampoline");
        return;
    }
    g_signal_tx_orig = (hook_fn_t)trampoline;
    g_signal_tx_hook_ok = 1u;
    SpeakaSetStatus("diag.v82.tx", "installed at image+0xDA414; TX AVDTP PDU + queue result, no radio delivery claim");
    SpeakaLogNow("V82 TX hook installed at image+0xDA414 (encoded AVDTP signaling queue; pass-through)");
}

/* v8.7: exact verified native L2CAP media-data indication at image+0xD8CF8.
 * ABI: w0=local CID, x1=packet pointer, w2=raw packet length.
 * CRITICAL: do not dereference x1, perform the CID lookup at +0xD917C,
 * read ANY channel structure, or patch Apple's one-instruction media stub.
 * Counter counts raw handler-entry packet lengths, NOT parsed SBC bytes.
 * v8.8 removes all logging/OS calls from this pass-through counter. */
#define MEDIA_HANDLER_ENTRY 0x000d8cf8ul
static const uint32_t MEDIA_HANDLER_SIG[4] = {
    0xd503237fu, 0xd10203ffu, 0xa90367fau, 0xa9045ff8u
};

static uint64_t hook_receive_media(uint64_t cid, uint64_t packet,
    uint64_t length, uint64_t a3, uint64_t a4, uint64_t a5,
    uint64_t a6, uint64_t a7)
{
    /* No pointer access or native helper calls before forwarding.  These
     * unsigned 32-bit values are the incoming raw w0 / w2 registers. */
    const unsigned raw_cid = (unsigned)(uint32_t)cid;
    const unsigned raw_bytes = (unsigned)(uint32_t)length;
    const unsigned calls = __sync_add_and_fetch(&g_media_calls, 1u);
    MediaHistory *record = &g_media_recent[(calls - 1u) & 7u];
    __atomic_store_n(&record->cid, raw_cid, __ATOMIC_RELAXED);
    __atomic_store_n(&record->bytes, raw_bytes, __ATOMIC_RELAXED);
    __atomic_store_n(&record->sequence, calls, __ATOMIC_RELEASE);

    /* Call Apple's original function exactly once, without changing any
     * argument value or altering the result. */
    const uint64_t result = g_media_orig
        ? g_media_orig(cid, packet, length, a3, a4, a5, a6, a7)
        : 0xffffffffffffffffull;

    __sync_add_and_fetch(&g_media_bytes, (unsigned long long)raw_bytes);
    return result;
}

static void install_media_hook(uintptr_t base, int uuid_ok)
{
    if (!g_sink_armed || !uuid_ok || !g_signal_rx_hook_ok || !g_signal_tx_hook_ok) return;
    uint32_t handler[4] = {0};
    if (!safe_peek(base + MEDIA_HANDLER_ENTRY, handler, sizeof handler) ||
        memcmp(handler, MEDIA_HANDLER_SIG, sizeof handler) != 0) {
        SpeakaLogNow("V87_MEDIA_REFUSED exact handler instruction signature mismatch");
        SpeakaSetStatus("diag.v86.media", "V87 REFUSED: exact bluetoothd media-handler signature differs");
        return;
    }
    void *trampoline = NULL;
    MSHookFunction((void *)(base + MEDIA_HANDLER_ENTRY),
                   (void *)hook_receive_media, &trampoline);
    if (!trampoline) {
        SpeakaLogNow("V87_MEDIA_FAILED: missing trampoline");
        SpeakaSetStatus("diag.v86.media", "V87 FAILED: no media-handler trampoline");
        return;
    }
    g_media_orig = (hook_fn_t)trampoline;
    g_media_hook_ok = 1u;
    SpeakaSetStatus("diag.v86.media", "V87 ARMED: raw handler-entry bytes=0; waiting for Windows START/media");
    SpeakaLogNow("V87 MEDIA hook installed at image+0xD8CF8; raw w0/w2 ring[8], no channel reads or packet dereferences");
}

/* Exact firmware's table is borrowed static storage, 27 function pointers.
 * Copy ALL entries, preserve their PAC bits, change only +0xb0. The native
 * register routine stores the table pointer at context+0x18 (+0xce3d4).
 * braaz at +0xd8fa4 authenticates the media function with IA, discriminator 0.
 * A 4-byte RET stub cannot safely hold an ordinary MSHookFunction patch. */
static uint64_t media_callback(uint64_t handle,uint64_t sequence,
    uint64_t timestamp,uint64_t payload,uint64_t length,uint64_t type,
    uint64_t marker,uint64_t error)
{
    (void)type;(void)marker;
    AudioRelayForward((uint32_t)handle,(uint32_t)sequence,(uint32_t)timestamp,
                   (const uint8_t *)(uintptr_t)payload,(uint32_t)length,(uint32_t)error,
                   __atomic_load_n(&g_sink_stream_handle,__ATOMIC_ACQUIRE));
    return handle; /* preserve x0 behavior of the replaced RET */
}
static int install_media_callback(uint64_t context)
{
    if(!g_relay_ready || !g_exact_build || !g_media_hook_ok) {
        SpeakaSetStatus("diag.audio_receive","REFUSED: %s",!g_relay_ready?"the shared audio ring is not ready":
            !g_exact_build?"this is not the analysed bluetoothd":"the audio hook is missing");
        return 0;
    }
    const uint32_t offsets[27]={
        0x384f94,0x385044,0x385104,0x3851c4,0x385274,0x38532c,0x3853f4,
        0x38549c,0x38554c,0x3855f4,0x385684,0x385714,0x3857ac,0x385844,
        0x3858f4,0x38598c,0x385a3c,0x385adc,0x385b84,0x385c24,0x385cd4,
        0x385d64,0x385dfc,0x385e00,0x385e1c,0x385ed4,0x385f6c};
    uint64_t table=0;uint32_t stub=0,branch=0,load=0,store=0;
    if(!safe_peek(context+0x18u,&table,8) || table!=g_image_base+0x92a248u ||
       !safe_peek(table,g_media_callbacks,sizeof g_media_callbacks) ||
       !safe_peek(g_image_base+0x385dfcu,&stub,4) || stub!=0xd65f03c0u ||
       !safe_peek(g_image_base+0xd8fa4u,&branch,4) || branch!=0xd61f091fu ||
       !safe_peek(g_image_base+0xd8f28u,&load,4) || load!=0xf9405900u ||
       !safe_peek(g_image_base+0xce3d4u,&store,4) || store!=0xf9000d16u) {
        SpeakaSetStatus("diag.audio_receive","REFUSED: callback table, stub or call site differs");
        SpeakaLogNow("V88_CALLBACK refused: exact table/stub/callsite mismatch");return 0;
    }
    for(unsigned i=0;i<27;i++) {
        uintptr_t pointer=(uintptr_t)ptrauth_strip((void *)(uintptr_t)g_media_callbacks[i],ptrauth_key_function_pointer);
        if(pointer!=g_image_base+offsets[i]) {
            SpeakaSetStatus("diag.audio_receive","REFUSED: callback table entry %u differs",i);
            SpeakaLogNow("V88_CALLBACK refused: table entry %u differs",i);return 0;
        }
    }
    void *raw=ptrauth_strip((void *)media_callback,ptrauth_key_function_pointer);
    g_media_callbacks[22]=(uint64_t)(uintptr_t)ptrauth_sign_unauthenticated(raw,ptrauth_key_function_pointer,0);
    uint64_t expected=table;
    if(!__atomic_compare_exchange_n((uint64_t *)(uintptr_t)(context+0x18u),&expected,
          (uint64_t)(uintptr_t)g_media_callbacks,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED)) {
        SpeakaSetStatus("diag.audio_receive","REFUSED: the callback table changed while it was being replaced");
        return 0;
    }
    SpeakaLogNow("V88_CALLBACK installed table_copy=27 slot=+0xb0 original=+0x385dfc IA=0; stub code untouched");
    SpeakaSetStatus("diag.audio_receive","v8.8 callback ready; shared ring -> helper after accepted START");
    return 1;
}

/* v8.3 one-shot: swap higher-level audio context AFTER the native Source
 * registration function returns. The original source-capability allocations
 * remain owned by the native daemon and are referenced by the new table.
 * Only the obsolete 192-byte parent table is deliberately leaked. */
static void register_sink_after_sources(uint64_t audio_context)
{
    if (!g_sink_armed || g_sink_attempted) return;
    /* v9.4: the same prerequisites as before, but a missing one is named in the status, not only the debug log, so
       Speaka's Start shows which step stalled. WAITING, not REFUSED: nothing is consumed and the next Bluetooth
       power-on tries again, so neither the app nor Settings counts it as a failure. */
    const char *missing =
        !g_config_shadow_returned ? "the seven-slot endpoint table was never applied" :
        !g_sink_caps_ready ? "the speaker's SBC capabilities were never captured" :
        (!g_source_reg_hook_ok || !g_orig[HOOK_REGISTER]) ? "the registration hooks are missing" :
        (!g_signal_rx_hook_ok || !g_sdp_hook_ok || !g_signal_tx_hook_ok || !g_sdp_orig) ?
            ((!g_sdp_hook_ok || !g_sdp_orig) ? "the advertising hook is missing" : "the signaling hooks are missing") :
        !g_media_hook_ok ? "the audio hook is missing" :
        g_native_source_success != 6 ? "Bluetooth did not register all six of its own endpoints" : NULL;
    if (missing) {
        SpeakaSetStatus("diag.v83.audio_table", "WAITING: %s", missing);
        SpeakaLogNow("V83 WAITING: %s", missing);
        return;
    }
    if (!__sync_bool_compare_and_swap(&g_sink_attempted, 0, 1)) return;
    /* From here the one attempt is used up, so every refusal below is final for this Bluetooth session. */
    /* AAC-only Sink experiment: the records AVDTP registers for SEID 7 - what the PC reads with GET_CAPABILITIES -
     * become AAC-LC as well, so they agree with the audio-table entry below. v9.8.84 changed only that entry, so the
     * PC would still have been offered SBC. Refused here, before anything native is touched, if the records are not
     * the captured SBC ones. */
    if (g_aac_sink_mode) {
        int rc = sink_caps_use_aac(&g_sink_caps);
        if (rc != 0) {
            SpeakaSetStatus("diag.v83.audio_table", "REFUSED: AAC mode could not rewrite the Sink's AVDTP capabilities (rc=%d)", rc);
            SpeakaLogNow("V93 REFUSED: AAC mode, the captured Sink capability records were not the expected SBC ones (rc=%d)", rc);
            return;
        }
        SpeakaLogHexdump("V93 SEP AVDTP caps", g_sink_caps.caps_words, sizeof g_sink_caps.caps_words);
        SpeakaLogHexdump("V93 SEP AVDTP codec", g_sink_caps.codec_bytes, AAC_CSI_BYTES);
    }
    uint64_t avdtp_context = 0;
    uint8_t avdtp_slots = 0;
    if (!safe_peek((uint64_t)(g_image_base + 0x9768b0u), &avdtp_context, sizeof avdtp_context) ||
        !avdtp_context || !safe_peek(avdtp_context + 0x60u, &avdtp_slots, 1) ||
        avdtp_slots != 7u) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: low-level AVDTP allocation is not 7 slots");
        SpeakaLogNow("V83 REFUSED: low-level AVDTP allocation is not 7 slots"); return;
    }
    if (!install_media_callback(avdtp_context)) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: the audio callback was not installed");
        return;
    }
    if (!audio_context) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: audio context pointer is null");
        SpeakaLogNow("V83 REFUSED: audio context pointer is null"); return;
    }
    struct {
        uint32_t used;
        uint32_t second;
        uint64_t table;
    } meta = {};
    if (!safe_peek(audio_context + 0x68u, &meta.used, sizeof meta.used) ||
        !safe_peek(audio_context + 0x6cu, &meta.second, sizeof meta.second) ||
        !safe_peek(audio_context + 0x78u, &meta.table, sizeof meta.table) ||
        meta.used != 6u || meta.second != 6u || !meta.table) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: audio table metadata expected (6,6,nonzero) observed (%u,%u)",
            meta.used, meta.second);
        SpeakaLogNow("V83 REFUSED: audio table metadata expected (6,6,nonzero) observed (%u,%u,%#llx)",
            meta.used, meta.second, (unsigned long long)meta.table); return;
    }
    /* malloc_type_calloc(6,32,...) constructs the original table; the
       allocator guarantees zero-initialized entries. calloc/free use the
       same libSystem allocator family. The daemon's destructor calls free. */
    uint8_t *new_table = (uint8_t *)calloc(7u, 32u);
    uint8_t *new_caps = (uint8_t *)calloc(1u, 3u * 16u + SINK_CODEC_TAIL_BYTES);
    if (!new_table || !new_caps) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: calloc failed");
        SpeakaLogNow("V83 REFUSED: calloc failed"); free(new_caps); free(new_table); return;
    }
    uint8_t *e7 = new_table + 6u * 32u;
    uint64_t source_caps = 0;
    uint64_t cap_context = 0;
    uint32_t source_seid = 0;
    if (!safe_peek(meta.table, new_table, 6u * 32u)) goto reject_before_swap;
    memcpy(&source_seid, new_table + 4u, sizeof source_seid);
    memcpy(&source_caps, new_table + 0x10u, sizeof source_caps);
    if (source_seid != 1u || !source_caps || !safe_peek(source_caps, new_caps, 3u * 16u))
        goto reject_before_swap;
    /* Verify the known native Source codec-capability pointer refers to
       the same audio context supplied to +0x3B4128. Distinguish these
       audio-context capability records from AVDTP codec-byte records. */
    memcpy(&cap_context, new_caps + 0x18u, sizeof cap_context);
    if (new_caps[0] != 1u || new_caps[16] != 7u || new_caps[18] != 0u ||
        new_caps[19] != 4u || new_caps[32] != 8u ||
        cap_context != audio_context) goto reject_before_swap;
    /* Entry 7 alone owns the codec bytes. The native Source codec object points at audio_context; do NOT edit it.
     * In SBC mode (the default): copy the standard A2DP SBC Sink capability bytes.
     * In AAC mode (SpeakaAACSink=YES): copy the 6-byte AAC-LC capability instead, and patch the codec-type and
     *   length fields in the capability record so bluetoothd advertises AAC. The tail is SINK_CODEC_TAIL_BYTES,
     *   which the typedefs above prove holds either codec's bytes.
     * Either way: only our private new_caps buffer is written; Apple's native tables are never modified. */
    if (g_aac_sink_mode) {
        uint8_t kSinkAAC[AAC_CSI_BYTES];
        aac_build_sink_capability(kSinkAAC);
        memcpy(new_caps + 3u * 16u, kSinkAAC, sizeof kSinkAAC);
        uint64_t sink_codec_ptr = (uint64_t)(uintptr_t)(new_caps + 3u * 16u);
        memcpy(new_caps + 0x18u, &sink_codec_ptr, sizeof sink_codec_ptr);
        /* codec-type byte: 0x02 = AAC; codec data length: 6 (was 4 for SBC). */
        new_caps[18] = A2DP_CODEC_AAC;
        new_caps[19] = 6u;
        SpeakaLogNow("V93 SEP CAPS: AAC-LC mode (codec_type=0x02 len=6): %02x %02x %02x %02x %02x %02x",
            kSinkAAC[0], kSinkAAC[1], kSinkAAC[2], kSinkAAC[3], kSinkAAC[4], kSinkAAC[5]);
    } else {
        static const uint8_t kSinkSBC[4] = {0x31u, 0x15u, 0x02u, 0xfau};
        memcpy(new_caps + 3u * 16u, kSinkSBC, sizeof kSinkSBC);
        uint64_t sink_codec_ptr = (uint64_t)(uintptr_t)(new_caps + 3u * 16u);
        memcpy(new_caps + 0x18u, &sink_codec_ptr, sizeof sink_codec_ptr);
    }
    {
        uint32_t seid = 7u, ncap = 3u;
        uint64_t pcap = (uint64_t)(uintptr_t)new_caps;
        memcpy(e7 + 0x04u, &seid, sizeof seid);
        memcpy(e7 + 0x10u, &pcap, sizeof pcap);
        e7[0x18u] = 1u; /* exact native initializer +0x3B3AA8 */
        memcpy(e7 + 0x1cu, &ncap, sizeof ncap);
    }
    /* The live pointer and both count fields must be changed together,
       after native Source registration completed, never before its loop. */
    memcpy((void *)(uintptr_t)(audio_context + 0x78u), &new_table, sizeof new_table);
    {
        uint32_t seven = 7u;
        memcpy((void *)(uintptr_t)(audio_context + 0x68u), &seven, sizeof seven);
        memcpy((void *)(uintptr_t)(audio_context + 0x6cu), &seven, sizeof seven);
    }
    g_leaked_original_table = (void *)(uintptr_t)meta.table;
    g_audio_table_extended = 1u;
    SpeakaSetStatus("diag.v83.audio_table", "REPLACED post native Source: entries=7, native=6, new_SEID=7; original parent block intentionally retained");
    SpeakaLogNow("V83 AUDIO_TABLE SWAPPED after native registration: count 6->7, old=%#llx retained, new=%p entry7_caps=%p",
        (unsigned long long)meta.table, new_table, new_caps);
    /* Use the SEID storage of the new high-level entry itself, exactly as
       Apple's registration loop passes entry+4 as output. */
    {
        uint64_t ret = g_orig[HOOK_REGISTER](0, 1,
            (uint64_t)(uintptr_t)&g_sink_caps.caps_words[0], 3, 1,
            (uint64_t)(uintptr_t)(e7 + 4u), 0, 0);
        uint32_t assigned_seid = 0;
        memcpy(&assigned_seid, e7 + 4u, sizeof assigned_seid);
        g_sink_caps.assigned_seid = (uint8_t)assigned_seid;
        SpeakaLogNow("V83 SEP RESULT ret=%#llx seid=%u type=SNK(1) audio_table=7 codec_mode=%s; PEER validation required",
            (unsigned long long)ret, assigned_seid, g_aac_sink_mode ? "AAC-LC" : "SBC");
        if (ret != 0) {
            /* The native routine did not register an endpoint. Restore the
               previous pointer/counts and release only our two new blocks.
               Original six children remain owned by Apple's original table. */
            uint32_t six=6u;
            void *old=(void *)(uintptr_t)meta.table;
            memcpy((void *)(uintptr_t)(audio_context + 0x78u), &old, sizeof old);
            memcpy((void *)(uintptr_t)(audio_context + 0x68u), &six, sizeof six);
            memcpy((void *)(uintptr_t)(audio_context + 0x6cu), &six, sizeof six);
            g_audio_table_extended=0u;
            g_leaked_original_table=NULL;
            free(new_caps);
            free(new_table);
            SpeakaSetStatus("diag.v83.audio_table", "ROLLED BACK: Sink registration returned %#llx; 6 entries restored", (unsigned long long)ret);
            return;
        }
        if (assigned_seid != 7u) {
            SpeakaSetStatus("diag.v83.audio_table", "UNSAFE TRIAL: assigned unexpected Sink SEID=%u; restart daemon, do not retry", assigned_seid);
            return; /* successful SEP registration may retain entry pointers */
        }
        g_sink_accepted = 1u;
        SpeakaSetStatus("diag.v82.sep", "LOCAL register=0 SEID=%u role=SNK with high-level audio entry; remote validation required", assigned_seid);
    }
    /* Source SDP was published inside the native loop before our post-hook.
       Add the new Sink SDP here explicitly rather than relying on the old
       Source-SDP hook timing. Uses the existing permanent SDP descriptor. */
    if (__sync_bool_compare_and_swap(&g_sink_sdp_registered, 0u, 1u)) {
        g_sink_sdp_handle = 0;
        uint64_t ret = g_sdp_orig((uint64_t)(uintptr_t)SinkSDPDescriptor(),
                                   (uint64_t)(uintptr_t)&g_sink_sdp_handle);
        SpeakaLogNow("V83 SDP add ret=%#llx handle=%#x class=0x110B; PEER SDP REQUIRED",
            (unsigned long long)ret, (unsigned)g_sink_sdp_handle);
        SpeakaSetStatus("diag.sdp_sink_0x110b", "LOCAL add result=%#llx; peer SDP required", (unsigned long long)ret);
        SpeakaSetStatus("diag.v82.sdp", "LOCAL add=%#llx handle=%#x; remote SDP 0x110B must be queried",
            (unsigned long long)ret, (unsigned)g_sink_sdp_handle);
        if (ret != 0) SpeakaSetStatus("diag.v83.audio_table", "SDP publish failed; restart daemon to recover");
    }
    return;
reject_before_swap:
    SpeakaLogNow("V83 REFUSED: native Source table/caps/SEID/context layout did not match expected image");
    SpeakaSetStatus("diag.v83.audio_table", "REFUSED: Source table or capability validation failed");
    free(new_caps);
    free(new_table);
}

/* Function identity and exact four-word signature established from uploaded
 * bluetoothd: +0x3B4128. It takes audio context as x0, returns w0 status.
 * This observer calls the original once, and only then runs the experiment. */
/* v8.9.2: Apple's registration loop runs on every Bluetooth power-up, not just at process start. Turning Bluetooth
 * (or Airplane Mode) off and on re-runs it INSIDE the same bluetoothd: Apple rebuilds its SDP records and six
 * Source endpoints from scratch, but the one-shot Sink (SEID 7 + record 0x110B) is not re-added, and the rebuilt
 * endpoint table is back to six slots. The PC then connects, finds no speaker endpoint and drops, over and over.
 * Re-adding the Sink here would mean repeating the private-memory steps on new objects untested, so instead the
 * loss is reported plainly and forwarding state is cleared; Start (a real bluetoothd restart) brings it back. */
static volatile unsigned g_native_loops = 0;
static uint64_t hook_native_registration(uint64_t audio_context)
{
    uint64_t ret = g_source_reg_orig ? g_source_reg_orig(audio_context) : 1u;
    unsigned loops = __sync_add_and_fetch(&g_native_loops, 1u);
    if (g_sink_armed) {
        SpeakaLogNow("V83 NATIVE_SOURCE_LOOP RETURN ret=%#llx observed_sources=%u; post-return boundary",
                   (unsigned long long)ret, (unsigned)g_native_source_success);
        if (loops > 1u && g_audio_table_extended) {
            pthread_mutex_lock(&g_stream_control_lock);   /* same lock SetConfigInd and signaling use */
            g_sink_accepted = 0u;
            AudioRelaySetActive(0);
            pthread_mutex_unlock(&g_stream_control_lock);
            SpeakaSetStatus("diag.v82.sep", "LOST: Bluetooth was turned off and on; speaker endpoint gone until bluetoothd restarts (tap Start)");
            SpeakaLogNow("V892 SINK_LOST native Source loop ran again (#%u): Bluetooth re-initialized inside this bluetoothd; "
                       "Sink SEID 7 and SDP 0x110B are not re-added. Tap Start to restart bluetoothd.", loops);
        }
        if (ret == 0) register_sink_after_sources(audio_context);
        else SpeakaSetStatus("diag.v83.audio_table", "REFUSED: native Source loop returned error %#llx", (unsigned long long)ret);
    }
    return ret;
}

static void install_native_registration_hook(uintptr_t base,int uuid_ok)
{
    if (!g_sink_armed || !uuid_ok) return;
    if (!g_orig[HOOK_REGISTER] || !g_signal_rx_hook_ok ||
        !g_signal_tx_hook_ok || !g_sdp_hook_ok || !g_config_hook_enabled) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: required RX/TX/SDP/config hooks missing");return;
    }
    uintptr_t target=base+SOURCE_REGISTRATION_ENTRY;
    uint32_t words[4]={0};
    if (!safe_peek(target,words,sizeof words) ||
        memcmp(words,SOURCE_REGISTRATION_SIG,sizeof words)!=0) {
        SpeakaSetStatus("diag.v83.audio_table", "REFUSED: registration-loop instruction signature mismatch");return;
    }
    void *trampoline=NULL;
    MSHookFunction((void*)target,(void*)hook_native_registration,&trampoline);
    if (!trampoline) {
        SpeakaSetStatus("diag.v83.audio_table", "FAILED: registration-loop hook returned no trampoline");return;
    }
    g_source_reg_orig=(source_registration_fn_t)trampoline;
    g_source_reg_hook_ok=1u;
    SpeakaLogNow("V83 native Source loop hooked at image+0x3B4128; post-original one-shot table expansion only");
    SpeakaSetStatus("diag.v83.audio_table", "ARMED: post-Source-loop table expansion; no changes until original returns");
}

static uint64_t hook_register(uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3,
                              uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7)
{
    note_call(HOOK_REGISTER, a0, a1, a2, a3, a4, a5);
    const unsigned n = g_calls[HOOK_REGISTER];

    /* Read only the one SEID output byte, not eight bytes from the caller. */
    uint8_t seid_before = 0, seid_after = 0;
    const int seid_readable = (n <= 16u) &&
         safe_peek(a5, &seid_before, sizeof seid_before);

    if (n <= 12u) {
        const unsigned ncap = (unsigned)a3;
        if (ncap >= 1u && ncap <= 6u) {
            const size_t len = (size_t)ncap * 16u;
            uint8_t caps[96];
            if (safe_peek(a2, caps, len))
                log_caps(caps, len);
            else
                SpeakaLogLine("v5: capability pointer %#llx was unreadable",
                        (unsigned long long)a2);
        } else {
            SpeakaLogLine("v5: refusing to interpret unexpected numCaps=%u", ncap);
        }
    }

    /* Capture one validated native SBC capability set into stable tweak-owned
       memory. Read-only observation of Source data; no source modification. */
    /* No capability mirroring: the v7.7 trial is retired. */

    /* Copy only the SBC Source capability bytes; never mutate Apple's memory.
       This is deliberately NOT an audio Sink success claim. */
    if (g_sink_armed && !g_sink_caps_ready && (uint32_t)a0 == 0 &&
        (uint32_t)a1 == 0 && (uint32_t)a3 == 3) {
        uint8_t raw[48], codec[4]; uint64_t ptr = 0;
        if (safe_peek(a2, raw, sizeof raw) && raw[16] == 7 &&
            raw[18] == 0 && raw[19] == 4) {
            memcpy(&ptr, raw + 24, sizeof ptr);
            if (safe_peek(ptr, codec, sizeof codec) &&
                sink_prepare_caps(&g_sink_caps, raw, codec) == 0) {
                g_sink_caps_ready = 1;
                SpeakaLogNow("V82 SEP caps: stable private copy of 3 native SBC capabilities ready");
            }
        }
    }

    /* Always forward all arguments untouched. */
    const uint64_t started = mach_absolute_time();
    const uint64_t result = g_orig[HOOK_REGISTER]
        ? g_orig[HOOK_REGISTER](a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    const uint64_t elapsed_us = mach_elapsed_us(started, mach_absolute_time());
    note_return(HOOK_REGISTER, result);
    if (n <= 25u || result != 0u) {
        SpeakaLogLine("TRACE AVDTP registration #%u role=%s type=%llu count=%llu numCaps=%llu ret=%#llx (%s) duration_us=%llu",
                n, avdtp_endpoint_name((uint32_t)a0, (uint32_t)a1),
                (unsigned long long)a0, (unsigned long long)a4, (unsigned long long)a3,
                (unsigned long long)result, avdtp_registration_result(result),
                (unsigned long long)elapsed_us);
        SpeakaSetStatus("diag.register_last", "#%u %s ret=%#llx us=%llu", n,
                      avdtp_endpoint_name((uint32_t)a0, (uint32_t)a1),
                      (unsigned long long)result, (unsigned long long)elapsed_us);
    }

    if (seid_readable && safe_peek(a5, &seid_after, sizeof seid_after)) {
        SpeakaLogLine("TRACE registration #%u SEID OUT BYTE before=%u after=%u (only one byte read)",
                n, (unsigned)seid_before, (unsigned)seid_after);
    }

    if ((uint32_t)a0 == AVDTP_MEDIA_AUDIO && (uint32_t)a1 == 1u) {
        /* Merely report any native Sink calls; NEVER inject or modify them. */
        SpeakaLogNow("TRACE observed native Audio Sink registration #%u ret=%#llx", n,
                   (unsigned long long)result);
        SpeakaSetStatus("diag.sink_native_observed", "yes - call #%u", n);
    }

    if ((uint32_t)a0 == AVDTP_MEDIA_AUDIO &&
        (uint32_t)a1 == AVDTP_TSEP_SRC) {
        if ((uint32_t)result == 0u) {
            const unsigned sources = __sync_add_and_fetch(&g_native_source_success, 1u);
            SpeakaSetStatus("diag.native_source_ok", "%u", sources);
            if (seid_readable)
                SpeakaLogLine("TRACE native A2DP Source #%u accepted (SEID=%u)",
                        sources, (unsigned)seid_after);
            else
                SpeakaLogLine("TRACE native A2DP Source #%u accepted; SEID output unreadable", sources);
            if (sources == 6u) {
                SpeakaLogNow("MILESTONE all six observed native Source endpoints registered; no Sink inserted");
                SpeakaSetStatus("diag.capacity", "six native Source registrations; free space unverified");
                snapshot_table_capacity();
                snapshot_allocation();
                /* Independently verify that the ORIGINAL constructor allocated
                 * seven entries. This is read-only and adds NO Sink endpoint. */
                if (g_config_shadow_returned) {
                    uint64_t context = 0;
                    uint8_t live_count = 0;
                    int readable = safe_peek((uint64_t)(g_image_base + 0x9768b0u), &context, sizeof context) &&
                                   safe_peek(context + 0x60u, &live_count, sizeof live_count);
                    SpeakaSetStatus("diag.v81_seven_slots",
                                  "ONE SHOT result: live_slots=%s%u expected=7 native_sources=6; v8.2 may separately attempt Sink",
                                  readable ? "" : "unreadable/", (unsigned)live_count);
                    SpeakaLogNow("V81_LIVE RESULT readable=%s live_slots=%u expected=7 sources=6; v8.2 gate follows",
                               readable ? "yes" : "no", (unsigned)live_count);
                }
                /* v8.3: never insert Sink inside the native Source loop.
                   The optional post-return hook runs after all native Source setup. */
                /* Did the optional getter probe reach the index-3 lookup before registration? */
                if (g_config_hook_enabled && g_config_index3_calls == 0u) {
                    SpeakaSetStatus("diag.v80_config_lookup",
                                  "NO_INDEX3_CALL_OBSERVED before six Sources; cannot infer hook timing");
                    SpeakaLogNow("V80_CFG_GET no index=3 call observed before native Sources registered");
                }

            }
        } else {
            SpeakaLogNow("FAIL native Source registration #%u status %#llx (%s)",
                       n, (unsigned long long)result, avdtp_registration_result(result));
            SpeakaSetStatus("diag.native_source_error", "call #%u status %#llx", n,
                          (unsigned long long)result);
        }
    }

    if (SpeakaExperimentsEnabled() && n == 1u) {
        SpeakaLogNow("WARN legacy experiment preference ON, but Sink injection is fully DISABLED");
    }
    return result;
}
/* --------------------------------------------------------------------------------------- v7 SDP observer
 * Entry verified in this exact 10,196,720-byte bluetoothd image at image+0x1e8e78.
 * The function has 20 direct call sites. Disassembly establishes that x0 is a two-list
 * descriptor (pointer,count at +0/+8 and pointer,count at +0x10/+0x18), and x1 is a
 * nullable 32-bit record-handle out pointer. This is NOT a public, stable ABI.
 *
 * Important: this hook is opt-in and does no mutation. In case a private ABI assumption
 * is wrong, the five normal diagnostic hooks still operate with this observer OFF.
 */
#define SDP_RECORD_ADD_ENTRY 0x001e8e78ul
static const uint32_t SDP_RECORD_ADD_SIG[4] = {
    0xd503237fu, 0xa9bd57f6u, 0xa9014ff4u, 0xa9027bfdu
};
static volatile unsigned g_sdp_calls = 0;
static volatile unsigned g_sdp_source_seen = 0;
static volatile unsigned g_sdp_sink_seen = 0;

static int sdp_probe_requested(void)
{
    /* Read the exact current-user domain used by the Preferences pane. */
    CFPropertyListRef v = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_SDP_PROBE), CFSTR(SPEAKA_PREFS_DOMAIN));
    int on = v && CFGetTypeID(v) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)v);
    if (v) CFRelease(v);
    return on;
}

static uint16_t load_u16(const uint8_t *b)
{
    uint16_t v = 0;
    memcpy(&v, b, sizeof v);
    return v;
}

static uint64_t load_u64(const uint8_t *b)
{
    uint64_t v = 0;
    memcpy(&v, b, sizeof v);
    return v;
}

static void log_sdp_attributes(unsigned n, const char *which, uint64_t address, unsigned count)
{
    /* Inspect at most eight 24-byte entries in a compact, bounded summary.
     * This avoids overflowing the 300-line Settings ring during SDP startup. */
    if (count == 0u) {
        SpeakaLogLine("SDP_PROBE #%u %s attrs=none", n, which);
        return;
    }
    if (!address || count > 128u) {
        SpeakaLogLine("SDP_PROBE #%u %s attrs=unreadable-or-implausible count=%u", n, which, count);
        return;
    }
    unsigned limit = count < 8u ? count : 8u;
    char info[150] = "";
    size_t used = 0;
    for (unsigned i = 0; i < limit; i++) {
        uint8_t attr[24];
        if (i > (UINT64_MAX - address) / 24u || !safe_peek(address + (uint64_t)i * 24u, attr, sizeof attr)) {
            (void)snprintf(info + used, sizeof info - used, " [#%u unreadable]", i);
            break;
        }
        /* Inspected function reads a 16-bit attribute ID at element +0 and a
         * one-byte element type at +8. Do not assume more about the private format. */
        unsigned attribute_id = (unsigned)load_u16(attr);
        unsigned element_type = (unsigned)attr[8];
        int added = snprintf(info + used, sizeof info - used, " %04x:t%02x", attribute_id, element_type);
        if (added < 0) break;
        if ((size_t)added >= sizeof info - used) { used = sizeof info - 1u; break; }
        used += (size_t)added;
        if (attribute_id == 1u && element_type == 6u) {
            /* Matched the precise sequence in the record-add function:
             * list[i].value (+16), then candidate 16-bit class at value+8.
             * Observing 0x110A here is suggestive, not independent remote proof. */
            uint64_t element_data = load_u64(attr + 16);
            uint16_t candidate_class = 0;
            if (element_data <= UINT64_MAX - 8u &&
                safe_peek(element_data + 8u, &candidate_class, sizeof candidate_class))
                SpeakaLogLine("SDP_PROBE #%u %s candidate_class16=0x%04x (private ABI)",
                        n, which, (unsigned)candidate_class);
        }
    }
    SpeakaLogLine("SDP_PROBE #%u %s attr_ids(type) count=%u first%u:%s%s", n, which, count, limit,
            info, count > limit ? " ..." : "");
}

/* Minimal, bounded extraction of the same class candidate we already log.
 * It is a private in-memory hint, NOT proof of remote SDP discoverability. */
static uint16_t sdp_class_hint(uint64_t descriptor)
{
    uint8_t lists[32];
    if (!safe_peek(descriptor, lists, sizeof lists)) return 0;
    uint64_t list = load_u64(lists);
    uint16_t count = load_u16(lists + 8);
    if (!list || count > 128u) return 0;
    unsigned n = count < 32u ? count : 32u;
    for (unsigned i = 0; i < n; ++i) {
        if ((uint64_t)i > (UINT64_MAX - list) / 24u) return 0;
        uint8_t attr[24];
        if (!safe_peek(list + (uint64_t)i * 24u, attr, sizeof attr)) return 0;
        if (load_u16(attr) == 1u && attr[8] == 6u) {
            uint64_t ptr = load_u64(attr + 16);
            uint16_t service = 0;
            if (ptr <= UINT64_MAX - 8u && safe_peek(ptr + 8u, &service, sizeof service))
                return service;
        }
    }
    return 0;
}

static uint64_t hook_sdp_add_record(uint64_t descriptor, uint64_t handle_out)
{
    unsigned n = __sync_add_and_fetch(&g_sdp_calls, 1u);
    const uint16_t service_hint = sdp_class_hint(descriptor);
    uint32_t handle_before = 0, handle_after = 0;
    const int handle_readable = handle_out && safe_peek(handle_out, &handle_before, sizeof handle_before);
    const int sample = n <= 24u || (n % 100u) == 0u;
    if (sample) {
        uint8_t header[32];
        if (safe_peek(descriptor, header, sizeof header)) {
            uint64_t first_ptr = load_u64(header);
            uint16_t first_count = load_u16(header + 8);
            uint64_t second_ptr = load_u64(header + 16);
            uint16_t second_count = load_u16(header + 24);
            SpeakaLogLine("SDP_PROBE #%u enter listA.count=%u listB.count=%u handle_out=%s",
                    n, (unsigned)first_count, (unsigned)second_count,
                    handle_readable ? "readable" : "unavailable");
            log_sdp_attributes(n, "listA", first_ptr, first_count);
            log_sdp_attributes(n, "listB", second_ptr, second_count);
        } else {
            SpeakaLogLine("SDP_PROBE #%u descriptor unreadable; forwarding untouched", n);
        }
    }
    /* The single and only invocation of the original routine. */
    uint64_t ret = g_sdp_orig ? g_sdp_orig(descriptor, handle_out) : 0xFFFFFFFFull;
    if (ret == 0 && service_hint == 0x110au) {
        unsigned seen = __sync_add_and_fetch(&g_sdp_source_seen, 1u);
        SpeakaSetStatus("diag.sdp_source_seen", "yes - local Source record-add succeeded (%u)", seen);
    } else if (ret == 0 && service_hint == 0x110bu) {
        unsigned seen = __sync_add_and_fetch(&g_sdp_sink_seen, 1u);
        SpeakaSetStatus("diag.sdp_sink_candidate_seen", "local 0x110B record-add observed (%u); remote unverified", seen);
        SpeakaLogNow("SDP_PROBE NOTE: local 0x110B class candidate added; Windows SDP visibility NOT verified");
    }
    if (ret == 0 && service_hint == 0x110a && g_sink_armed &&
        g_sink_accepted && g_audio_table_extended &&
        !g_sink_sdp_registered && g_sdp_orig) {
        if (__sync_bool_compare_and_swap(&g_sink_sdp_registered, 0u, 1u)) {
            const void *candidate = SinkSDPDescriptor();
            g_sink_sdp_handle = 0;
            uint64_t add = g_sdp_orig((uint64_t)(uintptr_t)candidate,
                                      (uint64_t)(uintptr_t)&g_sink_sdp_handle);
            SpeakaSetStatus("diag.sdp_sink_0x110b", "LOCAL add result=%#llx; remote 0x110B NOT VERIFIED", (unsigned long long)add);
            SpeakaSetStatus("diag.v82.sdp", "LOCAL add=%#llx handle=%#x; remote SDP 0x110B NOT VERIFIED",
                          (unsigned long long)add, (unsigned)g_sink_sdp_handle);
            SpeakaLogNow("V82 SDP add ret=%#llx handle=%#x class=0x110B; PEER SDP REQUIRED",
                       (unsigned long long)add, (unsigned)g_sink_sdp_handle);
        }
    }
    if (sample) {
        int after_readable = handle_out && safe_peek(handle_out, &handle_after, sizeof handle_after);
        SpeakaLogLine("SDP_PROBE #%u exit ret=%#llx out_handle=%s before=0x%08x after=0x%08x",
                n, (unsigned long long)ret, after_readable ? "readable" : "unavailable",
                (unsigned)handle_before, (unsigned)handle_after);
        SpeakaSetStatus("diag.sdp_probe_calls", "%u call(s), last return %#llx", n,
                      (unsigned long long)ret);
    }
    return ret;
}

static void install_optional_sdp_probe(uintptr_t base, int uuid_ok)
{
    if (!sdp_probe_requested() && !g_sink_armed) {
        SpeakaSetStatus("diag.sdp_probe", "OFF (default); no SDP hook installed");
        SpeakaLogNow("SDP_PROBE disabled by default; enable separately in Settings, then restart bluetoothd once");
        return;
    }
    if (!uuid_ok) {
        SpeakaSetStatus("diag.sdp_probe", "REFUSED: exact bluetoothd UUID mismatch");
        return;
    }
    const uintptr_t entry = base + SDP_RECORD_ADD_ENTRY;
    uint32_t current[4] = {0};
    if (!safe_peek(entry, current, sizeof current) ||
        memcmp(current, SDP_RECORD_ADD_SIG, sizeof current) != 0) {
        SpeakaSetStatus("diag.sdp_probe", "REFUSED: SDP record-add code signature mismatch");
        SpeakaLogNow("SDP_PROBE REFUSED: unexpected code at image+0x1e8e78");
        return;
    }
    if (!g_orig[HOOK_REGISTER] || !g_orig[HOOK_ADVERTISE]) {
        SpeakaSetStatus("diag.sdp_probe", "REFUSED: baseline hooks not both installed");
        return;
    }
    void *orig = NULL;
    SpeakaLogNow("SDP_PROBE installing at image+0x1e8e78 (PASS-THROUGH unless v8.2 one-shot Sink registration succeeds)");
    MSHookFunction((void *)entry, (void *)hook_sdp_add_record, &orig);
    if (!orig) {
        SpeakaSetStatus("diag.sdp_probe", "FAILED to install; restart with toggle OFF");
        SpeakaLogNow("SDP_PROBE FAILED: no trampoline from hook engine");
        return;
    }
    g_sdp_orig = (sdp_add_record_fn_t)orig;
    g_sdp_hook_ok = 1;
    SpeakaSetStatus("diag.sdp_probe", "installed; one-shot v8.2 may add separate Sink record");
    SpeakaLogNow("SDP_PROBE installed; only v8.2 armed trial after accepted Sink SEP may add 0x110B");
}

/* ------------------------------------------------------------------------------------------------ installing */

static void install_hooks(uintptr_t base, int uuid_ok)
{
    void *hook_fn[HOOK_COUNT];
    hook_fn[HOOK_REGISTER]     = (void *)hook_register;
    hook_fn[HOOK_ADVERTISE]    = (void *)hook_advertise;
    hook_fn[HOOK_SETCONFIGIND] = (void *)hook_setconfigind;
    hook_fn[HOOK_ACCEPT_SBC]   = (void *)hook_accept_sbc;
    hook_fn[HOOK_PUBLISH]      = (void *)hook_publish;

    /* 1. read the first four instruction words of every target with the fault-safe reader and compare */
    int match[HOOK_COUNT];
    unsigned nmatch = 0;
    for (int i = 0; i < HOOK_COUNT; i++) {
        const hook_target_t *t = &HOOK_TARGETS[i];
        uint32_t w[4] = {0, 0, 0, 0};
        int readable = safe_peek((uint64_t)(base + t->entry), w, sizeof w);
        match[i] = readable && memcmp(w, t->sig, sizeof w) == 0;
        nmatch += (unsigned)match[i];
        if (!match[i]) {
            if (readable)
                SpeakaLogNow("%s: code at image+%#lx is %08x %08x %08x %08x, expected %08x %08x %08x %08x", t->name,
                           (unsigned long)t->entry, w[0], w[1], w[2], w[3], t->sig[0], t->sig[1], t->sig[2], t->sig[3]);
            else
                SpeakaLogNow("%s: image+%#lx is not readable", t->name, (unsigned long)t->entry);
        }
    }

    /* 2. decide: the UUID is a hint, the code is the proof */
    int all_match  = (nmatch == (unsigned)HOOK_COUNT);
    int recognised = uuid_ok || all_match;
    if (uuid_ok) {
        SpeakaSetStatus("image.match", "yes - UUID matches the analysed build");
    } else if (all_match) {
        SpeakaSetStatus("image.match", "yes - code matches although the UUID differs");
        SpeakaLogNow("UUID differs from the analysed build, but the code at all %d targets matches: hooking", HOOK_COUNT);
    } else {
        SpeakaSetStatus("image.match", "NO - unknown build, hooks disabled");
        SpeakaLogNow("unknown daemon build: UUID differs and only %u of %d targets match: no hooks will be installed",
                   nmatch, HOOK_COUNT);
    }

    /* 3. hook */
    unsigned ok = 0, bad = 0;
    for (int i = 0; i < HOOK_COUNT; i++) {
        const hook_target_t *t = &HOOK_TARGETS[i];
        char key[56];
        snprintf(key, sizeof key, "hook.%s", t->name);

        if (!recognised) {
            SpeakaSetStatus(key, "NOT hooked: unknown daemon build");
            bad++;
            continue;
        }
        if (!match[i]) {
            SpeakaSetStatus(key, "skipped: code differs");
            bad++;
            continue;
        }

        const uint32_t *code = (const uint32_t *)(base + t->entry);
        SpeakaLogNow("hooking %s at image+%#lx (%p)", t->name, (unsigned long)t->entry, (const void *)code);
        void *orig = NULL;
        MSHookFunction((void *)code, hook_fn[i], &orig);
        if (!orig) {
            SpeakaLogNow("%s: hook engine returned no trampoline -> treating as failed", t->name);
            SpeakaSetStatus(key, "FAILED: no trampoline from the hook engine");
            bad++;
            continue;
        }
        g_orig[i] = (hook_fn_t)orig;
        SpeakaLogNow("%s hooked, original continues at %p", t->name, orig);
        SpeakaSetStatus(key, "installed");
        ok++;
    }
    SpeakaSetSummary(1u, (unsigned)recognised, ok, bad);
    SpeakaLogNow("hooks: %u installed, %u not installed", ok, bad);
}

/* v8.9: everything the live Sink path needs was installed at launch (registration itself happens later). */
static int sink_receive_ready(void)
{
    return g_sink_armed && g_relay_ready && g_media_hook_ok && g_source_reg_hook_ok &&
           g_signal_rx_hook_ok && g_signal_tx_hook_ok && g_sdp_hook_ok;
}

%ctor {
    const char *prog = getprogname();
    if (!prog || strcmp(prog, "bluetoothd") != 0) return;      /* the filter plist should guarantee this; be paranoid */

    SpeakaLogInit();
    SpeakaSessionStart();   /* v8.9: Settings Start/Stop works even if every hook below is refused */
    SpeakaSetStatus("diag.v89.build", "%s", SESSION_BUILD_TAG);
    SpeakaLogLine("V89 build %s: Start/Stop from Settings; output selection in BTPSAudioHelper", SESSION_BUILD_TAG);
    SpeakaLogNow("Speaka %s loaded into %s pid %d (built %s %s)", TWEAK_BUILD_TAG, prog, (int)getpid(), __DATE__, __TIME__);
    SpeakaSetStatus("build", "%s, %s %s", TWEAK_BUILD_TAG, __DATE__, __TIME__);
    SpeakaSetStatus("pid", "%d", (int)getpid());
    SpeakaSetStatus("diag.sdp_sink_0x110b", "not verified; optional trial may add local 0x110B candidate");
    SpeakaSetStatus("diag.audio_receive", "v8.8 OFF until both one-shot switches are armed");
    SpeakaSetStatus("diag.v78_table", "PENDING: read-only table scan after six normal Sources");
    SpeakaSetStatus("diag.v79_allocator", "PENDING: allocation geometry after six normal Sources");
    SpeakaSetStatus("diag.v80_config_lookup", "PENDING: optional observer not installed yet");
    SpeakaSetStatus("diag.v81_seven_slots", "OFF (default); no count overrides unless explicitly armed");
    SpeakaSetStatus("diag.v82.trial", "OFF pending one-shot arm");
    SpeakaSetStatus("diag.v83.audio_table", "OFF: no extra audio-context table by default");
    SpeakaSetStatus("diag.v82.sep", "NOT attempted");
    SpeakaSetStatus("diag.v82.sdp", "NOT published");
    SpeakaSetStatus("diag.v82.rx", "NOT hooked");
    SpeakaSetStatus("diag.v86.media", "V87 OFF: raw media entry counter not armed");
    SpeakaSetStatus("diag.v82.tx", "OFF: experimental TX hook not armed");
    SpeakaSetStatus("diag.session_id", "%ld-%d", (long)time(NULL), (int)getpid());
    SpeakaLogNow("SESSION start pid=%d optional one-shot 7-slot initializer config, OFF by default; no Sink registration", (int)getpid());

    char exe[512];
    uint32_t exe_size = sizeof exe;
    if (_NSGetExecutablePath(exe, &exe_size) != 0) snprintf(exe, sizeof exe, "?");
    SpeakaLogNow("executable path: %s", tail_of(exe, 120));
    SpeakaSetStatus("image.exec_path", "%s", tail_of(exe, 150));

    log_images();

    uint32_t index = 0;
    const struct mach_header_64 *mh = NULL;
    if (!find_main_image(&index, &mh)) {
        SpeakaLogNow("no MH_EXECUTE image found: no hooks will be installed");
        SpeakaSetStatus("image.match", "NO - main executable not found, hooks disabled");
        SpeakaSetSummary(1u, 0u, 0u, (unsigned)HOOK_COUNT);
        SpeakaSessionReport(0, 0);
        return;
    }

    uint8_t uuid[16] = {0};
    char have_s[40] = "?";
    int have = image_uuid(mh, uuid);
    if (have) uuid_string(uuid, have_s, sizeof have_s);
    int uuid_ok = have && memcmp(uuid, BLUETOOTHD_UUID_BYTES, 16) == 0;

    SpeakaLogNow("main executable is image[%u] at %p, UUID %s (analysed build: %s)", index, (const void *)mh, have_s, BLUETOOTHD_UUID);
    SpeakaSetStatus("image.index", "%u", index);
    SpeakaSetStatus("image.uuid", "%s", have_s);
    SpeakaSetStatus("image.expected", "%s", BLUETOOTHD_UUID);

    g_exact_build = uuid_ok;
    g_image_base = (uintptr_t)mh;
    install_hooks((uintptr_t)mh, uuid_ok);
    install_config_hook((uintptr_t)mh, uuid_ok);
    install_signal_rx((uintptr_t)mh, uuid_ok);
    install_signal_tx((uintptr_t)mh, uuid_ok);
    if(g_sink_armed && uuid_ok) g_relay_ready=AudioRelayPrepare();
    if(g_relay_ready) install_media_hook((uintptr_t)mh, uuid_ok);
    if(g_relay_ready) install_idle_guard((uintptr_t)mh, uuid_ok);
    install_optional_sdp_probe((uintptr_t)mh, uuid_ok);
    install_native_registration_hook((uintptr_t)mh, uuid_ok);
    SpeakaSessionReport(g_sink_armed, sink_receive_ready());
}
