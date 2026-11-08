//
//  SPRootSpawn.h
//  Speaka
//
//  Starts the update helper bundled in the app as root (posix_spawn with the root persona).
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Runs `path` as root (TrollStore persona spawn) and calls `onLine` for every line it
/// prints on stdout or stderr. Blocks until the process exits.
/// Returns its exit code, -1 if it couldn't be started, or -2 if it was killed.
int SpeakaSpawnRoot(NSString *path, NSArray<NSString *> *args, void (^onLine)(NSString *line));

NS_ASSUME_NONNULL_END
