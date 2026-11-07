#include "../shared/VolumeControl.h"
#include "../shared/VolumeMailbox.h"
/* AudioHelper.m - BTPSAudioHelper: plays the audio bluetoothd hands it. launchd starts it on demand, as mobile,
 * when the tweak connects to 127.0.0.1:37594 (layout/Library/LaunchDaemons/com.parv.speaka.audio.plist).
 *
 * Live SBC is decoded and played only in this process, never inside bluetoothd. The output follows the app's Audio
 * Output choice - the built-in speaker (default), headphones only, or any device iOS is using - minus a never-play
 * list (OutputPolicy.h). Since v9.1 a running output is kept through stream changes, and the helper never
 * re-activates its audio session faster than the rearm wait on its own: every session change re-routes iOS, which
 * made bluetoothd START or SUSPEND the PC's stream, which the helper answered with another session change.
 */
#include "CodecDecoder.h"
#include "AACSelfTest.h"
#include "OutputPolicy.h"
#include "../shared/PeerName.h"
#include "../shared/SpeakaShared.h"
#import <stdio.h>
#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <notify.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* v8.9 output selection; only the stream thread reads or writes these. */
static int g_output_mode=OUTPUT_MODE_SPEAKER;
static int g_aac_enabled=0;   /* SpeakaAAC: offer AAC-LC alongside SBC (experimental, SBC is the default) */
static NSArray<NSDictionary *> *g_never_outputs;
static int g_output_notify=-1;
static void publish_status(NSString *state,NSString *reason,BOOL force);
/* v9.1 session bookkeeping, stream thread only: whether the helper's AVAudioSession is active, and when the helper
 * itself last activated or deactivated it. A route change that arrives right after that is the helper's own echo. */
static int g_session_active;
static uint64_t g_session_changed_ms;
static uint64_t helper_ms(void);
/* v8.9.3: every helper line gets the same HH:MM:SS.mmm prefix as the daemon log, so the two can be read side by
 * side (the Settings export now shows both). Defined above PCM_CAPACITY: the render callback below never logs. */
static void hlog(FILE *stream,const char *fmt,...) __attribute__((format(printf,2,3)));
static void hlog(FILE *stream,const char *fmt,...) {
    struct timespec ts;struct tm tm;
    if(clock_gettime(CLOCK_REALTIME,&ts)) {ts.tv_sec=time(NULL);ts.tv_nsec=0;}
    time_t secs=ts.tv_sec;localtime_r(&secs,&tm);
    fprintf(stream,"%02d:%02d:%02d.%03ld ",tm.tm_hour,tm.tm_min,tm.tm_sec,(long)(ts.tv_nsec/1000000L));
    va_list ap;va_start(ap,fmt);vfprintf(stream,fmt,ap);va_end(ap);
    fflush(stream);
}

/* The codec layer logs through a plain (fmt, ...) sink; send it to stdout like every other helper line. */
static void hlog_line(const char *fmt,...) {
    char line[512];
    va_list ap;va_start(ap,fmt);vsnprintf(line,sizeof line,fmt,ap);va_end(ap);
    hlog(stdout,"%s",line);
}
/* v9.1: every activation and deactivation is logged with its time, so the helper log lines up with the phone's own
 * START/SUSPEND lines (V882_CONTROL ... by=phone) in the daemon log. */
static void note_session(int active,const char *why) {
    g_session_active=active;g_session_changed_ms=helper_ms();
    hlog(stdout,"V91_SESSION active=%d why=%s mode=%s\n",active,why,output_mode_name(g_output_mode));
}

#define PCM_CAPACITY 16384u /* stereo frames, <= 372ms; discard oldest on overflow */
#define AUDIO_FRAMES 512u
/* v9.9.1 jitter buffer. At stream start (and some Windows stacks ramp their A2DP transmit rate over ~25 s, sending
 * ~24 frames/s when 47 are needed) the render callback used to start on the first frame and interleave silence
 * with the sparse real frames: audible stutter. Instead it waits for PCM_PRIME stereo frames to accumulate, then
 * plays; if it ever drains to empty it re-primes rather than emitting choppy silence between real frames. This
 * only reorders when the gap is heard (a clean short wait, then smooth); it cannot create frames the source has
 * not sent, so a sustained arrival deficit still limits how soon smooth playback begins. 0.25 s is well within a
 * speaker's latency budget and covers ordinary Bluetooth jitter. */
#define PCM_PRIME 12000u    /* stereo frames (~0.25 s at 48 kHz) buffered before playback starts or resumes */
/* v9.9.4: a warm resume - a pause inside a connection that has already streamed at real time - needs far less lead:
 * the v9.8.91 resumes' packets arrived at most 38 ms apart. 3072 frames (64 ms) instead of 250 ms; an underrun
 * doubles the stream's target (to PCM_PRIME), so a jittery link earns its lead back instead of stuttering. */
#define PCM_PRIME_WARM 3072u
typedef struct {
    pthread_mutex_t lock;
    int16_t pcm[PCM_CAPACITY*2];
    unsigned read, count, rate;
    int32_t error, stopping;
    uint64_t dropped;
    uint64_t render_callbacks, submitted_frames, submitted_nonzero, silence_frames;
    uint64_t render_tick; /* cumulative enqueued PCM frames; never reset by logs */
    unsigned priming;     /* v9.9.1: 1 while filling the jitter buffer; the callback emits silence meanwhile */
    uint64_t reprimes;    /* times the buffer drained mid-stream and had to re-fill */
    int32_t input_starved;   /* v9.9.2: the input can't sustain playback: hold (silence, no drain, keep newest) */
    unsigned paused;         /* v9.9.2: an interruption stopped the queue; queue and PCM are kept for the resume */
    uint64_t silence_gated, silence_priming, silence_underrun, silence_busy;   /* why silence was played (render) */
    uint32_t decoded_frames; /* PCM frames decoded: the progress a paused helper reports to keep the lease */
    unsigned prime_target;   /* v9.9.4: this stream's lead (0 = PCM_PRIME); doubles after an underrun */
    unsigned trim_silence;   /* v9.9.4: a warm resume drops Windows' leading digital silence until real audio */
    SPVolumeMailbox *volume; /* immutable pointer until AudioQueueDispose completes */
    AudioQueueRef queue;
} Output;

