/* CodecDecoder.c - see CodecDecoder.h. Pure dispatch; the real work is in SBCDecoder and AACDecoder.
 *
 * The fallback is deliberate: it only ever falls back to SBC for a session the SRC actually negotiated as SBC. If a
 * session negotiated AAC but we cannot decode it (AAC off, bad config, or converter failure), the dispatch goes to
 * a DISABLED state that emits no audio, rather than feeding AAC/LATM packets to the SBC decoder (which would decode
 * them as noise). Interpreting AAC as SBC is exactly what must not happen. */
#include "CodecDecoder.h"
#include "../shared/AACConfig.h"

/* A private third codec value for "negotiated something we can't decode": never decode, stay silent. */
#define CODEC_DISABLED 0xffffffffu

uint32_t codec_decoder_begin(CodecDecoder *d, const AudioRing *header, int aac_enabled,
                             void (*log)(const char *fmt, ...)) {
    sbc_decoder_reset(&d->sbc);
    d->aac = NULL;
    if (header->codec == A2DP_CODEC_SBC) {
        d->codec = A2DP_CODEC_SBC;
        if (log) log("V93_CODEC sbc\n");
        return d->codec;
    }
    if (header->codec == A2DP_CODEC_AAC) {
        AACConfig cfg;
        if (!aac_enabled) {
            if (log) log("V93_CODEC aac negotiated but SpeakaAAC off; no audio (not decoding AAC as SBC)\n");
        } else if (!aac_parse_config(header->codec_config, &cfg) || !aac_config_supported(&cfg)) {
            if (log) log("V93_CODEC aac config invalid/unsupported; no audio\n");
        } else if (!(d->aac = aac_decoder_create(&cfg))) {
            if (log) log("V93_CODEC aac decoder setup failed; no audio\n");
        } else {
            d->codec = A2DP_CODEC_AAC;
            if (log) log("V93_CODEC aac rate=%u channels=%u\n", cfg.sample_rate, cfg.channels);
            return d->codec;
        }
        d->codec = CODEC_DISABLED;      /* AAC session we can't decode: silent, never SBC-misdecoded */
        return d->codec;
    }
    /* An unknown codec value: do not guess. Stay silent. */
    if (log) log("V93_CODEC unknown codec=%u; no audio\n", header->codec);
    d->codec = CODEC_DISABLED;
    return d->codec;
}

void codec_decoder_reset(CodecDecoder *d) {
    if (d->codec == A2DP_CODEC_AAC && d->aac) aac_decoder_reset(d->aac);
    else if (d->codec == A2DP_CODEC_SBC) sbc_decoder_reset(&d->sbc);
    /* CODEC_DISABLED: nothing to reset. */
}

int codec_decoder_decode(CodecDecoder *d, const AudioPacket *p, int16_t *pcm,
                         size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate) {
    (void)p;
    if (d->codec == A2DP_CODEC_AAC && d->aac)
        return aac_decoder_decode(d->aac, p, pcm, capacity, frames, samples, rate);
    if (d->codec == A2DP_CODEC_SBC)
        return sbc_decoder_decode(&d->sbc, p, pcm, capacity, frames, samples, rate);
    /* DISABLED: consume the packet, emit nothing. -13 distinguishes it in the decode log. */
    (void)pcm; (void)capacity;
    *frames = *samples = *rate = 0;
    return -13;
}

void codec_decoder_end(CodecDecoder *d) {
    if (d->aac) { aac_decoder_destroy(d->aac); d->aac = NULL; }
    d->codec = A2DP_CODEC_SBC;
}
