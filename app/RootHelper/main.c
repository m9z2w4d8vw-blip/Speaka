// speakahelper - Speaka's root helper. It only installs updates.
//
// Speaka spawns it as root (TrollStore's persona spawn, app/Speaka/Model/SPRootSpawn.m) and reads its output, one line
// per event, fields separated by tabs:
//
//   LOG <text>
//   ERR <text>
//
// Usage:
//   speakahelper install-update [--deb <path>] [--tipa <path>]
//   speakahelper finish-update [--tipa <path>] [--respring]      (started by install-update, in its own session)
//   speakahelper version
//
// install-update installs the tweak's .deb with dpkg first and reports how that went; the package's install script
// restarts Bluetooth. Then comes what closes Speaka, in its own session so it finishes after Speaka is gone: TrollStore's
// installer for the .tipa, then a respring. With only a .tipa it works like Caz's helper: TrollStore's installer runs in
// its own session, and its result comes back, so Speaka can fall back to TrollStore's link if it fails.
//
// Exit codes: 0 done, 2 usage, 3 not root, 4 an update file is missing or outside /var/mobile, 5 TrollStore's installer
// wasn't found, 6 it couldn't be started, 7 it failed, 8 dpkg failed, 9 dpkg wasn't found, 10 the last step couldn't be
// started.
//
// Built by app/Makefile. Test builds (-DSPEAKA_TEST, tests/test_speaka_app.py) run nothing: they take every absolute
// path under $SPEAKA_ROOT and print what they would run.

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <strings.h>
#include <stdarg.h>
#include <errno.h>
#include <limits.h>
#include <unistd.h>
#include <signal.h>
#include <dirent.h>
#include <fcntl.h>
#include <spawn.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#ifdef __APPLE__
#include <CoreFoundation/CoreFoundation.h>
#include <sys/sysctl.h>
#endif

#define SPEAKA_HELPER_VERSION "1.0"
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

// dpkg and the package's install scripts call launchctl, killall, chown and mkdir by name. Speaka starts this helper
// with an empty environment, so the rootless jailbreak's folders are named here.
static char *const k_dpkg_env[] = {
    "PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME=/var/root",
    NULL,
};
#define SPEAKA_DPKG "/var/jb/usr/bin/dpkg"
#define SPEAKA_TROLLSTORE "com.opa334.TrollStore"
#define SPEAKA_UPDATE_LOG "/tmp/FTABHarvest/SpeakaUpdate.log"

extern char **environ;

static char g_root[PATH_MAX] = "";   // test builds only: prefix for every absolute path
static char g_self[PATH_MAX] = "";   // this binary, to start the last step

// ---------------------------------------------------------------- output and paths

