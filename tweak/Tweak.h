/* Tweak.h - what the parts of the tweak offer each other: the log, the speaker endpoint's pure helpers, stream
 * control and its health rules, the audio relay, and Start/Stop. Pure C with no Apple types, so the host tests can
 * include it as it is. */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "../shared/AudioRing.h"

/* ---- the log ring and formatting helpers (Log.c) ---- */
/* tweak/Tweak.h - pure C helpers for the debug log (no Apple APIs, so they can be unit-tested anywhere). */

#ifdef __cplusplus
extern "C" {
#endif

#define LOG_RING_LINES 480     /* lines kept                                            */
#define LOG_LINE_MAX   300     /* bytes per line including the terminating NUL          */

typedef struct {
    char               line[LOG_RING_LINES][LOG_LINE_MAX];
    unsigned           head;    /* index of the OLDEST line                              */
    unsigned           count;   /* number of valid lines                                 */
    unsigned long long seq;     /* total lines ever pushed                               */
} log_ring_t;

void               log_ring_init(log_ring_t *r);
void               log_ring_push(log_ring_t *r, const char *text);   /* newlines -> spaces, truncated to fit */
unsigned           log_ring_count(const log_ring_t *r);
const char        *log_ring_at(const log_ring_t *r, unsigned i);     /* 0 = oldest; NULL if out of range     */
unsigned long long log_ring_seq(const log_ring_t *r);

/* "HH:MM:SS.mmm" in local time. `out` must hold at least 16 bytes. */
void               log_format_time(char *out, size_t cap);

/* "de ad be ef ..." for up to n bytes; returns the number of bytes rendered. */
size_t             log_hex(const void *p, size_t n, char *out, size_t cap);

#ifdef __cplusplus
}
#endif

/* ---- the debug log and status (Log.c) ---- */
/*
 * tweak/Tweak.h - debug log for code running INSIDE the Bluetooth daemon.
 *
 * Everything goes to the system log (os_log) and to a bounded in-memory ring. From there it is copied to every
 * channel in shared/SpeakaShared.h, and a startup self-test records which channels the daemon's sandbox actually allows.
 * The Settings pane (SpeakaPrefs) reads the same channels back.
 */

