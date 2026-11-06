/* Test driver for AACDecoder: builds AudioPackets in C, so the Python tests pass only plain numbers and bytes and
 * never mirror the packet layout. Linked either with tests/fakes/fake_audiotoolbox.c or with the real AudioToolbox. */
#include "AACDecoder.h"
#include <stdlib.h>
#include <string.h>

/* Exported for ctypes. */
AACDecoder *drv_create(int framing, int cookie, int prefer_software, unsigned rate, unsigned channels,
                       AACDecoderCreateStatus *st);
int drv_decode(AACDecoder *d, unsigned generation, unsigned sequence, unsigned timestamp, const uint8_t *data,
               unsigned length, int16_t *pcm, unsigned capacity, unsigned *samples, unsigned *rate);
unsigned drv_create_status_size(void);

AACDecoder *drv_create(int framing, int cookie, int prefer_software, unsigned rate, unsigned channels,
                       AACDecoderCreateStatus *st) {
    AACConfig cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.object_type = AAC_OBJECT_TYPE_MPEG4_LC;
    cfg.sample_rate = rate;
    cfg.channels = (uint8_t)channels;
    AACDecoderOptions opt;
    aac_decoder_default_options(&opt);
    if (framing >= 0) opt.framing = (AACFraming)framing;
    if (cookie >= 0) opt.cookie = (AACCookie)cookie;
    if (prefer_software >= 0) opt.prefer_software = prefer_software;
    return aac_decoder_create_ex(&cfg, &opt, st);
}

int drv_decode(AACDecoder *d, unsigned generation, unsigned sequence, unsigned timestamp, const uint8_t *data,
               unsigned length, int16_t *pcm, unsigned capacity, unsigned *samples, unsigned *rate) {
    AudioPacket p;
    memset(&p, 0, sizeof p);
    if (length > AUDIO_RING_PAYLOAD) return -100;
    p.generation = generation;
    p.sequence = (uint16_t)sequence;
    p.timestamp = timestamp;
    p.length = length;
    memcpy(p.data, data, length);
    unsigned frames = 0;
    return aac_decoder_decode(d, &p, pcm, capacity, &frames, samples, rate);
}

unsigned drv_create_status_size(void) { return (unsigned)sizeof(AACDecoderCreateStatus); }

/* The transport depacketizer on its own (shared/AACPacket.c), with plain-number results for ctypes:
 * info = [transport, header, aus, latm_error, latm_error_bit, incomplete, asc_seen]. */
AACPacketState *drv_packet_new(unsigned channels);
void drv_packet_free(AACPacketState *st);
int drv_packet_split(AACPacketState *st, const uint8_t *in, unsigned len, uint8_t *out, unsigned cap,
                     unsigned *au_len, unsigned max_aus, int *info);

AACPacketState *drv_packet_new(unsigned channels) {
    AACPacketState *st = (AACPacketState *)calloc(1, sizeof *st);
    static const uint8_t asc[2] = {0x11, 0x90};            /* AAC-LC 48 kHz stereo, as negotiated */
    if (st) aac_packet_init(st, asc, channels);
    return st;
}
void drv_packet_free(AACPacketState *st) { free(st); }
int drv_packet_split(AACPacketState *st, const uint8_t *in, unsigned len, uint8_t *out, unsigned cap,
                     unsigned *au_len, unsigned max_aus, int *info) {
    size_t lens[AAC_PACKET_MAX_AUS];
    AACPacketResult r;
    if (max_aus > AAC_PACKET_MAX_AUS) max_aus = AAC_PACKET_MAX_AUS;
    int n = aac_packet_split(st, in, len, out, cap, lens, max_aus, &r);
    for (int i = 0; i < n; ++i) au_len[i] = (unsigned)lens[i];
    info[0] = (int)r.transport; info[1] = (int)r.header; info[2] = (int)r.aus; info[3] = r.latm_error;
    info[4] = (int)r.latm_error_bit; info[5] = (int)r.incomplete; info[6] = (int)r.asc_seen;
    return n;
}
