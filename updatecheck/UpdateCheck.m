//
//  UpdateCheck.m - SpeakaUpdateCheck (v9.9.3): Settings > Update notifications, dependably.
//
//  The app's own background check (BGAppRefreshTask) runs only when iOS chooses - often hours apart, and never once
//  Speaka has been swiped away. This runs as mobile, kept running by launchd (v9.9.5: launchd's StartInterval doesn't
//  repeat on iOS - on the device it showed runs = 1 hours after install, with run interval = 900 s), and checks every
//  15 minutes by the wall clock. If the app's switch is on (mirrored
//  into Speaka's shared preferences), it asks GitHub for the releases - conditionally, so an unchanged list is a 304,
//  which doesn't count against GitHub's limit of 60 unauthenticated requests an hour - and when the newest release
//  that carries the app is newer than the installed Speaka and wasn't announced yet, it posts Speaka's notification.
//
//  It posts AS Speaka with -[UNUserNotificationCenter initWithBundleIdentifier:], allowed by
//  com.apple.private.usernotifications.bundle-identifiers = [com.parv.speaka] (entitlements.plist): the arrangement
//  Apple's mediaremoted and bluetoothd are signed with, each naming the one bundle it posts for. Nothing is announced
//  unless iOS reports Speaka's notifications as allowed, and a release counts as announced only once iOS accepted it.
//  Every run that checks writes one line to /tmp/FTABHarvest/SpeakaUpdateCheck.log.
//
//    SpeakaUpdateCheck --daemon   what launchd runs (v9.9.5): stays running, checks every 15 minutes
//    SpeakaUpdateCheck            one check, if the switch is on
//    SpeakaUpdateCheck --now      checks now, whatever the switch says
//    SpeakaUpdateCheck --test     posts a test notification now, to see that posting as Speaka works
//
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>
#include <stdarg.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "shared/SpeakaShared.h"
#include "shared/UpdatePolicy.h"

#define SPEAKA_PACKAGE_ID "com.parv.speaka"   /* the tweak's dpkg package (SpeakaUpdater.packageID) */

@interface UNUserNotificationCenter (SpeakaUpdateCheck)
- (instancetype)initWithBundleIdentifier:(NSString *)bundleIdentifier;
@end

static NSString *const kReleasesURL = @"https://api.github.com/repos/m9z2w4d8vw-blip/Speaka/releases?per_page=30";
static const char *const kLogDirectory = "/tmp/FTABHarvest";
static const char *const kLogPath = "/tmp/FTABHarvest/SpeakaUpdateCheck.log";
/* One line per call, opened each time: the checker runs for days, /tmp can be cleaned under it, and the file is
 * started afresh past 64 KB. */
static void ulog(const char *format, ...) __attribute__((format(printf, 1, 2)));
static void ulog(const char *format, ...) {
    char when[32];
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    strftime(when, sizeof when, "%Y-%m-%d %H:%M:%S", &local);
    va_list args;
    mkdir(kLogDirectory, 0777);                   /* /tmp is new after a reboot */
    struct stat st;
    FILE *log = fopen(kLogPath, stat(kLogPath, &st) == 0 && st.st_size > 64 * 1024 ? "w" : "a");
    if (log) {
        fprintf(log, "%s ", when);
        va_start(args, format); vfprintf(log, format, args); va_end(args);
        fputc('\n', log);
        fclose(log);
    }
    if (isatty(STDOUT_FILENO)) {                  /* run by hand: say it here too */
        va_start(args, format); vprintf(format, args); va_end(args);
        putchar('\n');
    }
}

static NSString *string_pref(CFStringRef key) {
    id value = CFBridgingRelease(CFPreferencesCopyAppValue(key, CFSTR(SPEAKA_PREFS_DOMAIN)));
    return [value isKindOfClass:[NSString class]] ? value : nil;
}

static void set_pref(CFStringRef key, id value) {
    CFPreferencesSetAppValue(key, (__bridge CFPropertyListRef)value, CFSTR(SPEAKA_PREFS_DOMAIN));
}

