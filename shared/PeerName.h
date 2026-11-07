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
#include <stdint.h>
#include <stdio.h>

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

/* The name the app shows, in this order (v9.9.5): the connection's own identity (tweak/PeerIdentity.c); then the
 * BluetoothManager device whose address the route's PC port carries (it
 * is the device actually streaming - unless it is an audio device); a computer BluetoothManager lists as connected;
 * the PC port's own name; the one paired computer, but only while audio is arriving; else NULL ("Your PC"). */
typedef struct {
    const char *connection;          /* v9.9.5: bluetoothd's record of the PC that configured the speaker */
    const char *identity;            /* the address-matched device's name, or NULL */
    int identity_is_audio;           /* that device is headphones or a speaker: not the PC */
    const char *connected_computer;  /* BluetoothManager: a connected computer */
    const char *route;               /* the PC port captured from the audio route */
    const char *paired_computer;     /* BluetoothManager: the only paired computer */
    int receiving;
} peer_names_t;

static inline const char *peer_choose_name(const peer_names_t *n)
{
    if (n->connection && n->connection[0]) return n->connection;
    if (n->identity && n->identity[0] && !n->identity_is_audio) return n->identity;
    if (n->connected_computer && n->connected_computer[0]) return n->connected_computer;
    if (n->route && n->route[0] && !n->identity_is_audio) return n->route;
    if (n->receiving && n->paired_computer && n->paired_computer[0]) return n->paired_computer;
    return NULL;
}

/* ---- the PC's identity from the connection itself (v9.9.6) ----
 * v9.8.94: the paired-device records were never found ("no paired-device records found", every connection), the
 * route listed the PC in only 2 of 7 connections (V102_PC_ROUTE), and BluetoothManager stayed unavailable. But when a
 * route does list the PC, iOS gives its name and its address together (name=LAPTOP-UGIVBIA5 uid=24:B2:B9:A1:A4:68-tacl),
 * and the PC's AVDTP configuration arrives with a native owner object (SetConfigInd x0). The tweak searches that object -
 * and the objects it points to, one level deep - for the exact six bytes of an address iOS has verified, in either byte
 * order. A 48-bit match by chance is effectively impossible, so a match shows where the connection keeps its peer's
 * address; that path is remembered, and from then on every configuration reads the PC's address straight from the
 * connection, before any audio. The app names an address only from a verified cache of iOS's own name/address pairs:
 * a different computer has a different address, so it can never inherit the last PC's name. */
#define PEER_SCAN_OWNER_BYTES 0x400u     /* searched at the owner object */
#define PEER_SCAN_CHILD_BYTES 0x200u     /* searched at each object the owner points to */
#define PEER_CACHE_SIZE 8u

/* "AA:BB:CC:DD:EE:FF" (or any UID carrying it) -> six bytes, most significant first. */
static inline int peer_address_bytes(const char *text, unsigned char out[6])
{
    char a[18];
    if (!peer_uid_address(text, a)) return 0;
    for (int i = 0; i < 6; ++i) {
        unsigned hi = (unsigned)(isdigit((unsigned char)a[i * 3]) ? a[i * 3] - '0' : a[i * 3] - 'A' + 10);
        unsigned lo = (unsigned)(isdigit((unsigned char)a[i * 3 + 1]) ? a[i * 3 + 1] - '0' : a[i * 3 + 1] - 'A' + 10);
        out[i] = (unsigned char)(hi << 4 | lo);
    }
    return 1;
}

static inline void peer_address_text(const unsigned char b[6], char out[18])
{
    static const char hex[] = "0123456789ABCDEF";
    for (int i = 0; i < 6; ++i) {
        out[i * 3] = hex[b[i] >> 4];
        out[i * 3 + 1] = hex[b[i] & 15u];
        out[i * 3 + 2] = i < 5 ? ':' : 0;
    }
}

/* Not an address: all zero or all ones (an unset field reads like that). */
static inline int peer_address_plausible(const unsigned char b[6])
{
    int zero = 1, ones = 1;
    for (int i = 0; i < 6; ++i) { zero &= b[i] == 0; ones &= b[i] == 0xffu; }
    return !zero && !ones;
}

/* Offset of addr in buf, forwards or byte-reversed (*reversed says which); -1 if absent. */
static inline long peer_find_address(const unsigned char *buf, size_t n, const unsigned char addr[6], int *reversed)
{
    for (size_t i = 0; i + 6 <= n; ++i) {
        int fwd = 1, rev = 1;
        for (int k = 0; k < 6 && (fwd || rev); ++k) {
            fwd &= buf[i + (size_t)k] == addr[k];
            rev &= buf[i + (size_t)k] == addr[5 - k];
        }
        if (fwd || rev) { if (reversed) *reversed = rev && !fwd; return (long)i; }
    }
    return -1;
}

/* Where a connection keeps its peer's address: owner+off1 (depth 0), or *(owner+off1)+off2 (depth 1). */
typedef struct { unsigned valid, depth, off1, off2, reversed; } peer_path_t;

/* As stored in preferences: "d0:0x58:0:f" or "d1:0x18:0x30:r". */
static inline int peer_path_format(const peer_path_t *p, char *out, size_t n)
{
    if (!p->valid) return 0;
    return snprintf(out, n, "d%u:%#x:%#x:%c", p->depth, p->off1, p->off2, p->reversed ? 'r' : 'f') > 0;
}

