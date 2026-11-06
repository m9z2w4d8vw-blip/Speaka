/* CodecDecoder.c - see CodecDecoder.h. Pure dispatch; the real work is in SBCDecoder and AACDecoder. */
#include "CodecDecoder.h"
#include "../shared/AACConfig.h"

uint32_t codec_decoder_begin(CodecDecoder *d, const AudioRing *header, int aac_enabled,
                             void (*log)(const char *fmt, ...)) {
    sbc_decoder_reset(&d->sbc);
    d->aac = NULL;
    d->codec = A2DP_CODEC_SBC;
    if (header->codec == A2DP_CODEC_AAC) {
        if (!aac_enabled) {
            if (log) log("V93_CODEC aac negotiated but SpeakaAAC off; using sbc\n");
        } else {
            AACConfig cfg;
            if (!aac_parse_config(header->codec_config, &cfg) || !aac_config_supported(&cfg)) {
                if (log) log("V93_CODEC aac config invalid/unsupported; using sbc\n");
            } else if (!(d->aac = aac_decoder_create(&cfg))) {
                if (log) log("V93_CODEC aac decoder setup failed; using sbc\n");
            } else {
                d->codec = A2DP_CODEC_AAC;
                if (log) log("V93_CODEC aac rate=%u channels=%u\n", cfg.sample_rate, cfg.channels);
            }
        }
    }
    if (d->codec == A2DP_CODEC_SBC && log) log("V93_CODEC sbc\n");
    return d->codec;
}

void codec_decoder_reset(CodecDecoder *d) {
    if (d->codec == A2DP_CODEC_AAC && d->aac) aac_decoder_reset(d->aac);
    else sbc_decoder_reset(&d->sbc);
}

int codec_decoder_decode(CodecDecoder *d, const AudioPacket *p, int16_t *pcm,
                         size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate) {
    if (d->codec == A2DP_CODEC_AAC && d->aac)
        return aac_decoder_decode(d->aac, p, pcm, capacity, frames, samples, rate);
    return sbc_decoder_decode(&d->sbc, p, pcm, capacity, frames, samples, rate);
}

void codec_decoder_end(CodecDecoder *d) {
    if (d->aac) { aac_decoder_destroy(d->aac); d->aac = NULL; }
    d->codec = A2DP_CODEC_SBC;
}