static void render(void *context,AudioQueueRef queue,AudioQueueBufferRef buffer) {
    Output *o=context;
    if(__atomic_load_n(&o->stopping,__ATOMIC_ACQUIRE)) return;
    __atomic_fetch_add(&o->render_callbacks,1,__ATOMIC_RELAXED);
    int16_t *out=buffer->mAudioData;
    unsigned take=0,nonzero=0;
    memset(out,0,AUDIO_FRAMES*4u);
    /* Never wait on the decoder. If it holds the PCM lock, emit silence. */
    int why=0;   /* 0 playing, 1 gated (input starved), 2 underrun (ring ran dry), 3 busy (lock), 4 priming the lead */
    if(pthread_mutex_trylock(&o->lock)==0) {
        /* v9.9.2: while the input can't sustain real time, hold: silence, and leave the PCM in place (the writer
         * keeps only the newest PCM_PRIME frames), so playback restarts at once from a full lead. */
        if(__atomic_load_n(&o->input_starved,__ATOMIC_RELAXED)) why=1;
        /* v9.9.1: hold off while priming; once the lead is built, play. If the buffer empties mid-stream, prime
         * again so the next audio is continuous instead of silence spliced between sparse frames. */
        else if(o->priming) { if(o->count>=(o->prime_target?o->prime_target:PCM_PRIME)) o->priming=0; else why=4; }
        else if(o->count==0) {
            o->priming=1; o->reprimes++; why=2;
            /* v9.9.4: a lead that ran dry was too small for this link: double it, up to the cold lead. */
            if(o->prime_target && o->prime_target<PCM_PRIME)
                o->prime_target=o->prime_target*2u<PCM_PRIME?o->prime_target*2u:PCM_PRIME;
        }
        if(!why) {
            take=o->count<AUDIO_FRAMES?o->count:AUDIO_FRAMES;
            for(unsigned i=0;i<take;i++) {
                unsigned slot=(o->read+i)%PCM_CAPACITY;
                out[i*2]=o->pcm[slot*2];out[i*2+1]=o->pcm[slot*2+1];
                nonzero+=(out[i*2]!=0)+(out[i*2+1]!=0);
            }
            o->read=(o->read+take)%PCM_CAPACITY;o->count-=take;
            if(take<AUDIO_FRAMES) why=2;   /* ran dry part-way through this buffer */
        }
        pthread_mutex_unlock(&o->lock);
    } else why=3;
    if(__atomic_load_n(&o->stopping,__ATOMIC_ACQUIRE)) return;
    /* Optional Speaka gain is applied AFTER SBC/AAC decode and ring dequeue.
     * Unity does no writes. No I/O, session changes, or allocation in this callback. */
    unsigned gain=sp_volume_mailbox_gain(o->volume);
    sp_volume_pcm(out,take*2u,gain);
    if(gain<127) {nonzero=0;for(unsigned i=0;i<take*2u;i++) nonzero+=out[i]!=0;}
    buffer->mAudioDataByteSize=AUDIO_FRAMES*4u;
    OSStatus e=AudioQueueEnqueueBuffer(queue,buffer,0,NULL);
    if(e) {
        /* A stop can begin between the check and enqueue. That is teardown,
         * not a playback fault to carry into the next START. */
        if(!__atomic_load_n(&o->stopping,__ATOMIC_ACQUIRE))
            __atomic_store_n(&o->error,e,__ATOMIC_RELAXED);
    } else {
        __atomic_fetch_add(&o->render_tick,take,__ATOMIC_RELAXED);
        __atomic_fetch_add(&o->submitted_frames,take,__ATOMIC_RELAXED);
        __atomic_fetch_add(&o->submitted_nonzero,nonzero,__ATOMIC_RELAXED);
        __atomic_fetch_add(&o->silence_frames,AUDIO_FRAMES-take,__ATOMIC_RELAXED);
        uint64_t *cause=why==1?&o->silence_gated:why==4?&o->silence_priming:why==3?&o->silence_busy:&o->silence_underrun;
        if(take<AUDIO_FRAMES) __atomic_fetch_add(cause,AUDIO_FRAMES-take,__ATOMIC_RELAXED);
    }
}
static void stop_output(Output *o,AudioRing *s,const char *reason,int release) {
    /* v9.1: tears down the AudioQueue; the audio session is released only when release is set. A queue that is
     * rebuilt at once (new sample rate, a re-applied speaker override) keeps its session: every session change
     * re-routes iOS, and that is what made bluetoothd START and SUSPEND the PC's stream in the 23:33 loop. */
    __atomic_store_n(&o->stopping,1,__ATOMIC_RELEASE);
    int had_queue=o->queue!=NULL;
    if(had_queue) {
        uint64_t current_generation=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
        hlog(stdout,"V882_AUDIO_STOP reason=%s generation=%llu active=%u session=%s\n",reason,
            (unsigned long long)(current_generation>>1),(unsigned)(current_generation&1u),release?"released":"kept");
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_HEALTH,
            (uint32_t)current_generation,
            (uint32_t)__atomic_load_n(&o->render_tick,__ATOMIC_RELAXED),0});
        AudioQueueStop(o->queue,true);
        AudioQueueDispose(o->queue,true);o->queue=NULL;o->rate=0;o->paused=0;
        __atomic_store_n(&o->error,0,__ATOMIC_RELAXED);
        pthread_mutex_lock(&o->lock);o->read=o->count=0;pthread_mutex_unlock(&o->lock);
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,0,1});
    }
    if(!release) return;
    int had_session=g_session_active;
    if(had_session) {
        [[AVAudioSession sharedInstance] setActive:NO withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:NULL];
        note_session(0,reason);
    }
    if(had_queue || had_session) publish_status(@"idle",[NSString stringWithUTF8String:reason]?:@"",NO);
}
static int speaker_selected(AVAudioSession *session) {
    NSArray<AVAudioSessionPortDescription *> *ports=session.currentRoute.outputs;
    return ports.count==1 && [ports.firstObject.portType isEqualToString:AVAudioSessionPortBuiltInSpeaker];
}
/* ---- v8.9 output selection ---------------------------------------------------------------------------- */
static output_kind_t port_kind(NSString *type) {
    if([type isEqualToString:AVAudioSessionPortBuiltInSpeaker]) return OUTPUT_PORT_SPEAKER;
    if([type isEqualToString:AVAudioSessionPortBuiltInReceiver]) return OUTPUT_PORT_RECEIVER;
    if([type isEqualToString:AVAudioSessionPortHeadphones] || [type isEqualToString:AVAudioSessionPortUSBAudio])
        return OUTPUT_PORT_HEADPHONES;
    if([type isEqualToString:AVAudioSessionPortBluetoothA2DP] || [type isEqualToString:AVAudioSessionPortBluetoothLE] ||
       [type isEqualToString:AVAudioSessionPortBluetoothHFP]) return OUTPUT_PORT_BLUETOOTH;
    return OUTPUT_PORT_OTHER;
}
static NSString *text_field(id item,NSString *key) {
    id value=[item isKindOfClass:[NSDictionary class]]?((NSDictionary *)item)[key]:nil;
    return [value isKindOfClass:[NSString class]]?value:@"";
}
/* Settings writes the mode and the never-play list; re-read on every connection and on SPEAKA_NOTIFY_OUTPUT. */
static void load_output_settings(void) {
    CFStringRef domain=CFSTR(SPEAKA_PREFS_DOMAIN);
    (void)CFPreferencesAppSynchronize(domain);
    id mode=CFBridgingRelease(CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_OUTPUT_MODE),domain));
    g_output_mode=output_mode_from_string([mode isKindOfClass:[NSString class]]?[(NSString *)mode UTF8String]:NULL);
    g_aac_enabled=CFPreferencesGetAppBooleanValue(CFSTR(SPEAKA_KEY_AAC),domain,NULL)?1:0;
    id never=CFBridgingRelease(CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_NEVER_OUTPUTS),domain));
    NSMutableArray<NSDictionary *> *list=[NSMutableArray array];
    if([never isKindOfClass:[NSArray class]])
        for(id item in (NSArray *)never)
            if([item isKindOfClass:[NSDictionary class]] && list.count<OUTPUT_MAX_NEVER) [list addObject:item];
    g_never_outputs=list;
    hlog(stdout,"V889_OUTPUT_SETTINGS mode=%s never_play=%lu\n",output_mode_name(g_output_mode),
            (unsigned long)list.count);
    fflush(stdout);
}
/* OUTPUT_ROUTE_OK when playback may use iOS's current output route; otherwise the refusal reason. */
static int route_verdict(AVAudioSession *session,NSString **device) {
    NSArray<AVAudioSessionPortDescription *> *outputs=session.currentRoute.outputs;
    output_port_t ports[OUTPUT_MAX_PORTS];output_never_t never[OUTPUT_MAX_NEVER];
    size_t count=outputs.count<OUTPUT_MAX_PORTS?outputs.count:OUTPUT_MAX_PORTS,never_count=0,bad=0;
    for(size_t i=0;i<count;i++) {
        AVAudioSessionPortDescription *port=outputs[i];
        ports[i]=(output_port_t){port_kind(port.portType),port.UID.UTF8String?:"",port.portName.UTF8String?:""};
    }
    for(NSDictionary *item in g_never_outputs) {
        if(never_count==OUTPUT_MAX_NEVER) break;
        never[never_count++]=(output_never_t){text_field(item,@"uid").UTF8String,text_field(item,@"name").UTF8String};
    }
    int verdict=output_route_check(g_output_mode,ports,count,never,never_count,&bad);
    if(device) *device=(verdict && bad<count)?outputs[bad].portName:nil;
    return verdict;
}
static dispatch_queue_t g_status_queue;
static NSString *g_status_state,*g_status_reason;
/* v9.9.4: the PC as the audio route listed it while the receiver was connected ({name,uid,type}), for the app to
 * name it; and whether the session mixes with other apps' audio (OutputPolicy.h, mix_state_t). */
static NSDictionary *g_pc_port;
static mix_state_t g_mix={1,0};
static uint64_t g_status_written_ms;
static uint64_t helper_ms(void) {
    struct timespec ts;
    if(clock_gettime(CLOCK_MONOTONIC,&ts)) return 0;
    return (uint64_t)ts.tv_sec*1000u+(uint64_t)ts.tv_nsec/1000000u;
}
/* Tells Settings what is playing where: immediately on a change, otherwise at most every 5 s. The write happens
 * on a private queue so a slow preferences daemon never stalls packet draining. */
