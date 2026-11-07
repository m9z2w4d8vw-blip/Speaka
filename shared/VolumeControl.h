/* AVRCP target volume, independent of Apple's private ABI and audio sessions.
 * One instance per peer/connection. Caller serializes access. No allocation. */
#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { SP_VOLUME_MAX=127, SP_AVRCP_CONTROL=0, SP_AVRCP_STATUS=1, SP_AVRCP_NOTIFY=3,
       SP_AVRCP_REJECTED=10, SP_AVRCP_ACCEPTED=9, SP_AVRCP_STABLE=12,
       SP_AVRCP_CHANGED=13, SP_AVRCP_INTERIM=15 };
typedef struct {
    /* Return nonzero only if the backend accepted the gain or the transport
     * accepted a response. send() must copy bytes before returning. */
    int (*apply)(void *, uint8_t value);
    int (*send)(void *, const uint8_t peer[6], uint8_t label, uint8_t response,
                const uint8_t *operands, size_t count);
    void *context;
} SPVolumeIO;
typedef struct {
    uint8_t peer[6], value, bound, subscribed, label, pending_local;
    uint64_t epoch;
} SPVolume;
void sp_volume_bind(SPVolume *, const uint8_t peer[6], uint64_t epoch, uint8_t initial);
void sp_volume_reset(SPVolume *);
/* Operands start with SIG company ID 00:19:58 (AV/C header is separate).
 * Return 1 when owned/consumed, 0 to delegate unchanged to the native handler.
 * existing_events is the native GetCapabilities list, to preserve its features. */
int sp_volume_command(SPVolume *, const SPVolumeIO *, const uint8_t peer[6],
                      uint64_t epoch, uint8_t label, uint8_t ctype,
                      const uint8_t *operands, size_t count,
                      const uint8_t *existing_events, size_t event_count);
int sp_volume_local(SPVolume *, const SPVolumeIO *, unsigned value);
/* Retry a failed local CHANGED response; never creates a new subscription. */
int sp_volume_flush(SPVolume *, const SPVolumeIO *);
/* Signed stereo/interleaved PCM; exact unity, exact digital mute, no clipping.
 * samples is the number of int16_t values, not the number of frames. */
void sp_volume_pcm(int16_t *pcm, size_t samples, unsigned value);
#ifdef __cplusplus
}
#endif
