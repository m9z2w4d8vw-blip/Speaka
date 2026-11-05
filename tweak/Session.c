/* Session.c - Start and Stop, requested by the app and handled inside bluetoothd (since v8.9.2).
 *
 * The notification handler runs on its own serial dispatch queue, never on a Bluetooth hook or receive thread.
 * A request is consumed (deleted) before it is acted on, so it is honoured at most once. Switches are written
 * and read back before the restart; if anything cannot be verified, Bluetooth is NOT restarted.
 *
 * The restart is SIGKILL to this very process. launchd only sees "bluetoothd exited on signal 9", exactly as
 * after `killall -9 bluetoothd`, which reliably relaunches it on the tested phone. bluetoothd runs as mobile
 * (which is why Settings can read the preferences it writes), so system-domain `launchctl kickstart` is not
 * available to it; v8.9.1 tried that and is reverted. Killing yourself needs no privilege and no sandbox rule.
 *
 * History: v8.9.0's "Still waiting" reports were NOT this restart failing. The running bluetoothd still had the
 * pre-v8.9 tweak loaded (installing a package does not restart daemons), so nothing was listening. The package
 * postinst now restarts bluetoothd so a freshly installed tweak is always the one running.
 */
#include "Tweak.h"
#include "../shared/SpeakaShared.h"
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <notify.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

#define SESSION_DOMAIN CFSTR(SPEAKA_PREFS_DOMAIN)

static dispatch_queue_t g_session_queue;
static int g_session_token = -1;
static int g_session_restarting;               /* only touched on g_session_queue */

static double session_now(void)
{
    return (double)time(NULL);
}

static int session_copy_string(CFDictionaryRef d, CFStringRef key, char *out, size_t cap)
{
    CFTypeRef v = CFDictionaryGetValue(d, key);
    return v && CFGetTypeID(v) == CFStringGetTypeID() &&
           CFStringGetCString((CFStringRef)v, out, (CFIndex)cap, kCFStringEncodingUTF8);
}

static int session_copy_double(CFDictionaryRef d, CFStringRef key, double *out)
{
    CFTypeRef v = CFDictionaryGetValue(d, key);
    return v && CFGetTypeID(v) == CFNumberGetTypeID() &&
           CFNumberGetValue((CFNumberRef)v, kCFNumberDoubleType, out);
}

/* Reads {action, nonce, time} under `key` and deletes it.
 * 0 = absent, 1 = read, -1 = could not be deleted (never act on it), -2 = malformed. */
static int session_take_record(CFStringRef key, char action[16], char nonce[48], double *when)
{
    (void)CFPreferencesAppSynchronize(SESSION_DOMAIN);
    CFPropertyListRef v = CFPreferencesCopyAppValue(key, SESSION_DOMAIN);
    if (!v) return 0;
    int ok = CFGetTypeID(v) == CFDictionaryGetTypeID() &&
             session_copy_string((CFDictionaryRef)v, CFSTR("action"), action, 16) &&
             session_copy_string((CFDictionaryRef)v, CFSTR("nonce"), nonce, 48) &&
             session_copy_double((CFDictionaryRef)v, CFSTR("time"), when);
    CFRelease(v);
    CFPreferencesSetAppValue(key, NULL, SESSION_DOMAIN);
    if (!CFPreferencesAppSynchronize(SESSION_DOMAIN)) return -1;
    return ok ? 1 : -2;
}

static int session_read_bool(CFStringRef key, int *out)
{
    CFPropertyListRef v = CFPreferencesCopyAppValue(key, SESSION_DOMAIN);
    int ok = v && CFGetTypeID(v) == CFBooleanGetTypeID();
    if (ok) *out = CFBooleanGetValue((CFBooleanRef)v) ? 1 : 0;
    if (v) CFRelease(v);
    return ok;
}

/* Sets (start) or clears (stop) exactly the two existing one-shot switches, plus a marker that lets the
 * relaunched daemon report back to Settings. Returns 1 only if everything reads back as written. */
static int session_write_switches(int start, const char *nonce, double when)
{
    CFBooleanRef arm = start ? kCFBooleanTrue : kCFBooleanFalse;
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE), arm, SESSION_DOMAIN);
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_SINK_ONCE), arm, SESSION_DOMAIN);
    /* The v8.2 gate refuses while the retired legacy flag is ON, so Start turns it off. */
    if (start) CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_EXPERIMENTS), kCFBooleanFalse, SESSION_DOMAIN);

    CFStringRef action = start ? CFSTR("start") : CFSTR("stop");
    CFStringRef nonce_s = CFStringCreateWithCString(kCFAllocatorDefault, nonce, kCFStringEncodingUTF8);
    CFNumberRef when_n = CFNumberCreate(kCFAllocatorDefault, kCFNumberDoubleType, &when);
    int marker_ok = 0;
    if (nonce_s && when_n) {
        const void *keys[3] = { CFSTR("action"), CFSTR("nonce"), CFSTR("time") };
        const void *values[3] = { action, nonce_s, when_n };
        CFDictionaryRef marker = CFDictionaryCreate(kCFAllocatorDefault, keys, values, 3,
                                                    &kCFTypeDictionaryKeyCallBacks,
                                                    &kCFTypeDictionaryValueCallBacks);
        if (marker) {
            CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_SESSION_PENDING), marker, SESSION_DOMAIN);
            CFRelease(marker);
            marker_ok = 1;
        }
    }
    if (nonce_s) CFRelease(nonce_s);
    if (when_n) CFRelease(when_n);
    if (!marker_ok || !CFPreferencesAppSynchronize(SESSION_DOMAIN)) return 0;

    int seven = -1, decisive = -1, legacy = 1;
    if (!session_read_bool(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE), &seven) || seven != start) return 0;
    if (!session_read_bool(CFSTR(SPEAKA_KEY_SINK_ONCE), &decisive) || decisive != start) return 0;
    if (start && (!session_read_bool(CFSTR(SPEAKA_KEY_EXPERIMENTS), &legacy) || legacy)) return 0;
    return 1;
}

