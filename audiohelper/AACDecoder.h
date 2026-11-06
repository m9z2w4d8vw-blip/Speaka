/* AACDecoder.h - decodes the PC's AAC-LC packets to interleaved stereo PCM with AudioToolbox's AudioConverter, a
 * public API (AudioConverterNewSpecific / AudioConverterNew / AudioConverterSetProperty /
 * AudioConverterFillComplexBuffer). No private Apple interfaces, no on-device offsets.
 *
 * Input is one AudioPacket from the shared ring per call: the A2DP media payload, an MPEG-4 LATM AudioMuxElement
 * (RFC 3016), or a fragment of one. LATM.c extracts the AAC access unit; the converter is handed that bare access
 * unit as one packet, with the stream's configuration as a magic cookie - the same shape AudioFileStream gives
 * AudioConverter in Apple's own pipeline. Output matches SBCDecoder: interleaved stereo int16 (mono is duplicated).
 */
#pragma once
#include "../shared/AudioRing.h"
#include "../shared/AACConfig.h"
#include <stdint.h>
#include <stddef.h>

typedef struct AACDecoder AACDecoder;

/* How access units are handed to the converter, and how it learns the stream configuration. Production uses the
 * defaults; the others exist so the on-device self-test can measure which ones Apple's decoder accepts. */
typedef enum { AAC_FRAMING_RAW = 0, AAC_FRAMING_ADTS = 1 } AACFraming;
typedef enum { AAC_COOKIE_ESDS = 0, AAC_COOKIE_ASC = 1, AAC_COOKIE_NONE = 2 } AACCookie;

typedef struct {
    AACFraming framing;        /* default RAW: the bare access unit, as AudioConverter expects for kAudioFormatMPEG4AAC */
    AACCookie cookie;          /* default ESDS: the cookie format AudioFileStream itself produces */
    int prefer_software;       /* default 1: Apple's software AAC decoder first (AudioConverterNewSpecific) */
} AACDecoderOptions;

typedef struct {
    int32_t specific_status;   /* OSStatus of AudioConverterNewSpecific (software decoder); 0 if not tried */
    int32_t new_status;        /* OSStatus of AudioConverterNew, the fallback; 0 if not needed */
    int32_t cookie_status;     /* OSStatus of setting the magic cookie; 0 if no cookie */
    int used_software;         /* 1 if the software-specific converter was created */
    int cookie_set;            /* 1 if the converter accepted the cookie (refusal is logged, not fatal) */
} AACDecoderCreateStatus;

void aac_decoder_default_options(AACDecoderOptions *);

/* Create a decoder for a negotiated, supported AACConfig (AAC-LC, 44.1/48 kHz, mono/stereo), with the production
 * defaults. Returns NULL if cfg is unsupported or no converter could be created. */
AACDecoder *aac_decoder_create(const AACConfig *cfg);
/* The same with explicit options; st (may be NULL) receives every AudioToolbox status, success or failure. */
AACDecoder *aac_decoder_create_ex(const AACConfig *cfg, const AACDecoderOptions *opt, AACDecoderCreateStatus *st);
void aac_decoder_destroy(AACDecoder *);

/* Reset at a stream boundary (new generation, or a sequence gap), like sbc_decoder_reset. Drops partial fragments. */
void aac_decoder_reset(AACDecoder *);

/* Decode one packet. Returns 0 with *samples stereo frames in pcm and *rate set; 1 if nothing came out yet (decoder
 * priming, or a fragment waiting for the rest of its AudioMuxElement); or a negative error:
 *   -1  bad arguments or packet        -6  AudioConverter error (aac_decoder_last_status has the OSStatus)
 *   -11 not a valid AudioMuxElement    -14 in-band configuration changed to one this decoder doesn't support
 *   -15 rebuilding the converter for an in-band configuration change failed
 * Writes at most `capacity` int16 samples (a stereo frame counts as 2). */
int aac_decoder_decode(AACDecoder *, const AudioPacket *packet, int16_t *pcm,
                       size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate);

/* Diagnostics, for logs and the self-test. */
int32_t aac_decoder_last_status(const AACDecoder *);     /* OSStatus of the last AudioConverterFillComplexBuffer */
unsigned aac_decoder_config_changes(const AACDecoder *); /* in-band configuration changes followed */
unsigned aac_decoder_fragments_joined(const AACDecoder *);  /* AudioMuxElements reassembled from fragments */
unsigned aac_decoder_fragments_dropped(const AACDecoder *); /* partial elements abandoned (a fragment was lost) */
