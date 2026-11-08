// speakahelper - Speaka's root helper. It only installs updates.
//
// Speaka spawns it as root (TrollStore's persona spawn, app/Speaka/Model/SPRootSpawn.m) and reads its output, one line
// per event, fields separated by tabs:
//
//   LOG <text>
//   ERR <text>
//
// Usage:
//   speakahelper install-update [--deb <path> | --tweak-from-app] [--tipa <path>]
//   speakahelper finish-update [--tipa <path>] [--tweak-from-app] [--respring]   (started by install-update, own session)
//   speakahelper kill-bluetooth                                  (Settings > Kill Bluetooth: killall -9 bluetoothd)
//   speakahelper remove-tweak                                    (Uninstall, first: the tweak and its files)
//   speakahelper uninstall-app                                   (Uninstall, last: the app, through TrollStore)
//   speakahelper finish-uninstall                                (started by uninstall-app, in its own session)
//   speakahelper version
//
// install-update installs the tweak's .deb with dpkg first and reports how that went; the package's install script
// restarts Bluetooth. Then comes what closes Speaka, in its own session so it finishes after Speaka is gone: TrollStore's
// installer for the .tipa, then a respring. With only a .tipa, TrollStore's installer runs in
// its own session, and its result comes back, so Speaka can fall back to TrollStore's link if it fails.
//
// v9.5: Speaka.app carries the tweak (Speaka.app/Speaka.deb), so one .tipa installs both. --tweak-from-app installs the
// tweak carried by the installed Speaka app, found by its bundle id. Without --tipa that's this app's, for Install Tweak
// or a tweak behind the app; with --tipa it's the new app's, so it runs in the last step, after TrollStore's installer.
//
// Exit codes: 0 done, 2 usage, 3 not root, 4 an update file is missing or outside its folders, 5 TrollStore's installer
// wasn't found, 6 it couldn't be started, 7 it failed, 8 dpkg failed, 9 dpkg wasn't found, 10 the last step couldn't be
// started, 11 the tweak carried in the app couldn't be copied out, 12 bluetoothd wasn't running (kill-bluetooth).
//
// Built by app/Makefile. Test builds (-DSPEAKA_TEST, tests/test_app.py) run nothing: they take every absolute
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

#define SPEAKA_HELPER_VERSION "1.3"
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
#define SPEAKA_APP_ID "com.parv.speaka"
#define SPEAKA_CARRIED_DEB "Speaka.app/Speaka.deb"                 // inside the app's bundle container
#define SPEAKA_TWEAK_TEMP "/var/tmp/speaka-tweak.XXXXXX"          // root's own copy, for dpkg
#define SPEAKA_PACKAGE "com.parv.speaka"
// Compatibility: the prefix of the helper's log and ring files before they were renamed (SpeakaAudioHelper.log,
// SpeakaAudioRing-*.shm). Only the uninstall cleanup uses it.
#define LEGACY_FILE_PREFIX "BTPS"
#define SPEAKA_DPKG_LIST "/var/jb/var/lib/dpkg/info/com.parv.speaka.list"   // there while dpkg has the tweak
#include "../../shared/SpeakaShared.h"                    // SPEAKA_LOG_DIR: the tweak's logs and shared memory
#define SPEAKA_CACHES "/var/mobile/Library/Caches/Speaka"           // downloaded updates (SpeakaUpdater)
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
// The app saves updates in /var/mobile/Library/Caches/Speaka, or failing that in the tweak's own /tmp/FTABHarvest
// (SpeakaUpdater.downloadFolders), so those are the two places a file may come from. /tmp is /private/var/tmp.
static const char *const k_update_dirs[] = {
    "/private/var/mobile/", "/var/mobile/",
    "/tmp/FTABHarvest/", "/private/tmp/FTABHarvest/", "/var/tmp/FTABHarvest/", "/private/var/tmp/FTABHarvest/",
};

static int update_file_ok(const char *path, const char *ext, const char *alt_ext) {
    int inside = 0;
    for (size_t i = 0; i < sizeof k_update_dirs / sizeof k_update_dirs[0] && !inside; i++) {
        char dir[PATH_MAX];
        rp(dir, k_update_dirs[i]);
        inside = starts_with(path, dir);
    }
    struct stat st;
    if (!inside) return 0;
    if (strstr(path, "/../")) return 0;
    if (lstat(path, &st) != 0 || !S_ISREG(st.st_mode)) return 0;
    return ends_with_ci(path, ext) || (alt_ext && ends_with_ci(path, alt_ext));
}

