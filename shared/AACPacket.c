/* AACPacket.c - see AACPacket.h. Each transport is parsed per its standard and must account for the whole packet. */
#include "AACPacket.h"
#include <string.h>

const char *aac_transport_name(AACTransport t) {
    switch (t) {
        case AAC_TRANSPORT_LATM: return "latm";
        case AAC_TRANSPORT_LOAS: return "loas";
        case AAC_TRANSPORT_ADTS: return "adts";
        case AAC_TRANSPORT_RFC3640: return "rfc3640";
        case AAC_TRANSPORT_LATM_OOB: return "latm-oob";
        case AAC_TRANSPORT_RAW: return "raw";
        default: return "none";
    }
}

void aac_packet_init(AACPacketState *st, const uint8_t asc[2], unsigned channels) {
    memset(st, 0, sizeof *st);
    st->oob.valid = 1;                     /* out of band: one access unit per element, no other data, no CRC */
    if (asc) { st->oob.asc[0] = asc[0]; st->oob.asc[1] = asc[1]; st->oob.asc_len = 2; }
    st->channels = channels;
}

/* Results go to a scratch area first and are kept only if the transport accounts for the whole packet. */
typedef struct { uint8_t *out; size_t cap, used; size_t *len; unsigned max, n; } Sink;

static int put_au(Sink *k, const uint8_t *p, size_t n) {
    if (!n || k->n >= k->max || k->used + n > k->cap) return 0;
    memcpy(k->out + k->used, p, n);
    k->len[k->n++] = n;
    k->used += n;
    return 1;
}

/* LATM elements back to back, each ending on its byte alignment, together exactly covering the packet. Returns 1,
 * 0 if the first element was cut short (*strong if it had parsed a StreamMuxConfig first), or -1. */
static int try_latm(const uint8_t *in, size_t len, int mux_config_present, LATMConfig *cfg, Sink *k,
                    unsigned *asc_seen, int *err, size_t *err_bit, unsigned *strong) {
    size_t bit = 0;
    while ((bit >> 3) < len) {
        size_t start = bit;
        unsigned carried = 0;
        int rc = latm_parse_element(in, len, &bit, mux_config_present, cfg, k->out, k->cap, &k->used, k->len,
                                    k->max, &k->n, &carried);
        if (carried) *asc_seen = 1;
        if (rc < 1) {
            *err = rc;
            *err_bit = bit;
            if (rc == LATM_E_TRUNCATED && start == 0) { *strong = carried; return 0; }
            return -1;
        }
    }
    return (bit >> 3) == len ? 1 : -1;
}

/* LOAS: AudioSyncStream() - 11-bit 0x2B7, 13-bit audioMuxLengthBytes, AudioMuxElement(1) filling exactly that. */
static int try_loas(const uint8_t *in, size_t len, LATMConfig *cfg, Sink *k, unsigned *asc_seen, unsigned *strong) {
    size_t pos = 0;
    while (pos < len) {
        if (len - pos < 3u) return -1;
        unsigned word = ((unsigned)in[pos] << 16) | ((unsigned)in[pos + 1] << 8) | in[pos + 2];
        if ((word >> 13) != 0x2B7u) return -1;
        size_t n = word & 0x1fffu;
        if (!n) return -1;
        if (pos + 3u + n > len) { if (pos) return -1; *strong = 1; return 0; }   /* first frame cut short: fragment */
        size_t bit = 0;
        unsigned carried = 0;
        int rc = latm_parse_element(in + pos + 3, n, &bit, 1, cfg, k->out, k->cap, &k->used, k->len, k->max, &k->n,
                                    &carried);
        if (carried) *asc_seen = 1;
        if (rc < 1 || bit != n * 8u) return -1;
        pos += 3u + n;
    }
    return 1;
}