/* The installed tweak's Version in dpkg's database - only if it's installed, not just remembered after a removal
 * (SpeakaUpdater.packageVersion(in:package:)). */
static NSString *installed_version(void) {
    for (NSString *path in @[@"/var/jb/var/lib/dpkg/status", @"/var/lib/dpkg/status"]) {
        NSString *status = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
        if (!status) continue;
        for (NSString *stanza in [status componentsSeparatedByString:@"\n\n"]) {
            NSString *name = nil, *version = nil;
            BOOL installed = NO;
            for (NSString *line in [stanza componentsSeparatedByString:@"\n"]) {
                NSCharacterSet *space = [NSCharacterSet whitespaceCharacterSet];
                if ([line hasPrefix:@"Package: "]) name = [[line substringFromIndex:9] stringByTrimmingCharactersInSet:space];
                else if ([line hasPrefix:@"Version: "]) version = [[line substringFromIndex:9] stringByTrimmingCharactersInSet:space];
                else if ([line hasPrefix:@"Status: "]) installed = [line hasSuffix:@" installed"];
            }
            if ([name isEqualToString:@SPEAKA_PACKAGE_ID]) return installed ? version : nil;
        }
        return nil;
    }
    return nil;
}

/* The newest published release that carries the app (.tipa, or .ipa), as the app's SpeakaUpdater.parse sees them:
 * drafts and pre-releases ignored, the title being the release's name or else its tag. */
static BOOL newest_release(NSData *body, NSString **tag, NSString **title) {
    id list = [NSJSONSerialization JSONObjectWithData:body options:0 error:NULL];
    if (![list isKindOfClass:[NSArray class]]) return NO;
    NSString *best = nil, *bestTitle = nil;
    for (id release in list) {
        if (![release isKindOfClass:[NSDictionary class]]) continue;
        if ([release[@"draft"] boolValue] || [release[@"prerelease"] boolValue]) continue;
        NSString *name = [release[@"tag_name"] isKindOfClass:[NSString class]] ? release[@"tag_name"] : nil;
        if (!name.length || !strpbrk(name.UTF8String, "0123456789")) continue;
        BOOL app = NO;
        NSArray *assets = [release[@"assets"] isKindOfClass:[NSArray class]] ? release[@"assets"] : @[];
        for (id asset in assets) {
            NSString *file = [asset isKindOfClass:[NSDictionary class]] && [asset[@"name"] isKindOfClass:[NSString class]]
                ? [asset[@"name"] lowercaseString] : nil;
            if ([file hasSuffix:@".tipa"] || [file hasSuffix:@".ipa"]) { app = YES; break; }
        }
        if (!app) continue;
        if (!best || sp_version_compare(name.UTF8String, best.UTF8String) > 0) {
            NSString *heading = [release[@"name"] isKindOfClass:[NSString class]] ? release[@"name"] : nil;
            best = name;
            bestTitle = heading.length ? heading : name;
        }
    }
    if (!best) return NO;
    *tag = best;
    *title = bestTitle;
    return YES;
}

