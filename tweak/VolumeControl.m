/* VolumeControl.m - AVRCP absolute volume for Speaka's sink, for ONE bluetoothd build (UUID-pinned).
 * ON by default since v9.9.11 (live-verified both ways, iOS 17.0 + Windows 11, v9.8.99). SpeakaVolumeSync=off turns
 * it off, =observe logs only. On: answers Windows' volume commands for the verified Speaka peer; the helper makes
 * the level iOS's output volume (or Speaka's PCM gain if the setter is missing or ineffective).
 *
 * Evidence: docs/AVRCP-VOLUME.md (static analysis of this bluetoothd), plus the v9.9.8 checks:
 *  - +0xe0ff4 (the Target vendor dispatcher) is (peer *, label, frame *): its first act on a vendor command is +0xdc8c8
 *    (peer, 0), which compares a 4-byte and a 2-byte field - a 6-byte Bluetooth address; the frame keeps opcode at +3,
 *    operands at +8 and their count at +0x10. PDU 0x50 selects callback slot +0xc0 (null in this build: rejected), and a
 *    populated callback would get (peer, label, volume byte) - the same peer and label this hook receives.
 *  - +0xe1a98 (Target registration; one caller, +0x54f670) writes its second argument into the Target's SDP
 *    SupportedFeatures: 0x1d1 or 0x11, never Category 2 (Monitor/Amplifier, bit 0x02, where absolute volume belongs).
 *    The Controller is registered with features 2: the iPhone only advertises absolute volume as a controller.
 *  - The peer is the A2DP device bluetoothd itself resolves for the stream (tweak/PeerIdentity.c, verified on the
 *    device); its address is bound here, not read from a learned memory path.
 * Not verified on the device: the hooks' runtime behaviour, the response path, threading/locking, and whether Windows
 * uses absolute volume with this Target at all. */
#include "Tweak.h"
#include "VolumeControl.h"
#include "../shared/VolumeControl.h"
#include "../shared/VolumeMailbox.h"
#include "../shared/AudioRing.h"
#include "../shared/PeerName.h"
#include "../shared/SpeakaShared.h"
#import <Foundation/Foundation.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <pthread.h>
#include <substrate.h>
#include <string.h>
#if __has_feature(ptrauth_calls)
#include <ptrauth.h>
#define NATIVE_FN(type, address) ((type)ptrauth_sign_unauthenticated((void *)(address),ptrauth_key_function_pointer,0))
#else
#define NATIVE_FN(type, address) ((type)(address))
#endif

/* Recovered from 0xe0ff4, 0xdecac and serializer 0xdd580. */
typedef struct {
    uint8_t ctype, subunit_type, subunit_id, opcode;
    uint32_t padding;
    const uint8_t *operands;
    uint16_t count;
    uint8_t tail[6];
} NativeFrame;
_Static_assert(offsetof(NativeFrame,operands)==8 && offsetof(NativeFrame,count)==16 &&
               sizeof(NativeFrame)==24,"analysed OI_AVRCP_FRAME layout");
typedef uint32_t (*Receive)(const uint8_t *,uint8_t,const NativeFrame *);
typedef uint32_t (*Send)(const uint8_t *,uint8_t,const NativeFrame *);
typedef void (*Disconnect)(const uint8_t *,uint32_t);
typedef void (*StackLock)(void);
typedef uint64_t (*RegisterTarget)(uint64_t,uint32_t,uint64_t,uint64_t);
/* v9.9.9: read-only observation points, upstream of every filter in receive() (docs/AVRCP-VOLUME.md):
 * +0xe1d9c: the Target's command callback (peer, label, frame) - registration signs it (adr+paciza) and stores it
 *           at the AVRCP state +0x28; every incoming command of every opcode reaches it first.
 * +0xccb08: AVCTP's incoming connection indication (?, peer, channel) - Windows opening AVRCP.
 * +0x54f8c4: "Connecting AVRCP profile to device %{public}s" (manager, device) - iOS opening AVRCP itself. */
typedef void (*TargetCommand)(const uint8_t *,uint64_t,const NativeFrame *);
typedef void (*AvctpIncoming)(uint64_t,const uint8_t *,uint64_t);
typedef uint64_t (*AvrcpConnect)(uint64_t,uint64_t);
static Receive original_receive;
static Disconnect original_disconnect;
static RegisterTarget original_register_target;
static TargetCommand original_target_command;
static AvctpIncoming original_avctp_incoming;
static AvrcpConnect original_avrcp_connect;
static unsigned seen_commands,seen_incoming,seen_outgoing;
/* v9.9.10: fail closed. A replacement whose trampoline is missing must not call it (a null call would crash
 * bluetoothd - at startup, for the registration hook, on every relaunch). It degrades instead and logs once. */
