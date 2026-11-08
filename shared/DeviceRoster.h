/* DeviceRoster.h - the devices on the Connect to page (v9.9.12). Header-only C: the tweak uses it to keep the history,
 * the app to merge its sources, and the host tests compile both.
 *
 * Each source proves something different, and a device's state comes only from what proves it:
 *   history     Speaka's own record, written by the tweak when a computer configured Speaka's A2DP stream
 *               (tweak/PeerIdentity.c). The ONLY source of "last connected": refreshing or scanning never writes it.
 *   paired      iOS's paired list (BluetoothManager): paired, its current name and class.
 *   linked      BluetoothManager reports a Bluetooth link to it now (not necessarily audio).
 *   seen        this scan saw it: "Available" only while that sighting is fresh (SP_SEEN_FRESH_S).
 *   streaming   Speaka's A2DP stream is from this address now (the app's SPStatus.connected and peerAddress).
 *   connecting  the app asked iOS to connect, and hasn't heard back (until SP_CONNECT_TIMEOUT_S).
 * Devices are identified by their Bluetooth address, never by name: a renamed PC stays one row. */
#ifndef SPEAKA_DEVICE_ROSTER_H
#define SPEAKA_DEVICE_ROSTER_H

#include <stdlib.h>
#include <string.h>

#define SP_ADDR_LEN 18                       /* "24:B2:B9:A1:A4:68" + NUL */
#define SP_NAME_LEN 64
#define SP_HISTORY_MAX 12                    /* computers remembered */
#define SP_HISTORY_RETENTION_S (180.0 * 86400.0)
#define SP_SEEN_FRESH_S 30.0                 /* a sighting older than this isn't "Available" */
#define SP_CONNECT_TIMEOUT_S 20.0            /* a connect request with no answer by then has failed */
#define SP_ROSTER_MAX 64

/* Bluetooth major device classes (bits 8-12 of the Class of Device). */
enum { SP_MAJOR_UNKNOWN = 0, SP_MAJOR_COMPUTER = 1, SP_MAJOR_PHONE = 2, SP_MAJOR_AUDIO = 4,
       SP_MAJOR_PERIPHERAL = 5, SP_MAJOR_IMAGING = 6, SP_MAJOR_WEARABLE = 7, SP_MAJOR_TOY = 8, SP_MAJOR_HEALTH = 9 };

typedef enum { SP_STATE_UNAVAILABLE = 0, SP_STATE_PAIRED = 1, SP_STATE_AVAILABLE = 2, SP_STATE_LINKED = 3,
               SP_STATE_CONNECTING = 4, SP_STATE_CONNECTED = 5 } sp_state;
typedef enum { SP_SECTION_RECENT = 0, SP_SECTION_NEARBY = 1, SP_SECTION_PAIRED = 2, SP_SECTION_HIDDEN = 3 } sp_section;
/* Where a name came from: a better source replaces a worse one; an empty name never replaces anything. */
enum { SP_NAME_NONE = 0, SP_NAME_HISTORY = 1, SP_NAME_DISCOVERY = 2, SP_NAME_IOS = 3 };

typedef struct {
    char address[SP_ADDR_LEN];
    char name[SP_NAME_LEN];
    double last_connected;                   /* Unix time of the last verified Speaka connection */
    unsigned count;                          /* verified connections */
} sp_history_entry;

typedef struct {
    char address[SP_ADDR_LEN];
    char name[SP_NAME_LEN];
    int name_rank;
    unsigned major;                          /* SP_MAJOR_*, 0 when unknown */
    int paired, linked, streaming, in_history, connecting;
    double last_connected, last_seen, connect_started;
} sp_device;

typedef struct {
    sp_device d[SP_ROSTER_MAX];
    int n;
} sp_roster;

