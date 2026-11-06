/* LATM.c - see LATM.h. Follows the syntax tables of ISO/IEC 14496-3 1.7.3: AudioMuxElement, StreamMuxConfig,
 * LatmGetValue, PayloadLengthInfo, PayloadMux, and the AAC-LC AudioSpecificConfig / GASpecificConfig. */
#include "LATM.h"
#include <string.h>

typedef struct { const uint8_t *data; size_t len; size_t bit; int eof; } BitReader;

static int br_read(BitReader *b, unsigned bits, uint32_t *out) {
    uint32_t v = 0;
    if (bits > 32u) return 0;
    for (unsigned i = 0; i < bits; ++i) {
        size_t byte = b->bit >> 3;
        if (byte >= b->len) { b->eof = 1; return 0; }
        v = (v << 1) | ((b->data[byte] >> (7u - (b->bit & 7u))) & 1u);
        b->bit++;
    }
    *out = v;
    return 1;
}

/* LatmGetValue(): bytesForValue (2 bits), then that many plus one bytes, big-endian. */
static int latm_get_value(BitReader *b, uint32_t *out) {
    uint32_t n, value = 0, tmp;
    if (!br_read(b, 2, &n)) return 0;
    for (uint32_t i = 0; i <= n; ++i) {
        if (!br_read(b, 8, &tmp)) return 0;
        value = (value << 8) | tmp;
    }
    *out = value;
    return 1;
}

/* AudioSpecificConfig, AAC-LC only: audioObjectType 2, a table sampling index, channelConfiguration 1 or 2, and
 * GASpecificConfig (frameLengthFlag 0, dependsOnCoreCoder, extensionFlag). Returns 1, or 0 with *why. */
static int parse_asc(BitReader *b, LATMConfig *cfg, int *why) {
    uint32_t aot, sfi, ch, flf, core, ext;
    if (!br_read(b, 5, &aot) || !br_read(b, 4, &sfi)) { *why = LATM_E_TRUNCATED; return 0; }
    if (aot != 2u || sfi == 0xfu) { *why = LATM_E_ASC; return 0; }
    if (!br_read(b, 4, &ch) || !br_read(b, 1, &flf) || !br_read(b, 1, &core)) { *why = LATM_E_TRUNCATED; return 0; }
    if ((ch != 1u && ch != 2u) || flf) { *why = LATM_E_ASC; return 0; }
    if (core) { uint32_t delay; if (!br_read(b, 14, &delay)) { *why = LATM_E_TRUNCATED; return 0; } }
    if (!br_read(b, 1, &ext)) { *why = LATM_E_TRUNCATED; return 0; }
    if (ext) { *why = LATM_E_ASC; return 0; }          /* AAC-LC carries no extension */
    unsigned v = (aot << 11) | (sfi << 7) | (ch << 3) | (flf << 2) | (core << 1) | ext;
    cfg->asc[0] = (uint8_t)(v >> 8);
    cfg->asc[1] = (uint8_t)(v & 0xffu);
    cfg->asc_len = 2;
    return 1;
}

/* StreamMuxConfig() for one program and one layer. Fills a fresh configuration; returns 0 on success or LATM_E_*. */
static int parse_stream_mux_config(BitReader *b, LATMConfig *out) {
    LATMConfig c;
    memset(&c, 0, sizeof c);
    uint32_t v, version_a = 0;
    if (!br_read(b, 1, &c.audio_mux_version)) return LATM_E_TRUNCATED;
    if (c.audio_mux_version == 1u && !br_read(b, 1, &version_a)) return LATM_E_TRUNCATED;
    if (version_a) return LATM_E_VERSION_A;
    if (c.audio_mux_version == 1u && !latm_get_value(b, &v)) return LATM_E_TRUNCATED;   /* taraBufferFullness */
    uint32_t same_framing, subframes, programs, layers;
    if (!br_read(b, 1, &same_framing) || !br_read(b, 6, &subframes) || !br_read(b, 4, &programs) ||
        !br_read(b, 3, &layers)) return LATM_E_TRUNCATED;
    if (!same_framing || programs || layers) return LATM_E_LAYOUT;
    c.num_sub_frames = subframes;
    /* Program 0 layer 0 always carries its own config (useSameConfig is implicit 0). */
    if (c.audio_mux_version == 0u) {
        int why = 0;
        if (!parse_asc(b, &c, &why)) return why;
    } else {
        uint32_t asc_bits;
        if (!latm_get_value(b, &asc_bits)) return LATM_E_TRUNCATED;
        size_t start = b->bit;
        int why = 0;
        if (!parse_asc(b, &c, &why)) return why;
        size_t used = b->bit - start;
        if (used > asc_bits) return LATM_E_ASC;
        for (size_t i = used; i < asc_bits; ++i) if (!br_read(b, 1, &v)) return LATM_E_TRUNCATED;   /* fillBits */
    }
    uint32_t frame_length_type;
    if (!br_read(b, 3, &frame_length_type)) return LATM_E_TRUNCATED;
    if (frame_length_type != 0u) return LATM_E_FRAME_LENGTH;
    if (!br_read(b, 8, &v)) return LATM_E_TRUNCATED;                                 /* latmBufferFullness */
    if (!br_read(b, 1, &c.other_data_present)) return LATM_E_TRUNCATED;
    if (c.other_data_present) {
        if (c.audio_mux_version == 1u) {
            if (!latm_get_value(b, &c.other_data_bits)) return LATM_E_TRUNCATED;
        } else {
            uint32_t esc, tmp;
            c.other_data_bits = 0;
            do {
                if (!br_read(b, 1, &esc) || !br_read(b, 8, &tmp)) return LATM_E_TRUNCATED;
                if (c.other_data_bits > (UINT32_MAX >> 8)) return LATM_E_LAYOUT;
                c.other_data_bits = (c.other_data_bits << 8) + tmp;
            } while (esc);
        }
    }
    if (!br_read(b, 1, &c.crc_present)) return LATM_E_TRUNCATED;
    if (c.crc_present && !br_read(b, 8, &v)) return LATM_E_TRUNCATED;                 /* crcCheckSum */
    c.valid = 1;
    *out = c;
    return 0;
}

