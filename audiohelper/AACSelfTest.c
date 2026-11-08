/* AACSelfTest.c - see AACSelfTest.h.
 *
 *   SpeakaAudioHelper --aac-selftest [--stage=entry|create|decode1|full] [--strategy=all|raw-esds|raw-asc|raw-none|adts-none]
 *
 * It runs inside the helper because the helper is a binary already proven to launch on the phone (Theos-built,
 * arm64 + arm64e, installed by dpkg), and it is the very binary that decodes AAC in production.
 *
 * stderr gets one marker line per step, written with write() so nothing is lost if the process is killed.
 * stdout gets the JSON report, unbuffered. Exit status: 0 if the production strategy decoded the fixture correctly,
 * 1 if it did not, 2 for a usage error, 3 if no converter could be created at --stage=create.
 *
 * The fixture is a 440 Hz tone, so correctness is checked against the ideal tone itself: after the first 4096
 * frames (encoder delay and priming), the decoded signal is fitted to a 440 Hz sine of free phase and amplitude, and
 * the fit's signal-to-residual ratio must reach 20 dB. ffmpeg's own decoder scores 34.6 dB on this fixture; a wrong
 * configuration or misframed packets give garbage near 0 dB. */
#include "AACSelfTest.h"
#include "AACDecoder.h"
#include "AACSelfTestFixture.h"
#include "../shared/AACConfig.h"
#include "../shared/AudioRing.h"
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define MAX_ELEMENTS 64u
#define STEADY_SKIP 4096u          /* frames: encoder delay + decoder priming */
#define TAIL_SKIP 1024u            /* frames: the encoder's final padding */
#define TONE_HZ 440.0

typedef struct { const char *name; AACFraming framing; AACCookie cookie; } Strategy;
static const Strategy kStrategies[] = {
    {"raw-esds", AAC_FRAMING_RAW, AAC_COOKIE_ESDS},    /* the production default; must be first */
    {"raw-asc", AAC_FRAMING_RAW, AAC_COOKIE_ASC},
    {"raw-none", AAC_FRAMING_RAW, AAC_COOKIE_NONE},
    {"adts-none", AAC_FRAMING_ADTS, AAC_COOKIE_NONE},  /* the pre-v9.8.8x behaviour, kept for comparison */
};
#define STRATEGY_COUNT (sizeof kStrategies / sizeof kStrategies[0])

static int16_t g_pcm[MAX_ELEMENTS * 1024u * 2u];   /* every decoded stereo frame of one strategy */

static void mark(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void mark(const char *fmt, ...) {
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line - 1, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n > sizeof line - 2) n = (int)(sizeof line - 2);
    line[n++] = '\n';
    ssize_t ignored = write(STDERR_FILENO, line, (size_t)n);
    (void)ignored;
}

/* The fixture is LOAS-framed (0x2B7 sync, 13-bit length); A2DP delivers bare AudioMuxElements, so strip LOAS. */
static unsigned split_elements(const uint8_t *data, size_t len, const uint8_t **elem, size_t *elem_len, unsigned max) {
    size_t off = 0;
    unsigned n = 0;
    while (off + 3 <= len && n < max) {
        unsigned word = ((unsigned)data[off] << 8) | data[off + 1];
        if ((word >> 5) != 0x2B7u) break;
        size_t frame_len = ((word & 0x1fu) << 8) | data[off + 2];
        if (off + 3 + frame_len > len) break;
        elem[n] = data + off + 3;
        elem_len[n] = frame_len;
        n++;
        off += 3 + frame_len;
    }
    return n;
}

typedef struct {
    const char *name;
    int created;
    AACDecoderCreateStatus cs;
    unsigned elements, decoded_ok, buffered, errors;
    int first_error;
    int32_t first_error_status;
    unsigned long long pcm_frames;
    unsigned rate;
    int peak;
    double rms, hz, snr_db;
    int pass;
} Result;

static void analyse(Result *r) {
    unsigned long long frames = r->pcm_frames;
    r->peak = 0; r->rms = 0; r->hz = 0; r->snr_db = -99.0;
    if (frames <= STEADY_SKIP + TAIL_SKIP + 1000u || !r->rate) return;
    unsigned long long start = STEADY_SKIP, end = frames - TAIL_SKIP, n = end - start;
    double sumsq = 0, s = 0, c = 0, w = 2.0 * M_PI * TONE_HZ / (double)r->rate;
    unsigned long long crossings = 0;
    for (unsigned long long i = start; i < end; ++i) {
        int v = g_pcm[i * 2];                          /* left channel */
        int mag = v < 0 ? -v : v;
        if (mag > r->peak) r->peak = mag;
        sumsq += (double)v * v;
        if (i > start && g_pcm[(i - 1) * 2] < 0 && v >= 0) crossings++;
        double t = (double)(i - start);
        s += v * sin(w * t);
        c += v * cos(w * t);
    }
    r->rms = sqrt(sumsq / (double)n);
    r->hz = (double)crossings * (double)r->rate / (double)n;
    double amp = 2.0 * sqrt(s * s + c * c) / (double)n, phase = atan2(c, s), residual = 0;
    for (unsigned long long i = start; i < end; ++i) {
        double e = g_pcm[i * 2] - amp * sin(w * (double)(i - start) + phase);
        residual += e * e;
    }
    r->snr_db = residual > 0 ? 10.0 * log10(sumsq / residual) : 99.0;
}

