#include "AudioRing.h"
#include <string.h>
_Static_assert(__atomic_always_lock_free(8, 0), "64-bit atomics must be lock free");
_Static_assert(sizeof(AudioRingHello)==24, "IPC layout");
_Static_assert(offsetof(AudioPacketSlot,value)==8, "slot layout");
_Static_assert(offsetof(AudioRingEventSlot,value)==8, "slot layout");

void audio_ring_init(AudioRing *s, uint32_t pid, uint64_t token) {
    memset(s,0,sizeof *s);
    s->version=AUDIO_RING_VERSION; s->size=sizeof *s; s->pid=pid; s->token=token;
    for(unsigned i=0;i<AUDIO_RING_SLOTS;i++) s->packets[i].turn=i;
    for(unsigned i=0;i<AUDIO_RING_EVENTS;i++) {
        s->forwarded[i].turn=i; s->decoded[i].turn=i;
    }
    __atomic_store_n(&s->magic,AUDIO_RING_MAGIC,__ATOMIC_RELEASE);
}
int audio_ring_pop(AudioRingQueue *q, void *slots, size_t stride,
                 unsigned n, void *value, size_t bytes) {
    uint64_t pos=__atomic_load_n(&q->read,__ATOMIC_RELAXED);
    for(unsigned attempt=0;attempt<8;attempt++) {
        uint8_t *cell=(uint8_t *)slots+(pos%n)*stride;
        uint64_t turn=__atomic_load_n((uint64_t *)cell,__ATOMIC_ACQUIRE);
        int64_t diff=(int64_t)(turn-(pos+1));
        if(diff<0) return 0;
        if(diff==0 && __atomic_compare_exchange_n(&q->read,&pos,pos+1,0,
                                                  __ATOMIC_RELAXED,__ATOMIC_RELAXED)) {
            if(value) memcpy(value,cell+8,bytes);
            __atomic_store_n((uint64_t *)cell,pos+n,__ATOMIC_RELEASE);
            return 1;
        }
        pos=__atomic_load_n(&q->read,__ATOMIC_RELAXED);
    }
    return 0;
}
int audio_ring_push(AudioRingQueue *q, void *slots, size_t stride,
                  unsigned n, const void *value, size_t bytes) {
    uint64_t pos=__atomic_load_n(&q->write,__ATOMIC_RELAXED);
    for(unsigned attempt=0;attempt<8;attempt++) {
        uint8_t *cell=(uint8_t *)slots+(pos%n)*stride;
        uint64_t turn=__atomic_load_n((uint64_t *)cell,__ATOMIC_ACQUIRE);
        int64_t diff=(int64_t)(turn-pos);
        if(diff==0 && __atomic_compare_exchange_n(&q->write,&pos,pos+1,0,
                                                  __ATOMIC_RELAXED,__ATOMIC_RELAXED)) {
            memcpy(cell+8,value,bytes);
            __atomic_store_n((uint64_t *)cell,pos+1,__ATOMIC_RELEASE);
            return 1;
        }
        if(diff<0 && audio_ring_pop(q,slots,stride,n,NULL,0))
            __atomic_fetch_add(&q->dropped,1,__ATOMIC_RELAXED);
        pos=__atomic_load_n(&q->write,__ATOMIC_RELAXED);
    }
    __atomic_fetch_add(&q->dropped,1,__ATOMIC_RELAXED);
    return 0;
}
int audio_ring_enqueue(AudioRing *s,const AudioPacket *p) {
    if(!p->length || p->length>AUDIO_RING_PAYLOAD) return 0;
    int ok=audio_ring_push(&s->media,s->packets,sizeof s->packets[0],AUDIO_RING_SLOTS,
                         p,offsetof(AudioPacket,data)+p->length);
    /* Shared-memory doorbell: helper polls this, no kernel notification here. */
    if(ok) __atomic_fetch_add(&s->doorbell,1,__ATOMIC_RELEASE);
    return ok;
}
int audio_ring_dequeue(AudioRing *s,AudioPacket *p) {
    return audio_ring_pop(&s->media,s->packets,sizeof s->packets[0],AUDIO_RING_SLOTS,p,sizeof *p);
}
void audio_ring_event(AudioRing *s,int helper,AudioRingEvent event) {
    audio_ring_push(helper?&s->decode_log:&s->forward_log,
                  helper?(void *)s->decoded:(void *)s->forwarded,
                  sizeof(AudioRingEventSlot),AUDIO_RING_EVENTS,&event,sizeof event);
}
