/* AACDecoder.c - see AACDecoder.h. Decodes AAC-LC to interleaved stereo PCM with AudioToolbox's AudioConverter, a
 * public API. Everything Apple-specific here is documented AudioToolbox; nothing is reverse-engineered.
 *
 * Validation: the LATM extraction is checked on the host against ffmpeg; the AudioConverter stage runs for real on
 * macOS in CI (tests/test_aac.py, RealAudioToolbox) and on the phone through `BTPSAudioHelper --aac-selftest`. AAC
 * stays behind the SpeakaAAC switch until a real Bluetooth AAC stream has been decoded on the device. */
#include "AACDecoder.h"
#include <AudioToolbox/AudioToolbox.h>
#include "../shared/LATM.h"
#include <stdlib.h>
#include <string.h>

#define ADTS_HEADER_BYTES 7u
/* AAC-LC allows 6144 bits per channel per frame (768 bytes); 8 KiB covers stereo and any LATM overhead. */
#define AAC_MAX_ELEMENT_BYTES 8192u
/* 'adec' and 'appl' (kAudioDecoderComponentType, kAppleSoftwareAudioCodecManufacturer), as plain numbers so the file
 * also compiles where four-character constants are warned about. */
#define SPK_AUDIO_DECODER_TYPE 0x61646563u
#define SPK_APPLE_SOFTWARE_MFR 0x6170706cu
/* Our input callback's "no more input for now" status: a distinctive value, never a real AudioToolbox error. */
#define SPK_NO_MORE_INPUT ((OSStatus)0x6e6d6f72) /* 'nmor' */

struct AACDecoder {
    AudioConverterRef converter;
    AACConfig config;
    AACDecoderOptions options;
    unsigned freq_index;
    uint8_t asc[2];                 /* the AudioSpecificConfig the converter was built for */
    AudioStreamBasicDescription out_format;
    uint64_t generation;
    unsigned next_sequence, seen;
    const uint8_t *in_data;         /* the one packet the input callback hands over */
    size_t in_size;
    AudioStreamPacketDescription in_packet_desc;
    int in_consumed;
    OSStatus last_status;
    unsigned config_changes, fragments_joined, fragments_dropped;
    uint8_t reasm[AAC_MAX_ELEMENT_BYTES];   /* an AudioMuxElement being reassembled from fragments */
    size_t reasm_len;
    uint32_t reasm_timestamp;
    int reasm_active;
    uint8_t framed[ADTS_HEADER_BYTES + AAC_MAX_ELEMENT_BYTES];  /* [ADTS header][access unit] */
    int16_t scratch[1024 * 2];
    AACPacketState packets;         /* the source's transport and LATM configuration, across packets */
    AACPacketResult last_packet;
    uint8_t aus[AAC_MAX_ELEMENT_BYTES];   /* the access units of one packet, back to back */
    size_t au_len[AAC_PACKET_MAX_AUS];
};

void aac_decoder_default_options(AACDecoderOptions *o) {
    o->framing = AAC_FRAMING_RAW;
    o->cookie = AAC_COOKIE_ESDS;
    o->prefer_software = 1;
}

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

/* AudioConverter pulls input through this callback: exactly one packet, then "no more input for now", so one
 * FillComplexBuffer call decodes one access unit. Returning a non-zero status with zero packets is the documented
 * way to pause; the converter keeps its state for the next call. */
