/* CodecDecoder.h - one handle over the two decoders, so the helper's receive loop decodes a packet without caring
 * which codec a session negotiated. The codec and its config come from the ring header (AudioRing.codec /
 * codec_config), set by the tweak at CONFIGURE. SBC is the default and needs no config; AAC builds an AudioConverter
 * from the 6-byte Codec Specific Information.
 *
 * Only the SBC path is validated on a device today. If AAC setup fails, or AAC is disabled, the helper falls back to
 * SBC, so a session is never left without a decoder. */
#pragma once
#include "../shared/AudioRing.h"
#include "SBCDecoder.h"
#include "AACDecoder.h"

typedef struct {
    uint32_t codec;            /* A2DP_CODEC_SBC or A2DP_CODEC_AAC, after any fallback */
    SBCDecoder sbc;            /* always present; the fallback and the default */
    AACDecoder *aac;           /* non-NULL only when codec==AAC and setup succeeded */
} CodecDecoder;

/* Set up for a session from the ring header. Reads header->codec and header->codec_config. On AAC, builds the AAC
 * decoder; if that fails (unsupported config, converter error, AAC disabled) it logs why and falls back to SBC.
 * `aac_enabled` is the SpeakaAAC experiment switch. Returns the codec actually in use. */
uint32_t codec_decoder_begin(CodecDecoder *, const AudioRing *header, int aac_enabled,
                             void (*log)(const char *fmt, ...));

/* Reset at a stream boundary, like sbc_decoder_reset: resets whichever decoder is in use. */
void codec_decoder_reset(CodecDecoder *);

/* Decode one packet through the active codec. Same contract as the per-codec decoders: 0 on success with PCM,
 * 1 if only buffered, negative on error. */
int codec_decoder_decode(CodecDecoder *, const AudioPacket *, int16_t *pcm,
                         size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate);

void codec_decoder_end(CodecDecoder *);
