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
#include "../shared/PeerName.h"
#include "../shared/SpeakaShared.h"
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
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
    } else {
        /* Nothing known about this connection: clear the last one's, so the app shows "Your PC", never the old PC. */
        SpeakaSetStatus("diag.v105.peer_address", "%s", "");
        SpeakaSetStatus("diag.v105.peer_name", "%s", "");
        SpeakaLogNow("V105_PEER_UNRESOLVED step=%s handle=%u nodes=%u", peer_status_text(st), handle, id.nodes);
    }
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_UTILITY, 0);
    void *context = (void *)(uintptr_t)connection;
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)3 * (int64_t)NSEC_PER_SEC), q, context, route_check);
    dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, (int64_t)10 * (int64_t)NSEC_PER_SEC), q, context, route_check);
}
