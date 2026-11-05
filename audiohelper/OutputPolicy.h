/* OutputPolicy.h - where the helper plays, and when it lets go: plain C with no Apple frameworks, so the host tests
 * run it, and the app shows the same three choices (SPTweakClient.m). */
#pragma once
#include <stddef.h>
#include <string.h>
#include <strings.h>
#include <stdint.h>

/* ---- where to play: the three modes and the never-play list ---- */
/* OutputPolicy.h - v8.9 audio output policy for BTPSAudioHelper.
 *
 * Pure C with no Apple frameworks, so the host tests can run it. AudioHelper.m classifies every
 * AVAudioSession output port into a output_kind_t using the real AVAudioSessionPort constants and then asks
 * this policy whether playback may start, or continue, on the current route.
 *
 *   speaker     exactly the iPhone's built-in speaker (the v8.8.1 behaviour, and the default)
 *   headphones  only WIRED or USB headphones/earbuds; waits if none is plugged in (v8.9.4: never Bluetooth)
 *   any         whatever iOS is using (Control Center / route picker), except devices on the never-play list
 *
 * Why headphones mode excludes Bluetooth (v8.9.4): the PC that sends the audio is itself connected over Bluetooth,
 * so iOS lists it as an A2DP output, and because it connects last iOS routes to it. The 21:39 log showed every
 * headphones-mode start going to LAPTOP-..., after which the PC closed the stream (5 s and 15 s later), and
 * stopping that output made the phone send SUSPEND. The never-play list remains for "any" mode.
 */

enum { OUTPUT_MODE_SPEAKER = 0, OUTPUT_MODE_HEADPHONES = 1, OUTPUT_MODE_ANY = 2 };

typedef enum {
    OUTPUT_PORT_OTHER = 0,      /* AirPlay, CarPlay, HDMI, line out, anything unknown */
    OUTPUT_PORT_SPEAKER = 1,    /* built-in speaker */
    OUTPUT_PORT_RECEIVER = 2,   /* built-in earpiece */
    OUTPUT_PORT_HEADPHONES = 3, /* wired or USB headphones / earbuds */
    OUTPUT_PORT_BLUETOOTH = 4,  /* Bluetooth A2DP / LE / HFP: includes the sending PC, which iOS lists as an output */
} output_kind_t;

typedef struct { output_kind_t kind; const char *uid; const char *name; } output_port_t;
typedef struct { const char *uid; const char *name; } output_never_t;

enum {
    OUTPUT_ROUTE_OK = 0,
    OUTPUT_ROUTE_NO_OUTPUT = 1,
    OUTPUT_ROUTE_NOT_SPEAKER = 2,
    OUTPUT_ROUTE_NOT_HEADPHONES = 3,
    OUTPUT_ROUTE_NEVER_LISTED = 4,
};

#define OUTPUT_MAX_PORTS 8u
#define OUTPUT_MAX_NEVER 16u

/* Unknown or missing values fall back to the speaker, the tested default. */
static inline int output_mode_from_string(const char *s)
{
    if (s && strcmp(s, "headphones") == 0) return OUTPUT_MODE_HEADPHONES;
    if (s && strcmp(s, "any") == 0) return OUTPUT_MODE_ANY;
    return OUTPUT_MODE_SPEAKER;
}

static inline const char *output_mode_name(int mode)
{
    return mode == OUTPUT_MODE_HEADPHONES ? "headphones" : mode == OUTPUT_MODE_ANY ? "any" : "speaker";
}

static inline int output_same(const char *a, const char *b)
{
    return a && b && a[0] && b[0] && strcasecmp(a, b) == 0;
}

/* The iPhone's own speaker and earpiece can never be excluded. */
static inline int output_never_listed(const output_port_t *port, const output_never_t *never, size_t count)
{
    if (port->kind == OUTPUT_PORT_SPEAKER || port->kind == OUTPUT_PORT_RECEIVER) return 0;
    for (size_t i = 0; i < count; i++)
        if (output_same(port->uid, never[i].uid) || output_same(port->name, never[i].name)) return 1;
    return 0;
}

