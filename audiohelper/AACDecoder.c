/* AACDecoder.c - see AACDecoder.h. Decodes AAC-LC to interleaved stereo PCM with AudioToolbox's AudioConverter,
 * a public API. Everything Apple-specific here is documented AudioToolbox; nothing is reverse-engineered.
 *
 * NOT YET VALIDATED ON A DEVICE. There is no Apple toolchain on the build host, so this file is compiled but not run
 * by CI; the host tests cover the pure-C framing and the AACConfig parsing, not AudioConverter itself. Until a device
 * test confirms AAC is negotiated and decoded (see the on-device procedure in docs/CHANGELOG.md / the PR notes), AAC
 * stays behind the SpeakaAAC experiment switch and SBC remains the default. */
#include "AACDecoder.h"
#include <AudioToolbox/AudioToolbox.h>
#include "../shared/LATM.h"
#include <stdlib.h>
#include <string.h>

/* An ADTS header is 7 bytes (no CRC). We rebuild one per frame from the negotiated config so AudioConverter sees a
 * self-describing AAC stream, independent of exactly how the SRC framed LATM/LOAS. ISO/IEC 13818-7. */
#define ADTS_HEADER_BYTES 7u

struct AACDecoder {
    AudioConverterRef converter;
    AACConfig config;
    unsigned freq_index;
    AudioStreamBasicDescription out_format;
    uint64_t generation;
    unsigned next_sequence, seen;
    /* The current input frame for the AudioConverter pull callback. */
    const uint8_t *in_data;
    size_t in_size;
    AudioStreamPacketDescription in_packet_desc;
    int in_consumed;
    uint8_t framed[ADTS_HEADER_BYTES + AUDIO_RING_PAYLOAD];   /* one ADTS-wrapped access unit */
};

static void build_adts(const AACDecoder *d, uint8_t header[ADTS_HEADER_BYTES], size_t frame_len) {
    size_t total = ADTS_HEADER_BYTES + frame_len;
    unsigned profile = 1u;                       /* AAC-LC = MPEG-4 object type 2, ADTS profile = type-1 = 1 */
    unsigned channels = d->config.channels;
    header[0] = 0xffu;
    header[1] = 0xf1u;                            /* MPEG-4, Layer 0, no CRC */
    header[2] = (uint8_t)((profile << 6) | (d->freq_index << 2) | ((channels >> 2) & 0x1u));
    header[3] = (uint8_t)(((channels & 0x3u) << 6) | ((total >> 11) & 0x3u));
    header[4] = (uint8_t)((total >> 3) & 0xffu);
    header[5] = (uint8_t)(((total & 0x7u) << 5) | 0x1fu);
    header[6] = 0xfcu;                            /* buffer fullness 0x7ff, 0 frames-1 */
}

/* AudioConverter pulls input through this callback. We hand it exactly one ADTS frame, then report "no more data"
 * (so one FillComplexBuffer call decodes one frame). */
static OSStatus pull_input(AudioConverterRef conv, UInt32 *packets, AudioBufferList *buffers,
                           AudioStreamPacketDescription **desc, void *context) {
    (void)conv;
    AACDecoder *d = (AACDecoder *)context;
    if (d->in_consumed || !d->in_data || !d->in_size) { *packets = 0; return 1; }  /* 1: our private end marker */
    buffers->mNumberBuffers = 1;
    buffers->mBuffers[0].mNumberChannels = d->config.channels;
    buffers->mBuffers[0].mDataByteSize = (UInt32)d->in_size;
    buffers->mBuffers[0].mData = (void *)(uintptr_t)d->in_data;
    d->in_packet_desc.mStartOffset = 0;
    d->in_packet_desc.mVariableFramesInPacket = 0;
    d->in_packet_desc.mDataByteSize = (UInt32)d->in_size;
    if (desc) *desc = &d->in_packet_desc;
    *packets = 1;
    d->in_consumed = 1;
    return noErr;
}