static OSStatus pull_input(AudioConverterRef conv, UInt32 *packets, AudioBufferList *buffers,
                           AudioStreamPacketDescription **desc, void *context) {
    (void)conv;
    AACDecoder *d = (AACDecoder *)context;
    if (d->in_consumed || !d->in_data || !d->in_size) { *packets = 0; return SPK_NO_MORE_INPUT; }
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

/* (Re)build the converter for d->config and d->options. Fills st with every status. Returns noErr on success. */
static OSStatus build_converter(AACDecoder *d, AACDecoderCreateStatus *st) {
    AACDecoderCreateStatus local;
    memset(&local, 0, sizeof local);
    if (!st) st = &local;
    memset(st, 0, sizeof *st);

    AudioStreamBasicDescription in = {0};
    in.mSampleRate = d->config.sample_rate;
    in.mFormatID = kAudioFormatMPEG4AAC;
    in.mFormatFlags = kMPEG4Object_AAC_LC;
    in.mChannelsPerFrame = d->config.channels;
    in.mFramesPerPacket = 1024;                  /* AAC-LC: 1024 PCM frames per access unit */

    AudioStreamBasicDescription out = {0};
    out.mSampleRate = d->config.sample_rate;
    out.mFormatID = kAudioFormatLinearPCM;
    out.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
    out.mChannelsPerFrame = d->config.channels;
    out.mBitsPerChannel = 16;
    out.mBytesPerFrame = (UInt32)(2u * d->config.channels);
    out.mFramesPerPacket = 1;
    out.mBytesPerPacket = out.mBytesPerFrame;
    d->out_format = out;

    AudioConverterRef conv = NULL;
    if (d->options.prefer_software) {
        AudioClassDescription cls;
        cls.mType = SPK_AUDIO_DECODER_TYPE;
        cls.mSubType = kAudioFormatMPEG4AAC;
        cls.mManufacturer = SPK_APPLE_SOFTWARE_MFR;
        st->specific_status = AudioConverterNewSpecific(&in, &out, 1, &cls, &conv);
        if (st->specific_status == noErr && conv) st->used_software = 1;
        else conv = NULL;
    }
    if (!conv) {
        st->new_status = AudioConverterNew(&in, &out, &conv);
        if (st->new_status != noErr) conv = NULL;
    }
    if (!conv) return st->new_status ? st->new_status : (st->specific_status ? st->specific_status : -1);

    if (d->options.cookie != AAC_COOKIE_NONE) {
        uint8_t cookie[64];
        size_t n = 0;
        if (d->options.cookie == AAC_COOKIE_ESDS) n = aac_build_esds(d->asc, 2, d->config.bitrate, cookie, sizeof cookie);
        else { cookie[0] = d->asc[0]; cookie[1] = d->asc[1]; n = 2; }
        st->cookie_status = n ? AudioConverterSetProperty(conv, kAudioConverterDecompressionMagicCookie, (UInt32)n, cookie)
                              : -1;
        /* A refused cookie is not fatal: the converter already has the rate, channels and object type. */
        st->cookie_set = st->cookie_status == noErr;
    }
    d->converter = conv;
    return noErr;
}

static int adopt_config(AACDecoder *d, const AACConfig *cfg) {
    unsigned freq_index = aac_sampling_frequency_index(cfg->sample_rate);
    if (freq_index == 0xfu || aac_build_asc(cfg, d->asc) != 2u) return 0;
    d->config = *cfg;
    d->freq_index = freq_index;
    return 1;
}

AACDecoder *aac_decoder_create_ex(const AACConfig *cfg, const AACDecoderOptions *opt, AACDecoderCreateStatus *st) {
    if (st) memset(st, 0, sizeof *st);
    if (!aac_config_supported(cfg)) return NULL;
    AACDecoder *d = calloc(1, sizeof *d);
    if (!d) return NULL;
    if (opt) d->options = *opt; else aac_decoder_default_options(&d->options);
    if (!adopt_config(d, cfg) || build_converter(d, st) != noErr) { free(d); return NULL; }
    aac_packet_init(&d->packets, d->asc, d->config.channels);
    return d;
}

AACDecoder *aac_decoder_create(const AACConfig *cfg) { return aac_decoder_create_ex(cfg, NULL, NULL); }

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
    d->reasm_active = 0;
    d->reasm_len = 0;
}

int32_t aac_decoder_last_status(const AACDecoder *d) { return d ? (int32_t)d->last_status : 0; }
unsigned aac_decoder_config_changes(const AACDecoder *d) { return d ? d->config_changes : 0; }
unsigned aac_decoder_fragments_joined(const AACDecoder *d) { return d ? d->fragments_joined : 0; }
unsigned aac_decoder_fragments_dropped(const AACDecoder *d) { return d ? d->fragments_dropped : 0; }
void aac_decoder_last_packet(const AACDecoder *d, AACPacketResult *out) {
    if (!out) return;
    if (d) *out = d->last_packet; else memset(out, 0, sizeof *out);
}

