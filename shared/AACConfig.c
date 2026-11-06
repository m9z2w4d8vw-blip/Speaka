/* AACConfig.c - see AACConfig.h. Pure C from the public A2DP AAC and MPEG-4 specifications; no OS calls, no Apple
 * internals, no on-device offsets. */
#include "AACConfig.h"

size_t aac_build_sink_capability(uint8_t out[AAC_CSI_BYTES]) {
    uint16_t freq = AAC_SAMPLE_RATE_44100 | AAC_SAMPLE_RATE_48000;
    out[0] = AAC_OBJECT_TYPE_MPEG2_LC | AAC_OBJECT_TYPE_MPEG4_LC;
    out[1] = (uint8_t)(freq >> 4);                                   /* high 8 of the 12 frequency bits */
    out[2] = (uint8_t)(((freq & 0x0fu) << 4) | AAC_CHANNELS_1 | AAC_CHANNELS_2);
    out[3] = 0x80u;                                                  /* VBR allowed, bitrate bits 22..16 = 0 */
    out[4] = 0x00u;                                                  /* bitrate bits 15..8 */
    out[5] = 0x00u;                                                  /* bitrate bits 7..0; 0 = unspecified */
    return AAC_CSI_BYTES;
}

/* Exactly one bit set? Used to reject a CONFIGURE that (wrongly) leaves a capability range instead of a choice. */
static int one_bit(unsigned v) { return v && (v & (v - 1u)) == 0u; }

static uint32_t rate_from_bits(uint16_t bits) {
    switch (bits) {
        case AAC_SAMPLE_RATE_8000:  return 8000;
        case AAC_SAMPLE_RATE_11025: return 11025;
        case AAC_SAMPLE_RATE_12000: return 12000;
        case AAC_SAMPLE_RATE_16000: return 16000;
        case AAC_SAMPLE_RATE_22050: return 22050;
        case AAC_SAMPLE_RATE_24000: return 24000;
        case AAC_SAMPLE_RATE_32000: return 32000;
        case AAC_SAMPLE_RATE_44100: return 44100;
        case AAC_SAMPLE_RATE_48000: return 48000;
        case AAC_SAMPLE_RATE_64000: return 64000;
        case AAC_SAMPLE_RATE_88200: return 88200;
        case AAC_SAMPLE_RATE_96000: return 96000;
        default: return 0;
    }
}

int aac_parse_config(const uint8_t csi[AAC_CSI_BYTES], AACConfig *out) {
    AACConfig c = {0,0,0,0,0};
    if (!csi || !out) return 0;
    uint8_t object = csi[0] & 0xf0u;                                 /* top 4 bits are the object types */
    uint16_t freq = (uint16_t)(((uint16_t)csi[1] << 4) | (csi[2] >> 4));
    uint8_t channels = csi[2] & 0x0fu;
    /* A chosen configuration names exactly one of each. A capability (a range) is not a valid CONFIGURE. */
    if (!one_bit(object) || !one_bit(freq) || !one_bit(channels)) { *out = c; return 0; }
    c.object_type = object;
    c.sample_rate = rate_from_bits(freq);
    c.channels = channels == AAC_CHANNELS_1 ? 1u : 2u;
    c.vbr = (csi[3] & 0x80u) ? 1u : 0u;
    c.bitrate = ((uint32_t)(csi[3] & 0x7fu) << 16) | ((uint32_t)csi[4] << 8) | csi[5];
    if (!c.sample_rate) { *out = c; return 0; }
    *out = c;
    return 1;
}

int aac_config_supported(const AACConfig *cfg) {
    if (!cfg) return 0;
    int lc = cfg->object_type == AAC_OBJECT_TYPE_MPEG2_LC || cfg->object_type == AAC_OBJECT_TYPE_MPEG4_LC;
    int rate = cfg->sample_rate == 44100u || cfg->sample_rate == 48000u;
    int ch = cfg->channels == 1u || cfg->channels == 2u;
    return lc && rate && ch;
}

unsigned aac_sampling_frequency_index(uint32_t sample_rate) {
    static const uint32_t table[] = {96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                                     16000, 12000, 11025, 8000, 7350};
    for (unsigned i = 0; i < sizeof table / sizeof table[0]; ++i)
        if (table[i] == sample_rate) return i;
    return 0xfu;
}

size_t aac_build_asc(const AACConfig *cfg, uint8_t out[2]) {
    if (!aac_config_supported(cfg)) return 0;
    unsigned freq_index = aac_sampling_frequency_index(cfg->sample_rate);
    if (freq_index == 0xfu) return 0;
    unsigned object = 2u;                                            /* AAC-LC */
    /* 5 bits object | 4 bits freq index | 4 bits channel config | 3 bits zero = 16 bits. */
    uint16_t asc = (uint16_t)((object << 11) | (freq_index << 7) | ((unsigned)cfg->channels << 3));
    out[0] = (uint8_t)(asc >> 8);
    out[1] = (uint8_t)(asc & 0xffu);
    return 2u;
}
