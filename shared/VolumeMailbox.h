/* Optional sidecar: AudioRing's existing layout and handshake stay unchanged. */
#pragma once
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
#define SP_VOLUME_MAILBOX_MAGIC 0x53505631u
typedef struct {
    uint32_t magic, version, pid, size;
    uint64_t token;
    uint64_t heartbeat_ms;  /* helper stream thread, CLOCK_MONOTONIC */
    uint32_t control;       /* atomic: bit 8 enabled; bits 0..6 gain */
    uint32_t reserved;
} SPVolumeMailbox;
SPVolumeMailbox *sp_volume_mailbox_open(const char *path,uint32_t pid,uint64_t token,int create);
void sp_volume_mailbox_close(SPVolumeMailbox *);
uint64_t sp_volume_now_ms(void);
int sp_volume_mailbox_ready(const SPVolumeMailbox *);
void sp_volume_mailbox_set(SPVolumeMailbox *,int enabled,unsigned value);
unsigned sp_volume_mailbox_gain(const SPVolumeMailbox *);
/* v9.9.10: Windows has engaged (a level is set) - sp_volume_mailbox_gain() alone can't tell 127 from "none". */
int sp_volume_mailbox_enabled(const SPVolumeMailbox *);
#ifdef __cplusplus
}
#endif