int aac_decoder_decode(AACDecoder *d, const AudioPacket *p, int16_t *pcm,
                       size_t capacity, unsigned *frames, unsigned *samples, unsigned *rate) {
    *frames = *samples = *rate = 0;
    if (!d || !p || !pcm || capacity < 2u) return -1;
    if (!p->length || p->length > AUDIO_RING_PAYLOAD) return -1;
    if (d->generation != p->generation || (d->seen && d->next_sequence != p->sequence)) {
        aac_decoder_reset(d);
        d->generation = p->generation;
    }
    d->seen = 1;
    d->next_sequence = (uint16_t)(p->sequence + 1);
    if (!d->converter && build_converter(d, NULL) != noErr) return -15;

    /* Reassembly (RFC 3016): every fragment of one AudioMuxElement carries the same RTP timestamp. A fragment of a
     * different timestamp means the earlier element lost a piece; abandon it and parse the new packet on its own. */
    const uint8_t *element = p->data;
    size_t element_len = p->length;
    if (d->reasm_active) {
        if (p->timestamp == d->reasm_timestamp && d->reasm_len + p->length <= sizeof d->reasm) {
            memcpy(d->reasm + d->reasm_len, p->data, p->length);
            d->reasm_len += p->length;
            element = d->reasm;
            element_len = d->reasm_len;
        } else {
            d->reasm_active = 0;
            d->reasm_len = 0;
            d->fragments_dropped++;
        }
    }

    AACPacketResult res;
    int n = aac_packet_split(&d->packets, element, element_len, d->aus, sizeof d->aus, d->au_len,
                             AAC_PACKET_MAX_AUS, &res);
    d->last_packet = res;
    if (n == 0) {
        if (element_len < sizeof d->reasm && !d->reasm_active) {   /* the first fragment: keep it, wait for the rest */
            memcpy(d->reasm, p->data, p->length);
            d->reasm_len = p->length;
            d->reasm_timestamp = p->timestamp;
            d->reasm_active = 1;
        }
        return 1;
    }
    if (element == d->reasm) d->fragments_joined++;
    d->reasm_active = 0;
    d->reasm_len = 0;
    if (n < 0) return -11;

    /* The in-band configuration is the stream's own: if it differs from what the converter was built for, follow it.
     * A rate change reaches the helper through *rate, which already restarts its output on a new rate. */
    const LATMConfig *inband = &d->packets.latm;
    if (res.asc_seen && inband->asc_len == 2u && (inband->asc[0] != d->asc[0] || inband->asc[1] != d->asc[1])) {
        AACConfig next;
        if (!aac_config_from_asc(inband->asc, 2, &next)) return -14;
        next.vbr = d->config.vbr;
        next.bitrate = d->config.bitrate;
        if (d->converter) { AudioConverterDispose(d->converter); d->converter = NULL; }
        if (!adopt_config(d, &next) || build_converter(d, NULL) != noErr) return -15;
        d->config_changes++;
    }

    /* Decode every access unit, appending its PCM. */
    size_t offset = 0, written = 0;
    unsigned decoded = 0, failed = 0;
    for (int i = 0; i < n; ++i) {
        const uint8_t *unit = d->aus + offset;
        size_t unit_len = d->au_len[i];
        offset += unit_len;
        if (unit_len + ADTS_HEADER_BYTES > sizeof d->framed) { failed++; continue; }
        if (d->options.framing == AAC_FRAMING_ADTS) {
            build_adts(d, d->framed, unit_len);
            memcpy(d->framed + ADTS_HEADER_BYTES, unit, unit_len);
            d->in_data = d->framed;
            d->in_size = ADTS_HEADER_BYTES + unit_len;
        } else {
            d->in_data = unit;                             /* the bare access unit */
            d->in_size = unit_len;
        }
        d->in_consumed = 0;
        size_t room = (capacity / 2u) - written;           /* stereo frames left in pcm */
        if (room == 0) break;
        UInt32 out_frames = room < 1024u ? (UInt32)room : 1024u;
        AudioBufferList out_list;
        out_list.mNumberBuffers = 1;
        out_list.mBuffers[0].mNumberChannels = d->config.channels;
        out_list.mBuffers[0].mDataByteSize = out_frames * d->out_format.mBytesPerFrame;
        out_list.mBuffers[0].mData = d->scratch;
        OSStatus status = AudioConverterFillComplexBuffer(d->converter, pull_input, d, &out_frames, &out_list, NULL);
        d->last_status = status == SPK_NO_MORE_INPUT ? noErr : status;   /* our own end-of-input marker is success */
        d->in_data = NULL;
        if (status != noErr && status != SPK_NO_MORE_INPUT) { failed++; continue; }
        if (out_frames == 0) continue;                     /* priming */
        int16_t *dst = pcm + written * 2u;
        if (d->config.channels == 2) {
            memcpy(dst, d->scratch, (size_t)out_frames * 2u * sizeof(int16_t));
        } else {
            for (UInt32 k = 0; k < out_frames; ++k) { dst[k * 2] = d->scratch[k]; dst[k * 2 + 1] = d->scratch[k]; }
        }
        written += out_frames;
        decoded++;
    }
    if (!written) {
        /* Everything the transport produced was rejected by the decoder: detect the transport afresh next time. */
        if (failed) { aac_packet_unlock(&d->packets); return -6; }
        return 1;                                          /* priming */
    }
    *samples = (unsigned)written;
    *frames = decoded;
    *rate = d->config.sample_rate;
    return 0;
}