#define VOLUME_NATIVE_FAILED 0xce5u   /* the status the native dispatcher returns for "no handler" (+0xe29fc) */
static unsigned missing_logged;
static void missing_trampoline(unsigned bit,const char *which) {
    if(__atomic_fetch_or(&missing_logged,bit,__ATOMIC_RELAXED)&bit) return;
    SpeakaLogNow("AVRCP_VOLUME FAILED CLOSED: %s has no original; degraded, not called",which);
}
static Send send_response;
static StackLock stack_lock,stack_unlock;
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static SPVolume state;
static SPVolumeMailbox *mailbox;
static uint64_t epoch;
static int mode,installed; /* 0 off, 1 observe, 2 experimental */
static uint8_t last_peer[6];
static int have_last_peer,configured;
/* v9.9.9: the initial volume. Until Windows engages absolute volume (registers for 0x0d or sets a volume) it
 * attenuates the stream itself, so Speaka stays at unity; once engaged it sends full scale and expects the sink to.
 * The level reported then is the last one this PC set (persisted) - so Windows adopting it causes no jump - or, for
 * a PC never seen, half: never maximum. */
#define SP_VOLUME_FIRST_DEFAULT 64u
static unsigned last_level=SP_VOLUME_FIRST_DEFAULT;
static int engaged;
/* v9.9.11: a level applied for a local change (the iPhone's own) is not Windows' choice; the helper only sets iOS's
 * volume for levels Windows chose. Set around sp_volume_local(), under `lock`. */
static int applying_local;
static unsigned observed;
static dispatch_queue_t queue;
static dispatch_source_t timer;
static const uint8_t native_events[]={1,2,8,9,10,11,12};

static int peek(uint64_t address,void *out,size_t n) {
    if(address<0x1000 || address>0x0000ffffffffffffull || n>4096) return 0;
    vm_size_t got=0;
    return vm_read_overwrite(mach_task_self(),(vm_address_t)address,(vm_size_t)n,
            (vm_address_t)out,&got)==KERN_SUCCESS && got==n;
}
/* The gain is kept in the mailbox the tweak created at install, so a value Windows sets before the helper runs (it
 * starts at configuration) is the value the helper plays at. */
static void persist_level(void *unused);
static int apply(void *unused,uint8_t value) {
    (void)unused;
    if(!mailbox) return 0;
    sp_volume_mailbox_set_from(mailbox,1,!applying_local,value);
    engaged=1;
    if(value!=last_level) {last_level=value;if(queue) dispatch_async_f(queue,NULL,persist_level);}
    SpeakaLogNow("AVRCP_VOLUME set value=%u (%u%%) helper_live=%d",value,(value*100u+63u)/127u,
                 sp_volume_mailbox_ready(mailbox));
    return 1;
}
static int send_reply(void *unused,const uint8_t peer[6],uint8_t label,uint8_t code,
                      const uint8_t *p,size_t n) {
    (void)unused;
    NativeFrame frame={code,9,0,0,0,p,(uint16_t)n,{0}};
    uint32_t result=send_response(peer,label,&frame);
    SpeakaLogNow("AVRCP_VOLUME tx label=%u response=0x%02x pdu=0x%02x status=%u",
                  label,code,p[3],result);
    return result==0;
}
static const SPVolumeIO io={apply,send_reply,NULL};
/* A new epoch, no subscription, unity gain - and the last verified Speaka PC bound again, so its AVRCP requests are
 * answered even if they arrive before the next A2DP configuration. */
