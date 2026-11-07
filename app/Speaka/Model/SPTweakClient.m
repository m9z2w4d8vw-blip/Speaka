//
//  SPTweakClient.m
//  Speaka
//
//  Every rule here mirrors settings/SettingsPage.m (the Settings page), so the app and Settings
//  always agree about the speaker's state. tests/test_app.py checks that they stay in step.
//

#import "SPTweakClient.h"
#import <AVFoundation/AVFoundation.h>
#import <CoreFoundation/CoreFoundation.h>
#include <notify.h>
#import "shared/SpeakaShared.h"
#include "shared/PeerName.h"

#pragma mark - preferences (same domain and scope as the tweak and the Settings page)

static void SPSynchronize(void)
{
    CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
}

static id SPCopyPref(NSString *key)   // call SPSynchronize() first
{
    return CFBridgingRelease(CFPreferencesCopyAppValue((__bridge CFStringRef)key, CFSTR(SPEAKA_PREFS_DOMAIN)));
}

static BOOL SPWritePref(NSString *key, id value)   // nil removes the key
{
    CFPreferencesSetAppValue((__bridge CFStringRef)key, (__bridge CFPropertyListRef)value, CFSTR(SPEAKA_PREFS_DOMAIN));
    return CFPreferencesAppSynchronize(CFSTR(SPEAKA_PREFS_DOMAIN));
}

static NSString *SPString(id value)
{
    return [value isKindOfClass:[NSString class]] ? value : @"";
}

/// v9.9.6: the verified name cache - iOS's own name/address pairs from route captures, at most PEER_CACHE_SIZE,
/// least recently seen evicted (shared/PeerName.h). It names a connection only when the connection's own address
/// (the tweak, tweak/PeerIdentity.c) matches one of its entries.
static NSString *const SPPeerCacheKey = @SPEAKA_APP_KEY_PEER_NAMES;

static void SPPeerCacheLoad(peer_cache_t *cache)
{
    memset(cache, 0, sizeof *cache);
    for (id item in [[NSUserDefaults standardUserDefaults] arrayForKey:SPPeerCacheKey]) {
        if (![item isKindOfClass:[NSDictionary class]]) continue;
        NSString *address = SPString(((NSDictionary *)item)[@"address"]), *name = SPString(((NSDictionary *)item)[@"name"]);
        id seen = ((NSDictionary *)item)[@"seen"];
        if (address.length && name.length)
            peer_cache_learn(cache, address.UTF8String, name.UTF8String, [seen isKindOfClass:[NSNumber class]] ? [seen doubleValue] : 0);
    }
}

static void SPPeerCacheSave(const peer_cache_t *cache)
{
    NSMutableArray *entries = [NSMutableArray array];
    for (unsigned i = 0; i < cache->n; i++)
        [entries addObject:@{ @"address": [NSString stringWithUTF8String:cache->e[i].address] ?: @"",
                              @"name": [NSString stringWithUTF8String:cache->e[i].name] ?: @"",
                              @"seen": @(cache->e[i].seen) }];
    [[NSUserDefaults standardUserDefaults] setObject:entries forKey:SPPeerCacheKey];
}

static NSDictionary *SPDaemonStatus(void)
{
    id status = SPCopyPref(@SPEAKA_KEY_STATUS);
    return [status isKindOfClass:[NSDictionary class]] ? status : @{};
}

#pragma mark - output names (as BTPSTypeLabel / BTPSOutputName / BTPSOutputsDescription)

static NSString *SPTypeLabel(NSString *type)
{
    if ([type isEqualToString:AVAudioSessionPortBluetoothA2DP] || [type isEqualToString:AVAudioSessionPortBluetoothLE] ||
        [type isEqualToString:AVAudioSessionPortBluetoothHFP]) return @"Bluetooth";
    if ([type isEqualToString:AVAudioSessionPortHeadphones]) return @"wired";
    if ([type isEqualToString:AVAudioSessionPortUSBAudio]) return @"USB";
    if ([type isEqualToString:AVAudioSessionPortAirPlay]) return @"AirPlay";
    if ([type isEqualToString:AVAudioSessionPortCarAudio]) return @"CarPlay";
    return nil;
}

static NSString *SPOutputName(NSDictionary *output)
{
    NSString *type = SPString(output[@"type"]);
    if ([type isEqualToString:AVAudioSessionPortBuiltInSpeaker]) return @"iPhone speaker";
    if ([type isEqualToString:AVAudioSessionPortBuiltInReceiver]) return @"iPhone earpiece";
    NSString *name = SPString(output[@"name"]);
    if (!name.length) name = @"(unnamed device)";
    NSString *label = SPTypeLabel(type);
    return label ? [NSString stringWithFormat:@"%@ (%@)", name, label] : name;
}

