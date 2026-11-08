/* PeerIdentity.c - the PC's address and name from its own connection, at configuration time (v9.9.7).
 *
 * SetConfigInd's x0 is a process-wide manager (bluetoothd loads it from a dispatch_once global for every connection), so
 * v9.8.95's search of it for the address never found it. The connection is the stream handle (x2), and the handler itself
 * looks it up - "SetConfigInd setting a2dp stream handle to %d for device %{public}s" - through a std::map<uint16_t,
 * Device *> in the manager; the Device holds the peer's address and its name (shared/PeerName.h has the layout and the
 * addresses it comes from). This repeats that lookup with reads only, before the handler runs, and publishes what it
 * finds: diag.v105.peer_address and diag.v105.peer_name, for the app's connection line - before any audio. Reads go
 * through vm_read_overwrite, so a stale pointer fails instead of crashing bluetoothd, and a failure names its step. A
 * route capture later in the connection (the helper's) is compared with it, for the log. */
#include "Tweak.h"
#include "VolumeControl.h"
#include "../shared/PeerName.h"
#include "../shared/DeviceRoster.h"
#include "../shared/SpeakaShared.h"
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <stdlib.h>
#include <string.h>

static volatile unsigned g_connection;     /* one per configuration; a stale check does nothing */
static double g_configured_at;             /* Unix time of the current configuration */
static unsigned char g_address[6];         /* what the connection said, for the route cross-check */
static unsigned g_have_address;

static int peek(uint64_t addr, void *out, size_t n)
{
    if (addr < 0x1000ull || addr > 0x0000FFFFFFFFFFFFull) return 0;
    vm_size_t got = 0;
    kern_return_t kr = vm_read_overwrite(mach_task_self(), (vm_address_t)addr, (vm_size_t)n, (vm_address_t)out, &got);
    return kr == KERN_SUCCESS && got == (vm_size_t)n;
}

static double now_unix(void) { return CFAbsoluteTimeGetCurrent() + 978307200.0; }

static void cf_cstring(CFTypeRef v, char *out, size_t n)
{
    out[0] = 0;
    if (v && CFGetTypeID(v) == CFStringGetTypeID()) (void)CFStringGetCString((CFStringRef)v, out, (CFIndex)n, kCFStringEncodingUTF8);
}

/* The helper's route capture, if it was published after this configuration: iOS's own address for the PC. */
static int fresh_capture(unsigned char addr[6], char *uid, size_t n)
{
    int ok = 0;
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    CFPropertyListRef status = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_HELPER_STATUS), CFSTR(SPEAKA_PREFS_DOMAIN));
    if (status && CFGetTypeID(status) == CFDictionaryGetTypeID()) {
        CFTypeRef time = CFDictionaryGetValue((CFDictionaryRef)status, CFSTR("time"));
        CFTypeRef pc = CFDictionaryGetValue((CFDictionaryRef)status, CFSTR("pc"));
        double when = 0;
        if (time && CFGetTypeID(time) == CFNumberGetTypeID()) (void)CFNumberGetValue((CFNumberRef)time, kCFNumberDoubleType, &when);
        if (when >= g_configured_at && pc && CFGetTypeID(pc) == CFDictionaryGetTypeID()) {
            cf_cstring(CFDictionaryGetValue((CFDictionaryRef)pc, CFSTR("uid")), uid, n);
            ok = peer_address_bytes(uid, addr);
        }
    }
    if (status) CFRelease(status);
    return ok;
}

static void route_check(void *context)
{
    unsigned connection = (unsigned)(uintptr_t)context;
    if (connection != __atomic_load_n(&g_connection, __ATOMIC_ACQUIRE) || !g_have_address) return;
    unsigned char captured[6];
    char uid[96], a[18];
    if (!fresh_capture(captured, uid, sizeof uid)) return;
    peer_address_text(g_address, a);
    SpeakaLogNow("V105_PEER_ROUTE_CHECK %s connection=%s route=%s",
                 memcmp(captured, g_address, 6) == 0 ? "match" : "MISMATCH", a, uid);
}

/* v9.9.12: the Connect to page's history. A computer that configured Speaka's A2DP stream is the one event that proves
 * a connection, so this - and nothing that merely lists or scans devices - writes "last connected". Off bluetoothd's
 * Bluetooth thread: read the list, record (shared/DeviceRoster.h), write it back, the way Log.c writes the status. */
struct history_visit { char address[SP_ADDR_LEN]; char name[SP_NAME_LEN]; double when; };

static double dict_number(CFDictionaryRef d, CFStringRef key)
{
    double v = 0;
    CFTypeRef n = CFDictionaryGetValue(d, key);
    if (n && CFGetTypeID(n) == CFNumberGetTypeID()) (void)CFNumberGetValue((CFNumberRef)n, kCFNumberDoubleType, &v);
    return v;
}

/* One at a time: two connections close together would otherwise each read the list, add one, and the later write
 * would drop the earlier one. */
static dispatch_queue_t g_history_queue;

static void make_history_queue(void *unused)
{
    (void)unused;
    g_history_queue = dispatch_queue_create("com.parv.speaka.history",
                                            dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_UTILITY, 0));
}