#ifdef __cplusplus
extern "C" {
#endif

/* Call once, as early as possible (the tweak's constructor). Safe to call twice. */
void SpeakaLogInit(void);

/* Normal log line: ring + os_log immediately, files/preferences shortly after, off the calling thread. */
void SpeakaLogLine(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Breadcrumb: same, but the file channels are written synchronously before returning. Use it right before anything
 * that might crash, so the last line before a crash survives. */
void SpeakaLogNow(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Hex dump as log lines, 16 bytes per line. */
void SpeakaLogHexdump(const char *label, const void *p, size_t n);

/* Key/value shown at the top of the pane (replaces an existing key). */
void SpeakaSetStatus(const char *key, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

/* How the hook installation went; published in the notification state word. */
void SpeakaSetSummary(unsigned loaded, unsigned uuid_ok, unsigned hooks_ok, unsigned hooks_bad);

/* Value of the "experiments" switch in the pane (re-read every few seconds). */
int  SpeakaExperimentsEnabled(void);

#ifdef __cplusplus
}
#endif

/* ---- names for codecs, endpoints and results in the log (SinkEndpoint.c) ---- */
/* tweak/Tweak.h -- allocation-free, portable AVDTP diagnostics.
 * These structures are from observation of the analysed bluetoothd build,
 * NOT a public/private stable Apple API. Never mutate capability bytes. */
#ifdef __cplusplus
extern "C" {
#endif

#define AVDTP_CAP_RECORD_BYTES 16u
#define AVDTP_CAP_MAX_RECORDS 8u

typedef struct {
    unsigned total_records;
    unsigned transport_records;
    unsigned codec_records;
    unsigned delay_records;
    unsigned unknown_records;
    unsigned codec_present;
    uint8_t media_type;     /* record +1: observed 0 for audio */
    uint8_t codec_type;     /* record +2: observed 00 SBC, 02 AAC, ff vendor */
    uint8_t codec_data_len; /* record +3: observed 4, 6, 14 */
    uint32_t fingerprint;   /* FNV-1a of copied capability bytes, diagnostics only */
} avdtp_caps_info_t;

/* Return 1 for a safe, bounded, whole-record buffer; 0 otherwise.
 * No copies or reads beyond len; out remains zeroed on invalid input. */
int avdtp_decode_caps(const uint8_t *buffer, size_t len, avdtp_caps_info_t *out);
const char *avdtp_codec_name(uint8_t type);
const char *avdtp_endpoint_name(uint32_t media, uint32_t direction);
const char *avdtp_registration_result(uint64_t result);
#ifdef __cplusplus
}
#endif

/* ---- the speaker endpoint's capability list (SinkEndpoint.c) ---- */
/* v7.7 experimental AVDTP Sink SEP probe: buffer preparation only.
 * NEVER call private bluetooth APIs from this module. The caller supplies
 * four copied SBC codec bytes; all pointer fields refer to owned storage.
 */
#ifdef __cplusplus
extern "C" {
#endif
typedef struct {
    uint64_t caps_words[6]; /* three 16-byte service capability records */
    uint8_t codec_bytes[8]; /* the codec record's bytes: 4 for SBC, 6 for AAC-LC (sink_caps_use_aac) */
    uint8_t assigned_seid; /* distinct from any native Source output byte */
} SinkCapabilities;
/* Returns zero on success; never reads beyond raw[48]/codec[4]. */
int sink_prepare_caps(SinkCapabilities *dst,
                               const uint8_t raw[48],const uint8_t codec[4]);

/* AAC-only Sink experiment (SpeakaAACSink): turn the captured SBC registration records into AAC-LC records - the
 * record shape iOS registers for its own AAC Sources (codec type 0x02 at record+2, 6 codec bytes at record+3) -
 * carrying Speaka's Sink capability (aac_build_sink_capability). Returns 0, or a negative value with the records
 * untouched if they are not the captured SBC records. Only this private copy changes. */
int sink_caps_use_aac(SinkCapabilities *caps);

/* v9.3 experimental, AAC. Build the AVDTP media-codec capability record Speaka would advertise for AAC-LC: the
 * 2-byte record header (media type audio = 0x00, codec type AAC = 0x02) followed by the 6-byte AAC Codec Specific
 * Information (AACConfig.h). This is the protocol-level record, from the public A2DP AAC spec. Writes 8 bytes to
 * out and returns 8. It does NOT register an endpoint; wiring a second SEP into bluetoothd's table is the native
 * side and is not implemented (see the AAC blockers in docs/CHANGELOG.md). */
size_t sink_build_aac_codec_record(uint8_t out[8]);
#ifdef __cplusplus
}
#endif

/* ---- checking bluetoothd's endpoint table before it grows (SinkEndpoint.c) ---- */
/* Read-only analysis of bluetoothd AVDTP endpoint slots (exact iOS build only). */
#ifdef __cplusplus
extern "C" {
#endif
#define SINK_TABLE_SLOT_BYTES 40u
#define SINK_TABLE_MAX_SLOTS 32u
#define SINK_TABLE_SEID_OFFSET 0x19u

typedef struct {
    unsigned total;
    unsigned occupied;
    unsigned vacant;
    unsigned duplicate_seids;
    uint8_t seids[SINK_TABLE_MAX_SLOTS];
} SinkTableCapacity;
/* Buffer must hold exactly (or at least) count * 40 bytes; never reads beyond it. */
int sink_analyze_table(const uint8_t *slots, size_t bytes, unsigned count, SinkTableCapacity *out);
#ifdef __cplusplus
}
#endif

/* ---- checking the allocator's geometry (SinkEndpoint.c) ---- */
/* v7.9: pure, read-only analysis of the AVDTP initialization geometry.
 * No Apple APIs, no allocation, no mutation. */
#ifdef __cplusplus
extern "C" {
#endif
#define SINK_CONTEXT_SIZE 0x68u
#define SINK_ENDPOINT_STRIDE 0x28u
#define SINK_SECONDARY_STRIDE 0x90u
#define SINK_TERTIARY_STRIDE 0x40u
#define SINK_MAX_COUNT 64u

typedef struct {
    uint8_t configured[3];
    uint8_t observed[3];
    unsigned bytes[3];
    unsigned config_matches_context;
    unsigned missing_table_mask;
} SinkAllocation;
/* first 3 config bytes; first 3 live-context bytes at +0x60;
 * pointer_present bits 0/1/2 for context+0/+8/+0x10. */
int sink_analyze_allocation(const uint8_t config[3], const uint8_t context_counts[3],
                     unsigned pointer_present, SinkAllocation *out);
#ifdef __cplusplus
}
#endif

/* ---- the seven-slot config copy (SinkEndpoint.c) ---- */
/* v8.1: validated copy of immutable index-3 AVDTP config; no Apple dependencies. */
#ifdef __cplusplus
extern "C" {
#endif
#define SINK_CONFIG_COPY_BYTES 32u
/* Must only copy an exact 6,3,3 original, preserving all other bytes.
 * trial_seven=0 mirrors unchanged; =1 changes only byte zero to 7. */
int sink_prepare_config_shadow(const uint8_t *source, size_t source_size,
                            uint8_t out[SINK_CONFIG_COPY_BYTES], int trial_seven);
#ifdef __cplusplus
}
#endif

/* ---- the Audio Sink SDP record (SinkEndpoint.c) ---- */
/* The v8.2 private SDP descriptor adapter. Verified from the exact bluetoothd
 * source record at image+0x91abd0 and its nested elements. */


#ifdef __cplusplus
extern "C" {
#endif
const void *SinkSDPDescriptor(void);
#ifdef __cplusplus
}
#endif

/* ---- sink health: whether the helper is still playing (AudioRelay.c) ---- */
/* Worker-only freshness policy. No receive-thread work or shared ABI change. */

typedef struct {
    uint64_t generation, media_counter, heartbeat_counter;
    uint64_t media_ms, heartbeat_ms, render_ms;
    uint32_t render_counter;
    unsigned media_seen, heartbeat_seen, render_seen, playing;
    unsigned holding, counter_seen;   /* v9.9.3: which counter the helper reports (decoded when holding) */
} SinkHealth;

static inline void sink_health_reset(SinkHealth *h,uint64_t generation,
                                         uint64_t media,uint64_t heartbeat) {
    memset(h,0,sizeof *h);h->generation=generation;   /* not (SinkHealth){0}: C++ includes this too */
    h->media_counter=media;h->heartbeat_counter=heartbeat;
}
static inline void sink_health_sample(SinkHealth *h,uint64_t media,
                                          uint64_t heartbeat,uint64_t now) {
    if(media!=h->media_counter) {h->media_counter=media;h->media_ms=now;h->media_seen=1;}
    if(heartbeat!=h->heartbeat_counter) {h->heartbeat_counter=heartbeat;h->heartbeat_ms=now;h->heartbeat_seen=1;}
}
/* state: 0 not playing, 1 playing (counter: rendered frames), 2 holding (counter: decoded frames - the output is held
 * on purpose while the stream is still received and decoded). v9.9.3: the two counters are different numbers, so a
 * switch between them sets a new baseline instead of counting as progress - otherwise alternating states with both
 * counters frozen would look like progress. The new counter must then advance like any other. */
static inline void sink_health_render(SinkHealth *h,uint32_t generation,
                                          uint32_t counter,unsigned state,uint64_t now) {
    if(!(h->generation&1u) || (uint32_t)h->generation!=generation) return;
    unsigned holding=state==2u,playing=state!=0u;
    if(h->counter_seen && holding!=h->holding) {
        h->holding=holding;h->playing=playing;h->render_counter=counter;
        if(!playing) h->render_seen=0;
        return;
    }
    h->holding=holding;h->counter_seen=1;h->playing=playing;
    if(playing && counter!=h->render_counter) {h->render_ms=now;h->render_seen=1;}
    h->render_counter=counter;
    if(!playing) h->render_seen=0;
}
static inline int sink_health_ready(const SinkHealth *h,int connected,uint64_t now) {
    return connected && (h->generation&1u) && h->playing &&
        h->media_seen && h->heartbeat_seen && h->render_seen &&
        now>=h->media_ms && now-h->media_ms<=1500u &&
        now>=h->heartbeat_ms && now-h->heartbeat_ms<=1000u &&
        now>=h->render_ms && now-h->render_ms<=1500u;
}

/* ---- stream control (StreamControl.c) ---- */
#ifdef __cplusplus
extern "C" {
#endif
typedef struct { uint64_t cid; unsigned signal,valid; } StreamTransaction;
typedef struct {
    unsigned local_seid,remote_seid;
    StreamTransaction pending[2][16];
} StreamControl;
/* Serialized by caller; direction 0=RX, 1=successfully queued TX.
 * -1=no change; 0=stop; 1=matched START accept. */
/* The Media Codec capability of a SET_CONFIGURATION command (AVDTP 8.9/8.21): which codec the PC chose, and its
 * codec-specific bytes. Returns 1 and fills the outputs, 0 if the PDU is not such a command. Pure; host-tested. */
int avdtp_setconfig_media_codec(const uint8_t *p,size_t length,unsigned *acp_seid,unsigned *media_type,
                                unsigned *codec_type,uint8_t *info,size_t info_cap,size_t *info_len);
int stream_control_signal(StreamControl *,unsigned direction,uint64_t cid,
                    const uint8_t *,size_t length);
#ifdef __cplusplus
}
#endif

/* ---- the audio relay (AudioRelay.c) ---- */
#ifdef __cplusplus
extern "C" {
#endif
int AudioRelayPrepare(void);
void AudioRelaySetActive(int active);
/* v9.9: the PC's stream state (AUDIO_STREAM_*, AudioRing.h), so the helper can tell a pause (keep the output) from a
 * close. Any open state also connects the helper at once, before the stream starts, so it owns the output before
 * the PC's START instead of activating mid-stream (which made Apple's A2DP code suspend the stream). */
void AudioRelaySetStreamState(uint32_t state);
/* v9.9.5: name the PC that just configured the speaker from bluetoothd's own device records (PeerIdentity.c);
 * asynchronous, never on the signalling thread. */
void SpeakaPeerIdentifySoon(void);
/* v9.3 experimental, AAC: publish the negotiated codec (A2DP_CODEC_SBC / A2DP_CODEC_AAC, AACConfig.h) and, for AAC,
 * its 6-byte Codec Specific Information into the ring header, so the helper builds the right decoder. Call before
 * AudioRelaySetActive(1). codec_config may be NULL for SBC. Today the tweak only ever negotiates SBC (a second,
 * AAC endpoint is not registered - see the AAC blockers in docs/CHANGELOG.md), so this is called with SBC; the
 * path exists so the helper and the protocol are ready the moment an AAC endpoint lands. */
void AudioRelaySetCodec(uint32_t codec, const uint8_t *codec_config, size_t len);
/* Control/idle-check threads only; queries a short worker-issued health lease. */
int AudioRelaySinkHealthy(void);
/* The sole media callback path; length is native w4, not an RTP length. */
void AudioRelayForward(uint32_t handle, uint32_t sequence, uint32_t timestamp,
                    const uint8_t *payload, uint32_t length, uint32_t error,
                    uint32_t expected_handle);
#ifdef __cplusplus
}
#endif

/* ---- Start and Stop (Session.c) ---- */
/* tweak/Tweak.h - v8.9 one-tap Start/Stop from Settings.
 *
 * Old flow: turn on two one-shot switches, then run `ssh ... "sudo killall -9 bluetoothd"`.
 * New flow: Settings writes a short request and posts a Darwin notification. The tweak, which is already running
 * inside bluetoothd, validates and consumes the request, sets or clears exactly the same two one-shot switches,
 * and then ends its own process with SIGKILL - the same thing `killall -9 bluetoothd` did. launchd relaunches
 * bluetoothd and the existing one-shot code consumes the switches exactly as before.
 * No root helper, shell, spawned process or new privilege is involved.
 *
 * The validation helpers below are pure C (no Apple frameworks) so the host tests can run them.
 */

#ifdef __cplusplus
extern "C" {
#endif

enum { SESSION_ACTION_NONE = 0, SESSION_ACTION_START = 1, SESSION_ACTION_STOP = 2 };

static inline int session_parse_action(const char *action)
{
    if (!action) return SESSION_ACTION_NONE;
    if (strcmp(action, "start") == 0) return SESSION_ACTION_START;
    if (strcmp(action, "stop") == 0) return SESSION_ACTION_STOP;
    return SESSION_ACTION_NONE;
}

/* Settings sends an NSUUID string. Accept 8..40 characters of hex digits and hyphens only. */
static inline int session_nonce_valid(const char *nonce)
{
    if (!nonce) return 0;
    size_t n = strlen(nonce);
    if (n < 8 || n > 40) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = nonce[i];
        int ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') || c == '-';
        if (!ok) return 0;
    }
    return 1;
}

/* A request is honoured only within 30 s of being written (5 s of clock tolerance into the future). */
static inline int session_request_fresh(double requested, double now)
{
    return requested > 0.0 && requested <= now + 5.0 && now - requested <= 30.0;
}

/* Registers the Settings listener. Call right after SpeakaLogInit(), before any hook can be refused. */
void SpeakaSessionStart(void);

/* Call at the end of the constructor: reports the outcome of a Settings-requested restart. */
void SpeakaSessionReport(int trial_armed, int receive_ready);

#ifdef __cplusplus
}
#endif

/* Connects to 127.0.0.1:port within a second, with 2 s send and receive timeouts. -1 if that fails. */