static void session_handle_request(void)
{
    if (g_session_restarting) return;
    char action[16] = {0}, nonce[48] = {0};
    double when = 0.0;
    int got = session_take_record(CFSTR(SPEAKA_KEY_SESSION_REQUEST), action, nonce, &when);
    if (got == 0) {
        SpeakaLogLine("V89_SESSION notification without a request; ignored");
        return;
    }
    int act = got == 1 ? session_parse_action(action) : SESSION_ACTION_NONE;
    const char *why = got == -1 ? "request could not be consumed" :
                      got == -2 ? "malformed request" :
                      act == SESSION_ACTION_NONE ? "unknown action" :
                      !session_nonce_valid(nonce) ? "bad nonce" :
                      !session_request_fresh(when, session_now()) ? "stale request" : NULL;
    if (why) {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION, "REFUSED nonce=%s reason=%s; Bluetooth not restarted",
                      session_nonce_valid(nonce) ? nonce : "-", why);
        SpeakaLogLine("V89_SESSION REFUSED: %s; Bluetooth not restarted", why);
        return;
    }
    int start = act == SESSION_ACTION_START;
    if (!session_write_switches(start, nonce, session_now())) {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION,
                      "REFUSED nonce=%s reason=switches could not be saved; Bluetooth not restarted", nonce);
        SpeakaLogLine("V89_SESSION REFUSED: one-shot switches could not be saved and verified");
        return;
    }
    g_session_restarting = 1;
    SpeakaLogNow("V89_SESSION %s nonce=%s: one-shot switches %s; restarting bluetoothd (SIGKILL, same as killall -9)",
               action, nonce, start ? "ARMED" : "cleared");
    (void)kill(getpid(), SIGKILL);
    _exit(1); /* only reached if SIGKILL was somehow refused; launchd restarts bluetoothd either way */
}

void SpeakaSessionStart(void)
{
    if (g_session_queue) return;
    g_session_queue = dispatch_queue_create("com.parv.speaka.v89.session", DISPATCH_QUEUE_SERIAL);
    if (!g_session_queue) {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION, "UNAVAILABLE: no worker queue; use the manual switches");
        return;
    }
    uint32_t r = notify_register_dispatch(SPEAKA_NOTIFY_SESSION, &g_session_token, g_session_queue, ^(int token) {
        (void)token;
        session_handle_request();
    });
    if (r == NOTIFY_STATUS_OK) {
        SpeakaLogLine("V89_SESSION Settings Start/Stop listener ready");
    } else {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION, "UNAVAILABLE: notify_register_dispatch=%u; use the manual switches",
                      (unsigned)r);
        SpeakaLogNow("V89_SESSION listener unavailable: notify_register_dispatch=%u", (unsigned)r);
    }
}

void SpeakaSessionReport(int trial_armed, int receive_ready)
{
    char action[16] = {0}, nonce[48] = {0};
    double when = 0.0;
    int got = session_take_record(CFSTR(SPEAKA_KEY_SESSION_PENDING), action, nonce, &when);
    if (got == 0) {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION, "idle: this Bluetooth launch was not requested from Settings");
        return;
    }
    int act = got == 1 ? session_parse_action(action) : SESSION_ACTION_NONE;
    double now = session_now();
    if (act == SESSION_ACTION_NONE || !session_nonce_valid(nonce) || when > now + 5.0 || now - when > 180.0) {
        SpeakaSetStatus(SPEAKA_STATUS_SESSION, "idle: ignored an old or malformed restart marker");
        SpeakaLogLine("V89_SESSION ignored an old or malformed restart marker");
        return;
    }
    const char *result = act == SESSION_ACTION_START
        ? (trial_armed ? (receive_ready ? "ARMED" : "ARMED_INCOMPLETE") : "REFUSED")
        : (trial_armed ? "STILL_ARMED" : "OFF");
    SpeakaSetStatus(SPEAKA_STATUS_SESSION, "%s nonce=%s pid=%d result=%s", action, nonce, (int)getpid(), result);
    SpeakaLogLine("V89_SESSION relaunched for %s nonce=%s pid=%d result=%s", action, nonce, (int)getpid(), result);
}
