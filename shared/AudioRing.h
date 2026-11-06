/* v8.8 cross-process ABI. No pointers, locks, allocation, or OS calls in queues. */
#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
#define AUDIO_RING_MAGIC 0x38565042u
#define AUDIO_RING_VERSION 1u
#define AUDIO_RING_SLOTS 32u
#define AUDIO_RING_PAYLOAD 2048u
#define AUDIO_RING_EVENTS 256u
#define AUDIO_HELPER_PORT 37594
#define AUDIO_RING_PATH_FORMAT "/tmp/FTABHarvest/BTPS-v88-%u-%016llx.shm"

typedef struct { uint64_t write, read, dropped; } AudioRingQueue;
typedef struct {
    uint64_t generation;
    uint32_t length, timestamp;
    uint16_t sequence, handle;
    uint8_t data[AUDIO_RING_PAYLOAD];
} AudioPacket;
typedef struct { uint64_t turn; AudioPacket value; } AudioPacketSlot;
/* Event additions preserve the existing shared-memory layout/version. */
enum { RING_EVENT_FORWARD=1, RING_EVENT_DECODE=2, RING_EVENT_AUDIO=3,
       RING_EVENT_PCM=4, RING_EVENT_RENDER=5, RING_EVENT_ROUTE=6,
       RING_EVENT_HEALTH=7 };
typedef struct { uint32_t kind, bytes, frames; int32_t result; } AudioRingEvent;
typedef struct { uint64_t turn; AudioRingEvent value; } AudioRingEventSlot;
/* The codec the tweak negotiated for this session, published in the header (not per packet, so the packet ABI is
 * unchanged). codec is A2DP_CODEC_SBC or A2DP_CODEC_AAC (AACConfig.h); codec_config holds the 6-byte AAC Codec
 * Specific Information when codec is AAC, so the helper builds its decoder without parsing native structures. Both
 * are written before AudioRelaySetActive(1) and read by the helper when the generation turns active. SBC leaves them
 * zero, which the helper reads as SBC, so an old helper and a new tweak still agree on SBC. */
typedef struct {
    uint32_t magic, version, size, pid;
    uint64_t token;
    /* generation includes active bit 0; increments on every START/STOP. */
    uint64_t generation, doorbell, helper_heartbeat;
    AudioRingQueue media, forward_log, decode_log;
    AudioPacketSlot packets[AUDIO_RING_SLOTS];
    AudioRingEventSlot forwarded[AUDIO_RING_EVENTS], decoded[AUDIO_RING_EVENTS];
    uint32_t codec;              /* 0 (==A2DP_CODEC_SBC) unless AAC was negotiated */
    uint8_t codec_config[8];     /* AAC Codec Specific Information (6 bytes used); zero for SBC */
} AudioRing;
typedef struct {
    uint32_t magic, version, size, pid;
    uint64_t token;
} AudioRingHello;

void audio_ring_init(AudioRing *, uint32_t pid, uint64_t token);
/* Bounded retries; on full, claim/discard oldest complete item, never overwrite
 * a reader's slot. If a preempted owner pins a slot, drop the new item instead.
 * This is intentionally not a spin-until-success queue. */
int audio_ring_push(AudioRingQueue *, void *slots, size_t stride,
                  unsigned capacity, const void *value, size_t bytes);
int audio_ring_pop(AudioRingQueue *, void *slots, size_t stride,
                 unsigned capacity, void *value, size_t bytes);
int audio_ring_enqueue(AudioRing *, const AudioPacket *);
int audio_ring_dequeue(AudioRing *, AudioPacket *);
void audio_ring_event(AudioRing *, int helper, AudioRingEvent);
#ifdef __cplusplus
}
#endif
