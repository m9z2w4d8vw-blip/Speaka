#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Optional experiment. None of these functions owns or changes an audio session. */
void SpeakaVolumeInstall(uintptr_t image_base,int exact_uuid);
/* The PC configured the speaker: the A2DP device's verified address (PeerIdentity.c), or NULL if unresolved. */
void SpeakaVolumeConfigure(const uint8_t *peer);
void SpeakaVolumeDisconnect(void);

#ifdef __cplusplus
}
#endif