static void emit(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

static int starts_with(const char *s, const char *p) { return strncmp(s, p, strlen(p)) == 0; }

static int ends_with_ci(const char *s, const char *suf) {
    size_t a = strlen(s), b = strlen(suf);
    return a >= b && strcasecmp(s + a - b, suf) == 0;
}

// Builds a path; on overflow returns an empty string, which every caller treats as "doesn't exist".
__attribute__((format(printf, 2, 3)))
static int pathf(char *out, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(out, PATH_MAX, fmt, ap);
    va_end(ap);
    if (n < 0 || n >= PATH_MAX) { out[0] = 0; return 0; }
    return 1;
}

// Absolute device path -> real path (adds the test root in test builds).
static void rp(char *out, const char *abs) { pathf(out, "%s%s", g_root, abs); }

static int is_dir(const char *p) {
    struct stat st;
    return lstat(p, &st) == 0 && S_ISDIR(st.st_mode);
}

// An update file: a regular file (not a link) inside /var/mobile, with the expected extension.
static int update_file_ok(const char *path, const char *ext, const char *alt_ext) {
    char mobile[PATH_MAX], mobile2[PATH_MAX];
    rp(mobile, "/private/var/mobile/");
    rp(mobile2, "/var/mobile/");
    struct stat st;
    if (!(starts_with(path, mobile) || starts_with(path, mobile2))) return 0;
    if (strstr(path, "/../")) return 0;
    if (lstat(path, &st) != 0 || !S_ISREG(st.st_mode)) return 0;
    return ends_with_ci(path, ext) || (alt_ext && ends_with_ci(path, alt_ext));
}

// ---------------------------------------------------------------- finding TrollStore (as in Caz's helper)

// The real thing needs CoreFoundation, which test builds never use, on any platform (CI runs them on macOS).
#if defined(__APPLE__) && !defined(SPEAKA_TEST)
// Bundle id from the container manager's metadata plist (binary plist, MCMMetadataIdentifier).
static int read_container_id(const char *dir, char *out, size_t len) {
    char p[PATH_MAX];
    pathf(p, "%s/.com.apple.mobile_container_manager.metadata.plist", dir);
    FILE *f = fopen(p, "rb");
    if (!f) return 0;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return 0; }
    long sz = ftell(f);
    rewind(f);
    if (sz <= 0 || sz > (1 << 20)) { fclose(f); return 0; }
    UInt8 *buf = malloc((size_t)sz);
    if (!buf) { fclose(f); return 0; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    CFDataRef data = CFDataCreateWithBytesNoCopy(NULL, buf, (CFIndex)got, kCFAllocatorMalloc);
    if (!data) { free(buf); return 0; }
    CFPropertyListRef pl = CFPropertyListCreateWithData(NULL, data, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(data);
    int ok = 0;
    if (pl && CFGetTypeID(pl) == CFDictionaryGetTypeID()) {
        CFTypeRef v = CFDictionaryGetValue((CFDictionaryRef)pl, CFSTR("MCMMetadataIdentifier"));
        if (v && CFGetTypeID(v) == CFStringGetTypeID())
            ok = CFStringGetCString((CFStringRef)v, out, (CFIndex)len, kCFStringEncodingUTF8) ? 1 : 0;
    }
    if (pl) CFRelease(pl);
    return ok;
}
#else
// Test builds: the fake metadata file simply contains the bundle id as text.
static int read_container_id(const char *dir, char *out, size_t len) {
    char p[PATH_MAX];
    pathf(p, "%s/.com.apple.mobile_container_manager.metadata.plist", dir);
    FILE *f = fopen(p, "r");
    if (!f) return 0;
    if (!fgets(out, (int)len, f)) { fclose(f); return 0; }
    fclose(f);
    out[strcspn(out, "\r\n")] = 0;
    return out[0] != 0;
}
#endif

// The installer inside TrollStore.app, found through the bundle container whose id is TrollStore's.
static int find_trollstore_helper(char *out) {
    char base[PATH_MAX];
    rp(base, "/private/var/containers/Bundle/Application");
    out[0] = 0;
    DIR *d = opendir(base);
    if (!d) return 0;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (de->d_name[0] == '.') continue;
        char dir[PATH_MAX], id[256] = "";
        if (!pathf(dir, "%s/%s", base, de->d_name) || !is_dir(dir)) continue;
        if (!read_container_id(dir, id, sizeof id) || strcmp(id, SPEAKA_TROLLSTORE) != 0) continue;
        pathf(out, "%s/TrollStore.app/trollstorehelper", dir);
        break;
    }
    closedir(d);
    return out[0] && access(out, F_OK) == 0;
}

// ---------------------------------------------------------------- running things

// Runs a program and waits for it; its output goes where this helper's goes. -1 if it couldn't start.
static int run_and_wait(const char *path, char *const argv[], char *const envp[], int own_session) {
#ifdef SPEAKA_TEST
    (void)envp;
    emit("LOG\t(test) would run %s%s", path, own_session ? " in its own session" : "");
    for (int i = 1; argv[i]; i++) emit("LOG\t(test)   %s", argv[i]);
    const char *fail = getenv("SPEAKA_TEST_FAIL");            // the basename of a program to fail, for the tests
    const char *name = strrchr(path, '/');
    return fail && name && strcmp(name + 1, fail) == 0 ? 1 : 0;
#else
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
#ifdef POSIX_SPAWN_SETSID
    if (own_session) posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSID);
#else
    (void)own_session;
#endif
    pid_t pid;
    int rc = posix_spawn(&pid, path, NULL, &attr, argv, envp);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) { errno = rc; return -1; }
    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
#endif
}

