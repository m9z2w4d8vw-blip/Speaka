/* LATM.h - MPEG-4 LATM AudioMuxElement parsing (ISO/IEC 14496-3, 1.7.3), for A2DP AAC (A2DP spec 4.5.4, RFC 3016).
 *
 * Implemented as the standard defines it, for single-program, single-layer AAC (what A2DP carries):
 *   - AudioMuxElement(muxConfigPresent) with muxConfigPresent 1 (in-band StreamMuxConfig, as RFC 3016 and LOAS use)
 *     or 0 (configuration out of band);
 *   - a StreamMuxConfig persisted across elements, so useSameStreamMux=1 elements parse with the last configuration;
 *   - numSubFrames: several access units in one element (an encoder packing frames into one packet);
 *   - audioMuxVersion 0 and 1 (LatmGetValue, taraBufferFullness, ascLen with fill bits);
 *   - otherData and the CRC byte;
 *   - byte_alignment() at the end, so a caller can check an element accounts for its bytes exactly.
 * Rejected with a specific reason rather than guessed at: audioMuxVersionA 1, several programs or layers,
 * allStreamsSameTimeFraming 0, frameLengthType other than 0, and AudioSpecificConfigs other than AAC-LC.
 * Pure C, no OS calls; access units are copied out (PayloadMux is generally not byte-aligned). */
#pragma once
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif

/* The last StreamMuxConfig seen: what a useSameStreamMux=1 element (or an out-of-band stream) is parsed with. */
typedef struct {
    unsigned valid;
    unsigned audio_mux_version;    /* 0 or 1 */
    unsigned num_sub_frames;       /* access units per element, minus one */
    unsigned other_data_present;
    uint32_t other_data_bits;
    unsigned crc_present;
    uint8_t asc[2];                /* the AAC-LC AudioSpecificConfig (5+4+4+3 bits), rebuilt byte-aligned */
    unsigned asc_len;              /* 2 when valid */
} LATMConfig;

/* Failure reasons (negative), so a log says exactly what was wrong and where. */
enum {
    LATM_E_TRUNCATED = -1,   /* ran out of input, or a declared length runs past it (what a fragment looks like) */
    LATM_E_VERSION_A = -2,   /* audioMuxVersionA 1 (reserved) */
    LATM_E_LAYOUT = -3,      /* more than one program or layer, or allStreamsSameTimeFraming 0 */
    LATM_E_ASC = -4,         /* an AudioSpecificConfig other than AAC-LC 44.1/48 kHz mono/stereo */
    LATM_E_FRAME_LENGTH = -5,/* frameLengthType other than 0 */
    LATM_E_NO_CONFIG = -6,   /* useSameStreamMux=1 (or out of band) with no configuration known */
    LATM_E_CAPACITY = -7,    /* the access units don't fit the caller's buffer or count */
    LATM_E_AU_LENGTH = -8,   /* an access unit of length 0 */
    LATM_E_ARGS = -9
};

/* Parse one AudioMuxElement starting at bit *bitpos of in[0..len). cfg is read (useSameStreamMux=1, or out of band)
 * and replaced when the element carries a StreamMuxConfig. Access units are appended to out at *out_used, their
 * lengths to au_len[*n_aus...]. On success returns the number of access units in this element (>= 1) and leaves
 * *bitpos after the element's byte_alignment(). On failure returns an LATM_E_* code with *bitpos at the failure;
 * out/au_len/n_aus may hold partial results, which the caller discards. */
/* *carried_config (may be NULL) is set to 1 if the element carried a StreamMuxConfig that parsed, even when the
 * element then ran out of input: strong evidence that a cut-short packet is a fragment of a real LATM element. */
int latm_parse_element(const uint8_t *in, size_t len, size_t *bitpos, int mux_config_present, LATMConfig *cfg,
                       uint8_t *out, size_t out_cap, size_t *out_used, size_t *au_len, unsigned max_aus,
                       unsigned *n_aus, unsigned *carried_config);

/* ---- the original single-element interface, kept for the self-test and existing callers ---- */
typedef struct {
    uint8_t asc[8];
    unsigned asc_len;          /* 2 if this element carried a StreamMuxConfig, else 0 */
    size_t au_len;             /* bytes of the first access unit, copied to au_out */
    unsigned used_same_mux;    /* 1 if useSameStreamMux */
    unsigned truncated;        /* on failure: 1 if the input ended early (a fragment), 0 if malformed */
} LATMFrame;

/* One AudioMuxElement(1) with no prior configuration (a useSameStreamMux=1 element is read with the defaults: one
 * access unit, frameLengthType 0). Copies the first access unit to au_out. Returns 1 or 0. */
int latm_parse_audiomuxelement(const uint8_t *in, size_t len, uint8_t *au_out, size_t au_cap, LATMFrame *out);

#ifdef __cplusplus
}
#endif
