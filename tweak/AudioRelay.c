/* AudioRelay.c - hands the PC's audio packets to the helper: the shared-memory ring, the worker thread that wakes
 * the helper over 127.0.0.1 (launchd starts it on demand), and the health check that keeps a stalled helper from
 * holding the stream. Plain POSIX, so the host tests compile it. */
#include "Tweak.h"
#include "../shared/AudioRing.h"
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdint.h>
#include <sys/select.h>
#include <sys/time.h>

/* ---- waking the helper: connect to 127.0.0.1:port within a second, with 2 s send and receive timeouts. launchd
 * starts the helper when something connects to its port. ---- */
/* tweak/AudioRelay.c - the loopback connect the audio relay uses to wake the helper (tweak/AudioRelay.c).
 * The v7.4/v7.5 probes that once lived here, which sent test PCM and a recorded SBC fixture to a helper started by
 * hand over SSH, were removed with the helper's test modes. */

static int timed_connect_port(int fd, uint16_t port)
{
    int flags=fcntl(fd,F_GETFL,0);
    if(flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0) return -1;
    struct sockaddr_in address={0};
    address.sin_family=AF_INET;
    address.sin_port=htons(port);
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    int r=connect(fd,(const struct sockaddr *)&address,sizeof address);
    if(r<0 && errno!=EINPROGRESS) return -1;
    if(r<0) {
        fd_set wfds;
        FD_ZERO(&wfds);FD_SET(fd,&wfds);
        struct timeval timeout={1,0};
        if(select(fd+1,NULL,&wfds,NULL,&timeout)!=1) { errno=ETIMEDOUT;return -1; }
        int e=0;socklen_t n=sizeof e;
        if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&e,&n)<0) return -1;
        if(e) {errno=e;return -1;}
    }
    if(fcntl(fd,F_SETFL,flags)<0) return -1;
    struct timeval timeout={2,0};
    (void)setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof timeout);
    (void)setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
#ifdef SO_NOSIGPIPE   /* Apple's; the host tests build this file too */
    int nosigpipe=1;(void)setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&nosigpipe,sizeof nosigpipe);
#endif
    return 0;
}

static int connect_loopback(uint16_t port)
{
    int fd=socket(AF_INET,SOCK_STREAM,0);
    if(fd>=0 && timed_connect_port(fd,port)<0) {close(fd);return -1;}
    return fd;
}

/* ---- the relay ---- */
/* Only this worker performs IPC/logging. Never called from the media thread. */

static AudioRing *g_shared;
/* v9.9: set once the PC has configured the speaker: the worker connects the helper without waiting for START. */
static volatile unsigned g_relay_warm;
static AudioRingHello g_hello;

/* v8.9.3: one V88_FORWARD and one V88_DECODE line per packet filled 94% of the log, so a Settings export showed only
 * the last ~7 seconds of a session. Successful packets are now counted and summarized every two seconds (and right
 * before any stream state change); anything unusual is still logged individually, at most RELAY_DETAILS_PER_WINDOW
 * lines per window, with a count of what was held back. */
#define RELAY_SUMMARY_MS 2000u
#define RELAY_DETAILS_PER_WINDOW 8u
typedef struct {
    uint64_t forwarded, forward_ok, forward_inactive, forward_other, forward_bytes;
    uint64_t decoded, decode_ok, decode_partial, decode_bad, decode_frames;
    unsigned details, held_back;
} RelayTally;
static RelayTally g_relay_tally;
static uint64_t g_relay_window_ms;
static AudioRingEvent g_relay_pending_pcm;     /* the helper sends PCM then RENDER; they are logged as one line */
static int g_relay_have_pcm;
static unsigned g_relay_health_why;          /* last helper reason bits; 0 = playing or an older helper (no reason) */
static int relay_detail_allowed(void) {
    if(g_relay_tally.details<RELAY_DETAILS_PER_WINDOW) {g_relay_tally.details++;return 1;}
    g_relay_tally.held_back++;return 0;
}
/* Writes the window's summary (if anything happened) and starts a new window. force=1 flushes immediately. */
static void relay_flush_tally(uint64_t now,int force) {
    if(!g_relay_window_ms) g_relay_window_ms=now;
    if(!force && now-g_relay_window_ms<RELAY_SUMMARY_MS) return;
    const RelayTally *t=&g_relay_tally;
    if(t->forwarded || t->decoded || t->held_back)
        SpeakaLogNow("V893_RX %llums forwarded=%llu ok=%llu inactive=%llu other=%llu bytes=%llu "
                   "decoded=%llu ok=%llu partial=%llu bad=%llu frames=%llu held_back=%u",
                   (unsigned long long)(now>=g_relay_window_ms?now-g_relay_window_ms:0),
                   (unsigned long long)t->forwarded,(unsigned long long)t->forward_ok,
                   (unsigned long long)t->forward_inactive,(unsigned long long)t->forward_other,
                   (unsigned long long)t->forward_bytes,(unsigned long long)t->decoded,
                   (unsigned long long)t->decode_ok,(unsigned long long)t->decode_partial,
                   (unsigned long long)t->decode_bad,(unsigned long long)t->decode_frames,t->held_back);
    memset(&g_relay_tally,0,sizeof g_relay_tally);
    g_relay_window_ms=now;
}
static uint64_t g_health_generation,g_health_deadline_ms;
static uint64_t monotonic_ms(void) {
    struct timespec ts;
    if(clock_gettime(CLOCK_MONOTONIC,&ts)) return 0;
    return (uint64_t)ts.tv_sec*1000u+(uint64_t)ts.tv_nsec/1000000u;
}
int AudioRelaySinkHealthy(void) {
    AudioRing *s=__atomic_load_n(&g_shared,__ATOMIC_ACQUIRE);
    if(!s) return 0;
    uint64_t healthy_generation=__atomic_load_n(&g_health_generation,__ATOMIC_ACQUIRE);
    uint64_t deadline=__atomic_load_n(&g_health_deadline_ms,__ATOMIC_ACQUIRE);
    uint64_t generation=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
    uint64_t now=monotonic_ms();
    return (generation&1u) && now && now<deadline &&
        healthy_generation==generation;
}

