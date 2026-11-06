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
    d->log = log;
    d->aac_since_reset = 0;
    d->aac_logged = 0;
    d->rate_hint = 48000u;
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
        } else {
            AACDecoderCreateStatus cs;
            if (!(d->aac = aac_decoder_create_ex(&cfg, NULL, &cs))) {
                if (log) log("V93_CODEC aac decoder setup failed specific_status=%d new_status=%d; no audio\n",
                             cs.specific_status, cs.new_status);
            } else {
                d->codec = A2DP_CODEC_AAC;
            d->rate_hint = cfg.sample_rate;
                if (log) log("V93_CODEC aac rate=%u channels=%u software=%d cookie_status=%d\n", cfg.sample_rate,
                             cfg.channels, cs.used_software, cs.cookie_status);
                return d->codec;
            }
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
    d->aac_since_reset = 0;                 /* describe the first packets of the next stream too */
    if (d->codec == A2DP_CODEC_AAC && d->aac) aac_decoder_reset(d->aac);
    else if (d->codec == A2DP_CODEC_SBC) sbc_decoder_reset(&d->sbc);
    /* CODEC_DISABLED: nothing to reset. */
}

int codec_decoder_decode(CodecDecoder *d, const AudioPacket *p, int16_t *pcm,
                         size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate) {
    (void)p;
    if (d->codec == A2DP_CODEC_AAC && d->aac) {
        int r = aac_decoder_decode(d->aac, p, pcm, capacity, frames, samples, rate);
        d->aac_since_reset++;
        /* Evidence for the log, from the helper's own thread (never bluetoothd's): how the packet was framed, what
         * came out, and its first 64 bytes. The first packets of each stream, then failures, within a total cap. */
        if (d->log && d->aac_logged < CODEC_AAC_LOG_TOTAL && (d->aac_since_reset <= CODEC_AAC_LOG_FIRST || r < 0)) {
            AACPacketResult res;
            aac_decoder_last_packet(d->aac, &res);
            char head[64 * 3 + 1];
            size_t shown = p->length < 64u ? p->length : 64u, at = 0;
            static const char hex[] = "0123456789abcdef";
            for (size_t i = 0; i < shown; ++i) {
                head[at++] = hex[p->data[i] >> 4];
                head[at++] = hex[p->data[i] & 15u];
                if (i + 1 < shown) head[at++] = ' ';
            }
            head[at] = 0;
            d->aac_logged++;
            d->log("V94_AAC_PKT n=%u len=%u seq=%u ts=%u result=%d frames=%u aus=%u transport=%s hdr=%u cfg=%u "
                   "latm_err=%d@%u status=%d head=%s\n", d->aac_since_reset, (unsigned)p->length,
                   (unsigned)p->sequence, (unsigned)p->timestamp, r, *samples, res.aus, aac_transport_name(res.transport),
                   res.header, res.asc_seen, res.latm_error, (unsigned)res.latm_error_bit,
                   (int)aac_decoder_last_status(d->aac), head);
        }
        return r;
    }
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

unsigned codec_decoder_rate_hint(const CodecDecoder *d) { return d && d->rate_hint ? d->rate_hint : 48000u; }