static void reset_locked(const char *why) {
    ++epoch;sp_volume_reset(&state);sp_volume_mailbox_set(mailbox,0,127);engaged=0;
    if(mode==2 && have_last_peer) sp_volume_bind(&state,last_peer,epoch,(uint8_t)last_level);
    char a[18];peer_address_text(last_peer,a);
    SpeakaLogNow("AVRCP_VOLUME reset why=%s epoch=%llu bound=%s",why,(unsigned long long)epoch,
                 state.bound?a:"none");
}
static void persist_level(void *unused) {
    (void)unused;
    int v=(int)last_level;
    CFNumberRef n=CFNumberCreate(kCFAllocatorDefault,kCFNumberIntType,&v);
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_LEVEL),n,CFSTR(SPEAKA_PREFS_DOMAIN));
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    if(n) CFRelease(n);
}
static void persist_peer(void *unused) {
    (void)unused;
    char a[18];
    pthread_mutex_lock(&lock);peer_address_text(last_peer,a);pthread_mutex_unlock(&lock);
    CFStringRef v=CFStringCreateWithCString(kCFAllocatorDefault,a,kCFStringEncodingUTF8);
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_PEER),v,CFSTR(SPEAKA_PREFS_DOMAIN));
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    if(v) CFRelease(v);
}
void SpeakaVolumeDisconnect(void) {
    if(!installed) return;
    pthread_mutex_lock(&lock);configured=0;reset_locked("stream_closed");pthread_mutex_unlock(&lock);
}
/* The PC configured the speaker: bind the A2DP device bluetoothd resolved for this stream (PeerIdentity.c). */
void SpeakaVolumeConfigure(const uint8_t *peer) {
    if(!installed) return;
    pthread_mutex_lock(&lock);
    configured=1;
    int changed=peer && (!have_last_peer || memcmp(last_peer,peer,6)!=0);
    if(peer) {memcpy(last_peer,peer,6);have_last_peer=1;}
    if(changed) last_level=SP_VOLUME_FIRST_DEFAULT;   /* another PC: its level is unknown */
    reset_locked(peer?"configured":"configured_unidentified");
    pthread_mutex_unlock(&lock);
    if(changed && queue) dispatch_async_f(queue,NULL,persist_peer);
}
static uint32_t receive(const uint8_t *address,uint8_t label,const NativeFrame *incoming) {
    NativeFrame frame;uint8_t peer[6],p[32];int consumed=0;
    if(peek((uint64_t)incoming,&frame,sizeof frame) && peek((uint64_t)address,peer,6) &&
       frame.subunit_type==9 && frame.subunit_id==0 && frame.opcode==0 &&
       frame.count>=4 && peek((uint64_t)frame.operands,p,frame.count<sizeof p?frame.count:sizeof p)) {
        pthread_mutex_lock(&lock);
        if(p[0]==0 && p[1]==0x19 && p[2]==0x58 &&
           (p[3]==0x50 || p[3]==0x31 || p[3]==0x10)) {
            if(mode==2)
                consumed=sp_volume_command(&state,&io,peer,epoch,label,frame.ctype,p,frame.count,
                                            native_events,sizeof native_events);
            /* Windows registered for volume: from now on it expects the sink to apply the level it was told. */
            if(consumed && p[3]==0x31 && state.subscribed && !engaged && mailbox) {
                sp_volume_mailbox_set_from(mailbox,1,0,state.value);engaged=1;   /* reported by Speaka, not Windows' */
                SpeakaLogNow("AVRCP_VOLUME engaged value=%u reported_as_current",state.value);
            }
            if(observed++<64) {
                char name[18];peer_address_text(peer,name);
                SpeakaLogNow("AVRCP_VOLUME rx peer=%s label=%u ctype=%u pdu=0x%02x param=0x%02x count=%u match=%d "
                              "configured=%d mode=%d handled=%s",name,label,frame.ctype,p[3],frame.count>7?p[7]:0,
                              frame.count,state.bound && !memcmp(state.peer,peer,6),configured,mode,
                              consumed?"speaka":"native");
            }
        }
        pthread_mutex_unlock(&lock);
    }
    /* A consumed volume command gets exactly one response path; everything else is native. */
    if(consumed) return 0;
    if(!original_receive) {missing_trampoline(1u,"receive");return VOLUME_NATIVE_FAILED;}
    return original_receive(address,label,incoming);
}
static void disconnected(const uint8_t *address,uint32_t reason) {
    uint8_t peer[6];
    if(peek((uint64_t)address,peer,6)) {
        pthread_mutex_lock(&lock);
        if(state.bound && !memcmp(state.peer,peer,6)) reset_locked("avrcp_disconnected");
        pthread_mutex_unlock(&lock);
    }
    if(original_disconnect) original_disconnect(address,reason);
    else missing_trampoline(2u,"disconnect");
}
/* The Target's SDP SupportedFeatures. observe: logged as advertised. experimental: Category 2 (Monitor/Amplifier) is
 * added - absolute volume belongs to it, and an AVRCP controller may not send SetAbsoluteVolume to a target that
 * doesn't claim it. Runs once, at bluetoothd's AVRCP registration. */
