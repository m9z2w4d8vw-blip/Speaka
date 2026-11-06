/* AACDecoder.h - decodes the PC's AAC-LC packets to interleaved stereo PCM, using Apple's AudioToolbox
 * AudioConverter (a public, documented API: AudioConverterNewSpecific / AudioConverterFillComplexBuffer). No private
 * Apple interfaces, no on-device offsets.
 *
 * The interface mirrors SBCDecoder so the helper's receive loop can hold either behind a tiny codec dispatch. Both
 * take one AudioPacket from the shared ring and emit interleaved stereo int16 PCM.
 *
 * What reaches us over A2DP for AAC is an LATM/LOAS payload inside an RTP frame (RFC 3016); the tweak has already
 * stripped the RTP header (as it does for SBC), so data[0..] is the A2DP media payload. We reframe it to ADTS using
 * the AudioSpecificConfig derived from the negotiated AACConfig, then let AudioConverter decode. This file is only
 * compiled into the helper, which already links AudioToolbox.
 */
#pragma once
#include "../shared/AudioRing.h"
#include "../shared/AACConfig.h"
#include <stdint.h>
#include <stddef.h>

typedef struct AACDecoder AACDecoder;

/* Create a decoder for a negotiated, supported AACConfig (AAC-LC, 44.1/48 kHz, mono/stereo). Returns NULL if cfg is
 * unsupported or AudioToolbox could not create the converter. */
AACDecoder *aac_decoder_create(const AACConfig *cfg);
void aac_decoder_destroy(AACDecoder *);

/* Reset decoder state at a stream boundary (new generation, or a sequence gap), like sbc_decoder_reset. */
void aac_decoder_reset(AACDecoder *);

/* Decode one packet. Emits interleaved stereo (mono is duplicated to both channels), matching SBCDecoder's output.
 * Returns 0 on success with *samples frames written and *rate set, 1 if the packet only buffered (no output yet),
 * or a negative value on error. Writes at most `capacity` int16 samples (stereo pairs counted as 2). */
int aac_decoder_decode(AACDecoder *, const AudioPacket *packet, int16_t *pcm,
                       size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate);