AACDecoder *aac_decoder_create(const AACConfig *cfg) {
    if (!aac_config_supported(cfg)) return NULL;
    AACDecoder *d = calloc(1, sizeof *d);
    if (!d) return NULL;
    d->config = *cfg;
    d->freq_index = aac_sampling_frequency_index(cfg->sample_rate);
    if (d->freq_index == 0xfu) { free(d); return NULL; }

    AudioStreamBasicDescription in = {0};
    in.mSampleRate = cfg->sample_rate;
    in.mFormatID = kAudioFormatMPEG4AAC;
    in.mFormatFlags = kMPEG4Object_AAC_LC;
    in.mChannelsPerFrame = cfg->channels;
    in.mFramesPerPacket = 1024;                  /* AAC-LC: 1024 PCM samples per frame */

    AudioStreamBasicDescription out = {0};
    out.mSampleRate = cfg->sample_rate;
    out.mFormatID = kAudioFormatLinearPCM;
    out.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
    out.mChannelsPerFrame = cfg->channels;
    out.mBitsPerChannel = 16;
    out.mBytesPerFrame = (UInt32)(2u * cfg->channels);
    out.mFramesPerPacket = 1;
    out.mBytesPerPacket = out.mBytesPerFrame;
    d->out_format = out;

    if (AudioConverterNew(&in, &out, &d->converter) != noErr || !d->converter) { free(d); return NULL; }
    return d;
}

void aac_decoder_destroy(AACDecoder *d) {
    if (!d) return;
    if (d->converter) AudioConverterDispose(d->converter);
    free(d);
}

void aac_decoder_reset(AACDecoder *d) {
    if (!d) return;
    if (d->converter) AudioConverterReset(d->converter);
    d->seen = 0;
    d->next_sequence = 0;
}

int aac_decoder_decode(AACDecoder *d, const AudioPacket *p, int16_t *pcm,
                       size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate) {
    *frames = *samples = *rate = 0;
    if (!d || !d->converter) return -1;
    if (!p->length || p->length > AUDIO_RING_PAYLOAD) return -1;
    if (d->generation != p->generation || (d->seen && d->next_sequence != p->sequence)) {
        aac_decoder_reset(d);
        d->generation = p->generation;
    }
    d->seen = 1;
    d->next_sequence = (uint16_t)(p->sequence + 1);

    /* A2DP AAC payload is an MPEG-4 LATM AudioMuxElement, not a bare access unit. Depacketize it to the AAC access
     * unit (PayloadMux), then ADTS-wrap THAT for AudioConverter. Wrapping the raw payload (the earlier bug) fed LATM
     * bytes to the decoder as if they were an access unit and produced garbage. A malformed element is a decode
     * error (negative return), which the caller treats as a failed AAC frame, never as SBC. */
    LATMFrame frame;
    /* Depacketize straight into the ADTS payload area, leaving room for the 7-byte header in front. */
    size_t au_cap = sizeof d->framed - ADTS_HEADER_BYTES;
    if (!latm_parse_audiomuxelement(p->data, p->length, d->framed + ADTS_HEADER_BYTES, au_cap, &frame))
        return -11;   /* not a valid AudioMuxElement */
    build_adts(d, d->framed, frame.au_len);
    d->in_data = d->framed;
    d->in_size = ADTS_HEADER_BYTES + frame.au_len;
    d->in_consumed = 0;

    /* AAC-LC yields 1024 frames per packet. Decode into a stereo-or-mono scratch, then widen mono to stereo. */
    UInt32 out_frames = 1024;
    if ((size_t)out_frames * 2u > capacity) out_frames = (UInt32)(capacity / 2u);
    static int16_t scratch[1024 * 2];
    AudioBufferList out_list;
    out_list.mNumberBuffers = 1;
    out_list.mBuffers[0].mNumberChannels = d->config.channels;
    out_list.mBuffers[0].mDataByteSize = out_frames * d->out_format.mBytesPerFrame;
    out_list.mBuffers[0].mData = scratch;

    OSStatus status = AudioConverterFillComplexBuffer(d->converter, pull_input, d, &out_frames, &out_list, NULL);
    if (status != noErr && status != 1) return -6;     /* 1 is our pull callback's benign end marker */
    if (out_frames == 0) return 1;                     /* buffered, nothing emitted yet */

    if (d->config.channels == 2) {
        memcpy(pcm, scratch, (size_t)out_frames * 2u * sizeof(int16_t));
    } else {
        for (UInt32 i = 0; i < out_frames; ++i) { pcm[i * 2] = scratch[i]; pcm[i * 2 + 1] = scratch[i]; }
    }
    *samples = out_frames;
    *frames = 1;
    *rate = d->config.sample_rate;
    return 0;
}