int latm_parse_element(const uint8_t *in, size_t len, size_t *bitpos, int mux_config_present, LATMConfig *cfg,
                       uint8_t *out, size_t out_cap, size_t *out_used, size_t *au_len, unsigned max_aus,
                       unsigned *n_aus, unsigned *carried_config) {
    if (carried_config) *carried_config = 0;
    if (!in || !bitpos || !cfg || !out || !out_used || !au_len || !n_aus) return LATM_E_ARGS;
    BitReader b = { in, len, *bitpos, 0 };
    if (mux_config_present) {
        uint32_t use_same;
        if (!br_read(&b, 1, &use_same)) { *bitpos = b.bit; return LATM_E_TRUNCATED; }
        if (!use_same) {
            LATMConfig fresh;
            int rc = parse_stream_mux_config(&b, &fresh);
            if (rc) { *bitpos = b.bit; return rc; }
            *cfg = fresh;
            if (carried_config) *carried_config = 1;
        }
    }
    if (!cfg->valid) { *bitpos = b.bit; return LATM_E_NO_CONFIG; }
    unsigned found = 0;
    for (unsigned sub = 0; sub <= cfg->num_sub_frames; ++sub) {
        /* PayloadLengthInfo, frameLengthType 0: MuxSlotLengthBytes as 255-continued bytes. */
        size_t slot = 0;
        uint32_t tmp;
        do {
            if (!br_read(&b, 8, &tmp)) { *bitpos = b.bit; return LATM_E_TRUNCATED; }
            slot += tmp;
        } while (tmp == 255u);
        if (!slot) { *bitpos = b.bit; return LATM_E_AU_LENGTH; }
        if (b.bit + slot * 8u > len * 8u) { *bitpos = b.bit; return LATM_E_TRUNCATED; }
        if (*n_aus >= max_aus || *out_used + slot > out_cap) { *bitpos = b.bit; return LATM_E_CAPACITY; }
        /* PayloadMux: slot bytes, copied out (generally not byte-aligned here). */
        uint8_t *dst = out + *out_used;
        if ((b.bit & 7u) == 0u) {
            memcpy(dst, in + (b.bit >> 3), slot);
            b.bit += slot * 8u;
        } else {
            for (size_t i = 0; i < slot; ++i) { br_read(&b, 8, &tmp); dst[i] = (uint8_t)tmp; }
        }
        au_len[*n_aus] = slot;
        (*n_aus)++;
        *out_used += slot;
        found++;
    }
    if (cfg->other_data_present) {
        if (b.bit + cfg->other_data_bits > len * 8u) { *bitpos = b.bit; return LATM_E_TRUNCATED; }
        b.bit += cfg->other_data_bits;
    }
    b.bit = (b.bit + 7u) & ~(size_t)7u;                                               /* byte_alignment() */
    if (b.bit > len * 8u) { *bitpos = b.bit; return LATM_E_TRUNCATED; }
    *bitpos = b.bit;
    return (int)found;
}

int latm_parse_audiomuxelement(const uint8_t *in, size_t len, uint8_t *au_out, size_t au_cap, LATMFrame *out) {
    LATMFrame z;
    memset(&z, 0, sizeof z);
    if (out) *out = z;
    if (!in || !out || !au_out || len == 0) return 0;
    /* Without a configuration, a useSameStreamMux=1 element is read with the defaults: one access unit, no other data. */
    LATMConfig cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.valid = 1;
    size_t bit = 0, used = 0, lens[64];
    unsigned n = 0;
    int rc = latm_parse_element(in, len, &bit, 1, &cfg, au_out, au_cap, &used, lens, 64, &n, NULL);
    if (rc < 1) {
        out->truncated = rc == LATM_E_TRUNCATED ? 1u : 0u;
        return 0;
    }
    out->used_same_mux = (in[0] >> 7) & 1u;
    if (!out->used_same_mux) { out->asc[0] = cfg.asc[0]; out->asc[1] = cfg.asc[1]; out->asc_len = 2; }
    out->au_len = lens[0];
    return 1;
}
