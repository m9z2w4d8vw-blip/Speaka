/* SBCDecoder.h - decodes the PC's SBC packets with Google's libsbc (vendor_sbc/), always to interleaved stereo. */
#pragma once
#include "vendor_sbc/sbc.h"
#include "../shared/AudioRing.h"
#define SBC_DECODER_PCM_SAMPLES (15u*SBC_MAX_SAMPLES*2u)
typedef struct {
    sbc_t sbc;
    uint64_t generation;
    unsigned rate, next_sequence, seen, fragment_bytes;
    uint8_t fragment[AUDIO_RING_PAYLOAD];
} SBCDecoder;
void sbc_decoder_reset(SBCDecoder *);
/* Always emits interleaved stereo; duplicates mono. 0=OK, 1=fragment pending. */
int sbc_decoder_decode(SBCDecoder *,const AudioPacket *,int16_t *pcm,
                    size_t capacity,unsigned *frames,unsigned *samples,unsigned *rate);