/* 1: posted. 0: Speaka's notifications aren't allowed (nothing posted). -1: posting failed. */
static int post_as_speaka(NSString *title, NSString *body, NSString *identifier) {
    if (![UNUserNotificationCenter instancesRespondToSelector:@selector(initWithBundleIdentifier:)]) {
        ulog("POST failed: this iOS has no -[UNUserNotificationCenter initWithBundleIdentifier:]");
        return -1;
    }
    UNUserNotificationCenter *center = [[UNUserNotificationCenter alloc] initWithBundleIdentifier:@SPEAKA_APP_BUNDLE_ID];
    if (!center) { ulog("POST failed: no notification center for " SPEAKA_APP_BUNDLE_ID); return -1; }
    dispatch_semaphore_t answered = dispatch_semaphore_create(0);
    __block NSInteger authorization = -1;
    __block NSInteger alerts = -1;
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
        authorization = settings.authorizationStatus;
        alerts = settings.alertSetting;
        dispatch_semaphore_signal(answered);
    }];
    if (dispatch_semaphore_wait(answered, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC))) {
        ulog("POST failed: iOS didn't report Speaka's notification settings (is the entitlement honoured?)");
        return -1;
    }
    /* UNAuthorizationStatus: 2 authorized, 3 provisional, 4 ephemeral (the ones that show a notification) */
    if (authorization != UNAuthorizationStatusAuthorized && authorization != 3 && authorization != 4) {
        ulog("POST skipped: Speaka's notifications aren't allowed (authorization %ld)", (long)authorization);
        return 0;
    }
    UNMutableNotificationContent *content = [UNMutableNotificationContent new];
    content.title = title;
    content.body = body;
    content.sound = [UNNotificationSound defaultSound];
    UNNotificationRequest *request = [UNNotificationRequest requestWithIdentifier:identifier content:content trigger:nil];
    __block NSError *failure = nil;
    [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
        failure = error;
        dispatch_semaphore_signal(answered);
    }];
    if (dispatch_semaphore_wait(answered, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC))) {
        ulog("POST failed: iOS didn't answer the request");
        return -1;
    }
    if (failure) {
        ulog("POST failed: %s", failure.description.UTF8String ?: "?");
        return -1;
    }
    ulog("POST ok: \"%s\" (alerts %s)", title.UTF8String ?: "", alerts == UNNotificationSettingEnabled ? "on" : "off");
    return 1;
}

/* One check: the newest release with the app; announced if it's newer than what's installed, wasn't announced yet,
 * and iOS allows Speaka's notifications. */
static void check_releases(void) {
    NSString *installed = installed_version();
    NSString *cached = string_pref(CFSTR(SPEAKA_KEY_UPDATE_NEWEST));      /* "tag\ttitle" from the last 200 */
    NSString *etag = cached ? string_pref(CFSTR(SPEAKA_KEY_UPDATE_ETAG)) : nil;
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:kReleasesURL]
                                                           cachePolicy:NSURLRequestReloadIgnoringLocalCacheData
                                                       timeoutInterval:20];
    [request setValue:@"application/vnd.github+json" forHTTPHeaderField:@"Accept"];
    [request setValue:@"Speaka-UpdateCheck" forHTTPHeaderField:@"User-Agent"];
    if (etag.length) [request setValue:etag forHTTPHeaderField:@"If-None-Match"];
    /* One session for the life of the process: a session per check would never be freed (v9.9.5: it runs for days). */
    static NSURLSession *session;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        session = [NSURLSession sessionWithConfiguration:[NSURLSessionConfiguration ephemeralSessionConfiguration]];
    });
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block NSData *body = nil;
    __block NSHTTPURLResponse *response = nil;
    __block NSError *error = nil;
    [[session dataTaskWithRequest:request completionHandler:^(NSData *data, NSURLResponse *answer, NSError *failure) {
        body = data;
        response = [answer isKindOfClass:[NSHTTPURLResponse class]] ? (NSHTTPURLResponse *)answer : nil;
        error = failure;
        dispatch_semaphore_signal(done);
    }] resume];
    if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC))) {
        ulog("CHECK failed: GitHub didn't answer in 30 s");
        return;
    }
    NSString *tag = nil, *title = nil;
    if (error || !response) {
        ulog("CHECK failed: %s", error.localizedDescription.UTF8String ?: "no answer");
        return;
    }
    if (response.statusCode == 304 && cached) {
        NSArray *parts = [cached componentsSeparatedByString:@"\t"];
        if (parts.count == 2) { tag = parts[0]; title = parts[1]; }
    } else if (response.statusCode == 200 && body) {
        if (!newest_release(body, &tag, &title)) {
            ulog("CHECK: no published release carries the app");
            return;
        }
        NSString *answerTag = response.allHeaderFields[@"ETag"];
        set_pref(CFSTR(SPEAKA_KEY_UPDATE_NEWEST), [NSString stringWithFormat:@"%@\t%@", tag, title]);
        set_pref(CFSTR(SPEAKA_KEY_UPDATE_ETAG), answerTag.length ? answerTag : nil);
    } else {
        NSString *remaining = response.allHeaderFields[@"X-RateLimit-Remaining"];
        ulog("CHECK failed: GitHub answered %ld%s", (long)response.statusCode,
             [remaining isEqualToString:@"0"] ? " (its hourly limit is used up)" : "");
        return;
    }
    if (!tag) { ulog("CHECK failed: the cached release couldn't be read"); return; }
    set_pref(CFSTR(SPEAKA_KEY_UPDATE_CHECKED), @(CFAbsoluteTimeGetCurrent()));
    NSString *announced = string_pref(CFSTR(SPEAKA_KEY_UPDATE_ANNOUNCED));
    if (!sp_update_should_announce(installed.UTF8String, tag.UTF8String, title.UTF8String, announced.UTF8String)) {
        ulog("CHECK %ld: installed %s, newest %s, announced %s - nothing to announce", (long)response.statusCode,
             installed.UTF8String ?: "unknown", tag.UTF8String, announced.UTF8String ?: "none");
        (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
        return;
    }
    int posted = post_as_speaka([NSString stringWithFormat:@"Speaka %@ is ready", title], @"Tap to update.",
                                [@"update-" stringByAppendingString:title]);
    if (posted > 0) set_pref(CFSTR(SPEAKA_KEY_UPDATE_ANNOUNCED), title);   /* only once iOS accepted it */
    ulog("CHECK %ld: installed %s, newest %s - %s", (long)response.statusCode, installed.UTF8String ?: "unknown",
         tag.UTF8String, posted > 0 ? "announced" : posted == 0 ? "not allowed to announce" : "posting failed");
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
}

