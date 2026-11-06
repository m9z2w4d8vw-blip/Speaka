/* AACConfig.h - the A2DP AAC codec, as pure data and functions with no OS calls or decoding.
 *
 * Everything here is from the public standards, not from any Apple binary:
 *   - the AAC Codec Specific Information Element, A2DP spec section 4.5 (the 6-byte capability payload that follows
 *     the media-codec record header);
 *   - MPEG-4 AudioSpecificConfig / the ADTS header (ISO/IEC 14496-3) for the decoder to initialise from.
 * BlueZ (a2dp-codecs.h) and Android (libbluetooth's a2dp_aac) implement the same layout; this is a clean-room
 * version from the specification. It carries no on-device offsets and makes no claim about how bluetoothd lays out
 * its endpoint table - that is the native side, kept behind the hook interface in Tweak.h.
 *
 * Shared between the tweak (advertising and parsing the record) and the helper (initialising the decoder), so it is
 * plain C and used by the host tests as-is.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif

/* The media-codec record's codec_type byte (A2DP spec; the same values avdtp_codec_name() already knows). */
#define A2DP_CODEC_SBC  0x00u
#define A2DP_CODEC_AAC  0x02u

/* The AAC Codec Specific Information Element is 6 bytes (A2DP spec 4.5.2), big-endian bitfields:
 *   byte0  object type bits (MPEG-2/4 AAC LC, LTP, Scalable, HE ...)
 *   byte1  sampling frequency, high 8 of 12 bits
 *   byte2  sampling frequency low 4 bits | channels bits
 *   byte3  VBR (bit7) | bitrate high 7 bits
 *   byte4  bitrate mid 8 bits
 *   byte5  bitrate low 8 bits
 * Speaka is a Sink, so it advertises what it accepts and reads the SRC's chosen configuration from CONFIGURE. */
#define AAC_CSI_BYTES 6u

/* Object type bits in byte 0 (A2DP spec table 4.5). We support AAC-LC only, as most SRCs (Windows, macOS) use. */
#define AAC_OBJECT_TYPE_MPEG2_LC 0x80u
#define AAC_OBJECT_TYPE_MPEG4_LC 0x40u
#define AAC_OBJECT_TYPE_MPEG4_LTP 0x20u
#define AAC_OBJECT_TYPE_MPEG4_SCALABLE 0x10u

/* Sampling-frequency bits spread across byte1 (high 8) and byte2 (low 4). A2DP spec table 4.6. */
#define AAC_SAMPLE_RATE_8000   0x0800u
#define AAC_SAMPLE_RATE_11025  0x0400u
#define AAC_SAMPLE_RATE_12000  0x0200u
#define AAC_SAMPLE_RATE_16000  0x0100u
#define AAC_SAMPLE_RATE_22050  0x0080u
#define AAC_SAMPLE_RATE_24000  0x0040u
#define AAC_SAMPLE_RATE_32000  0x0020u
#define AAC_SAMPLE_RATE_44100  0x0010u
#define AAC_SAMPLE_RATE_48000  0x0008u
#define AAC_SAMPLE_RATE_64000  0x0004u
#define AAC_SAMPLE_RATE_88200  0x0002u
#define AAC_SAMPLE_RATE_96000  0x0001u

/* Channel bits in byte 2 (A2DP spec table 4.7). */
#define AAC_CHANNELS_1 0x08u
#define AAC_CHANNELS_2 0x04u

/* A parsed AAC configuration (what the SRC chose in its CONFIGURE, or what we advertise). */
typedef struct {
    uint8_t object_type;   /* the single object-type bit from byte 0 */
    uint32_t sample_rate;  /* in Hz, decoded from the frequency bits; 0 if none/invalid */
    uint8_t channels;      /* 1 or 2; 0 if invalid */
    uint8_t vbr;           /* 1 if the VBR bit is set */
    uint32_t bitrate;      /* the 23-bit bitrate field, bits/s; 0 means "unspecified" per spec */
} AACConfig;

/* Fill the 6-byte capability payload Speaka advertises as a Sink: AAC-LC (both MPEG-2 and MPEG-4 LC object types),
 * 44.1 and 48 kHz, mono and stereo, VBR allowed, at most 320 kbps. Returns AAC_CSI_BYTES. */
size_t aac_build_sink_capability(uint8_t out[AAC_CSI_BYTES]);

/* Parse a 6-byte AAC Codec Specific Information payload. Returns 1 and fills out on a valid single-choice
 * configuration (exactly one object type, one sample rate, one channel mode), 0 otherwise with out zeroed.
 * Reads nothing beyond csi[AAC_CSI_BYTES]. */
int aac_parse_config(const uint8_t csi[AAC_CSI_BYTES], AACConfig *out);

/* Decide whether the SRC's chosen configuration (parsed from CONFIGURE) is one the helper's decoder supports:
 * AAC-LC, 44.1 or 48 kHz, mono or stereo. Returns 1 if supported. */
int aac_config_supported(const AACConfig *cfg);

/* The MPEG-4 sampling-frequency index (ISO/IEC 14496-3 table) for a rate, or 0xf for "not in the table". The decoder
 * uses this to build an AudioSpecificConfig / ADTS header. */
unsigned aac_sampling_frequency_index(uint32_t sample_rate);

/* Build a 2-byte MPEG-4 AudioSpecificConfig for AAC-LC from a parsed, supported config: 5 bits audioObjectType=2,
 * 4 bits samplingFrequencyIndex, 4 bits channelConfiguration, 3 bits zero. Returns 2, or 0 if cfg is unsupported.
 * This is standard MPEG-4; a decoder (Apple's AudioConverter, or any other) initialises from it. */
size_t aac_build_asc(const AACConfig *cfg, uint8_t out[2]);

/* The reverse, for an in-band AudioSpecificConfig (LATM StreamMuxConfig): read AAC-LC object type, sampling index and
 * channels from a 2-byte ASC. Returns 1 and fills out (object_type is set to MPEG-4 LC) only for an ASC this decoder
 * supports: AAC-LC, 1024-sample frames, no core coder, no extension, 44.1/48 kHz, mono/stereo. 0 otherwise. */
int aac_config_from_asc(const uint8_t *asc, size_t len, AACConfig *out);

/* Build the MPEG-4 ESDS magic cookie (ISO/IEC 14496-1 ES_Descriptor > DecoderConfigDescriptor > DecoderSpecificInfo,
 * plus SLConfigDescriptor) around an AudioSpecificConfig, in the 4-byte expandable-length layout Apple's own AAC
 * cookies use. This is what AudioFileStream hands AudioConverter as kAudioConverterDecompressionMagicCookie.
 * Returns the cookie length (39 for a 2-byte ASC), or 0 if cap is too small or asc_len is 0 or over 64. */
size_t aac_build_esds(const uint8_t *asc, size_t asc_len, uint32_t avg_bitrate, uint8_t *out, size_t cap);

#ifdef __cplusplus
}
#endif