/* Decode `count` elements (0 = all) with one strategy. */
static void run_strategy(const Strategy *st, const AACConfig *cfg, const uint8_t **elem, const size_t *elem_len,
                         unsigned elements, unsigned count, Result *r) {
    memset(r, 0, sizeof *r);
    r->name = st->name;
    AACDecoderOptions opt;
    aac_decoder_default_options(&opt);
    opt.framing = st->framing;
    opt.cookie = st->cookie;
    mark("STAGE before_create strategy=%s", st->name);
    AACDecoder *dec = aac_decoder_create_ex(cfg, &opt, &r->cs);
    mark("STAGE after_create strategy=%s created=%d software=%d specific_status=%d new_status=%d cookie_status=%d",
         st->name, dec != NULL, r->cs.used_software, r->cs.specific_status, r->cs.new_status, r->cs.cookie_status);
    if (!dec) return;
    r->created = 1;
    unsigned limit = count ? count : elements;
    unsigned long long room = sizeof g_pcm / sizeof g_pcm[0] / 2u;
    int16_t pcm[1024 * 2];
    for (unsigned i = 0; i < limit && i < elements; ++i) {
        AudioPacket pkt;
        memset(&pkt, 0, sizeof pkt);
        pkt.generation = 1;
        pkt.sequence = (uint16_t)i;
        pkt.timestamp = i * 1024u;
        pkt.length = (uint32_t)elem_len[i];
        memcpy(pkt.data, elem[i], elem_len[i]);
        r->elements++;
        if (i == 0) mark("STAGE before_first_decode strategy=%s", st->name);
        unsigned frames = 0, samples = 0, rate = 0;
        int result = aac_decoder_decode(dec, &pkt, pcm, sizeof pcm / sizeof pcm[0], &frames, &samples, &rate);
        int32_t status = aac_decoder_last_status(dec);
        mark("DECODE strategy=%s i=%u len=%zu result=%d frames=%u status=%d", st->name, i, elem_len[i], result,
             samples, status);
        if (result == 1) { r->buffered++; continue; }
        if (result < 0) {
            r->errors++;
            if (!r->first_error) { r->first_error = result; r->first_error_status = status; }
            continue;
        }
        r->decoded_ok++;
        r->rate = rate;
        if (r->pcm_frames + samples <= room) memcpy(g_pcm + r->pcm_frames * 2u, pcm, (size_t)samples * 2u * sizeof(int16_t));
        r->pcm_frames += samples;
    }
    aac_decoder_destroy(dec);
    analyse(r);
    unsigned long long need = elements > 2 ? (unsigned long long)(elements - 2u) * 1024u : 1u;
    r->pass = r->created && !r->errors && r->rate == cfg->sample_rate && r->pcm_frames >= need &&
              r->peak >= 1500 && r->rms >= 1000.0 && r->hz >= 430.0 && r->hz <= 450.0 && r->snr_db >= 20.0;
    mark("STAGE strategy_done strategy=%s decoded=%u errors=%u frames=%llu peak=%d rms=%.0f hz=%.1f snr_db=%.1f pass=%d",
         st->name, r->decoded_ok, r->errors, r->pcm_frames, r->peak, r->rms, r->hz, r->snr_db, r->pass);
}

static void print_result(const Result *r, int last) {
    printf("    { \"strategy\": \"%s\", \"created\": %s, \"software_decoder\": %s, \"specific_status\": %d, "
           "\"new_status\": %d, \"cookie_status\": %d, \"cookie_set\": %s,\n",
           r->name, r->created ? "true" : "false", r->cs.used_software ? "true" : "false", r->cs.specific_status,
           r->cs.new_status, r->cs.cookie_status, r->cs.cookie_set ? "true" : "false");
    printf("      \"elements\": %u, \"decoded_ok\": %u, \"buffered\": %u, \"errors\": %u, \"first_error\": %d, "
           "\"first_error_status\": %d,\n", r->elements, r->decoded_ok, r->buffered, r->errors, r->first_error,
           r->first_error_status);
    printf("      \"pcm_frames\": %llu, \"rate\": %u, \"channels\": 2, \"peak\": %d, \"rms\": %.1f, "
           "\"dominant_hz\": %.1f, \"tone_snr_db\": %.1f, \"pass\": %s }%s\n",
           r->pcm_frames, r->rate, r->peak, r->rms, r->hz, r->snr_db, r->pass ? "true" : "false", last ? "" : ",");
}