void AudioRelaySetCodec(uint32_t codec,const uint8_t *codec_config,size_t len) {
    AudioRing *s=__atomic_load_n(&g_shared,__ATOMIC_ACQUIRE);
    if(!s) return;
    /* Written before the generation turns active (AudioRelaySetActive), which the helper reads with acquire order,
     * so a plain store is ordered ahead of it. */
    memset(s->codec_config,0,sizeof s->codec_config);
    if(codec_config && len && len<=sizeof s->codec_config) memcpy(s->codec_config,codec_config,len);
    __atomic_store_n(&s->codec,codec,__ATOMIC_RELEASE);
}
void AudioRelaySetStreamState(uint32_t state) {
    AudioRing *s=__atomic_load_n(&g_shared,__ATOMIC_ACQUIRE);
    if(!s) return;
    __atomic_store_n(&s->stream_state,state,__ATOMIC_RELEASE);
    if(state!=AUDIO_STREAM_NONE && state!=AUDIO_STREAM_CLOSED) __atomic_store_n(&g_relay_warm,1u,__ATOMIC_RELEASE);
}
void AudioRelaySetActive(int active) {
    AudioRing *s=__atomic_load_n(&g_shared,__ATOMIC_ACQUIRE);
    if(!s) return;
    __atomic_store_n(&g_health_deadline_ms,0,__ATOMIC_RELEASE);
    uint64_t old=__atomic_load_n(&s->generation,__ATOMIC_RELAXED);
    /* Signaling is serialized by the native AVDTP dispatcher. */
    __atomic_store_n(&s->generation,((old&~1ull)+2u)|(active?1u:0u),__ATOMIC_RELEASE);
}
void AudioRelayForward(uint32_t handle,uint32_t sequence,uint32_t timestamp,
                    const uint8_t *payload,uint32_t length,uint32_t error,
                    uint32_t expected_handle) {
    AudioRing *s=__atomic_load_n(&g_shared,__ATOMIC_ACQUIRE);
    if(!s) return; /* callback is installed only after Prepare succeeds */
    uint64_t generation=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
    int result=!(generation&1u)?-1:
        (!handle || handle!=expected_handle)?-3:
        error?-4:(!payload || !length || length>AUDIO_RING_PAYLOAD)?-5:0;
    if(result==0) {
        AudioPacket p;
        p.generation=generation; p.length=length; p.timestamp=timestamp;
        p.sequence=(uint16_t)sequence; p.handle=(uint16_t)handle;
        memcpy(p.data,payload,length);
        result=audio_ring_enqueue(s,&p)?0:-2;
    }
    AudioRingEvent event={RING_EVENT_FORWARD,length,handle,result};
    audio_ring_event(s,0,event);
}
static void drain_logs(AudioRing *s,SinkHealth *health,uint64_t now) {
    AudioRingEvent e;
    for(unsigned i=0;i<AUDIO_RING_EVENTS;i++) {
        if(!audio_ring_pop(&s->forward_log,s->forwarded,sizeof s->forwarded[0],
                        AUDIO_RING_EVENTS,&e,sizeof e)) break;
        g_relay_tally.forwarded++;g_relay_tally.forward_bytes+=e.bytes;
        if(e.result==0) g_relay_tally.forward_ok++;
        else if(e.result==-1) g_relay_tally.forward_inactive++;   /* expected just after STOP/SUSPEND */
        else {
            g_relay_tally.forward_other++;
            if(relay_detail_allowed()) SpeakaLogNow("V88_FORWARD bytes=%u handle=%u result=%d",e.bytes,e.frames,e.result);
        }
    }
    for(unsigned i=0;i<AUDIO_RING_EVENTS;i++) {
        if(!audio_ring_pop(&s->decode_log,s->decoded,sizeof s->decoded[0],
                        AUDIO_RING_EVENTS,&e,sizeof e)) break;
        if(e.kind==RING_EVENT_HEALTH) {
            /* v9.9.2: 1 = playing (counter: rendered frames); 2 = holding through an interruption (counter: decoded
             * frames). Both keep the lease while their counter advances: the helper is consuming the stream. */
            sink_health_render(health,e.bytes,e.frames,e.result==1||e.result==2,now);
            g_relay_health_why=e.result<0?(unsigned)(-(int64_t)e.result):0u;
        }
        else if(e.kind==RING_EVENT_DECODE) {
            g_relay_tally.decoded++;g_relay_tally.decode_frames+=e.frames;
            if(e.result==0) g_relay_tally.decode_ok++;
            else if(e.result==1) g_relay_tally.decode_partial++;      /* SBC fragment still being reassembled */
            else {
                g_relay_tally.decode_bad++;
                if(relay_detail_allowed()) SpeakaLogNow("V88_DECODE frames=%u bytes=%u result=%d",e.frames,e.bytes,e.result);
            }
        }
        else if(e.kind==RING_EVENT_PCM) {g_relay_pending_pcm=e;g_relay_have_pcm=1;}
        else if(e.kind==RING_EVENT_RENDER) {
            if(g_relay_have_pcm)
                SpeakaLogNow("V881_PCM samples=%u nonzero=%d peak=%u | RENDER callbacks=%d pcm_frames=%u silence_frames=%u",
                           g_relay_pending_pcm.frames,g_relay_pending_pcm.result,g_relay_pending_pcm.bytes,
                           e.result,e.frames,e.bytes);
            else SpeakaLogNow("V881_RENDER callbacks=%d pcm_frames=%u silence_frames=%u",e.result,e.frames,e.bytes);
            g_relay_have_pcm=0;
        }
        else if(e.kind==RING_EVENT_ROUTE)
            SpeakaLogNow("V881_ROUTE route_ok=%u volume_pct=%u rate=%u",(unsigned)(e.result==0),e.bytes,e.frames);
        else SpeakaLogNow("V88_AUDIO status=%d rate=%u (0=started 1=stopped; errors are OSStatus)",e.result,e.frames);
    }
}
static int attach_helper(void) {
    int fd=connect_loopback(AUDIO_HELPER_PORT);
    if(fd<0) return -1;
    const uint8_t *p=(const uint8_t *)&g_hello;
    size_t at=0;
    while(at<sizeof g_hello) {
        ssize_t n=send(fd,p+at,sizeof g_hello-at,0);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) {close(fd);return -1;} at+=(size_t)n;
    }
    uint8_t ack=0;
    if(recv(fd,&ack,1,0)!=1 || ack!=88) {close(fd);return -1;}
    int flags=fcntl(fd,F_GETFL,0);
    if(flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0) {close(fd);return -1;}
    return fd;
}
static void *worker(void *arg) {
    AudioRing *s=arg;
    int fd=-1,last_ready=-1; unsigned ticks=0, retry=0, stale=0;
    uint64_t heartbeat=0,last_generation=0,last_drops=0,last_logs=0;
    SinkHealth health={0};
    for(;;) {
        uint64_t now=monotonic_ms();
        uint64_t generation=__atomic_load_n(&s->generation,__ATOMIC_ACQUIRE);
        uint64_t media=__atomic_load_n(&s->doorbell,__ATOMIC_ACQUIRE);
        uint64_t helper=__atomic_load_n(&s->helper_heartbeat,__ATOMIC_ACQUIRE);
        if(health.generation!=generation)
            sink_health_reset(&health,generation,media,helper);
        sink_health_sample(&health,media,helper,now);
        drain_logs(s,&health,now);
        relay_flush_tally(now,generation!=last_generation);
        if(generation!=last_generation) {
            SpeakaLogNow("V88_STREAM active=%u generation=%llu",(unsigned)(generation&1u),
                       (unsigned long long)(generation>>1));
            last_generation=generation;
        }
        if(fd>=0) {
            uint8_t c; ssize_t n=recv(fd,&c,1,MSG_PEEK);
            uint64_t h=__atomic_load_n(&s->helper_heartbeat,__ATOMIC_ACQUIRE);
            stale=h==heartbeat?stale+1:0;heartbeat=h;
            if(n==0 || (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) || stale>250) {
                close(fd);fd=-1;retry=50;stale=0;
                sink_health_reset(&health,generation,media,helper);
                SpeakaLogNow("V88_HELPER disconnected; retry remains off receive thread");
            }
        }
        if(retry) retry--;
        /* v9.9: also once the PC has configured the speaker (warm), so the helper owns the output before START. */
        if(fd<0 && ((generation&1u) || __atomic_load_n(&g_relay_warm,__ATOMIC_ACQUIRE)) && !retry) {
            fd=attach_helper();retry=100;
            SpeakaLogNow("V88_HELPER %s port=%u",fd>=0?"mapped shared ring":"activation failed",AUDIO_HELPER_PORT);
        }
        /* The lease also expires if this worker stalls. A real STOP changes
         * generation immediately, so an old lease cannot keep it alive. */
        int ready=now && sink_health_ready(&health,fd>=0,now);
        if(__atomic_load_n(&g_health_generation,__ATOMIC_RELAXED)!=generation) {
            __atomic_store_n(&g_health_deadline_ms,0,__ATOMIC_RELEASE);
            __atomic_store_n(&g_health_generation,generation,__ATOMIC_RELEASE);
        }
        __atomic_store_n(&g_health_deadline_ms,ready?now+500u:0,__ATOMIC_RELEASE);
        if(ready!=last_ready) {
            /* v8.9.4 why= bits from the helper: 0x1 stream inactive, 0x2 no queue, 0x4 queue not running,
             * 0x8 stopping, 0x10 enqueue error, 0x20 route not acceptable for the selected mode. */
            SpeakaLogNow("V882_HEALTH ready=%d generation=%llu connected=%d playing=%u media_age_ms=%lld heartbeat_age_ms=%lld render_age_ms=%lld why=0x%x",
                ready,(unsigned long long)(generation>>1),fd>=0,health.playing,
                health.media_seen?(long long)(now-health.media_ms):-1ll,
                health.heartbeat_seen?(long long)(now-health.heartbeat_ms):-1ll,
                health.render_seen?(long long)(now-health.render_ms):-1ll,g_relay_health_why);
            last_ready=ready;
        }
        if(++ticks%50==0) {
            uint64_t drops=__atomic_load_n(&s->media.dropped,__ATOMIC_RELAXED);
            uint64_t logs=__atomic_load_n(&s->forward_log.dropped,__ATOMIC_RELAXED)+
                          __atomic_load_n(&s->decode_log.dropped,__ATOMIC_RELAXED);
            if(drops!=last_drops || logs!=last_logs)
                SpeakaLogNow("V88_DROP packets=%llu log_records=%llu",(unsigned long long)drops,(unsigned long long)logs);
            last_drops=drops;last_logs=logs;
        }
        usleep(20000); /* dedicated worker, never the Bluetooth receive thread */
    }
    return NULL;
}
int AudioRelayPrepare(void) {
    if(g_shared) return 1;
    uint64_t token; arc4random_buf(&token,sizeof token);
    char path[160];
    snprintf(path,sizeof path,AUDIO_RING_PATH_FORMAT,(unsigned)getpid(),(unsigned long long)token);
    int fd=open(path,O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
    if(fd<0) {SpeakaLogNow("V88_PREPARE open failed errno=%d",errno);return 0;}
    AudioRing *s=MAP_FAILED;
    if(ftruncate(fd,sizeof *s)==0) s=mmap(NULL,sizeof *s,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    close(fd);
    if(s==MAP_FAILED) {unlink(path);SpeakaLogNow("V88_PREPARE mapping failed errno=%d",errno);return 0;}
    audio_ring_init(s,(uint32_t)getpid(),token); /* touches every page before use */
    if(mlock(s,sizeof *s)!=0) {
        SpeakaLogNow("V88_PREPARE mlock failed errno=%d; live forwarding refused",errno);
        munmap(s,sizeof *s);unlink(path);return 0;
    }
    g_hello=(AudioRingHello){AUDIO_RING_MAGIC,AUDIO_RING_VERSION,sizeof *s,(uint32_t)getpid(),token};
    pthread_t thread;
    int e=pthread_create(&thread,NULL,worker,s);
    if(e) {munlock(s,sizeof *s);munmap(s,sizeof *s);unlink(path);return 0;}
    pthread_detach(thread);
    __atomic_store_n(&g_shared,s,__ATOMIC_RELEASE);
    SpeakaLogNow("V88_PREPARE ready ring_bytes=%zu payload_capacity=65536 locked=1; helper waits for accepted START",sizeof *s);
    return 1;
}