/* v9.9.5: the switch, as the app last saved it - re-read each time, since this process runs for days. */
static int switch_on(void) {
    (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
    return CFPreferencesGetAppBooleanValue(CFSTR(SPEAKA_KEY_UPDATE_NOTIFY), CFSTR(SPEAKA_PREFS_DOMAIN), NULL) ? 1 : 0;
}

/* v9.9.5: every 15 minutes by the wall clock. Mach time - and with it sleep() - stops while the iPhone sleeps, so it
 * wakes each minute it's awake, compares the real time, and checks once 15 minutes of it have passed: right after a
 * long sleep, too. Turning the switch on checks at once; each change of the switch is logged, so a skipped check
 * never goes unseen. */
#define CHECK_EVERY_SECONDS 900
#define WAKE_EVERY_SECONDS 60
static void run_daemon(void) {
    ulog("DAEMON started: checks every %d minutes while Update notifications is on", CHECK_EVERY_SECONDS / 60);
    int was_on = -1;
    time_t next = 0;
    for (;;) {
        int on = switch_on();
        if (on != was_on) {
            ulog("%s", on ? "SWITCH on: checking" : "SWITCH off: no checks until Update notifications is turned on");
            if (on) next = 0;
            was_on = on;
        }
        if (on && time(NULL) >= next) {
            @autoreleasepool { check_releases(); }
            next = time(NULL) + CHECK_EVERY_SECONDS;
        }
        sleep(WAKE_EVERY_SECONDS);
    }
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        BOOL test = argc > 1 && strcmp(argv[1], "--test") == 0;
        BOOL now = argc > 1 && strcmp(argv[1], "--now") == 0;
        BOOL daemon = argc > 1 && strcmp(argv[1], "--daemon") == 0;
        (void)CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
        if (test) {
            int posted = post_as_speaka(@"Speaka update notifications work",
                                        @"This is a test from SpeakaUpdateCheck.", @"update-test");
            ulog("TEST %s", posted > 0 ? "posted" : posted == 0 ? "not allowed" : "failed");
            return posted > 0 ? 0 : 1;
        }
        if (daemon) {
            run_daemon();                                     /* never returns */
            return 0;
        }
        if (!now && !switch_on()) return 0;                   /* one check, only if the switch is on */
        check_releases();
    }
    return 0;
}
