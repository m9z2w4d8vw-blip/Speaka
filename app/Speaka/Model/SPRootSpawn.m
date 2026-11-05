//
//  SPRootSpawn.m
//  Speaka
//
//  Caz's RootSpawn.m, renamed. The app needs com.apple.private.persona-mgmt for this (Speaka.entitlements).
//

#import "SPRootSpawn.h"
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <stdlib.h>

// Private libsystem calls that let an app with com.apple.private.persona-mgmt
// start a child process as root. TrollStore apps use the same approach.
#define POSIX_SPAWN_PERSONA_FLAGS_OVERRIDE 1
extern int posix_spawnattr_set_persona_np(const posix_spawnattr_t * __restrict, uid_t, uint32_t);
extern int posix_spawnattr_set_persona_uid_np(const posix_spawnattr_t * __restrict, uid_t);
extern int posix_spawnattr_set_persona_gid_np(const posix_spawnattr_t * __restrict, uid_t);

static void SpeakaEmitLines(NSMutableData *pending, BOOL flushAll, void (^onLine)(NSString *)) {
    const char *bytes = pending.bytes;
    NSUInteger length = pending.length, start = 0;
    for (NSUInteger i = 0; i < length; i++) {
        if (bytes[i] != '\n') continue;
        NSString *line = [[NSString alloc] initWithBytes:bytes + start length:i - start encoding:NSUTF8StringEncoding];
        if (line) onLine(line);
        start = i + 1;
    }
    if (flushAll && start < length) {
        NSString *line = [[NSString alloc] initWithBytes:bytes + start length:length - start encoding:NSUTF8StringEncoding];
        if (line) onLine(line);
        start = length;
    }
    if (start > 0) [pending replaceBytesInRange:NSMakeRange(0, start) withBytes:NULL length:0];
}

int SpeakaSpawnRoot(NSString *path, NSArray<NSString *> *args, void (^onLine)(NSString *line)) {
    NSMutableArray<NSString *> *all = [NSMutableArray arrayWithObject:path];
    [all addObjectsFromArray:args];
    NSUInteger argc = all.count;
    char **argv = calloc(argc + 1, sizeof(char *));
    if (!argv) return -1;
    for (NSUInteger i = 0; i < argc; i++) argv[i] = strdup(all[i].UTF8String);

    int fds[2];
    if (pipe(fds) != 0) {
        for (NSUInteger i = 0; i < argc; i++) free(argv[i]);
        free(argv);
        onLine([NSString stringWithFormat:@"ERR\tCouldn't create a pipe: %s", strerror(errno)]);
        return -1;
    }

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_set_persona_np(&attr, 99, POSIX_SPAWN_PERSONA_FLAGS_OVERRIDE);
    posix_spawnattr_set_persona_uid_np(&attr, 0);
    posix_spawnattr_set_persona_gid_np(&attr, 0);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, fds[1], STDOUT_FILENO);
    posix_spawn_file_actions_adddup2(&actions, fds[1], STDERR_FILENO);
    posix_spawn_file_actions_addclose(&actions, fds[0]);

    pid_t pid = 0;
    int spawnError = posix_spawn(&pid, argv[0], &actions, &attr, argv, NULL);

    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attr);
    for (NSUInteger i = 0; i < argc; i++) free(argv[i]);
    free(argv);
    close(fds[1]);

    if (spawnError != 0) {
        close(fds[0]);
        onLine([NSString stringWithFormat:@"ERR\tCouldn't start the root helper: %s", strerror(spawnError)]);
        return -1;
    }

    NSMutableData *pending = [NSMutableData data];
    char buffer[8192];
    for (;;) {
        ssize_t n = read(fds[0], buffer, sizeof buffer);
        if (n > 0) {
            [pending appendBytes:buffer length:(NSUInteger)n];
            SpeakaEmitLines(pending, NO, onLine);
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            break;
        }
    }
    SpeakaEmitLines(pending, YES, onLine);
    close(fds[0]);

    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -2;
}
