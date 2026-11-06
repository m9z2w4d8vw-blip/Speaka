/* See AudioToolbox/AudioToolbox.h in this folder. A converter that behaves like Apple's at the API boundary: it pulls
 * input through the caller's callback until the callback stops it, returns the callback's status, and emits 1024
 * frames of a 440 Hz tone per packet. It also records what it was given, for the tests to inspect. */
#include "AudioToolbox/AudioToolbox.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef struct { AudioStreamBasicDescription in, out; double phase; unsigned noise; } FakeConverter;

/* Knobs and records, read and written by the tests through ctypes. */
int fake_at_fail_specific, fake_at_reject_cookie;
unsigned fake_at_created, fake_at_disposed, fake_at_packets, fake_at_bad_desc, fake_at_extra_packets;
OSType fake_at_class_type, fake_at_class_subtype, fake_at_class_mfr;
uint8_t fake_at_cookie[256];
UInt32 fake_at_cookie_len;
uint8_t fake_at_last_packet[8192];
UInt32 fake_at_last_packet_len;
uint8_t fake_at_packet_log[65536];      /* every packet, back to back, with fake_at_packet_lens */
UInt32 fake_at_packet_log_len, fake_at_packet_lens[256];

static OSStatus make(const AudioStreamBasicDescription *in, const AudioStreamBasicDescription *out, AudioConverterRef *c) {
    FakeConverter *f = calloc(1, sizeof *f);
    if (!f) return -108;
    f->in = *in; f->out = *out;
    /* FAKE_AT_OUTPUT=noise: emit noise instead of the tone, to prove the self-test's quality checks can fail. */
    const char *mode = getenv("FAKE_AT_OUTPUT");
    f->noise = mode && !strcmp(mode, "noise") ? 12345u : 0u;
    *c = (AudioConverterRef)f;
    fake_at_created++;
    return noErr;
}
OSStatus AudioConverterNew(const AudioStreamBasicDescription *in, const AudioStreamBasicDescription *out,
                           AudioConverterRef *c) { return make(in, out, c); }
OSStatus AudioConverterNewSpecific(const AudioStreamBasicDescription *in, const AudioStreamBasicDescription *out,
                                   UInt32 n, const AudioClassDescription *cls, AudioConverterRef *c) {
    if (n) { fake_at_class_type = cls->mType; fake_at_class_subtype = cls->mSubType; fake_at_class_mfr = cls->mManufacturer; }
    if (fake_at_fail_specific) { *c = NULL; return 0x666d743f; /* 'fmt?' */ }
    return make(in, out, c);
}
OSStatus AudioConverterDispose(AudioConverterRef c) { free(c); fake_at_disposed++; return noErr; }
OSStatus AudioConverterReset(AudioConverterRef c) { ((FakeConverter *)c)->phase = 0; return noErr; }
OSStatus AudioConverterSetProperty(AudioConverterRef c, AudioConverterPropertyID id, UInt32 size, const void *data) {
    (void)c;
    if (id != kAudioConverterDecompressionMagicCookie) return 0x70726f70; /* 'prop' */
    if (fake_at_reject_cookie) return 0x21646174;                        /* '!dat' */
    fake_at_cookie_len = size < sizeof fake_at_cookie ? size : (UInt32)sizeof fake_at_cookie;
    memcpy(fake_at_cookie, data, fake_at_cookie_len);
    return noErr;
}
OSStatus AudioConverterFillComplexBuffer(AudioConverterRef c, AudioConverterComplexInputDataProc proc, void *ctx,
                                         UInt32 *io, AudioBufferList *out, AudioStreamPacketDescription *pd) {
    (void)pd;
    FakeConverter *f = (FakeConverter *)c;
    UInt32 want = *io, produced = 0;
    for (;;) {
        UInt32 n = 1;
        AudioBufferList in;
        AudioStreamPacketDescription *desc = NULL;
        memset(&in, 0, sizeof in);
        OSStatus s = proc(c, &n, &in, &desc, ctx);
        if (s != noErr || n == 0) { *io = produced; out->mBuffers[0].mDataByteSize = produced * f->out.mBytesPerFrame; return s; }
        if (produced) { fake_at_extra_packets++; continue; }        /* a callback must hand over one packet per call */
        UInt32 len = in.mBuffers[0].mDataByteSize;
        if (!desc || desc->mDataByteSize != len || desc->mStartOffset != 0) fake_at_bad_desc++;
        fake_at_last_packet_len = len < sizeof fake_at_last_packet ? len : (UInt32)sizeof fake_at_last_packet;
        memcpy(fake_at_last_packet, in.mBuffers[0].mData, fake_at_last_packet_len);
        if (fake_at_packets < 256 && fake_at_packet_log_len + len <= sizeof fake_at_packet_log) {
            memcpy(fake_at_packet_log + fake_at_packet_log_len, in.mBuffers[0].mData, len);
            fake_at_packet_log_len += len;
            fake_at_packet_lens[fake_at_packets] = len;
        }
        fake_at_packets++;
        produced = want < 1024 ? want : 1024;
        int16_t *o = out->mBuffers[0].mData;
        UInt32 ch = f->out.mChannelsPerFrame;
        double step = 2.0 * 3.14159265358979323846 * 440.0 / f->out.mSampleRate;
        for (UInt32 i = 0; i < produced; ++i) {
            int16_t v = (int16_t)(2900.0 * sin(f->phase));
            f->phase += step;
            if (f->noise) { f->noise = f->noise * 1103515245u + 12345u; v = (int16_t)((int)((f->noise >> 16) % 6001u) - 3000); }
            for (UInt32 k = 0; k < ch; ++k) o[i * ch + k] = v;
        }
    }
}
