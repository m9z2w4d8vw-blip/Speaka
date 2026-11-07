/* PeerIdentity.c - the PC's address from its own connection (v9.9.6).
 *
 * v9.8.94: the paired-device records this used to read were never found on this iOS, the route listed the PC in only
 * 2 of 7 connections, and BluetoothManager stayed unavailable. When a route does list the PC, iOS gives its name and
 * address together (V102_PC_ROUTE name=LAPTOP-UGIVBIA5 uid=24:B2:B9:A1:A4:68-tacl); the helper publishes that pair.
 * The PC's AVDTP configuration arrives with a native owner object (SetConfigInd x0). With a route-verified address from
 * this very connection, this searches that object - and the objects it points to, one level deep - for the address's
 * exact six bytes (shared/PeerName.h). A match shows where the connection keeps its peer's address: the path is saved,
 * and every later configuration reads the address straight from the connection, immediately - before any audio. A path
 * that ever disagrees with a fresh route capture is forgotten. Reads go through vm_read_overwrite, so a stale pointer
 * fails instead of crashing bluetoothd; all of it runs on a background queue, never on the signalling thread. */
#include "Tweak.h"
#include "../shared/PeerName.h"
#include "../shared/SpeakaShared.h"
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <string.h>

static volatile uint64_t g_owner;          /* SetConfigInd x0 of the current connection */
static volatile unsigned g_connection;     /* one per configuration; a stale timer does nothing */
static double g_configured_at;             /* Unix time of the current configuration */
static unsigned g_published;               /* the address was published for the current connection */
static unsigned g_dumped;                  /* the owner object was dumped once this bluetoothd session */

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

static int load_path(peer_path_t *path)
{
    char text[64];
    CFPropertyListRef v = CFPreferencesCopyAppValue(CFSTR(SPEAKA_KEY_PEER_PATH), CFSTR(SPEAKA_PREFS_DOMAIN));
    cf_cstring(v, text, sizeof text);
    if (v) CFRelease(v);
    return peer_path_parse(text, path);
}

static void save_path(const peer_path_t *path)
{
    char text[64];
    CFStringRef value = NULL;
    if (path && peer_path_format(path, text, sizeof text))
        value = CFStringCreateWithCString(kCFAllocatorDefault, text, kCFStringEncodingUTF8);
    CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_PEER_PATH), value, CFSTR(SPEAKA_PREFS_DOMAIN));
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    if (value) CFRelease(value);
}

/* The helper's route capture, if it was published after this configuration: iOS's own address for the PC. */
static int fresh_capture(unsigned char addr[6], char *text, size_t n)
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
            char uid[96];
            cf_cstring(CFDictionaryGetValue((CFDictionaryRef)pc, CFSTR("uid")), uid, sizeof uid);
            ok = peer_address_bytes(uid, addr);
            if (ok) snprintf(text, n, "%s", uid);
        }
    }
    if (status) CFRelease(status);
    return ok;
}

static void publish(const unsigned char addr[6], const char *source)
{
    char text[18];
    peer_address_text(addr, text);
    SpeakaSetStatus("diag.v105.peer_address", "%s", text);
    if (!g_published) SpeakaLogNow("V105_PEER_ADDRESS address=%s source=%s", text, source);
    g_published = 1;
}

static void attempt(void *context)
{
    unsigned connection = (unsigned)(uintptr_t)context;
    if (connection != __atomic_load_n(&g_connection, __ATOMIC_ACQUIRE)) return;   /* a newer connection took over */
    uint64_t owner = __atomic_load_n(&g_owner, __ATOMIC_ACQUIRE);
    if (!owner) return;
    if (!g_dumped) {
        unsigned char head[0x100];
        if (peek(owner, head, sizeof head)) SpeakaLogHexdump("V105_CONNECTION_OWNER", head, sizeof head);
        g_dumped = 1;
    }
    peer_path_t path;
    unsigned char read[6], captured[6];
    char uid[96];
    int have_path = load_path(&path), have_read = have_path && peer_read_path(owner, peek, &path, read);
    int have_capture = fresh_capture(captured, uid, sizeof uid);
    if (have_capture && have_read && memcmp(read, captured, 6) != 0) {
        char a[18], b[18];
        peer_address_text(read, a); peer_address_text(captured, b);
        SpeakaLogNow("V105_PATH_MISMATCH path_address=%s route=%s: path forgotten", a, b);
        save_path(NULL);
        have_path = have_read = 0;
    }
    if (have_read) { publish(read, have_capture ? "path_confirmed_by_route" : "path"); return; }
    if (have_capture && !have_path) {
        peer_path_t learned;
        if (peer_scan_for_address(owner, peek, captured, &learned)) {
            char text[64];
            save_path(&learned);
            peer_path_format(&learned, text, sizeof text);
            SpeakaLogNow("V105_PATH_LEARNED path=%s from route uid=%s", text, uid);
            publish(captured, "learned_from_route");
        } else {
            SpeakaLogNow("V105_PATH_NOT_FOUND route uid=%s is not within %#x bytes of the owner or one pointer deep",
                         uid, PEER_SCAN_OWNER_BYTES);
        }
    }
}

void SpeakaPeerConnected(uint64_t owner)
{
    /* A new connection: forget the last address first, so a failed lookup leaves "Your PC", never the previous PC. */
    SpeakaSetStatus("diag.v105.peer_address", "%s", "");
    __atomic_store_n(&g_owner, owner, __ATOMIC_RELEASE);
    unsigned connection = __atomic_add_fetch(&g_connection, 1u, __ATOMIC_ACQ_REL);
    g_configured_at = now_unix();
    g_published = 0;
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_UTILITY, 0);
    void *context = (void *)(uintptr_t)connection;
    static const int64_t later_ms[] = { 1000, 3000, 10000, 30000 };   /* for a route capture this connection */
    dispatch_async_f(q, context, attempt);
    for (size_t i = 0; i < sizeof later_ms / sizeof later_ms[0]; ++i)
        dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, later_ms[i] * (int64_t)NSEC_PER_MSEC), q, context, attempt);
}