// TrollStore's installer, in its own session so it finishes even when installing closes Speaka (Caz's codes 5 to 7).
static int install_tipa(const char *path) {
    if (!update_file_ok(path, ".tipa", ".ipa")) {
        emit("ERR\tThe downloaded app wasn't found");
        return 4;
    }
    char helper[PATH_MAX];
    if (!find_trollstore_helper(helper)) {
        emit("ERR\tTrollStore's installer wasn't found");
        return 5;
    }
    emit("LOG\tInstalling the app with TrollStore");
    char *argv[] = { "trollstorehelper", "install", (char *)path, NULL };
    int code = run_and_wait(helper, argv, environ, 1);
    unlink(path);
    if (code == -1) {
        emit("ERR\tCouldn't start TrollStore's installer: %s", strerror(errno));
        return 6;
    }
    if (code != 0) {
        emit("ERR\tTrollStore's installer returned %d", code);
        return 7;
    }
    emit("LOG\tApp installed");
    return 0;
}

// dpkg -i, waiting for it: the package's install script restarts Bluetooth.
static int install_deb(const char *path) {
    char dpkg[PATH_MAX];
    rp(dpkg, SPEAKA_DPKG);
    if (access(dpkg, F_OK) != 0) {
        emit("ERR\tdpkg wasn't found at " SPEAKA_DPKG);
        return 9;
    }
    emit("LOG\tInstalling the tweak with dpkg");
    char *argv[] = { "dpkg", "-i", (char *)path, NULL };
    int code = run_and_wait(dpkg, argv, k_dpkg_env, 0);
    unlink(path);
    if (code != 0) {
        emit("ERR\tThe tweak didn't install (dpkg exited with %d). Nothing else was changed.", code);
        return 8;
    }
    emit("LOG\tTweak installed; Bluetooth restarted");
    return 0;
}

#if defined(__APPLE__) && !defined(SPEAKA_TEST)
// Kills every process whose executable is called `name` (Caz's kill_procs, by basename).
static int kill_named(const char *name) {
    int mib[3] = { CTL_KERN, KERN_PROC, KERN_PROC_ALL };
    size_t len = 0;
    if (sysctl(mib, 3, NULL, &len, NULL, 0) != 0 || len == 0) return 0;
    len += 16 * sizeof(struct kinfo_proc);
    struct kinfo_proc *procs = malloc(len);
    if (!procs) return 0;
    if (sysctl(mib, 3, procs, &len, NULL, 0) != 0) { free(procs); return 0; }
    int count = (int)(len / sizeof(struct kinfo_proc));
    int argmax = 0;
    size_t asz = sizeof argmax;
    int amib[2] = { CTL_KERN, KERN_ARGMAX };
    if (sysctl(amib, 2, &argmax, &asz, NULL, 0) != 0 || argmax <= 0) argmax = 65536;
    char *buf = malloc((size_t)argmax);
    if (!buf) { free(procs); return 0; }
    int killed = 0;
    for (int i = 0; i < count; i++) {
        pid_t pid = procs[i].kp_proc.p_pid;
        if (pid <= 1 || pid == getpid()) continue;
        int pmib[3] = { CTL_KERN, KERN_PROCARGS2, pid };
        size_t s = (size_t)argmax;
        if (sysctl(pmib, 3, buf, &s, NULL, 0) != 0 || s <= sizeof(int)) continue;
        buf[s - 1] = 0;
        const char *exe = buf + sizeof(int);       // the executable's path follows argc
        const char *base = strrchr(exe, '/');
        if (base && strcmp(base + 1, name) == 0 && kill(pid, SIGKILL) == 0) killed++;
    }
    free(buf);
    free(procs);
    return killed;
}
#else
// Test builds and host compile checks: nothing is killed.
__attribute__((unused)) static int kill_named(const char *name) { (void)name; return 0; }
#endif

// A respring: SpringBoard is killed and launchd starts it again, which also closes every app.
static void respring(void) {
    emit("LOG\tRespringing");
#ifdef SPEAKA_TEST
    emit("LOG\t(test) would kill SpringBoard");
#else
    sleep(1);                                       // let TrollStore and the install script finish their writes
    if (kill_named("SpringBoard") == 0) emit("ERR\tSpringBoard wasn't found");
#endif
}