/// v9.6: the name of a Bluetooth output in an audio route ({name, type} dictionaries, as the helper publishes them).
/// iOS publishes the streaming PC as a Bluetooth audio device and, while the speaker is idle, routes the phone's audio
/// to it: that's where the PC's name shows (outputs=LAPTOP-UGIVBIA5 [BluetoothA2DPOutput] in the 10:22 log). A Windows
/// default name (DESKTOP-..., LAPTOP-...) wins over other Bluetooth outputs.
static NSString *SPBluetoothOutputName(id outputs)
{
    if (![outputs isKindOfClass:[NSArray class]]) return @"";
    NSString *first = nil;
    for (id output in (NSArray *)outputs) {
        if (![output isKindOfClass:[NSDictionary class]]) continue;
        NSString *type = SPString(output[@"type"]);
        if (![type isEqualToString:AVAudioSessionPortBluetoothA2DP] && ![type isEqualToString:AVAudioSessionPortBluetoothHFP] &&
            ![type isEqualToString:AVAudioSessionPortBluetoothLE])
            continue;
        NSString *name = SPString(output[@"name"]);
        if (name.length == 0) continue;
        NSString *upper = name.uppercaseString;
        if ([upper hasPrefix:@"DESKTOP-"] || [upper hasPrefix:@"LAPTOP-"]) return name;
        if (!first) first = name;
    }
    return first ?: @"";
}

/// This app's own audio route, in the helper's shape. Reading it plays nothing and changes nothing.
static NSArray *SPOwnRouteOutputs(void)
{
    NSMutableArray *outputs = [NSMutableArray array];
    for (AVAudioSessionPortDescription *port in [AVAudioSession sharedInstance].currentRoute.outputs)
        [outputs addObject:@{ @"name": port.portName ?: @"", @"type": port.portType ?: @"" }];
    return outputs;
}

static NSString *SPOutputsDescription(id outputs)
{
    if (![outputs isKindOfClass:[NSArray class]]) return @"";
    NSMutableArray<NSString *> *names = [NSMutableArray array];
    for (id output in (NSArray *)outputs)
        if ([output isKindOfClass:[NSDictionary class]]) [names addObject:SPOutputName(output)];
    return [names componentsJoinedByString:@" + "];
}

#pragma mark - SPStatus

@implementation SPStatus

- (instancetype)init
{
    if (self = [super init]) {
        _detail = @"";
        _build = @"";
        _outputMode = @"speaker";
        _playingOn = @"";
        _waitingReason = @"";
        _diagnostics = @{};
        _routeName = @"";
        _routeUID = @"";
        _peerName = @"";
        _peerAddress = @"";
    }
    return self;
}

- (BOOL)canStart
{
    return self.session != SPSessionOn && self.session != SPSessionStarting;
}

- (BOOL)canStop
{
    return self.session == SPSessionOn || self.session == SPSessionStarting || self.session == SPSessionLost ||
           self.session == SPSessionFailed;
}

@end

#pragma mark - SPRequest

@interface SPRequest ()
@property (nonatomic, copy, readwrite) NSString *action;
@property (nonatomic, copy, readwrite) NSString *nonce;
@property (nonatomic, strong, readwrite) NSDate *started;
@end

@implementation SPRequest
@end

#pragma mark - SPTweakClient

@implementation SPTweakClient

+ (instancetype)sharedClient
{
    static SPTweakClient *client = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        client = [[SPTweakClient alloc] init];
    });
    return client;
}

+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes
{
    // Titles match the "Play audio on" list in settings/Resources/Root.plist.
    return @[
        @{ @"key": @"speaker", @"title": @"iPhone speaker", @"shortTitle": @"iPhone speaker",
           @"symbol": @"speaker.wave.2.fill", @"detail": @"Always the built-in loudspeaker" },
        @{ @"key": @"headphones", @"title": @"Wired headphones or earbuds", @"shortTitle": @"Wired headphones",
           @"symbol": @"headphones", @"detail": @"A plugged-in headset; never Bluetooth" },
        @{ @"key": @"any", @"title": @"Any device (follow iOS)", @"shortTitle": @"Any device",
           @"symbol": @"airplayaudio", @"detail": @"Wherever iOS plays, including Bluetooth" },
    ];
}