static uint64_t register_target(uint64_t descriptor,uint32_t features,uint64_t a2,uint64_t a3) {
    uint32_t advertised=mode==2?(features|0x02u):features;
    SpeakaLogNow("AVRCP_VOLUME sdp target_features=%#x advertised=%#x category2=%d",features,advertised,
                 (advertised&0x02u)!=0);
    SpeakaSetStatus("diag.v106.avrcp_sdp","target features %#x, advertised %#x",features,advertised);
    /* No original: report failure - bluetoothd logs "Failed to register as an AVRCP Target" and runs without it. */
    if(!original_register_target) {missing_trampoline(4u,"target registration");return VOLUME_NATIVE_FAILED;}
    return original_register_target(descriptor,advertised,a2,a3);
}
/* v9.9.9 observation: every incoming Target command, before any filter. Logged, then native. */
static void target_command(const uint8_t *address,uint64_t label,const NativeFrame *incoming) {
    unsigned n=__atomic_add_fetch(&seen_commands,1u,__ATOMIC_RELAXED);
    if(n<=64u || n%100u==0u) {
        NativeFrame frame;uint8_t peer[6],ops[8]={0};char a[18]="?";
        if(peek((uint64_t)address,peer,6)) peer_address_text(peer,a);
        if(peek((uint64_t)incoming,&frame,sizeof frame)) {
            if(frame.count) (void)peek((uint64_t)frame.operands,ops,frame.count<8?frame.count:8);
            SpeakaLogNow("AVRCP_VOLUME tg_rx #%u peer=%s label=%llu ctype=%u subunit=%u/%u opcode=%#x count=%u "
                         "ops=%02x%02x%02x%02x%02x%02x%02x%02x%s%s",n,a,(unsigned long long)label,frame.ctype,
                         frame.subunit_type,frame.subunit_id,frame.opcode,frame.count,ops[0],ops[1],ops[2],ops[3],
                         ops[4],ops[5],ops[6],ops[7],frame.opcode==0?" vendor_pdu=":"",
                         frame.opcode==0?(ops[3]==0x10?"0x10":ops[3]==0x31?"0x31":ops[3]==0x50?"0x50":"other"):"");
        } else SpeakaLogNow("AVRCP_VOLUME tg_rx #%u peer=%s frame_unreadable",n,a);
    }
    if(original_target_command) original_target_command(address,label,incoming);
    else missing_trampoline(8u,"target command");
}
static void avctp_incoming(uint64_t context,const uint8_t *address,uint64_t channel) {
    unsigned n=__atomic_add_fetch(&seen_incoming,1u,__ATOMIC_RELAXED);
    if(n<=16u) {
        uint8_t peer[6];char a[18]="?";
        if(peek((uint64_t)address,peer,6)) peer_address_text(peer,a);
        SpeakaLogNow("AVRCP_VOLUME avctp_incoming #%u peer=%s channel=%#llx",n,a,(unsigned long long)channel);
    }
    if(original_avctp_incoming) original_avctp_incoming(context,address,channel);
    else missing_trampoline(16u,"AVCTP incoming");
}
static uint64_t avrcp_connect(uint64_t manager,uint64_t device) {
    unsigned n=__atomic_add_fetch(&seen_outgoing,1u,__ATOMIC_RELAXED);
    uint8_t peer[6];char a[18]="?";
    if(n<=16u && peek(device+PEER_DEVICE_ADDRESS,peer,6)) peer_address_text(peer,a);
    uint64_t result=VOLUME_NATIVE_FAILED;
    if(original_avrcp_connect) result=original_avrcp_connect(manager,device);
    else missing_trampoline(32u,"AVRCP connect");
    if(n<=16u) SpeakaLogNow("AVRCP_VOLUME avrcp_outgoing #%u peer=%s result=%#llx",n,a,(unsigned long long)result);
    return result;
}
/* A local Speaka change (BTPSAudioHelper --volume) - the reverse direction, CHANGED to a registered Windows. */
static void tick(void *unused) {
    (void)unused;
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    CFPropertyListRef request=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_REQUEST),CFSTR(SPEAKA_PREFS_DOMAIN));
    int value=-1;double when=0;
    if(request) {
        if(CFGetTypeID(request)==CFDictionaryGetTypeID()) {
            CFTypeRef v=CFDictionaryGetValue(request,CFSTR("value"));
            CFTypeRef t=CFDictionaryGetValue(request,CFSTR("time"));
            if(v && CFGetTypeID(v)==CFNumberGetTypeID()) CFNumberGetValue(v,kCFNumberIntType,&value);
            if(t && CFGetTypeID(t)==CFNumberGetTypeID()) CFNumberGetValue(t,kCFNumberDoubleType,&when);
        }
        CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_REQUEST),NULL,CFSTR(SPEAKA_PREFS_DOMAIN));
        if(!CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN))) value=-1;
        CFRelease(request);
    }
    pthread_mutex_lock(&lock);
    int ready=state.bound && configured && sp_volume_mailbox_ready(mailbox);
    if(!ready) state.pending_local=0;
    pthread_mutex_unlock(&lock);
    if(!ready) return;
    /* Same stack mutex used around native AVRCP calls at 0x55a250/0x55a268. Lock order: native stack -> our state;
     * receive is assumed to run in the stack. This threading assumption still needs live validation. */
    stack_lock();pthread_mutex_lock(&lock);
    double age=CFAbsoluteTimeGetCurrent()-when;
    if(state.bound && value>=0 && value<=127 && age>=0 && age<2) {
        applying_local=1;
        int ok=sp_volume_local(&state,&io,(unsigned)value);
        applying_local=0;
        SpeakaLogNow("AVRCP_VOLUME local value=%d accepted=%d subscribed=%u",value,ok,state.subscribed);
    }
    (void)sp_volume_flush(&state,&io);
    pthread_mutex_unlock(&lock);stack_unlock();
}
static int load_last_peer(uint8_t out[6]) {
    CFPropertyListRef v=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_PEER),CFSTR(SPEAKA_PREFS_DOMAIN));
    char text[32]={0};int ok=0;
    if(v && CFGetTypeID(v)==CFStringGetTypeID() && CFStringGetCString(v,text,sizeof text,kCFStringEncodingUTF8))
        ok=peer_address_bytes(text,out) && peer_address_plausible(out);
    if(v) CFRelease(v);
    return ok;
}
void SpeakaVolumeInstall(uintptr_t base,int exact_uuid) {
    /* v9.9.11: on unless explicitly turned off (live-verified on iOS 17.0 with Windows 11). The experiment's old key
     * is removed so its values - off by default - never decide this. */
    CFPropertyListRef legacy=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_MODE),CFSTR(SPEAKA_PREFS_DOMAIN));
    if(legacy) {
        CFRelease(legacy);
        CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_MODE),NULL,CFSTR(SPEAKA_PREFS_DOMAIN));
        (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
        SpeakaLogNow("AVRCP_VOLUME migrated: the experiment's mode key removed; volume sync is standard");
    }
    mode=2;
    CFPropertyListRef v=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_SYNC),CFSTR(SPEAKA_PREFS_DOMAIN));
    if(v && CFGetTypeID(v)==CFStringGetTypeID()) {
        if(CFEqual(v,CFSTR("observe"))) mode=1;
        if(CFEqual(v,CFSTR("off"))) mode=0;
    }
    if(v) CFRelease(v);
    if(!mode) {SpeakaLogNow("AVRCP_VOLUME off (SpeakaVolumeSync=off)");return;}
    /* UUID alone is insufficient; require code AND native capability/table bytes. */
    static const struct {uintptr_t offset;uint32_t words[4];} checks[]={
        {0xe0ff4,{0xd503237f,0xa9ba6ffc,0xa90167fa,0xa9025ff8}},
        {0xe28e4,{0xd503237f,0xa9bf7bfd,0x910003fd,0xf0004548}},
        {0xa042c,{0xd503237f,0xa9be4ff4,0xa9017bfd,0x910043fd}},
        {0xa0528,{0xd503237f,0xa9be4ff4,0xa9017bfd,0x910043fd}},
        {0x55b2f4,{0xd503237f,0xa9be4ff4,0xa9017bfd,0x910043fd}},
        {0xe1a98,{0xd503237f,0xd102c3ff,0xa9075ff8,0xa90857f6}},
        {0xe1d9c,{0xd503237f,0xd10203ff,0xa9045ff8,0xa90557f6}},
        {0xccb08,{0xd503237f,0xd10143ff,0xa9015ff8,0xa90257f6}},
        {0x54f8c4,{0xd503237f,0xd10303ff,0xa9085ff8,0xa90957f6}}
    };
    int match=exact_uuid;uint32_t words[4];uint8_t events[7];uint64_t slot=1;
    for(size_t i=0;i<sizeof checks/sizeof checks[0];i++)
        match &= peek(base+checks[i].offset,words,sizeof words) && !memcmp(words,checks[i].words,sizeof words);
    match &= peek(base+0x779498,events,sizeof events) && !memcmp(events,native_events,sizeof events);
    match &= peek(base+0x936e38,&slot,sizeof slot) && slot==0;
    if(!match) {SpeakaLogNow("AVRCP_VOLUME refused: build/code/table mismatch");return;}
    send_response=NATIVE_FN(Send,base+0xe28e4);
    stack_lock=NATIVE_FN(StackLock,base+0xa042c);stack_unlock=NATIVE_FN(StackLock,base+0xa0528);
    if(mode==2) {
        AudioRingHello hello;
        if(!AudioRelayIdentity(&hello)) {SpeakaLogNow("AVRCP_VOLUME refused: no ring identity");return;}
        char sidecar[256];
        snprintf(sidecar,sizeof sidecar,AUDIO_RING_PATH_FORMAT ".volume",hello.pid,(unsigned long long)hello.token);
        mailbox=sp_volume_mailbox_open(sidecar,hello.pid,hello.token,1);
        if(!mailbox) {SpeakaLogNow("AVRCP_VOLUME refused: sidecar unavailable");return;}
        have_last_peer=load_last_peer(last_peer);
        CFPropertyListRef level=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_LEVEL),CFSTR(SPEAKA_PREFS_DOMAIN));
        int saved=-1;
        if(level && CFGetTypeID(level)==CFNumberGetTypeID()) CFNumberGetValue(level,kCFNumberIntType,&saved);
        if(level) CFRelease(level);
        if(have_last_peer && saved>=0 && saved<=127) last_level=(unsigned)saved;
    }
    /* Before main: the Target registers its SDP record during bluetoothd's startup, after this. */
    MSHookFunction((void *)(base+0xe1a98),(void *)register_target,(void **)&original_register_target);
    if(!original_register_target) {SpeakaLogNow("AVRCP_VOLUME registration trampoline missing");return;}
    /* Observation, both modes: logged and passed on unchanged. A missing trampoline only loses the log line. */
    MSHookFunction((void *)(base+0xe1d9c),(void *)target_command,(void **)&original_target_command);
    MSHookFunction((void *)(base+0xccb08),(void *)avctp_incoming,(void **)&original_avctp_incoming);
    MSHookFunction((void *)(base+0x54f8c4),(void *)avrcp_connect,(void **)&original_avrcp_connect);
    if(!original_target_command || !original_avctp_incoming || !original_avrcp_connect)
        SpeakaLogNow("AVRCP_VOLUME observation trampolines: command=%d incoming=%d outgoing=%d",
                     original_target_command!=NULL,original_avctp_incoming!=NULL,original_avrcp_connect!=NULL);
    MSHookFunction((void *)(base+0xe0ff4),(void *)receive,(void **)&original_receive);
    if(!original_receive) {SpeakaLogNow("AVRCP_VOLUME receive trampoline missing");return;}
    if(mode==2) {
        MSHookFunction((void *)(base+0x55b2f4),(void *)disconnected,(void **)&original_disconnect);
        if(!original_disconnect) {mode=1;SpeakaLogNow("AVRCP_VOLUME no disconnect trampoline; observe only");}
    }
    installed=1;
    queue=dispatch_queue_create("com.parv.speaka.volume",DISPATCH_QUEUE_SERIAL);
    pthread_mutex_lock(&lock);
    if(mode==2 && have_last_peer) {++epoch;sp_volume_bind(&state,last_peer,epoch,(uint8_t)last_level);}
    pthread_mutex_unlock(&lock);
    if(mode==2 && queue) {
        timer=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,queue);
        if(timer) {
            dispatch_source_set_timer(timer,dispatch_time(DISPATCH_TIME_NOW,100*NSEC_PER_MSEC),100*NSEC_PER_MSEC,20*NSEC_PER_MSEC);
            dispatch_set_context(timer,NULL);
            dispatch_source_set_event_handler_f(timer,tick);dispatch_resume(timer);
        } else SpeakaLogNow("AVRCP_VOLUME no timer: local changes disabled");
    }
    char a[18];peer_address_text(last_peer,a);
    SpeakaLogNow("AVRCP_VOLUME installed mode=%s last_peer=%s last_level=%u",
                 mode==2?"on":"observe",have_last_peer?a:"none",last_level);
}