/* v9.9.4: remember the PC if this route lists it (PeerName.h). Reading a route plays and changes nothing. */
static void capture_pc_port(AVAudioSessionRouteDescription *route,const char *when) {
    NSArray<AVAudioSessionPortDescription *> *outputs=route.outputs;
    if(outputs.count==0 || outputs.count>8) return;
    const char *names[8],*types[8];
    for(NSUInteger i=0;i<outputs.count;i++) {names[i]=outputs[i].portName.UTF8String?:"";types[i]=outputs[i].portType.UTF8String?:"";}
    int pick=peer_pick_port(names,types,(unsigned)outputs.count);
    if(pick<0) return;
    AVAudioSessionPortDescription *port=outputs[(NSUInteger)pick];
    NSDictionary *pc=@{@"name":port.portName?:@"",@"uid":port.UID?:@"",@"type":port.portType?:@""};
    @synchronized([AVAudioSession class]) {
        if([g_pc_port isEqualToDictionary:pc]) return;
        g_pc_port=pc;
    }
    hlog(stdout,"V102_PC_ROUTE name=%s uid=%s type=%s when=%s\n",port.portName.UTF8String?:"",
         port.UID.UTF8String?:"",port.portType.UTF8String?:"",when);
}
static void forget_pc_port(void) { @synchronized([AVAudioSession class]) { g_pc_port=nil; } }
static void publish_status(NSString *state,NSString *reason,BOOL force) {
    uint64_t now=helper_ms();
    reason=reason?:@"";
    BOOL changed=![state isEqualToString:g_status_state] || ![reason isEqualToString:g_status_reason];
    if(!force && !changed && now-g_status_written_ms<5000u) return;
    g_status_state=state;g_status_reason=reason;g_status_written_ms=now;
    AVAudioSession *session=[AVAudioSession sharedInstance];
    NSMutableArray *outputs=[NSMutableArray array];
    for(AVAudioSessionPortDescription *port in session.currentRoute.outputs)
        [outputs addObject:@{@"name":port.portName?:@"",@"type":port.portType?:@"",@"uid":port.UID?:@""}];
    /* v8.9.3: the route's output volume (0..1). The 21:05 log showed 0% while "playing", which is silent. */
    NSDictionary *pc=nil;
    @synchronized([AVAudioSession class]) { pc=g_pc_port; }
    NSDictionary *status=@{@"version":@1,@"state":state,@"reason":reason,
        @"mode":@(output_mode_name(g_output_mode)),@"outputs":outputs,@"volume":@(session.outputVolume),
        @"time":@([[NSDate date] timeIntervalSince1970]),@"pid":@(getpid()),@"pc":pc?:@{},
        @"mixing":@(g_mix.mixing)};
    if(!g_status_queue) g_status_queue=dispatch_queue_create("com.parv.speaka.helper.status",DISPATCH_QUEUE_SERIAL);
    dispatch_async(g_status_queue,^{
        CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_HELPER_STATUS),(__bridge CFPropertyListRef)status,CFSTR(SPEAKA_PREFS_DOMAIN));
        (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    });
}
static void log_route(AVAudioSession *session,const char *stage,unsigned rate,AudioRing *s) {
    NSString *device=nil;
    int verdict=route_verdict(session,&device);
    if(!strcmp(stage,"configured")) /* v9.1: the category actually in effect, next to the route it produced */
        hlog(stdout,"V91_SESSION_CONFIG category=%s options=0x%lx mode=%s\n",session.category.UTF8String?:"-",
             (unsigned long)session.categoryOptions,session.mode.UTF8String?:"-");
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_ROUTE,
        (uint32_t)(session.outputVolume*100.0f+0.5f),rate,verdict?-102:0});
    for(AVAudioSessionPortDescription *port in session.currentRoute.outputs)
        hlog(stdout,"V889_AUDIO_ROUTE stage=%s mode=%s type=%s name=%s rate=%u volume=%.3f route_ok=%d\n",
                stage,output_mode_name(g_output_mode),port.portType.UTF8String,port.portName.UTF8String,
                rate,session.outputVolume,!verdict);
    if(!session.currentRoute.outputs.count)
        hlog(stdout,"V889_AUDIO_ROUTE stage=%s mode=%s outputs=0 route_ok=0\n",stage,output_mode_name(g_output_mode));
    if(verdict)
        hlog(stdout,"V889_ROUTE_REFUSED stage=%s mode=%s reason=%s device=%s\n",stage,
                output_mode_name(g_output_mode),output_route_reason(verdict),(device?:@"-").UTF8String);
    fflush(stdout);
}
/* A refused route is reported once per distinct reason/device, not on every retry. */
static int g_last_refusal=-1;
static NSString *g_last_refusal_device;
static void note_refusal(AVAudioSession *session,const char *stage,unsigned rate,AudioRing *s,
                         int verdict,NSString *device) {
    NSString *name=device?:@"";
    BOOL repeat=verdict==g_last_refusal && [name isEqualToString:g_last_refusal_device?:@""];
    g_last_refusal=verdict;g_last_refusal_device=name;
    NSString *why=[NSString stringWithUTF8String:output_route_reason(verdict)];
    publish_status(@"waiting",name.length?[NSString stringWithFormat:@"%@: %@",why,name]:why,NO);
    if(repeat) return;
    log_route(session,stage,rate,s);
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,-102});
}
/* v9.9: public since iOS 14.5 - system alerts (incoming-call ringers and the like) don't interrupt this session; they
 * show as banners instead. The strongest priority a public API offers; a call itself still takes the audio. */
static void prefer_uninterrupted(AVAudioSession *session) {
    if([session respondsToSelector:@selector(setPrefersNoInterruptionsFromSystemAlerts:error:)])
        (void)[session setPrefersNoInterruptionsFromSystemAlerts:YES error:NULL];
}
static int configure_speaker(unsigned rate,AudioRing *s) {
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;
    const char *stage="category";
    /* Playback implicitly permits A2DP: v8.8 chose the sending laptop as output.
     * Speaker override is supported only with PlayAndRecord. No input queue,
     * microphone tap, recording request, Bluetooth or AirPlay option is used. */
    /* v9.9.4: DefaultToSpeaker, and MixWithOthers unless this session has fallen back (OutputPolicy.h). */
    BOOL ok=[session setCategory:AVAudioSessionCategoryPlayAndRecord
                           mode:AVAudioSessionModeDefault
                        options:(AVAudioSessionCategoryOptions)output_session_options(1,(int)g_mix.mixing) error:&error];
    if(ok) {prefer_uninterrupted(session);stage="activate";ok=[session setActive:YES error:&error];if(ok) note_session(1,"speaker");}
    if(ok) {stage="speaker_override";ok=[session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:&error];}
    if(!ok) {
        hlog(stderr,"V881_AUDIO_FAIL stage=%s domain=%s code=%ld error=%s\n",stage,
            (error.domain?:@"unknown").UTF8String,(long)error.code,
            (error.localizedDescription?:@"unknown").UTF8String);
        [session setActive:NO error:NULL];
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,-100});return -1;
    }
    /* Allow a bounded hardware-route transition on this helper worker only. */
    for(unsigned i=0;i<20 && !speaker_selected(session);i++) usleep(10000);
    log_route(session,"configured",rate,s);
    if(!speaker_selected(session)) {
        hlog(stderr,"V881_AUDIO_FAIL stage=route_check expected=built_in_speaker\n");
        [session setActive:NO error:NULL];
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,-102});return -1;
    }
    return 0;
}
/* v8.9 "any device" mode (headphones mode used this until v8.9.4; see configure_wired). The Playback category lets
 * iOS use wired, USB, Bluetooth and AirPlay outputs -
 * whatever is picked in Control Center - so no Bluetooth or AirPlay category option is needed or used. The route
 * is checked BEFORE activation, so iOS never starts output on a refused device such as the sending PC, and again
 * after activation. Returns -2 when the route is refused (the caller retries slowly), -1 on other failures. */
static int configure_external(unsigned rate,AudioRing *s) {
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;NSString *device=nil;
    const char *stage="category";
    BOOL ok=[session setCategory:AVAudioSessionCategoryPlayback mode:AVAudioSessionModeDefault
                         options:(AVAudioSessionCategoryOptions)output_session_options(0,(int)g_mix.mixing) error:&error];
    if(ok) {
        int verdict=route_verdict(session,&device);
        if(verdict) {note_refusal(session,"candidate",rate,s,verdict,device);return -2;}
        prefer_uninterrupted(session);stage="activate";ok=[session setActive:YES error:&error];if(ok) note_session(1,"any");
    }
    if(!ok) {
        hlog(stderr,"V889_AUDIO_FAIL stage=%s domain=%s code=%ld error=%s\n",stage,
            (error.domain?:@"unknown").UTF8String,(long)error.code,
            (error.localizedDescription?:@"unknown").UTF8String);
        [session setActive:NO error:NULL];
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,-100});return -1;
    }
    /* Allow a bounded hardware-route transition on this helper worker only. */
    for(unsigned i=0;i<20 && route_verdict(session,NULL);i++) usleep(10000);
    int verdict=route_verdict(session,&device);
    if(verdict) {
        [session setActive:NO withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:NULL];
        note_refusal(session,"configured",rate,s,verdict,device);return -2;
    }
    log_route(session,"configured",rate,s);
    return 0;
}
/* True when every current output is the built-in speaker or earpiece (and there is at least one). */
static int builtin_only(AVAudioSession *session) {
    NSArray<AVAudioSessionPortDescription *> *outputs=session.currentRoute.outputs;
    if(!outputs.count) return 0;
    for(AVAudioSessionPortDescription *port in outputs) {
        output_kind_t kind=port_kind(port.portType);
        if(kind!=OUTPUT_PORT_SPEAKER && kind!=OUTPUT_PORT_RECEIVER) return 0;
    }
    return 1;
}
/* v8.9.4 headphones mode: PlayAndRecord with NO Bluetooth option. Bluetooth outputs - including the sending PC, which
 * iOS lists as an A2DP output and picks because it connected last - are not eligible at all, and a plugged-in wired
 * or USB headset always wins, so earbuds work without replugging. No DefaultToSpeaker: with nothing plugged in the
 * route is the earpiece, which the policy refuses, so this mode never falls back to the loudspeaker. Any speaker
 * override left by speaker mode is cleared. No input queue, microphone tap or recording is ever started. */