- (SPStatus *)readStatus
{
    SPSynchronize();
    SPStatus *s = [[SPStatus alloc] init];
    NSDictionary *status = SPDaemonStatus();
    s.diagnostics = status ?: @{};   // the app's Start and Stop steps read the tweak's own statuses
    s.build = SPString(status[@"diag.v89.build"]);

    // The same rules, in the same order, as BTPSSessionSummary() in the Settings page.
    NSString *trial = SPString(status[@"diag.v82.trial"]);
    NSString *sep = SPString(status[@"diag.v82.sep"]);
    NSString *table = SPString(status[@"diag.v83.audio_table"]);
    if (status.count == 0) {
        s.session = SPSessionNoTweak;
    } else if ([sep hasPrefix:@"LOST"]) {
        s.session = SPSessionLost;
    } else if (!status[@"diag.v89.build"]) {
        s.session = SPSessionOldTweak;
    } else if ([trial hasPrefix:@"ARMED"]) {
        s.session = SPSessionStarting;
        for (NSString *bad in @[ @"REFUSED", @"ROLLED BACK", @"UNSAFE", @"FAILED", @"SDP publish failed" ]) {
            if ([table hasPrefix:bad]) {
                s.session = SPSessionFailed;
                s.detail = table;
                break;
            }
        }
        if (s.session == SPSessionStarting && [sep containsString:@"register=0"] && [sep containsString:@"SEID=7"])
            s.session = SPSessionOn;
    } else if ([trial hasPrefix:@"REFUSED"]) {
        s.session = SPSessionRefused;
        s.detail = trial;
    } else {
        s.session = SPSessionOff;
    }

    // What BTPSAudioHelper last reported, ignored once it is more than 20 s old (as in Settings).
    id helperValue = SPCopyPref(@SPEAKA_KEY_HELPER_STATUS);
    NSDictionary *helper = [helperValue isKindOfClass:[NSDictionary class]] ? helperValue : nil;
    id when = helper[@"time"];
    double time = [when isKindOfClass:[NSNumber class]] ? [when doubleValue] : 0.0;
    if (helper && [NSDate date].timeIntervalSince1970 - time > 20.0)
        helper = nil;
    NSString *state = SPString(helper[@"state"]);
    s.playing = [state isEqualToString:@"playing"];
    s.receiving = s.session == SPSessionOn && (s.playing || [state isEqualToString:@"waiting"]);
    s.waitingReason = [state isEqualToString:@"waiting"] ? SPString(helper[@"reason"]) : @"";
    s.playingOn = s.playing ? SPOutputsDescription(helper[@"outputs"]) : @"";

    // v9.6: a PC is connected while it streams, while it's paused (the helper idles, still attached), and from when it
    // sets the stream up (the tweak's diag.v95.peer), until it closes the stream or the helper's session ends.
    NSString *peer = SPString(status[@"diag.v95.peer"]);
    BOOL helperAttached = helper && ([state isEqualToString:@"playing"] || [state isEqualToString:@"waiting"] ||
                                     [state isEqualToString:@"idle"]);
    BOOL helperEnded = helper && [state isEqualToString:@"stopped"];
    BOOL peerUp = [peer hasPrefix:@"configured"] || [peer hasPrefix:@"streaming"] || [peer hasPrefix:@"paused"];
    s.connected = s.session == SPSessionOn && (helperAttached || (peerUp && !helperEnded));
    NSString *routeName = SPBluetoothOutputName(helper[@"outputs"]);
    NSString *routeUID = @"";
    // v9.9.7: the connection's own address and name - the tweak reads them from bluetoothd's Device for this stream
    // when the PC configures the speaker, before any audio - and iOS's name/address pair for the PC if a route listed it
    // during this connection (the helper's capture).
    NSString *connection = SPString(status[@"diag.v105.peer_address"]);
    NSString *connectionName = SPString(status[@"diag.v105.peer_name"]);
    NSString *captureName = @"", *captureUID = @"";
    id pc = helper[@"pc"];
    if ([pc isKindOfClass:[NSDictionary class]]) {
        captureName = SPString(((NSDictionary *)pc)[@"name"]);
        captureUID = SPString(((NSDictionary *)pc)[@"uid"]);
    }
    // A capture of another device than this connection's is never used for it.
    BOOL captureIsConnection = connection.length == 0 || peer_address_equal(connection.UTF8String, captureUID.UTF8String);
    if (captureName.length && captureIsConnection) {
        if (routeName.length == 0)
            routeName = captureName;
        routeUID = captureUID;
    }
    if (routeName.length == 0 && s.session == SPSessionOn)
        routeName = SPBluetoothOutputName(SPOwnRouteOutputs());
    s.routeName = routeName;
    s.routeUID = routeUID;
    // Learn verified pairs (the connection's own, a route capture's; a new PC, or a renamed one), then name the
    // connection: by its own name, else by its address.
    peer_cache_t cache;
    SPPeerCacheLoad(&cache);
    BOOL learned = NO;
    NSString *pairs[2][2] = { { connection, connectionName }, { captureUID, captureName } };
    for (int i = 0; i < 2; i++) {
        NSString *address = pairs[i][0], *name = pairs[i][1];
        if (address.length == 0 || name.length == 0) continue;
        const char *known = peer_cache_lookup(&cache, address.UTF8String);
        if (!known || strcmp(known, name.UTF8String) != 0) {
            peer_cache_learn(&cache, address.UTF8String, name.UTF8String, [[NSDate date] timeIntervalSince1970]);
            learned = YES;
        }
    }
    if (learned)
        SPPeerCacheSave(&cache);
    const char *resolved = peer_resolve(connection.length ? connection.UTF8String : NULL, connectionName.UTF8String,
                                        captureUID.UTF8String, captureName.UTF8String, &cache);
    s.peerAddress = connection;
    s.peerName = resolved ? ([NSString stringWithUTF8String:resolved] ?: @"") : @"";
    id volume = helper[@"volume"];
    s.volumeZero = s.playing && [volume isKindOfClass:[NSNumber class]] && [volume doubleValue] < 0.05;
    // v9.9.11: the negotiated codec of the stream playing now, as the helper decodes it - never the advertised
    // codecs or a setting. Empty unless a fresh status says it's playing (stale status is dropped above).
    NSString *codec = SPString(helper[@"codec"]);
    s.activeCodec = (s.session == SPSessionOn && s.playing &&
                     ([codec isEqualToString:@"AAC"] || [codec isEqualToString:@"SBC"])) ? codec : @"";

    NSString *mode = SPString(SPCopyPref(@SPEAKA_KEY_OUTPUT_MODE));
    s.outputMode = [[[SPTweakClient outputModes] valueForKey:@"key"] containsObject:mode] ? mode : @"speaker";
    return s;
}

