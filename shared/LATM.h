/* LATM.h - depacketize an A2DP AAC payload (an MPEG-4 LATM AudioMuxElement) into the raw AAC access unit.
 *
 * A2DP carries AAC as a byte-aligned AudioMuxElement with muxConfigPresent=1 (A2DP spec 4.5.3; Bluetooth Accessory
 * Design Guidelines; RFC 3016). My first decoder wrongly treated the payload as a bare access unit and ADTS-wrapped
 * it, which decodes to garbage. This parses the AudioMuxElement the way BlueZ and Android do, and hands back the
 * PayloadMux (the access unit) so a raw-AAC decoder can consume it.
 *
 * Scope, matching what A2DP actually sends and what AAC-LC needs (kept deliberately narrow and fully testable):
 *   - AudioMuxElement(muxConfigPresent=1), audioMuxVersion=0, single program, single layer;
 *   - StreamMuxConfig when useSameStreamMux=0, from which the AudioSpecificConfig is read;
 *   - PayloadLengthInfo for frameLengthType=0 (the 8-bit-chunked length AAC-LC uses);
 *   - one access unit per AudioMuxElement (A2DP does not pack multiple).
 * Anything outside this (version A, chained sub-frames, other frame-length types) is rejected rather than guessed.
 *
 * Pure C, no OS calls, no Apple internals, no on-device offsets. Shared so the host tests exercise it directly.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    /* The AudioSpecificConfig carried in StreamMuxConfig, if this element had one (useSameStreamMux=0). AAC-LC ASC
     * is 2 bytes. asc_len is 0 when the element reused a previously signalled config (useSameStreamMux=1). */
    uint8_t asc[8];
    unsigned asc_len;
    size_t au_len;            /* bytes of AAC access unit copied into the caller's au_out buffer */
    unsigned used_same_mux;   /* 1 if this element reused the prior StreamMuxConfig */
    /* On failure: 1 if the element was well-formed so far but ended early (the input ran out, or the declared
     * access-unit length exceeds what arrived). That is what a fragment of a larger AudioMuxElement looks like, so
     * the caller can append the next fragment and parse again. 0 for anything malformed or out of scope. */
    unsigned truncated;
} LATMFrame;

/* Parse one AudioMuxElement from in[0..len), copying the extracted AAC access unit (PayloadMux) into au_out (up to
 * au_cap bytes). The AU is copied, not referenced, because LATM PayloadMux is not byte-aligned within the element,
 * so the bytes must be shifted. Returns 1 and fills out->au_len (and out->asc when present) on success; 0 on any
 * malformed or out-of-scope element, with out zeroed. Reads nothing beyond in[len]; writes nothing beyond au_cap. */
int latm_parse_audiomuxelement(const uint8_t *in, size_t len, uint8_t *au_out, size_t au_cap, LATMFrame *out);

#ifdef __cplusplus
}
#endif
