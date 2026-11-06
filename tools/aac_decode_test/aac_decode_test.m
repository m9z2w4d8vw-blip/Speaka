/* aac_decode_test - a standalone iOS command-line test that decodes a real AAC-LATM fixture through Speaka's actual
 * AACDecoder.c / LATM.c / AACConfig.c and the real AudioToolbox AudioConverter, with no stubs and no Bluetooth.
 *
 * It exists because every CI check so far compiles the AAC decoder but never runs AudioConverter (no Apple toolchain
 * on the build host), and every device log so far negotiated SBC, so AAC -> PCM has never actually executed. This
 * runs it on the phone and reports machine-readable results.
 *
 * Input: the ffmpeg-generated LATM fixture, embedded at build time (fixture_data.h) so nothing is pushed alongside
 * the binary. The fixture is LOAS-framed (0x2B7 syncword + 13-bit length + AudioMuxElement); we strip LOAS to get
 * the bare AudioMuxElements that A2DP would deliver, then feed each as an AudioPacket to the real decoder, exactly
 * as the helper's receive loop does.
 *
 * Output: a JSON object on stdout with decoder status, per-stage counts, PCM frame count, peak and RMS amplitude,
 * the decoded rate/channels, and any errors. Exit 0 iff decoding produced the expected non-silent PCM.
 *
 * Build: tools/aac_decode_test/Makefile, against the iPhoneOS SDK; signed with ldid. Runs as mobile over SSH.
 */
#import <Foundation/Foundation.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../../shared/AACConfig.h"
#include "../../shared/AudioRing.h"
#include "../../audiohelper/AACDecoder.h"
#include "fixture_data.h"        /* generated: g_latm_loas[], g_latm_loas_len, g_expected_aus, g_expected_au_count */

/* Strip one LOAS frame at *off; returns the AudioMuxElement pointer/len, or NULL at end. */
static const uint8_t *next_audiomuxelement(const uint8_t *data, size_t len, size_t *off, size_t *elem_len) {
    if (*off + 3 > len) return NULL;
    unsigned word = (data[*off] << 8) | data[*off + 1];
    if ((word >> 5) != 0x2B7u) return NULL;             /* lost LOAS sync */
    size_t frame_len = ((word & 0x1fu) << 8) | data[*off + 2];
    if (*off + 3 + frame_len > len) return NULL;
    const uint8_t *elem = data + *off + 3;
    *elem_len = frame_len;
    *off += 3 + frame_len;
    return elem;
}

int main(void) {
    @autoreleasepool {
        /* The fixture is 48 kHz stereo AAC-LC (its StreamMuxConfig carries ASC 11 90); configure the decoder the
         * same way the tweak would from the negotiated A2DP capability. */
        AACConfig cfg = {0};
        cfg.object_type = AAC_OBJECT_TYPE_MPEG4_LC;
        cfg.sample_rate = 48000;
        cfg.channels = 2;

        printf("{\n");
        printf("  \"test\": \"aac_decode_on_device\",\n");
        printf("  \"fixture_bytes\": %zu,\n", (size_t)g_latm_loas_len);
        printf("  \"config\": { \"codec\": \"AAC-LC\", \"rate\": %u, \"channels\": %u },\n",
               cfg.sample_rate, cfg.channels);

        AACDecoder *dec = aac_decoder_create(&cfg);
        if (!dec) {
            printf("  \"decoder_created\": false,\n");
            printf("  \"error\": \"aac_decoder_create failed (AudioConverter unavailable or config rejected)\",\n");
            printf("  \"pass\": false\n}\n");
            return 2;
        }
        printf("  \"decoder_created\": true,\n");

        /* Walk the fixture's AudioMuxElements, feeding each to the decoder as the helper would. */
        int16_t pcm[4096 * 2];
        size_t off = 0, elem_len = 0;
        const uint8_t *elem;
        unsigned elements = 0, decoded_ok = 0, buffered = 0, errors = 0;
        unsigned long long total_frames = 0, nonzero_samples = 0;
        int peak = 0;
        double sumsq = 0.0;
        unsigned seen_rate = 0, seen_channels = 0;
        int first_error = 0;
        uint64_t sequence = 0;

        while ((elem = next_audiomuxelement(g_latm_loas, g_latm_loas_len, &off, &elem_len)) != NULL) {
            if (elem_len == 0 || elem_len > AUDIO_RING_PAYLOAD) { errors++; continue; }
            AudioPacket pkt;
            memset(&pkt, 0, sizeof pkt);
            pkt.generation = 1;                 /* a single active stream */
            pkt.length = (uint32_t)elem_len;
            pkt.sequence = (uint16_t)sequence++;
            memcpy(pkt.data, elem, elem_len);
            elements++;

            unsigned frames = 0, samples = 0, rate = 0;
            int r = aac_decoder_decode(dec, &pkt, pcm, sizeof pcm / sizeof pcm[0], &frames, &samples, &rate);
            if (r == 1) { buffered++; continue; }
            if (r < 0) { errors++; if (!first_error) first_error = r; continue; }
            decoded_ok++;
            if (rate) seen_rate = rate;
            if (samples) seen_channels = 2;     /* decoder always emits interleaved stereo */
            total_frames += samples;
            for (unsigned i = 0; i < samples * 2u; ++i) {
                int v = pcm[i];
                int mag = v < 0 ? -v : v;
                if (mag > peak) peak = mag;
                if (v) nonzero_samples++;
                sumsq += (double)v * (double)v;
            }
        }

        double rms = total_frames ? sqrt(sumsq / (double)(total_frames * 2ull)) : 0.0;
        int nonsilent = peak > 64;              /* real decoded tone, not a stray sample */

        printf("  \"elements\": %u,\n", elements);
        printf("  \"expected_elements\": %u,\n", (unsigned)g_expected_au_count);
        printf("  \"decoded_ok\": %u,\n", decoded_ok);
        printf("  \"buffered\": %u,\n", buffered);
        printf("  \"errors\": %u,\n", errors);
        printf("  \"first_error\": %d,\n", first_error);
        printf("  \"pcm_frames\": %llu,\n", total_frames);
        printf("  \"nonzero_samples\": %llu,\n", nonzero_samples);
        printf("  \"peak\": %d,\n", peak);
        printf("  \"rms\": %.1f,\n", rms);
        printf("  \"decoded_rate\": %u,\n", seen_rate);
        printf("  \"decoded_channels\": %u,\n", seen_channels);

        /* Pass criteria: most elements decoded, PCM produced, non-silent, correct rate/channels. AAC-LC emits 1024
         * frames per access unit, so expect roughly expected_elements * 1024 frames (minus any priming). */
        int enough = decoded_ok >= (g_expected_au_count * 9u / 10u);
        int frames_ok = total_frames >= (unsigned long long)g_expected_au_count * 900ull;
        int rate_ok = seen_rate == cfg.sample_rate;
        int pass = dec && enough && frames_ok && nonsilent && rate_ok && errors == 0;
        printf("  \"checks\": { \"enough_decoded\": %s, \"frames_ok\": %s, \"nonsilent\": %s, \"rate_ok\": %s },\n",
               enough ? "true" : "false", frames_ok ? "true" : "false",
               nonsilent ? "true" : "false", rate_ok ? "true" : "false");
        printf("  \"pass\": %s\n}\n", pass ? "true" : "false");

        aac_decoder_destroy(dec);
        return pass ? 0 : 1;
    }
}
