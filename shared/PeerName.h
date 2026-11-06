/* PeerName.h - which Bluetooth device is the PC streaming to Speaka, and what to call it (v9.9.4). Header-only, pure C,
 * shared by the helper (which captures the PC from the audio route) and the app (which names it), and host-tested.
 *
 * Why: the app named the PC from the audio route, where iOS lists the sending PC as a Bluetooth output whenever
 * nothing else holds the route (outputs=LAPTOP-UGIVBIA5 [BluetoothA2DPOutput]). Since v9.8.87 the helper holds its
 * session from the moment the PC connects, so the route only ever showed the speaker and the app fell back to "Your
 * PC". The helper now captures the PC from the route before its own activation (the change's previous route) and on
 * any route change that lists it; the app matches that port's address with BluetoothManager's devices. */
#pragma once
#include <stddef.h>
#include <string.h>
#include <ctype.h>

/* AVAudioSession's Bluetooth output port types (the public constants' string values). */
static inline int peer_port_is_bluetooth(const char *type)
{
    return type && (strcmp(type, "BluetoothA2DPOutput") == 0 || strcmp(type, "BluetoothHFP") == 0 ||
                    strcmp(type, "BluetoothLE") == 0);
}

static inline int peer_name_is_windows_default(const char *name)
{
    if (!name) return 0;
    static const char *const prefixes[] = { "DESKTOP-", "LAPTOP-" };
    for (size_t p = 0; p < 2; ++p) {
        size_t n = strlen(prefixes[p]), i = 0;
        for (; i < n && name[i] && toupper((unsigned char)name[i]) == prefixes[p][i]; ++i) {}
        if (i == n) return 1;
    }
    return 0;
}

/* The device address a Bluetooth port's UID carries ("AA:BB:CC:DD:EE:FF-tacl" and similar): the first run of six
 * two-digit hex groups separated by ':' or '-', anywhere in the UID. Writes "AA:BB:CC:DD:EE:FF" (upper case). */
static inline int peer_uid_address(const char *uid, char out[18])
{
    if (!uid) return 0;
    size_t len = strlen(uid);
    for (size_t start = 0; start + 17 <= len; ++start) {
        int ok = 1;
        for (size_t k = 0; k < 17 && ok; ++k) {
            char c = uid[start + k];
            ok = (k % 3 == 2) ? (c == ':' || c == '-') : isxdigit((unsigned char)c) != 0;
        }
        if (start + 17 < len && isxdigit((unsigned char)uid[start + 17])) ok = 0;   /* not part of a longer hex run */
        if (start > 0 && isxdigit((unsigned char)uid[start - 1])) ok = 0;
        if (!ok) continue;
        for (size_t k = 0; k < 17; ++k)
            out[k] = (k % 3 == 2) ? ':' : (char)toupper((unsigned char)uid[start + k]);
        out[17] = 0;
        return 1;
    }
    return 0;
}

static inline int peer_address_equal(const char *a, const char *b)
{
    char x[18], y[18];
    return peer_uid_address(a, x) && peer_uid_address(b, y) && strcmp(x, y) == 0;
}

/* Bluetooth Class of Device major class 4 is Audio/Video - headphones, speakers, AirPods - never the streaming PC. */
static inline int peer_class_is_audio(int has_class, unsigned major_index) { return has_class && major_index == 4u; }

/* Which of a route's outputs is the PC: a Windows default name wins over other Bluetooth outputs (headphones, say);
 * otherwise the first Bluetooth output. -1 if the route lists none. */
static inline int peer_pick_port(const char *const *names, const char *const *types, unsigned n)
{
    int first = -1;
    for (unsigned i = 0; i < n; ++i) {
        if (!peer_port_is_bluetooth(types[i]) || !names[i] || !names[i][0]) continue;
        if (peer_name_is_windows_default(names[i])) return (int)i;
        if (first < 0) first = (int)i;
    }
    return first;
}

/* The name the app shows, in this order: the BluetoothManager device whose address the route's PC port carries (it
 * is the device actually streaming - unless it is an audio device); a computer BluetoothManager lists as connected;
 * the PC port's own name; the one paired computer, but only while audio is arriving; else NULL ("Your PC"). */
typedef struct {
    const char *identity;            /* the address-matched device's name, or NULL */
    int identity_is_audio;           /* that device is headphones or a speaker: not the PC */
    const char *connected_computer;  /* BluetoothManager: a connected computer */
    const char *route;               /* the PC port captured from the audio route */
    const char *paired_computer;     /* BluetoothManager: the only paired computer */
    int receiving;
} peer_names_t;

static inline const char *peer_choose_name(const peer_names_t *n)
{
    if (n->identity && n->identity[0] && !n->identity_is_audio) return n->identity;
    if (n->connected_computer && n->connected_computer[0]) return n->connected_computer;
    if (n->route && n->route[0] && !n->identity_is_audio) return n->route;
    if (n->receiving && n->paired_computer && n->paired_computer[0]) return n->paired_computer;
    return NULL;
}