// Starts `speakahelper finish-update ...` in its own session without waiting, its output appended to the update log.
static int start_finish(const char *tipa) {
    char *argv[6];
    int n = 0;
    argv[n++] = "speakahelper";
    argv[n++] = "finish-update";
    if (tipa) { argv[n++] = "--tipa"; argv[n++] = (char *)tipa; }
    argv[n++] = "--respring";
    argv[n] = NULL;
#ifdef SPEAKA_TEST
    emit("LOG\t(test) would start %s in its own session, output to " SPEAKA_UPDATE_LOG, g_self);
    for (int i = 1; argv[i]; i++) emit("LOG\t(test)   %s", argv[i]);
    return 0;
#else
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
    // The log folder is the tweak's; without it, the last step still runs, just unlogged.
    const char *log = access("/tmp/FTABHarvest", W_OK) == 0 ? SPEAKA_UPDATE_LOG : "/dev/null";
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, log, O_WRONLY | O_CREAT | O_APPEND, 0644);
    posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO);
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
#ifdef POSIX_SPAWN_SETSID
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSID);
#endif
    pid_t pid;
    int rc = posix_spawn(&pid, g_self, &actions, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    posix_spawn_file_actions_destroy(&actions);
    if (rc != 0) {
        emit("ERR\tCouldn't start the last step of the update: %s", strerror(rc));
        return 10;
    }
    return 0;
#endif
}

// ---------------------------------------------------------------- commands

static int do_install_update(const char *deb, const char *tipa) {
    if (!deb && !tipa) {
        emit("ERR\tNothing to install");
        return 2;
    }
    if (deb && !update_file_ok(deb, ".deb", NULL)) {
        emit("ERR\tThe downloaded tweak wasn't found");
        return 4;
    }
    if (tipa && !update_file_ok(tipa, ".tipa", ".ipa")) {
        emit("ERR\tThe downloaded app wasn't found");
        return 4;
    }
    if (!deb) return install_tipa(tipa);           // Caz's way: Speaka waits, and falls back to TrollStore's link
    int code = install_deb(deb);
    if (code != 0) return code;                     // the app is left alone and nothing resprings
    if (start_finish(tipa) != 0) return 10;
    emit("LOG\t%s", tipa ? "Installing the app, then respringing" : "Respringing");
    return 0;
}

static int do_finish_update(const char *tipa, int resprings) {
    int code = tipa ? install_tipa(tipa) : 0;
    if (resprings) respring();                      // the tweak is new either way, so this always happens
    return code;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN);                       // Speaka may close before its last line arrives
    umask(022);
#ifdef SPEAKA_TEST
    const char *root = getenv("SPEAKA_ROOT");
    if (root) snprintf(g_root, sizeof g_root, "%s", root);
#else
    if (geteuid() != 0) {
        emit("ERR\tThe update helper isn't running as root. Install Speaka with TrollStore.");
        return 3;
    }
#endif
    if (!realpath(argv[0], g_self)) snprintf(g_self, sizeof g_self, "%s", argv[0]);
    if (argc < 2) {
        emit("ERR\tusage: speakahelper install-update [--deb <path>] [--tipa <path>] | finish-update [--tipa <path>] [--respring] | version");
        return 2;
    }
    const char *deb = NULL, *tipa = NULL;
    int resprings = 0;
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--deb") == 0 && i + 1 < argc) deb = argv[++i];
        else if (strcmp(argv[i], "--tipa") == 0 && i + 1 < argc) tipa = argv[++i];
        else if (strcmp(argv[i], "--respring") == 0) resprings = 1;
        else {
            emit("ERR\tUnknown option %s", argv[i]);
            return 2;
        }
    }
    if (strcmp(argv[1], "version") == 0) { emit("LOG\tspeakahelper %s", SPEAKA_HELPER_VERSION); return 0; }
    if (strcmp(argv[1], "install-update") == 0) return do_install_update(deb, tipa);
    if (strcmp(argv[1], "finish-update") == 0) return do_finish_update(tipa, resprings);
    emit("ERR\tUnknown command %s", argv[1]);
    return 2;
}
