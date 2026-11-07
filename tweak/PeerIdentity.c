/* PeerIdentity.c - name the PC that just connected, from bluetoothd's own paired-device records (v9.9.5).
 *
 * The app could name the PC only from the audio route (which lists it only while nothing else holds the route - never,
 * since Speaka holds it from the moment the PC connects) or through BluetoothManager (which reported "not ready" in
 * Test-92). This runs inside bluetoothd, which keeps a record per paired device: com.apple.MobileBluetooth.devices,
 * keyed by address, with Name / DefaultName and LastSeenTime. When the PC configures the speaker, the record seen most
 * recently is that PC - if that is unambiguous (shared/PeerName.h, peer_pick_recent). The result is published as
 * diag.v104.peer / diag.v104.peer_address for the app, and every record looked at is logged, so a wrong or missing
 * name can be explained from the log. Runs on a background queue, never on bluetoothd's signalling thread. */
#include "Tweak.h"
#include "../shared/PeerName.h"
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#define PEER_MAX_RECORDS 32u
#define PEER_MAX_FILE (4u * 1024u * 1024u)

typedef struct { char name[128]; char address[32]; double last_seen; int has_time; } PeerEntry;

static void cf_string(CFTypeRef v, char *out, size_t n)
{
    out[0] = 0;
    if (v && CFGetTypeID(v) == CFStringGetTypeID()) (void)CFStringGetCString((CFStringRef)v, out, (CFIndex)n, kCFStringEncodingUTF8);
}

static int cf_time(CFTypeRef v, double *out)
{
    if (!v) return 0;
    if (CFGetTypeID(v) == CFNumberGetTypeID()) {
        double d = 0;
        if (!CFNumberGetValue((CFNumberRef)v, kCFNumberDoubleType, &d) || d <= 0) return 0;
        *out = peer_unix_time(d);
        return 1;
    }
    if (CFGetTypeID(v) == CFDateGetTypeID()) { *out = CFDateGetAbsoluteTime((CFDateRef)v) + 978307200.0; return 1; }
    return 0;
}

/* One device record: { address : { Name, DefaultName, LastSeenTime, ... } }. */
static void add_record(CFStringRef key, CFTypeRef value, PeerEntry *e, unsigned *n)
{
    if (*n >= PEER_MAX_RECORDS || !key || !value || CFGetTypeID(value) != CFDictionaryGetTypeID()) return;
    CFDictionaryRef d = (CFDictionaryRef)value;
    PeerEntry *x = &e[*n];
    memset(x, 0, sizeof *x);
    cf_string(key, x->address, sizeof x->address);
    cf_string(CFDictionaryGetValue(d, CFSTR("Name")), x->name, sizeof x->name);
    if (!x->name[0]) cf_string(CFDictionaryGetValue(d, CFSTR("DefaultName")), x->name, sizeof x->name);
    x->has_time = cf_time(CFDictionaryGetValue(d, CFSTR("LastSeenTime")), &x->last_seen);
    if (x->address[0]) (*n)++;
}

static void add_dictionary(CFDictionaryRef root, PeerEntry *e, unsigned *n)
{
    CFIndex count = CFDictionaryGetCount(root);
    if (count <= 0 || count > 256) return;
    const void *keys[256], *values[256];
    CFDictionaryGetKeysAndValues(root, keys, values);
    for (CFIndex i = 0; i < count; ++i)
        if (CFGetTypeID(keys[i]) == CFStringGetTypeID()) add_record((CFStringRef)keys[i], values[i], e, n);
}

static int read_plist_file(const char *path, PeerEntry *e, unsigned *n)
{
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size <= 0 || st.st_size > (off_t)PEER_MAX_FILE) return 0;
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    CFMutableDataRef data = CFDataCreateMutable(kCFAllocatorDefault, 0);
    uint8_t chunk[8192];
    size_t got;
    while (data && (got = fread(chunk, 1, sizeof chunk, f)) > 0) CFDataAppendBytes(data, chunk, (CFIndex)got);
    fclose(f);
    if (!data) return 0;
    CFPropertyListRef plist = CFPropertyListCreateWithData(kCFAllocatorDefault, data, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(data);
    if (!plist) return 0;
    int ok = CFGetTypeID(plist) == CFDictionaryGetTypeID();
    if (ok) add_dictionary((CFDictionaryRef)plist, e, n);
    CFRelease(plist);
    return ok;
}