static int configure_wired(unsigned rate,AudioRing *s) {
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;NSString *device=nil;
    const char *stage="category";
    BOOL ok=[session setCategory:AVAudioSessionCategoryPlayAndRecord mode:AVAudioSessionModeDefault
                         options:(AVAudioSessionCategoryOptions)output_session_options(0,(int)g_mix.mixing) error:&error];
    if(ok) {
        (void)[session overrideOutputAudioPort:AVAudioSessionPortOverrideNone error:NULL];
        /* Before activating, only a built-in speaker/earpiece candidate is conclusive: nothing is plugged in, whether
         * iOS reports this inactive session's own route or the system's. Refuse then without disturbing other audio.
         * Any other candidate is not trusted - an inactive session may report the system route, e.g. the PC, which
         * this category cannot reach - so activate (safe: no Bluetooth is possible) and verify the real route below. */
        int verdict=route_verdict(session,&device);
        if(verdict && builtin_only(session)) {note_refusal(session,"candidate",rate,s,verdict,device);return -2;}
        prefer_uninterrupted(session);stage="activate";ok=[session setActive:YES error:&error];if(ok) note_session(1,"wired");
    }
    if(!ok) {
        hlog(stderr,"V894_AUDIO_FAIL stage=%s domain=%s code=%ld error=%s\n",stage,
            (error.domain?:@"unknown").UTF8String,(long)error.code,
            (error.localizedDescription?:@"unknown").UTF8String);
        [session setActive:NO error:NULL];
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,-100});return -1;
    }
    /* Allow a bounded hardware-route transition on this helper worker only. */
    for(unsigned i=0;i<20 && route_verdict(session,NULL);i++) usleep(10000);
    int verdict=route_verdict(session,&device);
    if(verdict) {
        [session setActive:NO withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:NULL];
        note_refusal(session,"configured",rate,s,verdict,device);return -2;
    }
    log_route(session,"configured",rate,s);
    return 0;
}
static int configure_output(unsigned rate,AudioRing *s) {
    if(g_output_mode==OUTPUT_MODE_HEADPHONES) return configure_wired(rate,s);
    return g_output_mode==OUTPUT_MODE_SPEAKER?configure_speaker(rate,s):configure_external(rate,s);
}
static int start_output(Output *o,unsigned rate,AudioRing *s) {
    AVAudioSession *session=[AVAudioSession sharedInstance];
    /* v9.1: a session that is still active on an acceptable route (a new sample rate, a re-applied speaker override)
     * is reused as it is; configuring it again would re-route iOS. Anything else is released and configured afresh. */
    int reuse=g_session_active && !route_verdict(session,NULL);
    if(g_session_active && !reuse) {
        [session setActive:NO withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:NULL];
        note_session(0,"not_reusable");
    }
    int configured=reuse?0:configure_output(rate,s);
    if(configured) {
        if(g_session_active) note_session(0,"configure_refused"); /* every configure_* failure deactivates */
        return configured;
    }
    __atomic_store_n(&o->stopping,1,__ATOMIC_RELEASE);
    __atomic_store_n(&o->error,0,__ATOMIC_RELAXED);
    pthread_mutex_lock(&o->lock);o->read=o->count=0;o->priming=1;pthread_mutex_unlock(&o->lock);  /* v9.9.1: prime before playing */
    AudioStreamBasicDescription format={0};
    format.mSampleRate=rate;format.mFormatID=kAudioFormatLinearPCM;
    format.mFormatFlags=kLinearPCMFormatFlagIsSignedInteger|kLinearPCMFormatFlagIsPacked;
    format.mBytesPerPacket=4;format.mFramesPerPacket=1;format.mBytesPerFrame=4;
    format.mChannelsPerFrame=2;format.mBitsPerChannel=16;
    OSStatus e=AudioQueueNewOutput(&format,render,o,NULL,NULL,0,&o->queue);
    for(unsigned i=0;!e && i<3;i++) {
        AudioQueueBufferRef b=NULL;
        e=AudioQueueAllocateBuffer(o->queue,AUDIO_FRAMES*4u,&b);
        if(!e) {
            memset(b->mAudioData,0,AUDIO_FRAMES*4u);b->mAudioDataByteSize=AUDIO_FRAMES*4u;
            e=AudioQueueEnqueueBuffer(o->queue,b,0,NULL);
        }
    }
    if(!e && route_verdict(session,NULL)) e=-102;
    if(!e) {
        __atomic_store_n(&o->stopping,0,__ATOMIC_RELEASE);
        e=AudioQueueStart(o->queue,NULL);
    }
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,rate,e});
    if(e) {stop_output(o,s,"start_error",1);return -1;}
    o->rate=rate;g_last_refusal=-1;
    log_route(session,"started",rate,s);
    publish_status(@"playing",@"",YES);
    return 0;
}
/* v9.1, speaker mode: a route change took the playing output off the speaker. iOS drops a speaker override whenever
 * the route changes (with wired earbuds plugged in, the earbuds take over), so the override is applied again on the
 * live session instead of deactivating it: deactivating re-routes iOS, which is what fed the 23:33 loop. The queue
 * stops first, so nothing plays on the wrong output meanwhile. Returns 0 when it plays on the speaker again. */
/* v9.9.2: an interruption. iOS has already stopped the queue and deactivated the session; keep the queue object and
 * the PCM (v9.9.1 disposed the queue and cleared the ring, though its comment said the PCM was kept). The decoder
 * runs on, so the stream stays current, and the helper reports "holding" so the idle guard keeps the stream. */
static void pause_output(Output *o,AudioRing *s,const char *reason) {
    if(!o->queue || o->paused) return;
    o->paused=1;
    (void)AudioQueuePause(o->queue);              /* already stopped by iOS; harmless, and explicit */
    uint64_t g=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
    hlog(stdout,"V100_OUTPUT_PAUSED reason=%s generation=%llu active=%u queue=kept pcm=kept\n",reason,
         (unsigned long long)(g>>1),(unsigned)(g&1u));
}
/* Resume a paused queue on a re-activated session: no new queue, no dispose. 0 ok; -1 activation refused (a call, an
 * alarm: keep the paused queue and back off); -2 route refused; -3 the stream's rate changed and -4 the queue would
 * not start (the caller rebuilds from scratch for both). */
static int resume_output(Output *o,unsigned rate,AudioRing *s) {
    if(!o->queue || !o->paused) return -1;
    if(rate && rate!=o->rate) return -3;
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;
    prefer_uninterrupted(session);
    if(![session setActive:YES error:&error]) {
        hlog(stdout,"V100_RESUME_FAIL stage=activate code=%ld\n",(long)error.code);return -1;
    }
    note_session(1,"resumed");
    if(g_output_mode==OUTPUT_MODE_SPEAKER) (void)[session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:NULL];
    if(route_verdict(session,NULL)) {hlog(stdout,"V100_RESUME_FAIL stage=route\n");return -2;}
    pthread_mutex_lock(&o->lock);
    unsigned lead=o->prime_target?o->prime_target:PCM_PRIME;
    if(o->count>lead) {unsigned drop=o->count-lead;o->read=(o->read+drop)%PCM_CAPACITY;o->count=lead;}
    o->priming=o->count<lead;                    /* resume from the newest lead */
    pthread_mutex_unlock(&o->lock);
    OSStatus e=AudioQueueStart(o->queue,NULL);
    if(e) {hlog(stdout,"V100_RESUME_FAIL stage=queue_start status=%d\n",(int)e);return -4;}
    o->paused=0;
    log_route(session,"resumed",o->rate,s);
    return 0;
}
/* v9.9.5: another app's non-mixing audio is playing alongside Speaka's mixing session. Activate once without
 * MixWithOthers - iOS then interrupts that app - and mix again at once, so the next activation can't take Speaka's
 * session (OutputPolicy.h, excl_*). The session stays active throughout: the route never leaves Speaka's output. */
static int assert_exclusive(void) {
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;
    int speaker=g_output_mode==OUTPUT_MODE_SPEAKER;
    NSString *category=g_output_mode==OUTPUT_MODE_ANY?AVAudioSessionCategoryPlayback:AVAudioSessionCategoryPlayAndRecord;
    g_session_changed_ms=helper_ms();          /* the route change this causes is our own echo */
    BOOL ok=[session setCategory:category mode:AVAudioSessionModeDefault
                         options:(AVAudioSessionCategoryOptions)output_session_options(speaker,0) error:&error];
    if(ok) ok=[session setActive:YES error:&error];
    long code=ok?0:(long)error.code;
    (void)[session setCategory:category mode:AVAudioSessionModeDefault
                       options:(AVAudioSessionCategoryOptions)output_session_options(speaker,1) error:NULL];
    if(speaker) (void)[session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:NULL];
    g_session_changed_ms=helper_ms();
    if(!ok) hlog(stdout,"V103_EXCLUSIVE_FAIL code=%ld\n",code);
    return ok?1:0;
}
static int reassert_speaker(Output *o,unsigned rate,AudioRing *s) {
    AVAudioSession *session=[AVAudioSession sharedInstance];NSError *error=nil;
    stop_output(o,s,"route_changed_reassert",0);
    BOOL ok=g_session_active && [session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:&error];
    for(unsigned i=0;ok && i<20 && !speaker_selected(session);i++) usleep(10000);
    int selected=ok && speaker_selected(session);
    hlog(stdout,"V91_SPEAKER_REASSERT ok=%d selected=%d code=%ld\n",(int)ok,selected,(long)error.code);
    return selected?start_output(o,rate,s):-1;
}
typedef struct { uint32_t samples,nonzero,peak; } PCMLevels;
static void measure_pcm(PCMLevels *level,const int16_t *pcm,unsigned frames) {
    level->samples+=frames*2u;
    for(unsigned i=0;i<frames*2u;i++) {
        int value=pcm[i];unsigned magnitude=value<0?(unsigned)-value:(unsigned)value;
        if(value) level->nonzero++;
        if(magnitude>level->peak) level->peak=magnitude;
    }
}
static void report_progress(Output *o,PCMLevels *level,AudioRing *s,uint64_t generation,const input_rate_t *in) {
    uint64_t callbacks=__atomic_exchange_n(&o->render_callbacks,0,__ATOMIC_RELAXED);
    uint64_t submitted=__atomic_exchange_n(&o->submitted_frames,0,__ATOMIC_RELAXED);
    uint64_t nonzero=__atomic_exchange_n(&o->submitted_nonzero,0,__ATOMIC_RELAXED);
    uint64_t silence=__atomic_exchange_n(&o->silence_frames,0,__ATOMIC_RELAXED);
    uint64_t gated=__atomic_exchange_n(&o->silence_gated,0,__ATOMIC_RELAXED);
    uint64_t priming=__atomic_exchange_n(&o->silence_priming,0,__ATOMIC_RELAXED);
    uint64_t underrun=__atomic_exchange_n(&o->silence_underrun,0,__ATOMIC_RELAXED);
    uint64_t busy=__atomic_exchange_n(&o->silence_busy,0,__ATOMIC_RELAXED);
    UInt32 running=0,size=sizeof running;
    OSStatus status=o->queue?AudioQueueGetProperty(o->queue,kAudioQueueProperty_IsRunning,&running,&size):-1;
    pthread_mutex_lock(&o->lock);unsigned queued=o->count;pthread_mutex_unlock(&o->lock);
    int route_ok=!route_verdict([AVAudioSession sharedInstance],NULL);
    hlog(stdout,"V881_PCM samples=%u nonzero=%u peak=%u\n",level->samples,level->nonzero,level->peak);
    uint64_t reprimes=__atomic_load_n(&o->reprimes,__ATOMIC_RELAXED);
    /* v9.9.2: silence by cause - gated = input starved (held on purpose), priming = building the 0.25 s lead,
     * underrun = the ring ran dry with the input judged healthy (output side), busy = PCM lock contention. */
    hlog(stdout,"V881_RENDER callbacks=%llu pcm_frames=%llu nonzero_samples=%llu silence_frames=%llu queued_frames=%u dropped_frames=%llu queue_running=%u query_status=%d route_ok=%d priming=%u reprimes=%llu in_rate=%u input=%s silence_gated=%llu silence_priming=%llu silence_underrun=%llu silence_busy=%llu paused=%u mode=%s\n",
        (unsigned long long)callbacks,(unsigned long long)submitted,(unsigned long long)nonzero,
        (unsigned long long)silence,queued,(unsigned long long)o->dropped,(unsigned)running,(int)status,route_ok,
        o->priming,(unsigned long long)reprimes,in?in->rate:0u,in&&in->starved?"starved":"healthy",
        (unsigned long long)gated,(unsigned long long)priming,(unsigned long long)underrun,(unsigned long long)busy,
        o->paused,output_mode_name(g_output_mode));
    fflush(stdout);
    /* v9.1: a kept output in a stream gap stays "idle" (PC paused) instead of flipping back to "playing". */
    if(o->queue && !o->paused && (generation&1u)) publish_status(route_ok?@"playing":@"waiting",route_ok?@"":@"output route changed",NO);
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_PCM,level->peak,level->samples,(int32_t)level->nonzero});
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_RENDER,(uint32_t)silence,(uint32_t)submitted,(int32_t)callbacks});
    *level=(PCMLevels){0};
}
/* v9.9.2: while the output is held (input starved, or paused by an interruption) keep only the newest PCM_PRIME
 * frames, so it restarts at once from a full lead at low latency. These trims are by design, not counted as drops. */