/* Returns OUTPUT_ROUTE_OK when playback may use this route; otherwise the reason. *offending receives the
 * index of the port that caused a refusal (0 when the reason is not tied to one port). */
static inline int output_route_check(int mode, const output_port_t *ports, size_t count,
                                       const output_never_t *never, size_t never_count, size_t *offending)
{
    if (offending) *offending = 0;
    if (!ports || count == 0) return OUTPUT_ROUTE_NO_OUTPUT;
    if (mode == OUTPUT_MODE_HEADPHONES || mode == OUTPUT_MODE_ANY) {
        for (size_t i = 0; i < count; i++) {
            if (output_never_listed(&ports[i], never, never_count)) {
                if (offending) *offending = i;
                return OUTPUT_ROUTE_NEVER_LISTED;
            }
            if (mode == OUTPUT_MODE_HEADPHONES && ports[i].kind != OUTPUT_PORT_HEADPHONES) {
                if (offending) *offending = i;
                return OUTPUT_ROUTE_NOT_HEADPHONES;
            }
        }
        return OUTPUT_ROUTE_OK;
    }
    return count == 1 && ports[0].kind == OUTPUT_PORT_SPEAKER ? OUTPUT_ROUTE_OK : OUTPUT_ROUTE_NOT_SPEAKER;
}

static inline const char *output_route_reason(int code)
{
    switch (code) {
    case OUTPUT_ROUTE_OK: return "ok";
    case OUTPUT_ROUTE_NO_OUTPUT: return "no output";
    case OUTPUT_ROUTE_NOT_SPEAKER: return "speaker not selected";
    case OUTPUT_ROUTE_NOT_HEADPHONES: return "no wired headphones plugged in";
    case OUTPUT_ROUTE_NEVER_LISTED: return "on the never-play list";
    default: return "unknown";
    }
}

/* ---- when to let go: holding the output through stream changes ---- */
/* OutputPolicy.h - v9.1: when BTPSAudioHelper may start its output, and when it lets go of it.
 *
 * Pure C with no Apple frameworks, so the host tests can run it (the same pattern as OutputPolicy.h).
 *
 * Why: the 23:33 export and its screen recording. With wired earbuds plugged in and "iPhone speaker" selected,
 * Control Center switched between "Headphones" and LAPTOP-UGIVBIA5 every 0.71 s for minutes and nothing played.
 * It was a feedback loop between the helper and iOS:
 *
 *   1. iOS lists the sending PC as a Bluetooth output, and Apple's own A2DP code in bluetoothd starts and suspends the
 *      PC's stream as if the iPhone were sending to it. All 25 STARTs and 25 SUSPENDs in the loop came from the
 *      phone (the PC only accepted them), each one right after the helper activated or deactivated its audio session.
 *   2. The tweak reports each START/SUSPEND as a new stream generation (+3 per cycle: 652, 655, 658, ...).
 *   3. The helper treated every generation change as a new stream: it stopped its AudioQueue, deactivated its session,
 *      and reactivated it about 0.4 s later for the next packets. Each of those session changes re-routed iOS, which
 *      made bluetoothd send the next START or SUSPEND. No AudioQueue lived longer than 30 ms.
 *
 * The policy breaks the loop on the helper's side, whichever iOS mechanism is behind step 1:
 *   - A running output is KEPT through a stream gap: a SUSPEND, a close and reopen, a track change or a pause. Only
 *     stale PCM is dropped. The audio session therefore never changes because the stream did. The output is released
 *     only after HOLD_MS without media, which also covers Windows closing and reopening the stream after the
 *     phone suspends it (21:35 log in TESTING-v8.9.4: SUSPEND, CLOSE 3.7 s later, reopen 6.1 s later).
 *   - After the helper lets go on its own (hold expired, route refused, start or enqueue error), it does not activate
 *     again for HOLD_REARM_MS, so it can never switch iOS's audio around faster than that, whatever the cause.
 *   - Starting from idle needs audible PCM. Exact digital silence (what the PC sends when the phone restarts a stream
 *     the PC had paused: 23:33:36 to 23:33:44, peak=0) never activates the session.
 * A user action (a new output setting) or a real device change (headphones plugged in or unplugged) skips the wait.
 */