/* The records, wherever this iOS keeps them: bluetoothd's preferences, then the Bluetooth system-group container. */
static const char *load_records(PeerEntry *e, unsigned *n)
{
    *n = 0;
    CFStringRef domain = CFSTR("com.apple.MobileBluetooth.devices");
    (void)CFPreferencesAppSynchronize(domain);
    CFArrayRef keys = CFPreferencesCopyKeyList(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    if (keys) {
        CFIndex count = CFArrayGetCount(keys);
        for (CFIndex i = 0; i < count && *n < PEER_MAX_RECORDS; ++i) {
            CFStringRef key = (CFStringRef)CFArrayGetValueAtIndex(keys, i);
            if (CFGetTypeID(key) != CFStringGetTypeID()) continue;
            CFPropertyListRef value = CFPreferencesCopyAppValue(key, domain);
            if (value) { add_record(key, value, e, n); CFRelease(value); }
        }
        CFRelease(keys);
        if (*n) return "preferences";
    }
    static const char *const fixed[] = {
        "/var/mobile/Library/Preferences/com.apple.MobileBluetooth.devices.plist",
        "/var/root/Library/Preferences/com.apple.MobileBluetooth.devices.plist",
    };
    for (size_t i = 0; i < sizeof fixed / sizeof fixed[0]; ++i)
        if (read_plist_file(fixed[i], e, n) && *n) return fixed[i];
    DIR *dir = opendir("/private/var/containers/Shared/SystemGroup");
    if (dir) {
        struct dirent *ent;
        static char found[512];
        while ((ent = readdir(dir))) {
            if (ent->d_name[0] == '.') continue;
            snprintf(found, sizeof found,
                     "/private/var/containers/Shared/SystemGroup/%s/Library/Preferences/com.apple.MobileBluetooth.devices.plist",
                     ent->d_name);
            if (read_plist_file(found, e, n) && *n) { closedir(dir); return found; }
        }
        closedir(dir);
    }
    return NULL;
}

static void identify(const char *when)
{
    static PeerEntry entries[PEER_MAX_RECORDS];
    unsigned n = 0;
    const char *source = load_records(entries, &n);
    double now = CFAbsoluteTimeGetCurrent() + 978307200.0;
    if (!source) {
        SpeakaLogNow("V104_PEER when=%s none: no paired-device records found (preferences or system group)", when);
        return;
    }
    peer_record_t r[PEER_MAX_RECORDS];
    for (unsigned i = 0; i < n; ++i) {
        r[i] = (peer_record_t){ entries[i].name, entries[i].address, entries[i].last_seen, entries[i].has_time };
        SpeakaLogNow("V104_PEER_RECORD when=%s address=%s name=%s seen_ago_s=%.0f", when, entries[i].address,
                     entries[i].name, entries[i].has_time ? now - entries[i].last_seen : -1.0);
    }
    int pick = peer_pick_recent(r, n, now);
    if (pick >= 0) {
        SpeakaSetStatus("diag.v104.peer", "%s", entries[pick].name);
        SpeakaSetStatus("diag.v104.peer_address", "%s", entries[pick].address);
        SpeakaLogNow("V104_PEER when=%s name=%s address=%s source=%s records=%u", when, entries[pick].name,
                     entries[pick].address, source, n);
    } else {
        SpeakaLogNow("V104_PEER when=%s unnamed reason=%s source=%s records=%u", when,
                     pick == -2 ? "two_devices_seen_together" : "none_seen_recently", source, n);
    }
}

/* dispatch_*_f callbacks: plain C, no blocks. */
static void identify_later(void *when) { identify((const char *)when); }

void SpeakaPeerIdentifySoon(void)
{
    /* A new connection: forget the last PC first, so a failed lookup leaves "Your PC", never the previous name. Then
     * look now, and again after bluetoothd has had time to update the record's LastSeenTime. */
    SpeakaSetStatus("diag.v104.peer", "%s", "");
    SpeakaSetStatus("diag.v104.peer_address", "%s", "");
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_UTILITY, 0);
    static const char *const when[] = { "configured", "configured+10s", "configured+60s" };
    dispatch_async_f(q, (void *)when[0], identify_later);
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)10 * NSEC_PER_SEC), q, (void *)when[1], identify_later);
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)60 * NSEC_PER_SEC), q, (void *)when[2], identify_later);
}