// ---------------------------------------------------------------- finding TrollStore

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

// `inside`, in the bundle container of the app whose id is `bundle_id`: TrollStore's, or Speaka's own.
static int find_in_app(const char *bundle_id, const char *inside, char *out) {
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
        if (!read_container_id(dir, id, sizeof id) || strcmp(id, bundle_id) != 0) continue;
        pathf(out, "%s/%s", dir, inside);
        break;
    }
    closedir(d);
    return out[0] != 0;
}

// The installer inside TrollStore.app.
static int find_trollstore_helper(char *out) {
    return find_in_app(SPEAKA_TROLLSTORE, "TrollStore.app/trollstorehelper", out) && access(out, F_OK) == 0;
}

// The tweak carried inside the installed Speaka.app: a regular file, never a link. Only TrollStore writes there.
static int find_carried_tweak(char *out) {
    struct stat st;
    return find_in_app(SPEAKA_APP_ID, SPEAKA_CARRIED_DEB, out) && lstat(out, &st) == 0 && S_ISREG(st.st_mode);
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

// TrollStore's installer, in its own session so it finishes even when installing closes Speaka (exit codes 5 to 7).
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
    if (code == -1) {
        // The jailbreak's programs only run while it's active: after a reboot, until it's run again.
        emit("ERR\tdpkg couldn't start (%s). Is the jailbreak active? Run it again, then try once more.", strerror(errno));
        return 8;
    }
    if (code != 0) {
        emit("ERR\tThe tweak didn't install (dpkg exited with %d). Nothing else was changed.", code);
        return 8;
    }
    emit("LOG\tTweak installed; Bluetooth restarted");
    return 0;
}

// Copies a file into a new one, which must not exist yet.
static int copy_file(const char *from, const char *to) {
    int in = open(from, O_RDONLY);
    if (in < 0) return -1;
    int out = open(to, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (out < 0) { int e = errno; close(in); errno = e; return -1; }
    char buf[65536];
    ssize_t got;
    int ok = 1;
    while (ok && (got = read(in, buf, sizeof buf)) != 0) {
        if (got < 0) { if (errno == EINTR) continue; ok = 0; break; }
        for (ssize_t done = 0; done < got; ) {
            ssize_t put = write(out, buf + done, (size_t)(got - done));
            if (put < 0) { if (errno == EINTR) continue; ok = 0; break; }
            done += put;
        }
    }
    int e = errno;
    close(in);
    if (close(out) != 0) ok = 0;
    errno = e;
    return ok ? 0 : -1;
}

// The tweak carried in the installed Speaka.app, through dpkg. It's copied into a folder only root can write first:
// install_deb removes what it installed, and the app's own copy has to stay.
static int install_carried_tweak(void) {
    char carried[PATH_MAX];
    if (!find_carried_tweak(carried)) {
        emit("ERR\tThe tweak inside Speaka wasn't found");
        return 4;
    }
    char dir[PATH_MAX], copy[PATH_MAX];
    rp(dir, SPEAKA_TWEAK_TEMP);
    if (!mkdtemp(dir)) {
        emit("ERR\tCouldn't make a folder for the tweak: %s", strerror(errno));
        return 11;
    }
    pathf(copy, "%s/Speaka.deb", dir);
    if (copy_file(carried, copy) != 0) {
        emit("ERR\tCouldn't copy the tweak out of Speaka: %s", strerror(errno));
        unlink(copy);
        rmdir(dir);
        return 11;
    }
    int code = install_deb(copy);
    unlink(copy);
    rmdir(dir);
    return code;
}

#if defined(__APPLE__) && !defined(SPEAKA_TEST)
// Kills every process whose executable is called `name`, matched by basename.
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

// Starts this helper again with `argv`, in its own session and without waiting, its output appended to `log`.
static int start_detached(char *const argv[], const char *log) {
#ifdef SPEAKA_TEST
    emit("LOG\t(test) would start %s in its own session, output to %s", g_self, log);
    for (int i = 1; argv[i]; i++) emit("LOG\t(test)   %s", argv[i]);
    return 0;
#else
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
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
        emit("ERR\tCouldn't start the last step: %s", strerror(rc));
        return 10;
    }
    return 0;
#endif
}