/* ADTS: 12-bit 0xFFF, layer 0, frame_length including the 7- (or 9-, with CRC) byte header, one raw data block. */
static int try_adts(const uint8_t *in, size_t len, Sink *k, unsigned *strong) {
    size_t pos = 0;
    while (pos < len) {
        if (len - pos < 7u) return -1;
        const uint8_t *h = in + pos;
        if (h[0] != 0xffu || (h[1] & 0xf6u) != 0xf0u) return -1;   /* syncword, layer 0 */
        size_t header = (h[1] & 1u) ? 7u : 9u;                     /* protection_absent */
        size_t frame = ((size_t)(h[3] & 3u) << 11) | ((size_t)h[4] << 3) | (h[5] >> 5);
        if ((h[6] & 3u) != 0u || frame <= header) return -1;       /* one raw_data_block only */
        if (pos + frame > len) { if (pos) return -1; *strong = 1; return 0; }
        if (!put_au(k, h + header, frame - header)) return -1;
        pos += frame;
    }
    return 1;
}

/* RFC 3640 AAC-hbr: AU-headers-length (bits), 16-bit AU headers (13-bit AU-size, 3-bit index 0), then the AUs. */
static int try_rfc3640(const uint8_t *in, size_t len, Sink *k) {
    if (len < 4u) return -1;
    size_t header_bits = ((size_t)in[0] << 8) | in[1];
    if (!header_bits || header_bits % 16u) return -1;
    size_t n = header_bits / 16u, data = 2u + n * 2u, total = 0;
    if (n > k->max || data > len) return -1;
    for (size_t i = 0; i < n; ++i) {
        unsigned h = ((unsigned)in[2 + i * 2] << 8) | in[3 + i * 2];
        if ((h & 7u) != 0u || !(h >> 3)) return -1;                /* AU-index / AU-index-delta 0, non-empty */
        total += h >> 3;
    }
    if (data + total != len) return -1;
    size_t pos = data;
    for (size_t i = 0; i < n; ++i) {
        size_t size = (((unsigned)in[2 + i * 2] << 8) | in[3 + i * 2]) >> 3;
        if (!put_au(k, in + pos, size)) return -1;
        pos += size;
    }
    return 1;
}

/* A bare access unit. raw_data_block() is a sequence of syntactic elements; encoders may put fill (FIL) or data
 * stream (DSE) elements first (ffmpeg's first frame carries its name in one). Walk those by their own byte counts,
 * then require the first audio element to be a single channel (SCE) or channel pair (CPE) element. */
static int raw_plausible(const uint8_t *in, size_t len) {
    size_t bit = 0, end = len * 8u;
#define TAKE(n, v) do { if (bit + (n) > end) return 0; v = 0; for (unsigned i_ = 0; i_ < (n); ++i_, ++bit) \
        v = (v << 1) | ((in[bit >> 3] >> (7u - (bit & 7u))) & 1u); } while (0)
    for (unsigned guard = 0; guard < 8u; ++guard) {
        unsigned id, v, count;
        TAKE(3, id);
        if (id == 0u || id == 1u) return 1;                       /* SCE, CPE */
        if (id == 4u) {                                            /* DSE */
            unsigned align;
            TAKE(4, v); TAKE(1, align); TAKE(8, count);
            if (count == 255u) { TAKE(8, v); count += v; }
            if (align) bit = (bit + 7u) & ~(size_t)7u;
            if (bit + (size_t)count * 8u > end) return 0;
            bit += (size_t)count * 8u;
        } else if (id == 6u) {                                     /* FIL */
            TAKE(4, count);
            if (count == 15u) { TAKE(8, v); count += v - 1u; }
            if (bit + (size_t)count * 8u > end) return 0;
            bit += (size_t)count * 8u;
        } else {
            return 0;                                              /* CCE, LFE, PCE or END first: not this stream */
        }
    }
#undef TAKE
    return 0;
}

static int try_raw(const uint8_t *in, size_t len, unsigned channels, Sink *k) {
    (void)channels;
    if (!raw_plausible(in, len)) return -1;
    return put_au(k, in, len) ? 1 : -1;
}