#define HOLD_MS  8000u   /* keep a running output this long without media */
#define HOLD_REARM_MS 3000u   /* minimum time between letting go on our own and the next activation */

typedef struct {
    uint64_t media_ms;   /* last decoded packet written to the running output */
    uint64_t rearm_ms;   /* automatic starts are refused before this time; 0 = no wait */
    unsigned running;    /* an AudioQueue is running on an active session */
} hold_state_t;

enum {
    HOLD_START_OK = 0,
    HOLD_START_RUNNING = 1,   /* already playing: just write the PCM */
    HOLD_START_REARM = 2,     /* the helper let go less than HOLD_REARM_MS ago */
    HOLD_START_SILENT = 3,    /* nothing audible to play yet */
};

/* Why the helper releases its output. Only AUTOMATIC releases arm the rearm wait. */
enum { HOLD_RELEASE_AUTOMATIC = 1, HOLD_RELEASE_REQUESTED = 0 };

static inline void hold_started(hold_state_t *h, uint64_t now)
{
    h->running = 1;
    h->media_ms = now;
    h->rearm_ms = 0;
}

/* A decoded packet of the current stream reached the running output (silence counts: the PC is still sending). */
static inline void hold_media(hold_state_t *h, uint64_t now)
{
    if (h->running) h->media_ms = now;
}

/* A new stream generation while playing: keep the output. Returns 1 when the caller must keep the queue and session
 * and drop only the stale PCM; 0 when nothing is running. It never asks for a stop: that is the whole point. */
static inline int hold_generation_changed(const hold_state_t *h)
{
    return h->running ? 1 : 0;
}

/* True once a running output has had no media for the hold time. */
static inline int hold_expired(const hold_state_t *h, uint64_t now)
{
    return h->running && now >= h->media_ms && now - h->media_ms >= HOLD_MS;
}

static inline void hold_released(hold_state_t *h, uint64_t now, int automatic)
{
    h->running = 0;
    h->rearm_ms = automatic ? now + HOLD_REARM_MS : 0;
}

/* A user action or a real device change: the next start may happen at once. */
static inline void hold_rearm_now(hold_state_t *h)
{
    h->rearm_ms = 0;
}

/* May the helper start its output for a decoded packet? audible = the packet decoded to at least one nonzero sample. */
static inline int hold_may_start(const hold_state_t *h, uint64_t now, int audible)
{
    if (h->running) return HOLD_START_RUNNING;
    if (h->rearm_ms && now < h->rearm_ms) return HOLD_START_REARM;
    if (!audible) return HOLD_START_SILENT;
    return HOLD_START_OK;
}

static inline const char *hold_start_reason(int code)
{
    switch (code) {
    case HOLD_START_OK: return "ok";
    case HOLD_START_RUNNING: return "running";
    case HOLD_START_REARM: return "rearm_wait";
    case HOLD_START_SILENT: return "silent_stream";
    default: return "unknown";
    }
}

/* AVAudioSessionRouteChangeReason values (public, stable since iOS 6). A route change the helper did not cause and
 * that is a real device arrival or removal lets the next start skip the rearm wait. */
enum { HOLD_ROUTE_NEW_DEVICE = 1, HOLD_ROUTE_OLD_DEVICE_GONE = 2 };

static inline int hold_route_is_device_change(long reason)
{
    return reason == HOLD_ROUTE_NEW_DEVICE || reason == HOLD_ROUTE_OLD_DEVICE_GONE;
}

static inline const char *hold_route_reason_name(long reason)
{
    switch (reason) {
    case 0: return "unknown";
    case 1: return "new_device";
    case 2: return "old_device_gone";
    case 3: return "category_change";
    case 4: return "override";
    case 6: return "wake_from_sleep";
    case 7: return "no_suitable_route";
    case 8: return "route_configuration_change";
    default: return "other";
    }
}