// Starts `speakahelper finish-update ...` in its own session without waiting, its output appended to the update log.
static int start_finish(const char *tipa, int tweak_from_app) {
    char *argv[7];
    int n = 0;
    argv[n++] = "speakahelper";
    argv[n++] = "finish-update";
    if (tipa) { argv[n++] = "--tipa"; argv[n++] = (char *)tipa; }
    if (tweak_from_app) argv[n++] = "--tweak-from-app";
    argv[n++] = "--respring";
    argv[n] = NULL;
    // The log folder is the tweak's; without it, the last step still runs, just unlogged.
    return start_detached(argv, access(SPEAKA_LOG_DIR, W_OK) == 0 ? SPEAKA_UPDATE_LOG : "/dev/null");
}

// Speaka's own files in `folder`: regular files whose names start with one of `prefixes`, and never anything else.
// The folder itself goes only if that leaves it empty.
static void remove_matching(const char *folder, const char *const prefixes[], size_t count) {
    DIR *d = opendir(folder);
    if (!d) return;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (de->d_name[0] == '.') continue;
        int ours = 0;
        for (size_t i = 0; i < count && !ours; i++) ours = starts_with(de->d_name, prefixes[i]);
        char path[PATH_MAX];
        struct stat st;
        if (ours && pathf(path, "%s/%s", folder, de->d_name) && lstat(path, &st) == 0 && S_ISREG(st.st_mode))
            unlink(path);
    }
    closedir(d);
    rmdir(folder);
}

// ---------------------------------------------------------------- commands