static int run(AACPacketState *st, AACTransport t, const uint8_t *in, size_t len, Sink *k, AACPacketResult *res,
               LATMConfig *latm_out, unsigned *strong) {
    unsigned asc_seen = 0;
    int err = 0;
    size_t err_bit = 0;
    int rc;
    unsigned weak = 0;
    LATMConfig cfg = st->latm, oob = st->oob;
    switch (t) {
        case AAC_TRANSPORT_LATM: rc = try_latm(in, len, 1, &cfg, k, &asc_seen, &err, &err_bit, strong);
            if (rc < 0 && !res->latm_error) { res->latm_error = err; res->latm_error_bit = err_bit; }
            break;
        case AAC_TRANSPORT_LOAS: rc = try_loas(in, len, &cfg, k, &asc_seen, strong); break;
        case AAC_TRANSPORT_ADTS: rc = try_adts(in, len, k, strong); break;
        case AAC_TRANSPORT_RFC3640: rc = try_rfc3640(in, len, k); break;
        case AAC_TRANSPORT_LATM_OOB: rc = try_latm(in, len, 0, &oob, k, &asc_seen, &err, &err_bit, &weak); break;
        case AAC_TRANSPORT_RAW: rc = try_raw(in, len, st->channels, k); break;
        default: rc = -1;
    }
    if (rc == 1) { *latm_out = cfg; res->asc_seen = asc_seen; }
    return rc;
}

int aac_packet_split(AACPacketState *st, const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                     size_t *au_len, unsigned max_aus, AACPacketResult *res) {
    AACPacketResult r;
    memset(&r, 0, sizeof r);
    if (res) *res = r;
    if (!st || !in || !len || !out || !au_len || !max_aus) return -1;
    /* Strong transports (a sync word, or length fields that must add up exactly) without and then with a one-byte
     * media payload header, before the weak ones (out-of-band LATM, a bare access unit). */
    static const struct { AACTransport t; unsigned header; } order[] = {
        {AAC_TRANSPORT_LOAS, 0}, {AAC_TRANSPORT_ADTS, 0}, {AAC_TRANSPORT_RFC3640, 0}, {AAC_TRANSPORT_LATM, 0},
        {AAC_TRANSPORT_LOAS, 1}, {AAC_TRANSPORT_ADTS, 1}, {AAC_TRANSPORT_RFC3640, 1}, {AAC_TRANSPORT_LATM, 1},
        {AAC_TRANSPORT_LATM_OOB, 0}, {AAC_TRANSPORT_RAW, 0}, {AAC_TRANSPORT_LATM_OOB, 1}, {AAC_TRANSPORT_RAW, 1},
    };
    int incomplete = 0;
    unsigned strong_fragment = 0;
    /* The remembered transport first, then every transport without and with a one-byte media payload header. */
    for (int pass = -1; pass < (int)(sizeof order / sizeof order[0]); ++pass) {
        AACTransport t;
        unsigned header;
        if (pass < 0) { if (!st->locked) continue; t = st->locked; header = st->locked_header; }
        else { t = order[pass].t; header = order[pass].header; }
        if (header && (len < 2u || (in[0] & 0xf0u) || !(in[0] & 0x0fu))) continue;
        /* A packet a framed transport recognised as cut short is a fragment: the weak fallbacks never get it. */
        if (strong_fragment && (t == AAC_TRANSPORT_LATM_OOB || t == AAC_TRANSPORT_RAW)) continue;
        Sink k = { out, out_cap, 0, au_len, max_aus, 0 };
        LATMConfig cfg;
        unsigned strong = 0;
        int rc = run(st, t, in + header, len - header, &k, &r, &cfg, &strong);
        /* A fragment of the stream's own transport: wait for the rest, never let a weaker framing misread it. */
        if (rc == 0 && pass < 0) { r.transport = t; r.incomplete = 1; if (res) *res = r; return 0; }
        /* Before a transport is known, only strong evidence (a sync word, or a StreamMuxConfig that parsed) makes a
         * cut-short packet a fragment; anything weaker that merely runs out of input is no evidence at all. */
        if (rc == 0 && !header && strong) { incomplete = 1; strong_fragment = 1; }
        if (rc != 1 || !k.n) continue;
        if (header && k.n != (in[0] & 0x0fu)) continue;           /* the header's frame count must match */
        st->latm = cfg;
        st->locked = t;
        st->locked_header = header;
        r.transport = t;
        r.header = header;
        r.aus = k.n;
        if (res) *res = r;
        return (int)k.n;
    }
    r.incomplete = (unsigned)incomplete;
    if (res) *res = r;
    return incomplete ? 0 : -1;
}

void aac_packet_unlock(AACPacketState *st) {
    if (st) { st->locked = AAC_TRANSPORT_NONE; st->locked_header = 0; }
}
