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
    unsigned interrupted;     /* v9.9.2: another session holds the audio; only the interruption rules may resume */
    uint64_t interrupted_ms;  /* when it began */
    uint64_t resumed_ms;      /* when the output last came back after one, to recognise another app fighting */
    unsigned fights;          /* re-interrupted within OWN_FIGHT_WINDOW_MS of coming back, consecutively */
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
    /* v9.9.2: a refused route means the session did activate - any interruption is over; now it waits for a device. */
    if (route_refused) { h->interrupted = 0; h->next_ms = OWN_NEVER; return; }
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

/* ---- interruptions (v9.9.2) ----
 * v9.9.1 parked for the "ended" notification with an 8 s backstop. In the v9.8.89 test (11:05:31) the other app kept
 * playing, so no "ended" came: 8.8 s of silence, then the backstop's reactivation - during which Apple's A2DP code
 * suspended the PC's stream (11:05:39.816), because nothing was holding the idle guard's lease. Now:
 *   - "ended", or the other app's audio stopping (polled; it doesn't always send "ended"), resumes at once;
 *   - if the other app is still playing after OWN_TAKEOVER_MS, Speaka takes the output back - the receiver is meant to
 *     win - and if the app takes it straight back, each takeover waits twice as long (to OWN_TAKEOVER_MAX_MS), so two
 *     players can never loop;
 *   - nothing else (a START, a route change) resumes an interrupted output (own_now refuses);
 *   - a call or an alarm makes the activation fail: own_start_failed backs off and the interruption stays in force.
 * The queue and PCM are kept across the interruption, and the helper keeps the stream's lease (it decodes on). */
#define OWN_TAKEOVER_MS 3000u
#define OWN_TAKEOVER_MAX_MS 24000u
#define OWN_FIGHT_WINDOW_MS 10000u
static inline void own_interrupted(own_state_t *h, uint64_t now)
{
    h->running = 0;
    h->retaking = 1;
    if (h->resumed_ms && now >= h->resumed_ms && now - h->resumed_ms < OWN_FIGHT_WINDOW_MS) {
        if (h->fights < 8u) h->fights++;
    } else {
        h->fights = 0;
    }
    h->interrupted = 1;
    h->interrupted_ms = now;
    unsigned shift = h->fights < 3u ? h->fights : 3u;
    uint64_t wait = (uint64_t)OWN_TAKEOVER_MS << shift;
    h->next_ms = now + (wait > OWN_TAKEOVER_MAX_MS ? OWN_TAKEOVER_MAX_MS : wait);
}

/* The interruption is over: iOS said so ("ended"), or the other app's audio stopped. Resume now. */
static inline void own_interruption_cleared(own_state_t *h, uint64_t now)
{
    if (h->interrupted && !h->running) { h->next_ms = now; h->failures = 0; }
}

/* The output came back after an interruption (or started for the first time). */
static inline void own_resumed(own_state_t *h, uint64_t now)
{
    if (h->interrupted) h->resumed_ms = now;
    h->interrupted = 0;
    own_started(h, now);
}

