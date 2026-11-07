/* SessionCheck.h - the app's check of the tweak's live endpoint-count result (diag.v81_seven_slots), header-only C
 * so the host tests run the very code the app runs (SPTweakClient.m includes it; tests/test_app.py compiles it).
 *
 * v9.9.1: since 9.9 a launch uses one of two layouts - dual (Apple's six Sources + SEID 7 AAC-LC + the SBC Sink: 8
 * endpoint slots) or the single fallback (six Sources + SEID 7 AAC-LC: 7). The tweak reports the live count with the
 * count that launch's shadow returned and the layout it chose:
 *     "ONE SHOT result: live_slots=8 expected=8 layout=dual native_sources=6; ..."
 * Done only when the count was readable, equals what was expected, and what was expected is the layout's own count -
 * so a seven-slot allocation can't pass in an eight-slot launch, nor the reverse. 9.9 wrote "expected=7" for every
 * layout, which made a correct eight-slot launch fail ("No room for the speaker"). A result without a layout (a tweak
 * before 9.9.1) passes only as that tweak's single-Sink success. */
#ifndef SPEAKA_SESSION_CHECK_H
#define SPEAKA_SESSION_CHECK_H

#include <string.h>

enum { SP_ROOM_NOT_REPORTED = 0, SP_ROOM_DONE = 1, SP_ROOM_FAILED = 2 };

/* A decimal count starting at p, ending at a space, ';' or the end; -1 if p doesn't start one (e.g. "unreadable/8"). */
static inline long sp_room_count(const char *p) {
    long n = 0;
    int digits = 0;
    if (!p) return -1;
    for (; *p >= '0' && *p <= '9'; p++) {
        if (++digits > 3) return -1;
        n = n * 10 + (*p - '0');
    }
    return digits && (*p == ' ' || *p == ';' || *p == '\0') ? n : -1;
}

static inline int sp_room_verdict(const char *s) {
    static const char prefix[] = "ONE SHOT result: live_slots=";
    static const char legacy_success[] = "ONE SHOT result: live_slots=7 expected=7 ";
    if (!s || strncmp(s, prefix, sizeof prefix - 1) != 0) return SP_ROOM_NOT_REPORTED;
    const char *layout = strstr(s, " layout=");
    if (!layout)
        return strncmp(s, legacy_success, sizeof legacy_success - 1) == 0 ? SP_ROOM_DONE : SP_ROOM_FAILED;
    layout += sizeof " layout=" - 1;
    long want = strncmp(layout, "dual ", 5) == 0 ? 8 : strncmp(layout, "single ", 7) == 0 ? 7 : -1;
    const char *expected = strstr(s, " expected=");
    long live = sp_room_count(s + sizeof prefix - 1);
    long wanted = expected ? sp_room_count(expected + sizeof " expected=" - 1) : -1;
    return want > 0 && wanted == want && live == want ? SP_ROOM_DONE : SP_ROOM_FAILED;
}

#endif