static int do_install_update(const char *deb, const char *tipa, int tweak_from_app) {
    if (!deb && !tipa && !tweak_from_app) {
        emit("ERR\tNothing to install");
        return 2;
    }
    if (deb && tweak_from_app) {
        emit("ERR\tA downloaded tweak or the app's own, not both");
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
    if (tipa && tweak_from_app) {
        // The new tweak comes inside the new app, so everything happens in the last step, which outlives Speaka:
        // TrollStore's installer, then the tweak from the app it installed, then the respring.
        if (start_finish(tipa, 1) != 0) return 10;
        emit("LOG\tInstalling the app, then its tweak, then respringing");
        return 0;
    }
    if (!deb && !tweak_from_app) return install_tipa(tipa);   // Speaka waits, and falls back to its link
    int code = deb ? install_deb(deb) : install_carried_tweak();
    if (code != 0) return code;                                // the app is left alone and nothing resprings
    if (start_finish(tipa, 0) != 0) return 10;
    emit("LOG\t%s", tipa ? "Installing the app, then respringing" : "Respringing");
    return 0;
}

// Settings > Kill Bluetooth: what the README's `killall -9 bluetoothd` does, from the app. launchd starts bluetoothd
// again at once, without the speaker, so devices reconnect and the speaker is off until Start.
static int do_kill_bluetooth(void) {
#ifdef SPEAKA_TEST
    emit("LOG\t(test) would kill bluetoothd");
    return 0;
#else
    if (kill_named("bluetoothd") == 0) {
        emit("ERR\tBluetooth (bluetoothd) wasn't running");
        return 12;
    }
    emit("LOG\tBluetooth killed; iOS starts it again");
    return 0;
#endif
}

// Uninstall, first (v9.8): the tweak, then what it leaves behind. dpkg removes the package (its remove script stops the
// audio helper); bluetoothd is killed so the removed tweak unloads, and launchd starts a clean one; Settings closes so
// Settings > Speaka goes; then Speaka's files. The app removes Speaka's preferences next, as mobile, now that nothing
// rewrites them, and then runs uninstall-app.
static int do_remove_tweak(void) {
    char listed[PATH_MAX];
    rp(listed, SPEAKA_DPKG_LIST);
    if (access(listed, F_OK) == 0) {
        char dpkg[PATH_MAX];
        rp(dpkg, SPEAKA_DPKG);
        if (access(dpkg, F_OK) != 0) {
            emit("ERR\tdpkg wasn't found at " SPEAKA_DPKG);
            return 9;
        }
        emit("LOG\tRemoving the tweak with dpkg");
        char *argv[] = { "dpkg", "-r", SPEAKA_PACKAGE, NULL };
        int code = run_and_wait(dpkg, argv, k_dpkg_env, 0);
        if (code == -1) {
            emit("ERR\tdpkg couldn't start (%s). Is the jailbreak active? Run it again, then try once more.", strerror(errno));
            return 8;
        }
        if (code != 0) {
            emit("ERR\tThe tweak didn't uninstall (dpkg exited with %d). Nothing else was removed.", code);
            return 8;
        }
    } else {
        emit("LOG\tThe tweak isn't installed");
    }
#ifdef SPEAKA_TEST
    emit("LOG\t(test) would kill bluetoothd and Preferences");
#else
    kill_named("bluetoothd");
    kill_named("Preferences");
#endif
    char folder[PATH_MAX];
    // Speaka*: this version's logs and audio-ring files. LEGACY_FILE_PREFIX: the same files from versions before the
    // helper and ring were renamed, so an upgraded phone is cleaned as fully as a new one.
    static const char *const logs[] = { "Speaka", LEGACY_FILE_PREFIX };
    rp(folder, SPEAKA_LOG_DIR);
    remove_matching(folder, logs, sizeof logs / sizeof logs[0]);
    static const char *const all[] = { "" };
    rp(folder, SPEAKA_CACHES);
    remove_matching(folder, all, 1);
    emit("LOG\tTweak removed; Bluetooth restarted");
    return 0;
}

// Uninstall, last: TrollStore removes the app, in a step of its own that outlives Speaka (removing the app closes it,
// and takes this helper with it). Nothing is logged: Speaka's log folder has just gone.
static int do_uninstall_app(void) {
    char helper[PATH_MAX];
    if (!find_trollstore_helper(helper)) {
        emit("ERR\tTrollStore's installer wasn't found");
        return 5;
    }
    char *argv[] = { "speakahelper", "finish-uninstall", NULL };
    if (start_detached(argv, "/dev/null") != 0) return 10;
    emit("LOG\tUninstalling the app");
    return 0;
}

static int do_finish_uninstall(void) {
    char helper[PATH_MAX];
    if (!find_trollstore_helper(helper)) {
        emit("ERR\tTrollStore's installer wasn't found");
        return 5;
    }
#ifndef SPEAKA_TEST
    sleep(1);                                       // let Speaka read that the last step started
#endif
    char *argv[] = { "trollstorehelper", "uninstall", SPEAKA_APP_ID, NULL };
    int code = run_and_wait(helper, argv, environ, 0);
    if (code == -1) {
        emit("ERR\tCouldn't start TrollStore's installer: %s", strerror(errno));
        return 6;
    }
    if (code != 0) {
        emit("ERR\tTrollStore's installer returned %d", code);
        return 7;
    }
    return 0;
}

static int do_finish_update(const char *tipa, int tweak_from_app, int resprings) {
    int code = tipa ? install_tipa(tipa) : 0;
    if (code == 0 && tweak_from_app) code = install_carried_tweak();   // the app TrollStore just installed
    if (resprings) respring();                      // Speaka has closed either way, so the screen is reset too
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
        emit("ERR\tusage: speakahelper install-update [--deb <path> | --tweak-from-app] [--tipa <path>] | "
             "finish-update [--tipa <path>] [--tweak-from-app] [--respring] | kill-bluetooth | remove-tweak | "
             "uninstall-app | version");
        return 2;
    }
    const char *deb = NULL, *tipa = NULL;
    int resprings = 0, tweak_from_app = 0;
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--deb") == 0 && i + 1 < argc) deb = argv[++i];
        else if (strcmp(argv[i], "--tipa") == 0 && i + 1 < argc) tipa = argv[++i];
        else if (strcmp(argv[i], "--respring") == 0) resprings = 1;
        else if (strcmp(argv[i], "--tweak-from-app") == 0) tweak_from_app = 1;
        else {
            emit("ERR\tUnknown option %s", argv[i]);
            return 2;
        }
    }
    if (strcmp(argv[1], "version") == 0) { emit("LOG\tspeakahelper %s", SPEAKA_HELPER_VERSION); return 0; }
    if (strcmp(argv[1], "kill-bluetooth") == 0) return do_kill_bluetooth();
    if (strcmp(argv[1], "remove-tweak") == 0) return do_remove_tweak();
    if (strcmp(argv[1], "uninstall-app") == 0) return do_uninstall_app();
    if (strcmp(argv[1], "finish-uninstall") == 0) return do_finish_uninstall();
    if (strcmp(argv[1], "install-update") == 0) return do_install_update(deb, tipa, tweak_from_app);
    if (strcmp(argv[1], "finish-update") == 0) return do_finish_update(tipa, tweak_from_app, resprings);
    emit("ERR\tUnknown command %s", argv[1]);
    return 2;
}