/* "24-b2-b9-a1-a4-68", "24:B2:B9:A1:A4:68" -> "24:B2:B9:A1:A4:68". 0 if it isn't six hex pairs, or is all zero. */
static inline int sp_address_normalize(const char *in, char out[SP_ADDR_LEN]) {
    static const char hex[] = "0123456789ABCDEF";
    int nonzero = 0;
    if (!in) return 0;
    for (int i = 0; i < 6; i++) {
        int v = 0;
        for (int k = 0; k < 2; k++) {
            char c = *in++;
            int x = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
            if (x < 0) return 0;
            v = v * 16 + x;
        }
        if (v) nonzero = 1;
        out[i * 3] = hex[v >> 4];
        out[i * 3 + 1] = hex[v & 15];
        if (i < 5) {
            char sep = *in++;
            if (sep != ':' && sep != '-') return 0;
            out[i * 3 + 2] = ':';
        }
    }
    out[17] = 0;
    return *in == 0 && nonzero;
}

/* The major class's index from whichever form iOS reports: the index itself (1), the masked major class (0x100),
 * major and minor together (0x10C) or the whole Class of Device (0x2A010C) - as SPConnectedDevice.m reads it. */
static inline unsigned sp_major_from_class(unsigned value) {
    return value >= 0x100 ? (value >> 8) & 0x1F : value & 0x1F;
}

static inline void sp_copy_name(char out[SP_NAME_LEN], const char *name) {
    size_t n = strlen(name);
    if (n >= SP_NAME_LEN) {
        n = SP_NAME_LEN - 1;
        while (n > 0 && ((unsigned char)name[n] & 0xC0) == 0x80) n--;   /* don't cut a UTF-8 character */
    }
    memcpy(out, name, n);
    out[n] = 0;
}

/* Drops what's older than the retention, keeping the order (most recent first). */
static inline int sp_history_prune(sp_history_entry *list, int n, double now) {
    int kept = 0;
    for (int i = 0; i < n; i++)
        if (now - list[i].last_connected <= SP_HISTORY_RETENTION_S) list[kept++] = list[i];
    return kept;
}

/* A verified connection from `address` at `now`: it moves to the front, its count goes up, and a non-empty `name`
 * replaces the one remembered (a PC renamed since). The oldest beyond SP_HISTORY_MAX go. Returns the new count, or -1
 * for an address that isn't one. `list` has room for SP_HISTORY_MAX entries. */
static inline int sp_history_record(sp_history_entry *list, int n, const char *address, const char *name, double now) {
    char a[SP_ADDR_LEN];
    if (!sp_address_normalize(address, a)) return -1;
    if (n > SP_HISTORY_MAX) n = SP_HISTORY_MAX;
    sp_history_entry e;
    int i = 0;
    while (i < n && strcmp(list[i].address, a) != 0) i++;
    if (i < n) {
        e = list[i];
        memmove(&list[i], &list[i + 1], (size_t)(n - i - 1) * sizeof e);
        n--;
    } else {
        memset(&e, 0, sizeof e);
        memcpy(e.address, a, SP_ADDR_LEN);
    }
    if (name && name[0]) sp_copy_name(e.name, name);
    e.last_connected = now;
    e.count++;
    if (n >= SP_HISTORY_MAX) n = SP_HISTORY_MAX - 1;
    memmove(&list[1], &list[0], (size_t)n * sizeof e);
    list[0] = e;
    return n + 1;
}

static inline void sp_roster_clear(sp_roster *r) { r->n = 0; }

static inline sp_device *sp_roster_find(sp_roster *r, const char *address) {
    char a[SP_ADDR_LEN];
    if (!sp_address_normalize(address, a)) return NULL;
    for (int i = 0; i < r->n; i++)
        if (strcmp(r->d[i].address, a) == 0) return &r->d[i];
    return NULL;
}

/* The row for `address`, made if it's new: one per device, whichever sources report it. */
static inline sp_device *sp_roster_upsert(sp_roster *r, const char *address) {
    char a[SP_ADDR_LEN];
    if (!sp_address_normalize(address, a)) return NULL;
    sp_device *d = sp_roster_find(r, a);
    if (d) return d;
    if (r->n >= SP_ROSTER_MAX) return NULL;
    d = &r->d[r->n++];
    memset(d, 0, sizeof *d);
    memcpy(d->address, a, SP_ADDR_LEN);
    return d;
}

