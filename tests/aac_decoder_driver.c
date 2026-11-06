/* Test driver for AACDecoder: builds AudioPackets in C, so the Python tests pass only plain numbers and bytes and
 * never mirror the packet layout. Linked either with tests/fakes/fake_audiotoolbox.c or with the real AudioToolbox. */
#include "AACDecoder.h"
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