static void hold_pcm(Output *o,const int16_t *pcm,unsigned frames) {
    if(frames>PCM_PRIME) {pcm+=(frames-PCM_PRIME)*2u;frames=PCM_PRIME;}
    pthread_mutex_lock(&o->lock);
    if(o->count+frames>PCM_PRIME) {
        unsigned drop=o->count+frames-PCM_PRIME;if(drop>o->count) drop=o->count;
        o->read=(o->read+drop)%PCM_CAPACITY;o->count-=drop;
    }
    for(unsigned i=0;i<frames;i++) {
        unsigned slot=(o->read+o->count+i)%PCM_CAPACITY;
        o->pcm[slot*2]=pcm[i*2];o->pcm[slot*2+1]=pcm[i*2+1];
    }
    o->count+=frames;pthread_mutex_unlock(&o->lock);
}
static void write_pcm(Output *o,const int16_t *pcm,unsigned frames) {
    pthread_mutex_lock(&o->lock);
    if(frames>PCM_CAPACITY-o->count) {
        unsigned discard=frames-(PCM_CAPACITY-o->count);
        o->read=(o->read+discard)%PCM_CAPACITY;o->count-=discard;o->dropped+=discard;
    }
    for(unsigned i=0;i<frames;i++) {
        unsigned slot=(o->read+o->count+i)%PCM_CAPACITY;
        o->pcm[slot*2]=pcm[i*2];o->pcm[slot*2+1]=pcm[i*2+1];
    }
    o->count+=frames;pthread_mutex_unlock(&o->lock);
}
/* v9.1: the stream changed (the PC or the phone started, suspended, closed or reconfigured it) while the output was
 * running. Keep the queue and the session; drop only the PCM queued for the old stream. */
