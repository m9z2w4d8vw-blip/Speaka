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
/* v9.9.13: the device whose AVRCP connection holds the registration configured (or closed) an A2DP stream. AVRCP
 * notifications last as long as the AVRCP connection, or until CHANGED completes them - not the A2DP stream: the same
 * peer keeps its registration, label, level and any pending CHANGED under the new epoch. Another peer, or none bound,
 * is bound fresh (sp_volume_bind). Returns 1 when a live registration was kept. Connecting from the iPhone brings AVRCP
 * up first: Windows registers, then configures A2DP ~10 s later, and never registers again while it holds one. */
int sp_volume_rebind(SPVolume *, const uint8_t peer[6], uint64_t epoch, uint8_t initial);
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
/* ---- one user-facing volume: iOS output volume (v9.9.10) ----
 * The phone's buttons, Control Center and HUD all show iOS's output volume; Windows' slider drives the sink through
 * SetAbsoluteVolume. They become one volume: Windows' level is set as iOS's output volume (AVSystemController
 * setActiveCategoryVolumeTo:, the call mediaremoted makes), the user's iOS changes go to Windows as CHANGED, and
 * Speaka's own PCM gain stays at unity - no second attenuation. If the setter is missing or ineffective, the domain falls
 * back to Speaka's gain (Windows -> gain, no reverse sync). This is the pure policy; the helper does the I/O. */
#define SP_DOMAIN_ECHO_MS 1000u      /* iOS changes this soon after a set are that set's own, never the user's */
#define SP_DOMAIN_SET_MS 50u         /* at most one set per 50 ms while Windows drags; the newest level wins */
#define SP_DOMAIN_VERIFY_MS 400u     /* a set is read back this long after */
#define SP_DOMAIN_MAX_FAILURES 3u    /* consecutive ineffective sets before falling back to Speaka's gain */
typedef struct {
    int ios;                   /* 1: iOS output volume is the volume; 0: Speaka's gain (fallback) */
    int target, pending, seen; /* last level set on iOS, a coalesced level waiting, last iOS level seen (-1: none) */
    uint64_t target_ms, last_set_ms;
    int verify;
    unsigned failures;
} SPVolumeDomain;
void sp_domain_init(SPVolumeDomain *, int ios);
/* Windows asked for level. Returns the level to set on iOS now, or -1 (none needed, or coalesced for later). */
int sp_domain_remote(SPVolumeDomain *, int level, int current_ios, uint64_t now);
/* Each tick: a coalesced level whose rate limit has passed, or -1. */
int sp_domain_due(SPVolumeDomain *, int current_ios, uint64_t now);
void sp_domain_set(SPVolumeDomain *, int level, uint64_t now);
/* iOS reports level. 0: no change or the domain's own echo; 1: the user's change, for Windows; 2: the first reading. */
int sp_domain_observed(SPVolumeDomain *, int level, uint64_t now);
/* The read-back of the last set. Returns 1 when the domain just fell back to Speaka's gain. */
int sp_domain_verify(SPVolumeDomain *, int readback, uint64_t now);
/* Signed stereo/interleaved PCM; exact unity, exact digital mute, no clipping.
 * samples is the number of int16_t values, not the number of frames. */
void sp_volume_pcm(int16_t *pcm, size_t samples, unsigned value);
#ifdef __cplusplus
}
#endif