- (BOOL)setOutputMode:(NSString *)mode
{
    if (![[[SPTweakClient outputModes] valueForKey:@"key"] containsObject:mode])
        return NO;
    if (!SPWritePref(@SPEAKA_KEY_OUTPUT_MODE, mode))
        return NO;
    // The audio helper re-reads the mode when this is posted, so the change applies mid-song.
    notify_post(SPEAKA_NOTIFY_OUTPUT);
    return YES;
}

// Writes the request, then wakes the tweak inside bluetoothd. The tweak validates and deletes the request, sets or
// clears the two one-shot switches itself and restarts bluetoothd. The app never writes those switches directly.
- (SPRequest *)sendAction:(NSString *)action error:(NSString **)error
{
    NSString *nonce = [NSUUID UUID].UUIDString;
    NSDictionary *request = @{ @"action": action, @"nonce": nonce, @"time": @([NSDate date].timeIntervalSince1970) };
    if (!SPWritePref(@SPEAKA_KEY_SESSION_REQUEST, request)) {
        if (error) *error = @"Could not save the request (preferences did not synchronize). Nothing was changed.";
        return nil;
    }
    uint32_t posted = notify_post(SPEAKA_NOTIFY_SESSION);
    if (posted != NOTIFY_STATUS_OK) {
        SPWritePref(@SPEAKA_KEY_SESSION_REQUEST, nil);
        if (error) *error = [NSString stringWithFormat:@"Could not reach Bluetooth (notify_post=%u). Nothing was changed.", posted];
        return nil;
    }
    SPRequest *pending = [[SPRequest alloc] init];
    pending.action = action;
    pending.nonce = nonce;
    pending.started = [NSDate date];
    return pending;
}

- (SPRequestState)finish:(SPRequest *)request ok:(BOOL)ok title:(NSString *)title message:(NSString *)message
{
    request.succeeded = ok;
    request.resultTitle = title;
    request.resultMessage = message;
    return SPRequestFinished;
}

