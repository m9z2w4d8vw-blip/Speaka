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

/* ---- owning the output for the whole Speaka session (v9.9) ---- */
/* Why: the v9.8.86 device test. iOS treats the sending PC as a Bluetooth output, so Apple's A2DP code suspends "its"
 * stream whenever the helper activates its audio session (the route moves to the speaker) and starts it whenever the
 * helper deactivates. v9.1 released the session after 8 s without media, so every pause longer than that became a
 * loop - release, phone START, 3 s rearm, activation, phone SUSPEND, 8 s, release - 13 STARTs and 13 SUSPENDs from
 * the phone in two minutes, with resuming on the PC landing somewhere in the middle. While the session simply stayed
 * active (09:34:43-09:35:08), 25 s decoded without a single phone-initiated SUSPEND.
 *
 * So the helper owns the output for as long as the Speaka session lasts:
 *   - it activates as soon as it is connected - the tweak connects when the PC configures the speaker, before the
 *     stream starts - so the PC's START moves nothing;
 *   - it keeps the session and queue through pauses, track changes, START/SUSPEND, close and reopen (the queue plays
 *     silence when there is no PCM), and never lets go for lack of media;
 *   - if anything takes the output (another app's session, an error) it takes it back: OWN_RETAKE_MS after another
 *     app, with a backoff after a failure, at once when an interruption ends or a device changes. A phone call or an
 *     alarm makes the attempts fail, so they simply back off until its interruption ends;
 *   - a route the selected mode can't use (headphones mode with nothing plugged in) waits for a device change rather
 *     than retrying, which would toggle the session;
 *   - it lets go only when the session ends (Stop, a bluetoothd restart: the helper's connection closes), or
 *     OWN_CLOSED_RELEASE_MS after the PC closed its stream and opened none, when there is no stream left to start. */

#define OWN_RETAKE_MS 250u              /* another session took the output: take it back this soon */
#define OWN_RETRY_MS 1000u              /* after a failed start, times the consecutive failures ... */
#define OWN_RETRY_MAX_MS 4000u          /* ... up to this */
#define OWN_CLOSED_RELEASE_MS 30000u    /* the PC closed its stream: keep the output this long for a reopen */
#define OWN_NEVER UINT64_MAX            /* wait for a device change, a stream change or a user action */

typedef struct {
    unsigned running;    /* a queue is running on an active session */
    uint64_t next_ms;    /* the earliest (re)start attempt; OWN_NEVER waits for an event */
    unsigned failures;   /* consecutive failed starts, for the backoff */
    unsigned retaking;   /* something else took the output, and the next start takes it back */
    uint64_t closed_ms;  /* when the PC closed its stream; 0 while a stream is configured */
    unsigned released;   /* let go after the close grace; the next configuration owns again */
} own_state_t;

static inline void own_init(own_state_t *h) { memset(h, 0, sizeof *h); }   /* next_ms 0: start at once */

static inline int own_closed_expired(const own_state_t *h, uint64_t now)
{
    return h->closed_ms && now >= h->closed_ms && now - h->closed_ms >= OWN_CLOSED_RELEASE_MS;
}

static inline int own_may_start(const own_state_t *h, uint64_t now)
{
    return !h->running && !h->released && h->next_ms != OWN_NEVER && now >= h->next_ms &&
           !own_closed_expired(h, now);
}

static inline void own_started(own_state_t *h, uint64_t now)
{
    (void)now;
    h->running = 1;
    h->failures = 0;
    h->retaking = 0;
    h->next_ms = 0;
}

/* route_refused: the selected mode's output isn't there (start_output's -2): wait for a device change. */
static inline void own_start_failed(own_state_t *h, uint64_t now, int route_refused)
{
    h->running = 0;
    if (route_refused) { h->next_ms = OWN_NEVER; return; }
    if (h->failures < 16u) h->failures++;
    uint64_t wait = (uint64_t)OWN_RETRY_MS * h->failures;
    h->next_ms = now + (wait > OWN_RETRY_MAX_MS ? OWN_RETRY_MAX_MS : wait);
}

/* Another session took the output (an interruption began): take it back shortly. */
static inline void own_taken(own_state_t *h, uint64_t now)
{
    h->running = 0;
    h->retaking = 1;
    h->next_ms = now + OWN_RETAKE_MS;
}

/* Start as soon as possible: an interruption ended, a device was plugged in or picked, or the PC started a stream. */
static inline void own_now(own_state_t *h, uint64_t now)
{
    if (h->running) return;
    h->next_ms = now;
    h->failures = 0;
}

/* The output was stopped on purpose to be rebuilt (new output settings, media services reset): rebuild at once. */
static inline void own_restart(own_state_t *h, uint64_t now)
{
    h->running = 0;
    h->failures = 0;
    h->next_ms = now;
}

/* The PC's stream (AUDIO_STREAM_* in AudioRing.h): any open state keeps or regains ownership; a close starts the
 * release grace. A pause is an open state: that is the point. */
static inline void own_stream(own_state_t *h, unsigned state, uint64_t now)
{
    if (state == 4u) {                                     /* AUDIO_STREAM_CLOSED */
        if (!h->closed_ms) h->closed_ms = now ? now : 1u;
    } else if (state != 0u) {                              /* configured, streaming, paused */
        h->closed_ms = 0;
        if (h->released) { h->released = 0; h->next_ms = now; h->failures = 0; }
        else if (state == 2u) own_now(h, now);             /* a START is a good moment to retry a failed output */
    }
}

/* True when a running output should be let go: the PC closed its stream and opened none since. */
static inline int own_should_release(const own_state_t *h, uint64_t now)
{
    return h->running && own_closed_expired(h, now);
}

static inline void own_released(own_state_t *h)
{
    h->running = 0;
    h->released = 1;
}

/* AVAudioSessionRouteChangeReason values (public, stable since iOS 6). A route change the helper did not cause and
 * that is a real device arrival or removal lets a waiting output start at once. */
enum { OWN_ROUTE_NEW_DEVICE = 1, OWN_ROUTE_OLD_DEVICE_GONE = 2 };

static inline int own_route_is_device_change(long reason)
{
    return reason == OWN_ROUTE_NEW_DEVICE || reason == OWN_ROUTE_OLD_DEVICE_GONE;
}

static inline const char *own_route_reason_name(long reason)
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
