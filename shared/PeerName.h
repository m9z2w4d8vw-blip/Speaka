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

/* ---- the PC's identity from the connection itself (v9.9.7) ----
 * v9.8.95: V105_PATH_NOT_FOUND every time. SetConfigInd's x0 is not the connection: bluetoothd's wrapper (image
 * +0x386708, reached through a closure vtable at +0x92a540) loads it from a dispatch_once global (+0x96fb68) for every
 * connection - a process-wide manager, with mutexes inside (the dump's 58 54 55 4D is 0x4D555458, Darwin's pthread mutex
 * signature). The connection is the stream handle (x2). The handler itself resolves it, and logs the result ("SetConfigInd
 * setting a2dp stream handle to %d for device %{public}s"):
 *   +0x3730e8 (manager, handle): a std::map<uint16_t, Device *> at manager+0x1f8 - root at +0x200 (also the end node),
 *             node key (uint16) at +0x20, left at +0x0, right at +0x8; +0x374ab8 (map::at) returns node+0x28, the Device *.
 *   +0x4108f8 prints device+0x80 as %02X:%02X:%02X:%02X:%02X:%02X: the peer's address, six bytes in display order.
 *   +0x3def70 returns its name: the std::string at device+0x118 if not empty, else the one at device+0xf0 (the power-
 *             logging path pairs this name with device+0x80, so both belong to the same object).
 * The address was three pointers from x0, so last release's one-level scan could never have found it. This walks the same
 * structure with reads only, for this one bluetoothd build (Speaka refuses any other), and says which step failed. */
#define PEER_MGR_STREAM_MAP 0x1f8u     /* std::map<uint16_t, Device *> in the manager */
#define PEER_MAP_ROOT 0x8u             /* the map's end node; its first word is the root */
#define PEER_NODE_LEFT 0x0u
#define PEER_NODE_RIGHT 0x8u
#define PEER_NODE_KEY 0x20u            /* uint16 stream handle */
#define PEER_NODE_VALUE 0x28u          /* Device * */
#define PEER_DEVICE_ADDRESS 0x80u      /* six bytes, display order */
#define PEER_DEVICE_NAME_PREFERRED 0x118u
#define PEER_DEVICE_NAME_FALLBACK 0xf0u
#define PEER_MAP_MAX_DEPTH 32u
#define PEER_NAME_MAX 248u
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

/* A heap pointer as it sits in an object (data pointers aren't signed; the top byte is masked anyway). */
static inline int peer_pointer(uint64_t v, uint64_t *out)
{
    v &= 0x0000FFFFFFFFFFFFull;
    if (v < 0x100000000ull || v >= 0x1000000000ull || (v & 7u)) return 0;
    *out = v;
    return 1;
}

typedef int (*peer_peek_fn)(uint64_t address, void *out, size_t n);

/* What the walk found, and where it stopped: unreadable memory and an absent handle are different answers. */
enum {
    PEER_OK = 0, PEER_NO_MANAGER, PEER_ROOT_UNREADABLE, PEER_MAP_EMPTY, PEER_NODE_UNREADABLE, PEER_TOO_DEEP,
    PEER_HANDLE_ABSENT, PEER_VALUE_UNREADABLE, PEER_ADDRESS_UNREADABLE, PEER_ADDRESS_IMPLAUSIBLE
};
static inline const char *peer_status_text(int st)
{
    static const char *const t[] = { "ok", "no_manager", "root_unreadable", "map_empty", "node_unreadable",
                                      "too_deep", "handle_absent", "device_unreadable", "address_unreadable",
                                      "address_implausible" };
    return st >= 0 && st <= PEER_ADDRESS_IMPLAUSIBLE ? t[st] : "?";
}

typedef struct {
    uint64_t device;
    unsigned char address[6];
    char name[PEER_NAME_MAX + 1];
    unsigned nodes;                  /* map nodes visited */
    int name_source;                 /* 1: device+0x118, 2: device+0xf0, 0: none */
} peer_identity_t;

/* A libc++ std::string as Apple lays it out: the high bit of byte 23 marks a long string, whose data pointer is at +0
 * and size at +8; a short string is inline, its size in byte 23. Returns the length copied (0 if empty or unreadable). */
