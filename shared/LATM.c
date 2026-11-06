/* LATM.c - see LATM.h. A bit-reader over the AudioMuxElement, following ISO/IEC 14496-3 Table 1.28 (AudioMuxElement),
 * 1.21 (StreamMuxConfig), 1.23 (PayloadLengthInfo). Clean-room from the spec; BlueZ (a2dp_aac) and FFmpeg's LATM
 * code do the same traversal. No OS calls, no Apple internals. */
#include "LATM.h"
#include <string.h>

typedef struct { const uint8_t *data; size_t len; size_t bit; } BitReader;

static int br_read(BitReader *b, unsigned bits, uint32_t *out) {
    uint32_t v = 0;
    if (bits > 32) return 0;
    for (unsigned i = 0; i < bits; ++i) {
        size_t byte = b->bit >> 3;
        if (byte >= b->len) return 0;                 /* never read past the buffer */
        unsigned shift = 7u - (b->bit & 7u);
        v = (v << 1) | ((b->data[byte] >> shift) & 1u);
        b->bit++;
    }
    *out = v;
    return 1;
}

/* Collect `bits` into asc[] at bit offset *asc_bits (MSB first), growing the byte length. Keeps the AudioSpecificConfig
 * we parse inline, so the decoder gets the exact ASC the stream carried. */
static int asc_put(uint8_t *asc, unsigned cap_bytes, unsigned *asc_bits, uint32_t value, unsigned bits) {
    for (unsigned i = 0; i < bits; ++i) {
        unsigned bit = (value >> (bits - 1u - i)) & 1u;
        unsigned pos = *asc_bits;
        if ((pos >> 3) >= cap_bytes) return 0;
        if (bit) asc[pos >> 3] |= (uint8_t)(1u << (7u - (pos & 7u)));
        (*asc_bits)++;
    }
    return 1;
}

/* StreamMuxConfig (Table 1.21), the A2DP / AAC-LC subset: audioMuxVersion=0, single program, single layer. The
 * AudioSpecificConfig is parsed INLINE (not length-prefixed) per ISO/IEC 14496-3: for AAC-LC that is
 * audioObjectType(5) + samplingFrequencyIndex(4) + channelConfiguration(4) + GASpecificConfig(3) = 16 bits, which
 * is exactly the 2-byte ASC the A2DP capability also carries. We reconstruct those 2 bytes for the decoder. */