static inline int peer_path_parse(const char *text, peer_path_t *p)
{
    unsigned depth, off1, off2;
    char order;
    memset(p, 0, sizeof *p);
    if (!text || sscanf(text, "d%u:%x:%x:%c", &depth, &off1, &off2, &order) != 4) return 0;
    if (depth > 1u || off1 >= PEER_SCAN_OWNER_BYTES || off2 >= PEER_SCAN_CHILD_BYTES || (order != 'f' && order != 'r'))
        return 0;
    if (depth == 1u && (off1 % 8u)) return 0;
    *p = (peer_path_t){1, depth, off1, off2, order == 'r' ? 1u : 0u};
    return 1;
}

/* A heap pointer as it sits in an object (data pointers aren't signed; the top byte is masked anyway). */
static inline int peer_pointer(uint64_t v, uint64_t *out)
{
    v &= 0x0000FFFFFFFFFFFFull;
    if (v < 0x100000000ull || v >= 0x1000000000ull || (v & 7u)) return 0;
    *out = v;
    return 1;
}

typedef int (*peer_peek_fn)(uint64_t address, void *out, size_t n);

/* Search the owner object and the objects it points to for addr. Returns 1 with *path set on a match. */
static inline int peer_scan_for_address(uint64_t owner, peer_peek_fn peek, const unsigned char addr[6], peer_path_t *path)
{
    unsigned char top[PEER_SCAN_OWNER_BYTES], child[PEER_SCAN_CHILD_BYTES];
    int reversed = 0;
    memset(path, 0, sizeof *path);
    if (!owner || !peek(owner, top, sizeof top)) return 0;
    long at = peer_find_address(top, sizeof top, addr, &reversed);
    if (at >= 0) { *path = (peer_path_t){1, 0, (unsigned)at, 0, (unsigned)reversed}; return 1; }
    for (unsigned off = 0; off + 8u <= sizeof top; off += 8u) {
        uint64_t raw, target;
        memcpy(&raw, top + off, 8);
        if (!peer_pointer(raw, &target) || !peek(target, child, sizeof child)) continue;
        at = peer_find_address(child, sizeof child, addr, &reversed);
        if (at >= 0) { *path = (peer_path_t){1, 1, off, (unsigned)at, (unsigned)reversed}; return 1; }
    }
    return 0;
}

/* Read the address a learned path points at. Returns 1 with a plausible address. */
static inline int peer_read_path(uint64_t owner, peer_peek_fn peek, const peer_path_t *path, unsigned char out[6])
{
    unsigned char raw[6];
    uint64_t base = owner;
    if (!path->valid || !owner) return 0;
    if (path->depth == 1u) {
        uint64_t ptr;
        if (!peek(owner + path->off1, &ptr, sizeof ptr) || !peer_pointer(ptr, &base)) return 0;
        if (!peek(base + path->off2, raw, sizeof raw)) return 0;
    } else if (!peek(owner + path->off1, raw, sizeof raw)) {
        return 0;
    }
    for (int i = 0; i < 6; ++i) out[i] = path->reversed ? raw[5 - i] : raw[i];
    return peer_address_plausible(out);
}

/* The app's verified cache: address -> name, from iOS's own name/address pairs only. Bounded; least recently seen
 * goes first; a pair with a known address updates its name (the PC was renamed). */
typedef struct { char address[18]; char name[128]; double seen; } peer_cache_entry_t;
typedef struct { peer_cache_entry_t e[PEER_CACHE_SIZE]; unsigned n; } peer_cache_t;

static inline void peer_cache_learn(peer_cache_t *c, const char *uid_or_address, const char *name, double now)
{
    char a[18];
    if (!name || !name[0] || !peer_uid_address(uid_or_address, a)) return;
    unsigned slot = c->n;
    for (unsigned i = 0; i < c->n; ++i) if (!strcmp(c->e[i].address, a)) { slot = i; break; }
    if (slot == c->n) {
        if (c->n < PEER_CACHE_SIZE) c->n++;
        else { slot = 0; for (unsigned i = 1; i < c->n; ++i) if (c->e[i].seen < c->e[slot].seen) slot = i; }
    }
    memcpy(c->e[slot].address, a, sizeof a);
    snprintf(c->e[slot].name, sizeof c->e[slot].name, "%s", name);
    c->e[slot].seen = now;
}

static inline const char *peer_cache_lookup(const peer_cache_t *c, const char *uid_or_address)
{
    char a[18];
    if (!peer_uid_address(uid_or_address, a)) return NULL;
    for (unsigned i = 0; i < c->n; ++i) if (!strcmp(c->e[i].address, a)) return c->e[i].name;
    return NULL;
}

/* The connection's name. connection: the address the tweak read from this connection (or NULL); capture: the route's
 * name and UID seen during this connection (or NULLs). A known connection address is named only by a capture of that
 * same address or by the cache - never by another device. With no connection address, only this connection's capture. */
static inline const char *peer_resolve(const char *connection, const char *capture_uid, const char *capture_name,
                                       const peer_cache_t *cache)
{
    char a[18];
    int have_connection = peer_uid_address(connection, a);
    if (have_connection) {
        if (capture_name && capture_name[0] && peer_address_equal(connection, capture_uid)) return capture_name;
        return peer_cache_lookup(cache, connection);
    }
    return capture_name && capture_name[0] && peer_uid_address(capture_uid, a) ? capture_name : NULL;
}