static void keep_output(Output *o,uint64_t generation) {
    /* Emptied on purpose (a new stream or a pause): prime again without counting it as an underrun. */
    pthread_mutex_lock(&o->lock);o->read=o->count=0;o->priming=1;pthread_mutex_unlock(&o->lock);
    hlog(stdout,"V91_STREAM_CHANGE generation=%llu active=%u output=kept\n",(unsigned long long)(generation>>1),
         (unsigned)(generation&1u));
}
static void report_health(Output *o,AudioRing *s,uint64_t generation) {
    /* "holding" (2): the output is held on purpose - paused by an interruption (v9.9.2) or gated because the input
     * can't sustain playback (v9.9.3) - while the stream is active. The counter is decode progress, so the idle guard
     * keeps the stream exactly as long as the helper keeps receiving and decoding it, and no longer (OutputPolicy.h,
     * health_holding). Gated, the queue still runs, so v9.8.90 reported "playing" with a frozen render count. */
    if(health_holding((generation&1u)!=0,o->queue!=NULL,(int)o->paused,
                      (int)__atomic_load_n(&o->input_starved,__ATOMIC_RELAXED))) {
        audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_HEALTH,(uint32_t)generation,
            __atomic_load_n(&o->decoded_frames,__ATOMIC_RELAXED),2});
        return;
    }
    UInt32 running=0,size=sizeof running;
    OSStatus status=o->queue?AudioQueueGetProperty(o->queue,kAudioQueueProperty_IsRunning,&running,&size):-1;
    /* v8.9.4: the reason bits travel as a negative result so the daemon can log them; 1 still means playing.
     * 0x1 stream inactive, 0x2 no queue/query failed, 0x4 queue not running, 0x8 stopping, 0x10 enqueue error,
     * 0x20 route not acceptable for the selected mode. */
    unsigned why=0;
    if(!(generation&1u)) why|=0x1u;
    if(status) why|=0x2u; else if(!running) why|=0x4u;
    if(__atomic_load_n(&o->stopping,__ATOMIC_ACQUIRE)) why|=0x8u;
    if(__atomic_load_n(&o->error,__ATOMIC_RELAXED)) why|=0x10u;
    if(route_verdict([AVAudioSession sharedInstance],NULL)) why|=0x20u;
    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_HEALTH,(uint32_t)generation,
        (uint32_t)__atomic_load_n(&o->render_tick,__ATOMIC_RELAXED),why?-(int32_t)why:1});
}
static int read_exact(int fd,void *dst,size_t size) {
    uint8_t *p=dst;
    while(size) {
        ssize_t n=recv(fd,p,size,0);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) return -1;
        p+=n;size-=(size_t)n;
    }
    return 0;
}
static void stream(int fd) {
    struct timeval timeout={2,0};int one=1;
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
    setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof timeout);
    setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&one,sizeof one);
    AudioRingHello hello;
    if(read_exact(fd,&hello,sizeof hello) || hello.magic!=AUDIO_RING_MAGIC ||
       hello.version!=AUDIO_RING_VERSION || hello.size!=sizeof(AudioRing) || !hello.pid) return;
    char path[160];snprintf(path,sizeof path,AUDIO_RING_PATH_FORMAT,hello.pid,(unsigned long long)hello.token);
    int file=open(path,O_RDWR|O_NOFOLLOW);if(file<0) {perror("V88_MAP open");return;}
    struct stat st;
    if(fstat(file,&st) || !S_ISREG(st.st_mode) || st.st_uid!=geteuid() ||
       (st.st_mode&0777)!=0600 || st.st_size!=(off_t)sizeof(AudioRing) ||
       st.st_nlink!=1 || flock(file,LOCK_EX|LOCK_NB)) {close(file);return;}
    AudioRing *s=mmap(NULL,sizeof *s,PROT_READ|PROT_WRITE,MAP_SHARED,file,0);
    if(s==MAP_FAILED) {close(file);return;}
    if(__atomic_load_n(&s->magic,__ATOMIC_ACQUIRE)!=hello.magic ||
       s->version!=hello.version || s->size!=hello.size || s->pid!=hello.pid || s->token!=hello.token) {
        munmap(s,sizeof *s);close(file);return;
    }
    Output *o=calloc(1,sizeof *o);
    if(!o) {munmap(s,sizeof *s);close(file);return;}
    pthread_mutex_init(&o->lock,NULL);
    uint8_t ack=88;
    int flags=fcntl(fd,F_GETFL,0);
    if(send(fd,&ack,1,0)!=1 || flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0) {
        pthread_mutex_destroy(&o->lock);free(o);munmap(s,sizeof *s);close(file);return;
    }
    char volume_path[256];
    snprintf(volume_path,sizeof volume_path,AUDIO_RING_PATH_FORMAT ".volume",hello.pid,(unsigned long long)hello.token);
    o->volume=sp_volume_mailbox_open(volume_path,hello.pid,hello.token,0);
    load_output_settings();g_last_refusal=-1;
    if(g_output_notify<0 && notify_register_check(SPEAKA_NOTIFY_OUTPUT,&g_output_notify)!=NOTIFY_STATUS_OK)
        g_output_notify=-1;
    if(g_output_notify>=0) {int primed=0;(void)notify_check(g_output_notify,&primed);} /* first check is always "changed" */
    publish_status(@"starting",@"",YES);
    /* v9.3 experimental: the codec comes from the ring header, set by the tweak at CONFIGURE; SBC is the default
     * and the fallback. The PCM scratch fits the larger of an SBC burst and an AAC-LC frame (1024 stereo). */
    CodecDecoder decoder;codec_decoder_begin(&decoder,s,g_aac_enabled,hlog_line);
    /* Room for the larger of an SBC burst and a packet of several AAC frames (CodecDecoder.h). */
    #define CODEC_PCM_SAMPLES (SBC_DECODER_PCM_SAMPLES>CODEC_DECODER_PCM_SAMPLES?SBC_DECODER_PCM_SAMPLES:CODEC_DECODER_PCM_SAMPLES)
    AudioPacket packet;int16_t pcm[CODEC_PCM_SAMPLES];
    uint64_t generation=0,doorbell=UINT64_MAX;
    unsigned diagnostic_ticks=0;
    /* v9.9: the helper owns the output for the whole session (OutputPolicy.h): it starts now, before any packet, and
     * keeps it through pauses; it takes it back if something else takes it. */
    own_state_t own;own_init(&own);
    uint32_t stream_state=__atomic_load_n(&s->stream_state,__ATOMIC_ACQUIRE);
    if(stream_state==AUDIO_STREAM_CLOSED) own_released(&own);   /* reconnected after the PC closed: wait for it */
    unsigned rate_hint=codec_decoder_rate_hint(&decoder);
    int own_logged=-1;
    /* v9.9.4: each helper session starts mixing (OutputPolicy.h) with no PC known. The route before the first
     * activation is the system's, which lists the PC: read it now (reading changes nothing), and on every change. */
    mix_init(&g_mix);forget_pc_port();
    capture_pc_port([AVAudioSession sharedInstance].currentRoute,"before_activation");
    int warm=0;uint64_t healthy_since=0;   /* v9.9.4: has this connection streamed at real time yet? */
    excl_state_t excl;excl_init(&excl);uint64_t last_hint_poll=0;int hint_seen=-1;   /* v9.9.5 */
    unsigned volume_seen=127;   /* v9.9.8: the AVRCP gain last reported as reaching the output */
    int ios_volume_seen=-1;uint64_t ios_volume_polled=0;   /* v9.9.9: iOS output volume, observed only */
    input_rate_t input;input_rate_reset(&input,helper_ms(),(int)(__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE)&1u));
    uint64_t other_quiet_since=0,last_poll=0;   /* v9.9.2: the other app's audio stopping clears an interruption */
    PCMLevels level={0};
    __block int interruption=0,route_changed=0,interruption_should_resume=0;__block long interruption_reason=-1;
    __block long route_reason=-1;__block int route_device=0,media_reset=0;
    id observer=[[NSNotificationCenter defaultCenter] addObserverForName:AVAudioSessionInterruptionNotification
        object:nil queue:nil usingBlock:^(NSNotification *note) {
            unsigned type=[note.userInfo[AVAudioSessionInterruptionTypeKey] unsignedIntValue];
            id reason=note.userInfo[@"AVAudioSessionInterruptionReasonKey"];
            __atomic_store_n(&interruption_reason,[reason isKindOfClass:[NSNumber class]]?[reason longValue]:-1L,
                             __ATOMIC_RELAXED);
            /* v9.9.1: on an "ended" notification iOS says whether we should resume (the ShouldResume option). An
             * interrupting media app normally sends it; a call that is still up does not. */
            unsigned opts=[note.userInfo[AVAudioSessionInterruptionOptionKey] unsignedIntValue];
            __atomic_store_n(&interruption_should_resume,
                             (type!=AVAudioSessionInterruptionTypeBegan && (opts&AVAudioSessionInterruptionOptionShouldResume))?1:0,
                             __ATOMIC_RELAXED);
            __atomic_store_n(&interruption,type==AVAudioSessionInterruptionTypeBegan?1:2,__ATOMIC_RELEASE);
        }];
    id route_observer=[[NSNotificationCenter defaultCenter] addObserverForName:AVAudioSessionRouteChangeNotification
        object:nil queue:nil usingBlock:^(NSNotification *note) {
            /* v9.1: keep the reason; a real device arrival or removal is remembered until the loop reads it. */
            id value=note.userInfo[AVAudioSessionRouteChangeReasonKey];
            long reason=[value isKindOfClass:[NSNumber class]]?[value longValue]:-1L;
            __atomic_store_n(&route_reason,reason,__ATOMIC_RELAXED);
            if(own_route_is_device_change(reason)) __atomic_store_n(&route_device,1,__ATOMIC_RELAXED);
            /* v9.9.4: the route before this change (when the helper itself activates, the system route, which
             * lists the PC) and after it (another app routing to the PC): either may name the PC. */
            id previous=note.userInfo[AVAudioSessionRouteChangePreviousRouteKey];
            if([previous isKindOfClass:[AVAudioSessionRouteDescription class]]) capture_pc_port(previous,"previous_route");
            capture_pc_port([AVAudioSession sharedInstance].currentRoute,"route_change");
            __atomic_store_n(&route_changed,1,__ATOMIC_RELEASE);
        }];
    /* v9.1: after a media-services reset the old session and queue are gone; start again from nothing. */
    id reset_observer=[[NSNotificationCenter defaultCenter] addObserverForName:AVAudioSessionMediaServicesWereResetNotification
        object:nil queue:nil usingBlock:^(NSNotification *note) {
            (void)note;__atomic_store_n(&media_reset,1,__ATOMIC_RELEASE);
        }];
    for(;;) {
        @autoreleasepool {
            uint8_t byte;ssize_t n=recv(fd,&byte,1,MSG_PEEK);
            if(n==0 || (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR)) break;
            __atomic_fetch_add(&s->helper_heartbeat,1,__ATOMIC_RELEASE);
            uint64_t now=helper_ms();
            if(o->volume) {
                __atomic_store_n(&o->volume->heartbeat_ms,sp_volume_now_ms(),__ATOMIC_RELEASE);
                /* v9.9.8: the gain the render callback now applies to every decoded buffer (AAC and SBC alike). */
                unsigned gain=sp_volume_mailbox_gain(o->volume);
                if(gain!=volume_seen) {
                    hlog(stdout,"V106_VOLUME gain=%u/127 (%u%%) output=%s\n",gain,(gain*100u+63u)/127u,
                         o->queue&&!o->paused?"playing":"idle");
                    volume_seen=gain;
                }
                /* v9.9.9: does this daemon see the iPhone's volume buttons / Control Center? (public, read-only;
                 * nothing is sent or applied - iPhone-to-Windows needs one volume domain, docs/AVRCP-VOLUME.md) */
                if(o->queue && now-ios_volume_polled>=500u) {
                    ios_volume_polled=now;
                    int ios=(int)([AVAudioSession sharedInstance].outputVolume*127.0f+0.5f);
                    if(ios!=ios_volume_seen) {
                        if(ios_volume_seen>=0) hlog(stdout,"V106_IOS_VOLUME %d/127 (was %d) - observed only\n",ios,ios_volume_seen);
                        ios_volume_seen=ios;
                    }
                }
            }
            /* Every path below keeps own.running equal to "a queue is running"; this only guards against a miss. */
            if((!o->queue || o->paused) && own.running) own_start_failed(&own,now,0);
            else if(o->queue && !o->paused && !own.running) own_started(&own,now);
            if(__atomic_exchange_n(&media_reset,0,__ATOMIC_ACQUIRE)) {
                hlog(stdout,"V91_MEDIA_SERVICES_RESET queue=%d session=%d\n",o->queue!=NULL,g_session_active);
                stop_output(o,s,"media_services_reset",1);codec_decoder_reset(&decoder);
                own_restart(&own,now);
            }
            int changed=__atomic_exchange_n(&interruption,0,__ATOMIC_ACQUIRE);
            if(changed) {
                hlog(stdout,"V882_INTERRUPTION type=%s reason=%ld queue=%d\n",changed==1?"began":"ended",
                     __atomic_load_n(&interruption_reason,__ATOMIC_RELAXED),o->queue!=NULL);fflush(stdout);
                if(changed==1) {
                    /* v9.9.2: iOS has already stopped our queue and deactivated our session. What is kept: the
                     * AudioQueue object (paused, not disposed), the newest 0.25 s of PCM (hold_pcm trims the ring while
                     * paused), the decoder and its state (packets keep decoding), and the stream's idle-guard lease
                     * (report_health says "holding" while decoding progresses). What is not: the session (iOS ended it)
                     * - no NotifyOthers deactivation is sent, which would tell the interrupting app to carry on.
                     * Resuming (OutputPolicy.h, own_interrupted): at once on "ended" or when the other app's audio
                     * stops; otherwise a takeover after OWN_TAKEOVER_MS, doubling if the app takes it straight back. */
                    pause_output(o,s,"interruption_began");
                    if(g_session_active) note_session(0,"interrupted");
                    own_interrupted(&own,now);other_quiet_since=0;
                } else {
                    int resume=__atomic_load_n(&interruption_should_resume,__ATOMIC_RELAXED);
                    hlog(stdout,"V882_INTERRUPTION_END should_resume=%d\n",resume);
                    own_interruption_cleared(&own,now);   /* ended: resume now, whatever iOS advised */
                }
            }
            int settings_changed=0;
            if(g_output_notify>=0 && notify_check(g_output_notify,&settings_changed)==NOTIFY_STATUS_OK && settings_changed) {
                load_output_settings();g_last_refusal=-1;
                stop_output(o,s,"output_settings_changed",1);codec_decoder_reset(&decoder);
                own_restart(&own,now);                 /* a user action: rebuild on the new output at once */
            }
            if(__atomic_exchange_n(&route_changed,0,__ATOMIC_ACQUIRE)) {
                AVAudioSession *session=[AVAudioSession sharedInstance];
                long reason=__atomic_load_n(&route_reason,__ATOMIC_RELAXED);
                int device=__atomic_exchange_n(&route_device,0,__ATOMIC_RELAXED);
                int own_echo=g_session_changed_ms && now>=g_session_changed_ms && now-g_session_changed_ms<1500u;
                hlog(stdout,"V91_ROUTE_CHANGE reason=%ld(%s) device_change=%d own_echo=%d playing=%d\n",reason,
                     own_route_reason_name(reason),device,own_echo,o->queue!=NULL);
                if(o->queue && !o->paused) {
                    log_route(session,"changed",o->rate,s);
                    if(route_verdict(session,NULL)) {
                        /* v9.1: speaker mode re-applies its override on the live session before giving up. */
                        if(g_output_mode==OUTPUT_MODE_SPEAKER && !reassert_speaker(o,o->rate,s)) {
                            own_started(&own,helper_ms());publish_status(@"playing",@"",YES);
                        } else if(mix_route_lost(&g_mix)) {
                            /* v9.9.4: mixing let another app's audio take the shared route from Speaka's output.
                             * Play exclusively from now on, as before v9.9.4: rebuild without MixWithOthers. */
                            hlog(stdout,"V102_MIX fallback=exclusive reason=route_lost mode=%s\n",output_mode_name(g_output_mode));
                            stop_output(o,s,"mix_fallback",0);codec_decoder_reset(&decoder);
                            own_restart(&own,helper_ms());
                        } else {
                            /* The selected mode's output went away (headphones unplugged): wait for it to return. */
                            stop_output(o,s,"route_changed",1);codec_decoder_reset(&decoder);
                            own_start_failed(&own,helper_ms(),g_output_mode==OUTPUT_MODE_HEADPHONES);
                        }
                    } else publish_status(@"playing",@"",YES);
                } else if(device || !own_echo) {
                    own_now(&own,now);                 /* a device was plugged in or picked: try it now */
                }
            }
            uint64_t current=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
            if(current!=generation) {
                /* A running output is KEPT (v9.1, OutputPolicy.h): stopping it here re-routes iOS, which makes
                 * bluetoothd START or SUSPEND the PC's stream. Only stale PCM is dropped. */
                if(own.running) keep_output(o,current);
                codec_decoder_reset(&decoder);generation=current;
                /* v9.9.2: a new stream (or a pause) is measured afresh; a pause is not starvation. */
                if(input.starved) hlog(stdout,"V100_INPUT healthy reason=stream_change\n");
                input_rate_reset(&input,now,(int)(current&1u));__atomic_store_n(&o->input_starved,0,__ATOMIC_RELAXED);
                /* v9.9.4: a resume on a warm connection gets the 64 ms lead, the two-window gate and its leading
                 * silence trimmed; a cold stream keeps the 0.25 s lead and the early starvation verdict. */
                if(warm) input_rate_warm(&input);
                pthread_mutex_lock(&o->lock);
                o->prime_target=warm?PCM_PRIME_WARM:PCM_PRIME;o->trim_silence=warm&&(current&1u);
                pthread_mutex_unlock(&o->lock);
                healthy_since=0;
                if(!(current&1u)) publish_status(@"idle",@"PC paused or stopped the stream",NO);
                else if(o->queue && !o->paused) publish_status(@"playing",@"",NO);
            }
            int32_t audio_error=__atomic_exchange_n(&o->error,0,__ATOMIC_RELAXED);
            if(audio_error) {
                audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_AUDIO,0,o->rate,audio_error});
                stop_output(o,s,"enqueue_error",1);
                own_start_failed(&own,now,0);
            }
            /* v9.9: the PC's stream state. A pause keeps everything; a close lets go after OWN_CLOSED_RELEASE_MS
             * unless the PC opens a stream again. There is no release for lack of media any more. */
            uint32_t state=__atomic_load_n(&s->stream_state,__ATOMIC_ACQUIRE);
            if(state!=stream_state) {
                hlog(stdout,"V99_STREAM state=%u (was %u) output=%d\n",state,stream_state,o->queue!=NULL);
                /* v9.9.6: a new configuration is a new connection: a route capture belongs to the connection it was
                 * made in, so the tweak never mistakes the last PC's capture for this one's (PeerIdentity.c). */
                if(state==AUDIO_STREAM_CONFIGURED) forget_pc_port();
                own_stream(&own,state,now);stream_state=state;
            }
            /* v9.9.4: a connection is warm once a stream has played 2 s at real time without being held. */
            if((generation&1u) && o->queue && !o->paused && !input.starved && input.windows>0) {
                if(!healthy_since) healthy_since=now;
                else if(!warm && now-healthy_since>=2000u) {
                    warm=1;hlog(stdout,"V102_WARM the connection streams at real time: resumes use a %u-frame lead\n",PCM_PRIME_WARM);
                }
            } else healthy_since=0;
            if(state==AUDIO_STREAM_CLOSED && warm) warm=0;   /* a new connection starts cold */
            if(own_should_release(&own,now)) {
                hlog(stdout,"V99_OWN release: the PC closed its stream %u s ago and opened none\n",OWN_CLOSED_RELEASE_MS/1000u);
                stop_output(o,s,"stream_closed",1);codec_decoder_reset(&decoder);own_released(&own);forget_pc_port();
            }
            /* v9.9.2: while interrupted, the other app's audio stopping clears the interruption (it doesn't always send
             * "ended"). Quiet must hold for 0.5 s, beginning at least 0.5 s after the interruption, so the moment the
             * other app is still activating is not mistaken for it having stopped. */
            if(own.interrupted && now-last_poll>=250u) {
                last_poll=now;
                if([[AVAudioSession sharedInstance] isOtherAudioPlaying]) other_quiet_since=0;
                else if(!other_quiet_since) other_quiet_since=now;
                if(other_quiet_since && now-other_quiet_since>=500u && now-own.interrupted_ms>=500u &&
                   own.next_ms>now) {
                    hlog(stdout,"V100_INTERRUPTION_CLEARED by=other_audio_stopped after_ms=%llu\n",
                         (unsigned long long)(now-own.interrupted_ms));
                    own_interruption_cleared(&own,now);
                }
            }
            /* v9.9: own the output: start it now - at connection, before the PC's first packet - and take it back
             * whenever it is lost. The queue plays silence until PCM arrives. v9.9.2: after an interruption the same
             * queue is resumed in place; a new one is built only if there is none or resuming fails. */
            if((!o->queue || o->paused) && own_may_start(&own,now)) {
                int retake=own.interrupted||own.retaking;
                uint64_t lost_ms=own.interrupted?now-own.interrupted_ms:0;
                int resumed=0,started;
                if(o->queue && o->paused) {
                    started=resume_output(o,rate_hint,s);
                    if(started==-4 || started==-3) {          /* rebuild: the queue can't be resumed, or a new rate */
                        stop_output(o,s,started==-3?"resume_rate_changed":"resume_failed",0);
                        started=start_output(o,rate_hint,s);
                    } else resumed=!started;
                } else started=start_output(o,rate_hint,s);
                if(started) own_start_failed(&own,helper_ms(),started==-2);
                else own_resumed(&own,helper_ms());
                /* Ready before the PC plays: the app shows "playing" only once the stream is active (report_progress). */
                if(!started && !(generation&1u)) publish_status(@"idle",@"ready for the PC",YES);
                else if(!started) publish_status(@"playing",@"",YES);
                int code=started?(started==-2?2:1):0;
                if(code!=own_logged || retake) {
                    hlog(stdout,"V99_OWN %s%s rate=%u failures=%u fights=%u lost_ms=%llu next_in_ms=%lld\n",
                         retake?"retake ":"",!started?(resumed?"resumed_in_place":"started"):started==-2?"waiting_for_output":"start_failed",
                         rate_hint,own.failures,own.fights,(unsigned long long)lost_ms,
                         own.next_ms==OWN_NEVER?-1ll:(own.next_ms>now?(long long)(own.next_ms-now):0ll));
                    own_logged=code;
                }
            }
            /* v9.9.5: exclusive audible playback (OutputPolicy.h). Only while Speaka's output runs and its session
             * mixes; a call or an alarm interrupts it and is handled by the interruption path instead. */
            if(o->queue && !o->paused && g_mix.mixing && now-last_hint_poll>=200u) {
                last_hint_poll=now;
                AVAudioSession *session=[AVAudioSession sharedInstance];
                int hint=session.secondaryAudioShouldBeSilencedHint?1:0;
                if(hint!=hint_seen) {
                    hlog(stdout,"V103_OTHER_AUDIO nonmixing=%d other_playing=%d\n",hint,session.isOtherAudioPlaying?1:0);
                    hint_seen=hint;
                }
                if(hint && excl_should_assert(&excl,now)) {
                    int ok=assert_exclusive();
                    excl_asserted(&excl,now);
                    hlog(stdout,"V103_EXCLUSIVE asserted=%d asserts=%u fights=%u backoff_ms=%u\n",ok,excl.asserts,excl.fights,excl.backoff_ms);
                }
            }
            /* v9.9.2: close the input-rate window on time even when no packet arrives. */
            if(input.window_ms) {
                int change=input_rate_add(&input,0,o->rate?o->rate:rate_hint,now);
                if(change) {
                    if(change>0) warm=0;
                    if(change<0) {pthread_mutex_lock(&o->lock);o->priming=o->count<PCM_PRIME;pthread_mutex_unlock(&o->lock);}
                    __atomic_store_n(&o->input_starved,change>0,__ATOMIC_RELAXED);
                    hlog(stdout,"V100_INPUT %s rate=%u/%u (%u%%) starvations=%u starved_total_ms=%llu\n",
                         change>0?"starved":"healthy",input.rate,o->rate?o->rate:rate_hint,
                         (o->rate?o->rate:rate_hint)?(unsigned)((uint64_t)input.rate*100u/(o->rate?o->rate:rate_hint)):0u,
                         input.starvations,(unsigned long long)input.starved_total_ms);
                }
            }
            uint64_t signal=__atomic_load_n(&s->doorbell,__ATOMIC_ACQUIRE);
            if(signal!=doorbell) {
                for(unsigned i=0;i<AUDIO_RING_SLOTS && audio_ring_dequeue(s,&packet);i++) {
                    if(!(generation&1u) || packet.generation!=generation) continue;
                    unsigned frames=0,samples=0,rate=0;
                    int result=codec_decoder_decode(&decoder,&packet,pcm,CODEC_PCM_SAMPLES,&frames,&samples,&rate);
                    audio_ring_event(s,1,(AudioRingEvent){RING_EVENT_DECODE,packet.length,frames,result});
                    if(result || !samples) continue;
                    measure_pcm(&level,pcm,samples);
                    /* Recheck after decoding so a concurrent STOP cannot queue stale PCM. */
                    if(__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE)!=generation) break;
                    rate_hint=rate;                     /* the next (re)start uses the stream's real rate */
                    __atomic_fetch_add(&o->decoded_frames,samples,__ATOMIC_RELAXED);
                    int change=input_rate_add(&input,samples,o->rate?o->rate:rate,helper_ms());
                    if(change) {
                        if(change>0) warm=0;
                        if(change<0) {pthread_mutex_lock(&o->lock);o->priming=o->count<PCM_PRIME;pthread_mutex_unlock(&o->lock);}
                        __atomic_store_n(&o->input_starved,change>0,__ATOMIC_RELAXED);
                        hlog(stdout,"V100_INPUT %s rate=%u/%u starvations=%u starved_total_ms=%llu\n",
                             change>0?"starved":"healthy",input.rate,o->rate?o->rate:rate,input.starvations,
                             (unsigned long long)input.starved_total_ms);
                    }
                    if(o->queue && !o->paused && o->rate!=rate) {
                        /* v9.1: a new sample rate rebuilds only the queue; the session stays active. */
                        stop_output(o,s,"sample_rate_changed",0);
                        int restarted=start_output(o,rate,s);
                        if(restarted) {
                            stop_output(o,s,"sample_rate_restart_failed",1);
                            own_start_failed(&own,helper_ms(),restarted==-2);
                        } else own_started(&own,helper_ms());
                    }
                    /* v9.9.4: a warm resume starts with Windows' silent frames (14:52:14: four 16-byte packets, ~85 ms);
                     * playing them only delays the music, so digital silence is dropped until the first real sample. */
                    int keep=1;
                    if(o->trim_silence) {
                        int silent=1;
                        for(unsigned k=0;k<samples*2u && silent;k++) silent=pcm[k]==0;
                        if(silent) keep=0; else o->trim_silence=0;
                    }
                    /* v9.9.2: held (input starved, or paused by an interruption): keep only the newest lead. */
                    if(keep && o->queue && (o->paused || __atomic_load_n(&o->input_starved,__ATOMIC_RELAXED))) hold_pcm(o,pcm,samples);
                    else if(keep && o->queue) write_pcm(o,pcm,samples);
                }
                /* If bounded drain left packets, poll again instead of losing a wakeup. */
                if(__atomic_load_n(&s->media.read,__ATOMIC_RELAXED)==
                   __atomic_load_n(&s->media.write,__ATOMIC_ACQUIRE)) doorbell=signal;
            }
            diagnostic_ticks++;
            if(diagnostic_ticks%40u==0) report_health(o,s,generation);
            if(diagnostic_ticks%200u==0 && ((generation&1u) || level.samples))
                report_progress(o,&level,s,generation,&input);
        }
        /* v8.9.4: this loop runs on the helper's main thread and never ran its run loop, and not one route change or
         * interruption was handled in any session, although earbuds were plugged in during playback. If AVAudioSession
         * delivers these through the main thread's run loop or main queue, they could never arrive. Draining it
         * without blocking costs nothing if they arrive on another thread; the 5 ms doorbell cadence is kept. */
        @autoreleasepool {CFRunLoopRunInMode(kCFRunLoopDefaultMode,0,true);}
        usleep(5000); /* shared-memory doorbell polling, <=5ms nominal wake latency */
    }
    [[NSNotificationCenter defaultCenter] removeObserver:observer];
    [[NSNotificationCenter defaultCenter] removeObserver:route_observer];
    [[NSNotificationCenter defaultCenter] removeObserver:reset_observer];
    stop_output(o,s,"ipc_closed",1);codec_decoder_end(&decoder);
    if(o->volume) __atomic_store_n(&o->volume->heartbeat_ms,0,__ATOMIC_RELEASE);
    sp_volume_mailbox_close(o->volume);pthread_mutex_destroy(&o->lock);free(o);
    publish_status(@"stopped",@"Bluetooth speaker session ended",YES);
    munmap(s,sizeof *s);close(file);
}
static int stream_service_main(void) {
    hlog(stdout,"V889_HELPER build=v9.1.0-output-select (v8.8.2 idle guard retained, v9.1 output hold)\n");fflush(stdout);
    int (*activate)(const char *,int **,size_t *)=dlsym(RTLD_DEFAULT,"launch_activate_socket");
    int *fds=NULL;size_t count=0;
    if(!activate || activate("Stream",&fds,&count) || !count) {
        hlog(stderr,"V88_SERVICE socket activation unavailable; run via launchd\n");free(fds);return 2;
    }
    int listener=fds[0];for(size_t i=1;i<count;i++) close(fds[i]);free(fds);
    for(;;) {
        fd_set set;FD_ZERO(&set);FD_SET(listener,&set);struct timeval idle={10,0};
        int ready=select(listener+1,&set,NULL,NULL,&idle);
        if(ready<0 && errno==EINTR) continue;
        if(ready<=0) break;
        int fd=accept(listener,NULL,NULL);
        if(fd>=0) {@autoreleasepool {stream(fd);} close(fd);}
    }
    close(listener);return 0;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if(argc==3 && strcmp(argv[1],"--volume-mode")==0) {
            if(strcmp(argv[2],"off") && strcmp(argv[2],"observe") && strcmp(argv[2],"experimental")) return 2;
            CFStringRef mode=CFStringCreateWithCString(NULL,argv[2],kCFStringEncodingUTF8);
            if(!mode) return 1;
            CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_MODE),mode,CFSTR(SPEAKA_PREFS_DOMAIN));
            CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_REQUEST),NULL,CFSTR(SPEAKA_PREFS_DOMAIN));
            CFRelease(mode);
            if(!CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN))) return 1;
            hlog(stdout,"AVRCP volume mode saved: %s; tap Stop then Start in Speaka to apply\n",argv[2]);
            return 0;
        }
        /* Local Speaka gain for the opt-in experiment. This does not set iOS system volume. */
        if(argc==3 && strcmp(argv[1],"--volume")==0) {
            char *end=NULL;long value=strtol(argv[2],&end,10);
            if(!argv[2][0] || !end || *end || value<0 || value>127) return 2;
            CFPropertyListRef mode=CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_VOLUME_MODE),CFSTR(SPEAKA_PREFS_DOMAIN));
            int enabled=mode && CFGetTypeID(mode)==CFStringGetTypeID() && CFEqual(mode,CFSTR("experimental"));if(mode) CFRelease(mode);
            if(!enabled) {hlog(stderr,"AVRCP volume experiment is not enabled for this user\n");return 2;}
            NSDictionary *request=@{@"value":@(value),@"time":@(CFAbsoluteTimeGetCurrent())};
            CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_REQUEST),(__bridge CFPropertyListRef)request,CFSTR(SPEAKA_PREFS_DOMAIN));
            if(!CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN))) return 1;
            hlog(stdout,"Local volume request submitted: %ld/127; check AVRCP_VOLUME local accepted= in daemon log\n",value);
            return 0;
        }
        if (argc == 2 && strcmp(argv[1], "--stream-service") == 0) return stream_service_main();
        /* A diagnostic, run by hand over SSH: decodes an embedded AAC fixture through the real decoder (AACSelfTest.c).
         * It touches nothing else - no ring, no preferences, no audio output. */
        if (argc >= 2 && strcmp(argv[1], "--aac-selftest") == 0) return aac_selftest_main(argc, argv);
        hlog(stderr, "usage: BTPSAudioHelper --stream-service (launchd starts it: com.parv.speaka.audio)\n"
                     "       BTPSAudioHelper --volume-mode off|observe|experimental (run as mobile; restart Speaka)\n"
                     "       BTPSAudioHelper --volume 0..127 (experimental Speaka gain, not iOS system volume)\n"
                     "       BTPSAudioHelper --aac-selftest [--stage=entry|create|decode1|full] [--strategy=NAME|all]\n");   /* timestamped, as every line is */
        return 2;
    }
}