/* Start as soon as possible: an interruption ended, a device was plugged in or picked, or the PC started a stream. */
static inline void own_now(own_state_t *h, uint64_t now)
{
    /* v9.9.2: a START, a route change or a device change must not resume over an interruption: only
     * own_interruption_cleared and the takeover deadline may. In v9.9.1 both paths reset next_ms. */
    if (h->running || h->interrupted) return;
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

/* ---- input starvation (v9.9.2) ----
 * The v9.8.85, .87 and .89 device logs: for the first ~40 s after bluetoothd starts, the PC's packets arrive at roughly
 * half of real time (13-27 a second instead of 47), in order, with contiguous RTP sequence numbers and timestamps -
 * nothing lost and nothing for a buffer to wait for. v9.9.1's 0.25 s jitter buffer filled, played, ran dry and
 * re-primed 26 times: bursts of sound separated by silence. A buffer cannot make half-rate input continuous, so the
 * helper measures the decoded PCM rate against the output rate and, while the input cannot sustain playback, holds the
 * output silent (keeping only the newest 0.25 s) instead of bursting; when the input is back at real time it plays at
 * once from that lead. Healthy streams never trip it. 1 s windows: one AAC packet is ~2% of a window, so ordinary
 * Bluetooth jitter (a packet or two either way) stays clear of both thresholds - the device's healthy windows ran
 * 96-100% of real time and its starved ones 28-57%. (500 ms windows with a 97% bar, tried first, recovered 4 s late
 * and re-tripped on jitter in the measured-pattern test: one packet was 4.3% of a window.) */
#define INPUT_WINDOW_MS 1000u
#define INPUT_STARVED_PCT 80u        /* a window below this share of real time: starved */
#define INPUT_HEALTHY_PCT 92u        /* ... and this many consecutive windows at or above this: healthy again */
#define INPUT_RECOVER_WINDOWS 1u
/* v9.9.4: a cold stream - the first on a connection, or after starvation - gets its first verdict after 400 ms, not
 * 1 s, below 70%. Starved input (31% in the v9.8.91 test) fills the 0.25 s lead in ~0.8 s, so the gate now holds
 * before the first audio instead of after a second of choppy sound; a healthy stream fills it in 0.25 s, before
 * the verdict, and a healthy first window (~19 packets) is far above 70%. A warm resume instead needs two starved
 * 1 s windows: one slow second (Windows easing in) must not gate an established stream. */
#define INPUT_FIRST_WINDOW_MS 400u
#define INPUT_FIRST_STARVED_PCT 70u
#define INPUT_WARM_STARVE_WINDOWS 2u

typedef struct {
    uint64_t window_ms;          /* start of the current window; 0 = not measuring yet */
    unsigned armed;              /* an active stream: the first decoded PCM starts the first window */
    uint64_t frames;             /* PCM frames decoded in it */
    unsigned rate;               /* the last complete window, frames per second */
    unsigned starved;            /* the output is being held because the input can't sustain it */
    unsigned healthy_windows;    /* consecutive healthy windows while starved */
    uint64_t starved_ms;         /* when the current starvation began */
    uint64_t starved_total_ms;   /* across the stream, for the log */
    unsigned starvations;
    unsigned warm;               /* v9.9.4: a resume on a connection that has already streamed healthily */
    unsigned windows;            /* complete windows so far in this stream */
    unsigned starved_windows;    /* consecutive starved windows (warm streams need INPUT_WARM_STARVE_WINDOWS) */
} input_rate_t;

/* A stream (re)started or paused: start measuring afresh. A pause is not starvation. */
static inline void input_rate_reset(input_rate_t *r, uint64_t now, int active)
{
    (void)now;
    memset(r, 0, sizeof *r);
    r->armed = active ? 1u : 0u;
}

/* Count decoded PCM; call often (with 0 frames too) so windows close on time. Returns +1 when the output must now be
 * held (starved), -1 when it may play again, 0 otherwise. output_rate: the queue's frames per second. */
static inline int input_rate_add(input_rate_t *r, unsigned frames, unsigned output_rate, uint64_t now)
{
    if (!output_rate) return 0;
    if (!r->window_ms) {                                  /* the first PCM of the stream starts measuring */
        if (!r->armed || !frames) return 0;
        r->window_ms = now ? now : 1u;
    }
    r->frames += frames;
    int first_cold = !r->warm && r->windows == 0;
    uint64_t window = first_cold ? INPUT_FIRST_WINDOW_MS : INPUT_WINDOW_MS;
    if (now < r->window_ms || now - r->window_ms < window) return 0;
    uint64_t elapsed = now - r->window_ms;
    r->rate = (unsigned)(r->frames * 1000u / elapsed);
    r->frames = 0;
    r->window_ms = now;
    uint64_t pct = (uint64_t)r->rate * 100u / output_rate;
    r->windows++;
    if (!r->starved) {
        unsigned bar = first_cold ? INPUT_FIRST_STARVED_PCT : INPUT_STARVED_PCT;
        r->starved_windows = pct < bar ? r->starved_windows + 1u : 0u;
        if (r->starved_windows >= (r->warm ? INPUT_WARM_STARVE_WINDOWS : 1u)) {
            r->starved = 1; r->healthy_windows = 0; r->starved_ms = now; r->starvations++; r->starved_windows = 0;
            return 1;
        }
        return 0;
    }
    if (pct >= INPUT_HEALTHY_PCT) {
        if (++r->healthy_windows >= INPUT_RECOVER_WINDOWS) {
            r->starved = 0;
            r->starved_total_ms += now - r->starved_ms;
            return -1;
        }
    } else {
        r->healthy_windows = 0;
    }
    return 0;
}

/* ---- which counter keeps the stream's lease (v9.9.3) ----
 * The idle guard keeps the PC's stream only while the helper's reported counter advances. Playing, that is the
 * rendered-frame count. But when the output is held on purpose - paused by an interruption, or gated because the
 * input can't sustain playback - nothing renders, so the helper reports "holding" with its decoded-frame count:
 * the lease then lasts exactly as long as the stream is still being received and decoded. v9.8.90 covered only the
 * pause; its input gate reported "playing" with a frozen render count, the lease lapsed 1.5 s into every gated
 * stretch (12:05:57, 12:07:08), and Apple's A2DP code suspended the stream ~10 s later. */
static inline int health_holding(int stream_active, int has_queue, int paused, int input_starved)
{
    return stream_active && has_queue && (paused || input_starved);
}

/* Mark a freshly reset stream as a warm resume (v9.9.4). */
static inline void input_rate_warm(input_rate_t *r) { r->warm = 1; }

/* ---- the audio session's category options per output mode (v9.9.4) ----
 * v9.8.91: TikTok's non-mixing session interrupted Speaka's (14:52:27, 14:52:32), and while it played iOS sent its
 * audio to the PC - Apple's A2DP code started the PC's stream at 14:52:32.736 and suspended it when TikTok stopped
 * (14:52:38.536). Speaka's session now mixes, so an ordinary app's audio plays alongside it instead of taking it, and
 * Speaka's session stays active - and with it a route that, for these categories, can't be the PC. Never
 * AllowBluetooth or AllowBluetoothA2DP: PlayAndRecord without them cannot reach the sending PC; "any" uses Playback
 * and keeps its own never-play check. Values are AVAudioSessionCategoryOptions (public, stable). */
#define SESSION_OPT_MIX_WITH_OTHERS 0x1u
#define SESSION_OPT_ALLOW_BLUETOOTH 0x4u
#define SESSION_OPT_DEFAULT_TO_SPEAKER 0x8u
#define SESSION_OPT_ALLOW_BLUETOOTH_A2DP 0x20u
static inline unsigned output_session_options(int speaker_mode, int mixing)
{
    return (mixing ? SESSION_OPT_MIX_WITH_OTHERS : 0u) | (speaker_mode ? SESSION_OPT_DEFAULT_TO_SPEAKER : 0u);
}

/* Mixing is the default. If another app's audio pulls the shared route away from Speaka's output and re-asserting it
 * fails, the session stops mixing once - exclusive playback takes the route back, as before v9.9.4 - for the rest of
 * the helper's session, so the two never alternate. */
typedef struct { unsigned mixing, fallbacks; } mix_state_t;
static inline void mix_init(mix_state_t *m) { m->mixing = 1; m->fallbacks = 0; }
static inline int mix_route_lost(mix_state_t *m)
{
    if (!m->mixing) return 0;
    m->mixing = 0;
    m->fallbacks++;
    return 1;
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