static int parse_stream_mux_config(BitReader *b, uint8_t *asc, unsigned *asc_len) {
    uint32_t audio_mux_version, all_same, num_sub, num_prog, num_layer;
    if (!br_read(b, 1, &audio_mux_version)) return 0;
    if (audio_mux_version != 0) return 0;             /* version A (1) is out of scope and A2DP does not use it */
    if (!br_read(b, 1, &all_same)) return 0;          /* allStreamsSameTimeFraming */
    if (!br_read(b, 6, &num_sub)) return 0;           /* numSubFrames: 0 => one sub-frame */
    if (num_sub != 0) return 0;
    if (!br_read(b, 4, &num_prog)) return 0;          /* numProgram: 0 => one program */
    if (num_prog != 0) return 0;
    if (!br_read(b, 3, &num_layer)) return 0;         /* numLayer: 0 => one layer */
    if (num_layer != 0) return 0;

    /* AudioSpecificConfig, inline. We support AAC-LC (audioObjectType 2) only. */
    unsigned asc_bits = 0;
    uint8_t asc_buf[8] = {0};
    uint32_t object_type, freq_index, channels;
    if (!br_read(b, 5, &object_type)) return 0;
    if (object_type != 2u) return 0;                  /* AAC-LC only; other object types are out of scope */
    if (!br_read(b, 4, &freq_index)) return 0;
    if (freq_index == 0xfu) return 0;                 /* explicit frequency (24 bits) is out of scope */
    if (!br_read(b, 4, &channels)) return 0;
    if (channels != 1u && channels != 2u) return 0;
    /* GASpecificConfig for AAC-LC: frameLengthFlag(1), dependsOnCoreCoder(1), extensionFlag(1). */
    uint32_t frame_length_flag, depends_core, extension_flag;
    if (!br_read(b, 1, &frame_length_flag)) return 0;
    if (!br_read(b, 1, &depends_core)) return 0;
    if (depends_core) { uint32_t core_delay; if (!br_read(b, 14, &core_delay)) return 0; }
    if (!br_read(b, 1, &extension_flag)) return 0;
    if (extension_flag) return 0;                     /* AAC-LC has none; reject rather than guess */
    /* Rebuild the 2-byte ASC from the fields (frameLengthFlag etc. are the GAS 3 bits). */
    if (!asc_put(asc_buf, sizeof asc_buf, &asc_bits, object_type, 5) ||
        !asc_put(asc_buf, sizeof asc_buf, &asc_bits, freq_index, 4) ||
        !asc_put(asc_buf, sizeof asc_buf, &asc_bits, channels, 4) ||
        !asc_put(asc_buf, sizeof asc_buf, &asc_bits, frame_length_flag, 1) ||
        !asc_put(asc_buf, sizeof asc_buf, &asc_bits, depends_core, 1) ||
        !asc_put(asc_buf, sizeof asc_buf, &asc_bits, extension_flag, 1)) return 0;
    unsigned bytes = (asc_bits + 7u) / 8u;
    for (unsigned i = 0; i < bytes; ++i) asc[i] = asc_buf[i];
    *asc_len = bytes;

    /* frameLengthType must be 0 for AAC-LC (variable length, signalled in PayloadLengthInfo). */
    uint32_t frame_len_type;
    if (!br_read(b, 3, &frame_len_type)) return 0;
    if (frame_len_type != 0) return 0;
    uint32_t latm_buffer_fullness;
    if (!br_read(b, 8, &latm_buffer_fullness)) return 0;
    /* otherDataPresent / crcCheckPresent: read and require absent, matching the A2DP byte-aligned subset. */
    uint32_t other_data, crc_present;
    if (!br_read(b, 1, &other_data)) return 0;
    if (other_data) return 0;
    if (!br_read(b, 1, &crc_present)) return 0;
    if (crc_present) { uint32_t crc; if (!br_read(b, 8, &crc)) return 0; }
    return 1;
}

int latm_parse_audiomuxelement(const uint8_t *in, size_t len, uint8_t *au_out, size_t au_cap, LATMFrame *out) {
    LATMFrame z;
    memset(&z, 0, sizeof z);
    if (out) *out = z;
    if (!in || !out || !au_out || len == 0) return 0;

    BitReader b = { in, len, 0 };
    uint32_t use_same;
    if (!br_read(&b, 1, &use_same)) return 0;         /* AudioMuxElement(muxConfigPresent=1): useSameStreamMux */
    out->used_same_mux = use_same ? 1u : 0u;
    if (!use_same) {
        if (!parse_stream_mux_config(&b, out->asc, &out->asc_len)) { *out = z; return 0; }
    }

    /* PayloadLengthInfo (Table 1.23) for frameLengthType==0: MuxSlotLengthBytes as a run of 0xFF chunks + remainder. */
    size_t au_len = 0;
    for (;;) {
        uint32_t chunk;
        if (!br_read(&b, 8, &chunk)) { *out = z; return 0; }
        au_len += chunk;
        if (chunk != 255u) break;
        if (au_len > len) { *out = z; return 0; }
    }

    /* PayloadMux is au_len bytes, read through the bit reader because it is generally NOT byte-aligned within the
     * element (StreamMuxConfig and PayloadLengthInfo are bit fields). Copy the shifted bytes out. */
    if (au_len == 0 || au_len > au_cap) { *out = z; return 0; }
    if (b.bit + au_len * 8u > len * 8u) { *out = z; return 0; }   /* must fit in what remains */
    for (size_t i = 0; i < au_len; ++i) {
        uint32_t byte;
        if (!br_read(&b, 8, &byte)) { *out = z; return 0; }
        au_out[i] = (uint8_t)byte;
    }
    out->au_len = au_len;
    return 1;
}