int aac_selftest_main(int argc, const char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    mark("SPEAKA_SELFTEST BEGIN built=%s %s pid=%d exe=%s", __DATE__, __TIME__, (int)getpid(), argv[0]);
    mark("STAGE entry");
    const char *stage = "full", *only = "all";
    for (int i = 2; i < argc; ++i) {
        if (!strncmp(argv[i], "--stage=", 8)) stage = argv[i] + 8;
        else if (!strncmp(argv[i], "--strategy=", 11)) only = argv[i] + 11;
        else { mark("usage: SpeakaAudioHelper --aac-selftest [--stage=entry|create|decode1|full] [--strategy=NAME|all]"); return 2; }
    }
    if (!strcmp(stage, "entry")) { mark("STAGE exit code=0"); return 0; }

    const uint8_t *elem[MAX_ELEMENTS];
    size_t elem_len[MAX_ELEMENTS];
    unsigned elements = split_elements(g_selftest_latm, g_selftest_latm_len, elem, elem_len, MAX_ELEMENTS);
    mark("STAGE fixture bytes=%zu elements=%u expected=%u", (size_t)g_selftest_latm_len, elements,
         g_selftest_expected_elements);
    if (elements != g_selftest_expected_elements) { mark("STAGE exit code=1 reason=fixture"); return 1; }

    AACConfig cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.object_type = AAC_OBJECT_TYPE_MPEG4_LC;     /* the fixture's StreamMuxConfig: AAC-LC, 48 kHz, stereo */
    cfg.sample_rate = 48000;
    cfg.channels = 2;

    if (!strcmp(stage, "create")) {
        AACDecoderCreateStatus cs;
        AACDecoder *dec = aac_decoder_create_ex(&cfg, NULL, &cs);
        mark("STAGE after_create created=%d software=%d specific_status=%d new_status=%d cookie_status=%d",
             dec != NULL, cs.used_software, cs.specific_status, cs.new_status, cs.cookie_status);
        aac_decoder_destroy(dec);
        mark("STAGE exit code=%d", dec ? 0 : 3);
        return dec ? 0 : 3;
    }
    unsigned count = !strcmp(stage, "decode1") ? 1u : 0u;
    if (!count && strcmp(stage, "full")) { mark("unknown --stage=%s", stage); return 2; }

    Result results[STRATEGY_COUNT];
    unsigned ran = 0;
    int default_pass = 0, default_ran = 0, all_pass = 1;
    for (unsigned s = 0; s < STRATEGY_COUNT; ++s) {
        if (strcmp(only, "all") && strcmp(only, kStrategies[s].name)) continue;
        run_strategy(&kStrategies[s], &cfg, elem, elem_len, elements, count, &results[ran]);
        if (s == 0) { default_ran = 1; default_pass = results[ran].pass; }
        if (!results[ran].pass) all_pass = 0;
        ran++;
    }
    if (!ran) { mark("unknown --strategy=%s", only); return 2; }

    mark("STAGE json");
    printf("{\n  \"test\": \"aac_selftest\",\n  \"built\": \"%s %s\",\n  \"stage\": \"%s\",\n", __DATE__, __TIME__, stage);
    printf("  \"fixture\": { \"bytes\": %zu, \"elements\": %u, \"codec\": \"AAC-LC\", \"rate\": 48000, \"channels\": 2, "
           "\"tone_hz\": 440 },\n", (size_t)g_selftest_latm_len, elements);
    printf("  \"results\": [\n");
    for (unsigned i = 0; i < ran; ++i) print_result(&results[i], i + 1 == ran);
    printf("  ],\n  \"working_strategies\": [");
    int first = 1;
    for (unsigned i = 0; i < ran; ++i)
        if (results[i].pass) { printf("%s\"%s\"", first ? "" : ", ", results[i].name); first = 0; }
    printf("],\n  \"production_strategy\": \"raw-esds\",\n");
    /* At --stage=decode1 a single access unit can't meet the full-fixture checks; report, don't judge. */
    /* The verdict is the production strategy's when it ran; otherwise every requested strategy must pass. */
    int pass = count ? 0 : (default_ran ? default_pass : all_pass);
    printf("  \"pass\": %s\n}\n", count ? "null" : (pass ? "true" : "false"));
    int code = count ? 0 : (pass ? 0 : 1);
    mark("STAGE exit code=%d", code);
    return code;
}