static inline size_t peer_read_std_string(uint64_t at, peer_peek_fn peek, char *out, size_t cap)
{
    unsigned char raw[24];
    out[0] = 0;
    if (!cap || !peek(at, raw, sizeof raw)) return 0;
    size_t n;
    if (raw[23] & 0x80u) {
        uint64_t data, size, ptr;
        memcpy(&data, raw, 8); memcpy(&size, raw + 8, 8);
        if (!size || size > PEER_NAME_MAX || size + 1 > cap || !peer_pointer(data, &ptr) || !peek(ptr, out, (size_t)size))
            return 0;
        n = (size_t)size;
    } else {
        n = raw[23];
        if (n > 22u || n + 1 > cap) return 0;
        memcpy(out, raw, n);
    }
    out[n] = 0;
    for (size_t i = 0; i < n; ++i) if ((unsigned char)out[i] < 0x20u) { out[0] = 0; return 0; }   /* not a name */
    return n;
}

/* bluetoothd's own lookup, as reads: the stream map's node for this handle, its Device, the Device's address and name. */
static inline int peer_identify_stream(uint64_t manager, unsigned handle, peer_peek_fn peek, peer_identity_t *id)
{
    memset(id, 0, sizeof *id);
    if (!manager) return PEER_NO_MANAGER;
    uint64_t raw, node;
    if (!peek(manager + PEER_MGR_STREAM_MAP + PEER_MAP_ROOT, &raw, sizeof raw)) return PEER_ROOT_UNREADABLE;
    if (!raw) return PEER_MAP_EMPTY;
    if (!peer_pointer(raw, &node)) return PEER_NODE_UNREADABLE;
    for (;;) {
        uint16_t key;
        if (++id->nodes > PEER_MAP_MAX_DEPTH) return PEER_TOO_DEEP;
        if (!peek(node + PEER_NODE_KEY, &key, sizeof key)) return PEER_NODE_UNREADABLE;
        if (key == handle) break;
        if (!peek(node + (handle < key ? PEER_NODE_LEFT : PEER_NODE_RIGHT), &raw, sizeof raw)) return PEER_NODE_UNREADABLE;
        if (!raw) return PEER_HANDLE_ABSENT;
        if (!peer_pointer(raw, &node)) return PEER_NODE_UNREADABLE;
    }
    if (!peek(node + PEER_NODE_VALUE, &raw, sizeof raw) || !peer_pointer(raw, &id->device)) return PEER_VALUE_UNREADABLE;
    if (!peek(id->device + PEER_DEVICE_ADDRESS, id->address, 6)) return PEER_ADDRESS_UNREADABLE;
    if (!peer_address_plausible(id->address)) return PEER_ADDRESS_IMPLAUSIBLE;
    if (peer_read_std_string(id->device + PEER_DEVICE_NAME_PREFERRED, peek, id->name, sizeof id->name)) id->name_source = 1;
    else if (peer_read_std_string(id->device + PEER_DEVICE_NAME_FALLBACK, peek, id->name, sizeof id->name)) id->name_source = 2;
    return PEER_OK;
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

/* The connection's name. connection / connection_name: the address and name the tweak read from this connection's own
 * Device (or NULL); capture: the route's name and UID seen during this connection (or NULLs). The connection's own name
 * comes first; a known connection address is otherwise named only by a capture of that same address or by the cache -
 * never by another device. With no connection address, only this connection's capture. */
static inline const char *peer_resolve(const char *connection, const char *connection_name, const char *capture_uid,
                                       const char *capture_name, const peer_cache_t *cache)
{
    char a[18];
    int have_connection = peer_uid_address(connection, a);
    if (have_connection) {
        if (connection_name && connection_name[0]) return connection_name;
        if (capture_name && capture_name[0] && peer_address_equal(connection, capture_uid)) return capture_name;
        return peer_cache_lookup(cache, connection);
    }
    return capture_name && capture_name[0] && peer_uid_address(capture_uid, a) ? capture_name : NULL;
}