static inline void sp_device_name(sp_device *d, const char *name, int rank) {
    if (!d || !name || !name[0] || rank < d->name_rank) return;
    sp_copy_name(d->name, name);
    d->name_rank = rank;
}

static inline void sp_device_class(sp_device *d, unsigned raw) {
    unsigned major = sp_major_from_class(raw);
    if (d && major) d->major = major;
}

/* Windows names a PC DESKTOP-... or LAPTOP-... until someone renames it. */
static inline int sp_looks_like_windows_pc(const char *name) {
    char upper[9] = {0};
    for (int i = 0; i < 8 && name && name[i]; i++) upper[i] = (char)(name[i] >= 'a' && name[i] <= 'z' ? name[i] - 32 : name[i]);
    return strncmp(upper, "DESKTOP-", 8) == 0 || strncmp(upper, "LAPTOP-", 7) == 0;
}

/* What the row says now. Connected needs Speaka's own stream from it; Available needs a fresh sighting. */
static inline sp_state sp_device_state(const sp_device *d, double now) {
    if (d->streaming) return SP_STATE_CONNECTED;
    if (d->connecting && now - d->connect_started < SP_CONNECT_TIMEOUT_S) return SP_STATE_CONNECTING;
    if (d->linked) return SP_STATE_LINKED;
    if (d->last_seen > 0 && now - d->last_seen < SP_SEEN_FRESH_S) return SP_STATE_AVAILABLE;
    if (d->paired) return SP_STATE_PAIRED;
    return SP_STATE_UNAVAILABLE;
}

/* A device that can send audio into Speaka: a computer or a phone (A2DP sources), or one that already has. Audio
 * devices (the iPhone's own headphones), peripherals and wearables never are; an unclassified one only if it's named
 * like a Windows PC. */
static inline int sp_device_can_send_audio(const sp_device *d) {
    if (d->in_history || d->streaming) return 1;
    if (d->major == SP_MAJOR_COMPUTER || d->major == SP_MAJOR_PHONE) return 1;
    return d->major == SP_MAJOR_UNKNOWN && sp_looks_like_windows_pc(d->name);
}

/* Recently connected: Speaka's history, and the computer streaming now. Nearby: seen or linked now, not in the
 * history. Other paired: paired computers and phones that never connected to Speaka - shown apart, never as history. */
static inline sp_section sp_device_section(const sp_device *d, double now) {
    if (!sp_device_can_send_audio(d)) return SP_SECTION_HIDDEN;
    if (d->in_history || d->streaming) return SP_SECTION_RECENT;
    sp_state s = sp_device_state(d, now);
    if (s == SP_STATE_AVAILABLE || s == SP_STATE_LINKED || s == SP_STATE_CONNECTING) return SP_SECTION_NEARBY;
    if (d->paired) return SP_SECTION_PAIRED;
    return SP_SECTION_HIDDEN;
}

/* The rows of one section, in order: connected first, then connecting, linked, available, paired; then the most
 * recently connected, the most recently seen, and by name. Returns how many indices went into `out`. */
static inline int sp_roster_order(const sp_roster *r, sp_section section, double now, int *out, int max) {
    int n = 0;
    for (int i = 0; i < r->n && n < max; i++)
        if (sp_device_section(&r->d[i], now) == section) out[n++] = i;
    for (int i = 1; i < n; i++) {                                   /* insertion sort: a few dozen rows */
        int key = out[i], j = i - 1;
        const sp_device *k = &r->d[key];
        for (; j >= 0; j--) {
            const sp_device *o = &r->d[out[j]];
            int ks = (int)sp_device_state(k, now), os = (int)sp_device_state(o, now);
            int before;
            if (ks != os) before = ks > os;
            else if (k->last_connected != o->last_connected) before = k->last_connected > o->last_connected;
            else if (k->last_seen != o->last_seen) before = k->last_seen > o->last_seen;
            else before = strcmp(k->name[0] ? k->name : k->address, o->name[0] ? o->name : o->address) < 0;
            if (!before) break;
            out[j + 1] = out[j];
        }
        out[j + 1] = key;
    }
    return n;
}

#endif