// The state machine of -pollSession in the Settings page.
- (SPRequestState)pollRequest:(SPRequest *)request
{
    SPSynchronize();
    NSDictionary *status = SPDaemonStatus();
    NSString *session = SPString(status[@SPEAKA_STATUS_SESSION]);
    BOOL mine = [session containsString:[@"nonce=" stringByAppendingString:request.nonce]];
    BOOL start = [request.action isEqualToString:@"start"];
    NSTimeInterval elapsed = -[request.started timeIntervalSinceNow];

    if (mine && [session hasPrefix:@"REFUSED"])
        return [self finish:request ok:NO title:@"Bluetooth was not restarted" message:session];
    if (mine && !request.relaunched) {
        request.relaunched = YES;
        if (!start)
            return [self finish:request ok:YES title:@"Bluetooth restarted"
                        message:@"The speaker is off and Bluetooth is back to normal."];
        if ([session containsString:@"result=REFUSED"]) {
            NSString *why = SPString(status[@"diag.v82.trial"]);
            return [self finish:request ok:NO title:@"Speaker not started"
                        message:[NSString stringWithFormat:@"Bluetooth restarted, but the tweak refused the trial.\n\n%@",
                                                           why.length ? why : session]];
        }
    }
    if (request.relaunched && start) {
        SPStatus *now = [self readStatus];
        if (now.session == SPSessionOn)
            return [self finish:request ok:YES title:@"The iPhone is now a Bluetooth speaker"
                        message:@"On your PC, connect to this iPhone and choose it as the audio output.\n\n"
                                @"This lasts until Bluetooth restarts. Turning Bluetooth or Airplane Mode off and on "
                                @"also removes it; tap Start again after any of these."];
        if (now.session == SPSessionFailed || now.session == SPSessionRefused)
            return [self finish:request ok:NO title:@"Speaker not started" message:now.detail];
    }
    if (elapsed > (request.relaunched ? 45.0 : 25.0)) {
        // Never suggest turning Bluetooth off and on: that does NOT restart bluetoothd, and it removes a running
        // speaker endpoint until the next restart.
        NSString *message = request.relaunched
            ? @"Bluetooth restarted and the speaker is armed, but its registration has not been reported yet. "
              @"Make sure Bluetooth is turned on, then check again in a few seconds."
            : @"Bluetooth did not report a restart. If devices do not reconnect within a minute, restart the Bluetooth "
              @"daemon with your usual sudo killall -9 bluetoothd command, then try Start again.";
        return [self finish:request ok:NO title:@"Still waiting" message:message];
    }
    return SPRequestWaiting;
}

#pragma mark - The app's own settings

static NSString *const SPUpdateNotificationsKey = @"updateNotifications";
static NSString *const SPNotifiedVersionKey = @"notifiedUpdateVersion";

+ (BOOL)updateNotificationsEnabled
{
    return [[NSUserDefaults standardUserDefaults] boolForKey:SPUpdateNotificationsKey];
}

// v9.9.11: the AAC experiment's switches are gone - AAC-LC and SBC are both offered, the PC chooses, and the
// helper decodes whichever it negotiated. Their saved values are removed once (nothing reads them).
+ (void)removeLegacyCodecSettings
{
    if (SPCopyPref(@SPEAKA_KEY_AAC) != nil) SPWritePref(@SPEAKA_KEY_AAC, nil);
    if (SPCopyPref(@SPEAKA_KEY_AAC_SINK) != nil) SPWritePref(@SPEAKA_KEY_AAC_SINK, nil);
}

+ (void)setUpdateNotificationsEnabled:(BOOL)enabled
{
    [[NSUserDefaults standardUserDefaults] setBool:enabled forKey:SPUpdateNotificationsKey];
}

+ (void)removeLeftoverPreferences
{
    CFStringRef domain = CFSTR(SPEAKA_PREFS_DOMAIN);
    CFArrayRef keys = CFPreferencesCopyKeyList(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    if (keys) {
        for (NSString *key in (__bridge NSArray *)keys) {
            if ([key hasPrefix:@"Speaka"])            // never Bluetooth's own settings
                CFPreferencesSetValue((__bridge CFStringRef)key, NULL, domain,
                                      kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
        }
        CFRelease(keys);
        CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    }
    NSString *app = [NSBundle mainBundle].bundleIdentifier;
    if (app.length) [[NSUserDefaults standardUserDefaults] removePersistentDomainForName:app];
}

+ (NSString *)notifiedVersion
{
    id version = [[NSUserDefaults standardUserDefaults] objectForKey:SPNotifiedVersionKey];
    return [version isKindOfClass:[NSString class]] ? version : nil;
}

+ (void)setNotifiedVersion:(NSString *)version
{
    [[NSUserDefaults standardUserDefaults] setObject:version forKey:SPNotifiedVersionKey];
}

@end