static void record_history(void *context)
{
    struct history_visit *visit = context;
    sp_history_entry list[SP_HISTORY_MAX];
    int n = 0;
    CFStringRef domain = CFSTR(SPEAKA_PREFS_DOMAIN);
    (void)CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    CFPropertyListRef old = CFPreferencesCopyValue(CFSTR(SPEAKA_KEY_CONNECTION_HISTORY), domain,
                                                   kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    if (old && CFGetTypeID(old) == CFArrayGetTypeID()) {
        CFIndex count = CFArrayGetCount((CFArrayRef)old);
        for (CFIndex i = 0; i < count && n < SP_HISTORY_MAX; i++) {
            CFTypeRef item = CFArrayGetValueAtIndex((CFArrayRef)old, i);
            if (!item || CFGetTypeID(item) != CFDictionaryGetTypeID()) continue;
            CFDictionaryRef d = (CFDictionaryRef)item;
            char address[64], name[SP_NAME_LEN];
            cf_cstring(CFDictionaryGetValue(d, CFSTR("address")), address, sizeof address);
            cf_cstring(CFDictionaryGetValue(d, CFSTR("name")), name, sizeof name);
            sp_history_entry e;
            memset(&e, 0, sizeof e);
            if (!sp_address_normalize(address, e.address)) continue;
            sp_copy_name(e.name, name);
            e.last_connected = dict_number(d, CFSTR("last"));
            e.count = (unsigned)dict_number(d, CFSTR("count"));
            list[n++] = e;
        }
    }
    if (old) CFRelease(old);
    n = sp_history_prune(list, n, visit->when);
    n = sp_history_record(list, n, visit->address, visit->name, visit->when);
    if (n > 0) {
        CFMutableArrayRef out = CFArrayCreateMutable(kCFAllocatorDefault, n, &kCFTypeArrayCallBacks);
        for (int i = 0; out && i < n; i++) {
            CFMutableDictionaryRef d = CFDictionaryCreateMutable(kCFAllocatorDefault, 4, &kCFTypeDictionaryKeyCallBacks,
                                                                 &kCFTypeDictionaryValueCallBacks);
            CFStringRef address = CFStringCreateWithCString(kCFAllocatorDefault, list[i].address, kCFStringEncodingUTF8);
            CFStringRef name = CFStringCreateWithCString(kCFAllocatorDefault, list[i].name, kCFStringEncodingUTF8);
            double count = (double)list[i].count;
            CFNumberRef last = CFNumberCreate(kCFAllocatorDefault, kCFNumberDoubleType, &list[i].last_connected);
            CFNumberRef times = CFNumberCreate(kCFAllocatorDefault, kCFNumberDoubleType, &count);
            if (d && address && name && last && times) {
                CFDictionarySetValue(d, CFSTR("address"), address);
                CFDictionarySetValue(d, CFSTR("name"), name);
                CFDictionarySetValue(d, CFSTR("last"), last);
                CFDictionarySetValue(d, CFSTR("count"), times);
                CFArrayAppendValue(out, d);
            }
            if (d) CFRelease(d);
            if (address) CFRelease(address);
            if (name) CFRelease(name);
            if (last) CFRelease(last);
            if (times) CFRelease(times);
        }
        if (out) {
            CFPreferencesSetValue(CFSTR(SPEAKA_KEY_CONNECTION_HISTORY), out, domain, kCFPreferencesCurrentUser,
                                  kCFPreferencesAnyHost);
            Boolean ok = CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
            SpeakaLogNow("V110_HISTORY recorded %s \"%s\": %d computer%s remembered%s", visit->address, visit->name, n,
                         n == 1 ? "" : "s", ok ? "" : " (NOT SAVED)");
            CFRelease(out);
        }
    }
    free(visit);
}

void SpeakaPeerConnected(uint64_t manager, unsigned handle)
{
    unsigned connection = __atomic_add_fetch(&g_connection, 1u, __ATOMIC_ACQ_REL);
    g_configured_at = now_unix();
    g_have_address = 0;
    peer_identity_t id;
    int st = peer_identify_stream(manager, handle, peek, &id);
    if (st == PEER_OK) {
        char a[18];
        peer_address_text(id.address, a);
        memcpy(g_address, id.address, 6);
        g_have_address = 1;
        SpeakaSetStatus("diag.v105.peer_address", "%s", a);
        SpeakaSetStatus("diag.v105.peer_name", "%s", id.name);
        SpeakaLogNow("V105_PEER address=%s name=%s name_field=%s handle=%u nodes=%u source=stream_map", a, id.name,
                     id.name_source == 1 ? "0x118" : id.name_source == 2 ? "0xf0" : "none", handle, id.nodes);
        SpeakaVolumeConfigure(id.address);   /* v9.9.8: AVRCP volume answers this device (VolumeControl.m) */
        struct history_visit *visit = calloc(1, sizeof *visit);
        if (visit) {
            memcpy(visit->address, a, SP_ADDR_LEN);
            sp_copy_name(visit->name, id.name);
            visit->when = g_configured_at;
            static dispatch_once_t once;
            dispatch_once_f(&once, NULL, make_history_queue);
            dispatch_async_f(g_history_queue, visit, record_history);
        }
    } else {
        /* Nothing known about this connection: clear the last one's, so the app shows "Your PC", never the old PC. */
        SpeakaSetStatus("diag.v105.peer_address", "%s", "");
        SpeakaSetStatus("diag.v105.peer_name", "%s", "");
        SpeakaLogNow("V105_PEER_UNRESOLVED step=%s handle=%u nodes=%u", peer_status_text(st), handle, id.nodes);
        SpeakaVolumeConfigure(NULL);
    }
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_UTILITY, 0);
    void *context = (void *)(uintptr_t)connection;
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)3 * (int64_t)NSEC_PER_SEC), q, context, route_check);
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)10 * (int64_t)NSEC_PER_SEC), q, context, route_check);
}
