//
//  SPTweakClient.h
//  Speaka
//
//  Talks to the Speaka tweak exactly like Settings > Speaka does: through the com.apple.bluetooth
//  preferences and the Darwin notifications defined in shared/SpeakaShared.h at the repository root.
//
//  The SwiftUI screens call it through Speaka-Bridging-Header.h. Every method they use has its Swift name spelled
//  out (NS_SWIFT_NAME), so Swift's automatic renaming can never change it: pollRequest: would otherwise arrive
//  in Swift as poll(_:).
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, SPSession) {
    SPSessionNoTweak = 0,   // no status from the tweak at all
    SPSessionOldTweak,      // bluetoothd still runs a build without the Start/Stop listener
    SPSessionOff,
    SPSessionStarting,      // armed; the speaker endpoint is not registered yet
    SPSessionOn,
    SPSessionLost,          // Bluetooth was turned off and on, which removes the speaker endpoint
    SPSessionFailed,
    SPSessionRefused,
};

/// One snapshot of what the tweak and its audio helper report.
@interface SPStatus : NSObject
@property (nonatomic) SPSession session;
@property (nonatomic, copy) NSString *detail;        // failure or refusal text, else empty
@property (nonatomic, copy) NSString *build;         // diag.v89.build, e.g. "v8.9.4-one-tap-output-select"
@property (nonatomic, copy) NSString *outputMode;    // "speaker" | "headphones" | "any"
@property (nonatomic) BOOL receiving;                // a PC is streaming: the helper is playing or waiting
@property (nonatomic) BOOL playing;                  // audio is coming out somewhere
@property (nonatomic, copy) NSString *playingOn;     // e.g. "iPhone speaker", "Headphones (wired)"
@property (nonatomic, copy) NSString *waitingReason; // e.g. "no wired headphones plugged in"
@property (nonatomic) BOOL volumeZero;
@property (nonatomic, copy) NSString *activeCodec;   // v9.9.11: "AAC" or "SBC" - the codec the helper is decoding for the
                                                     // stream playing now; empty when nothing plays or the status is stale
@property (nonatomic, copy) NSDictionary<NSString *, id> *diagnostics; // the tweak's whole status block, as published
@property (nonatomic) BOOL connected;                // v9.6: a PC is connected: streaming, paused, or just set up
@property (nonatomic, copy) NSString *routeName;     // v9.6: a Bluetooth output's name in the audio route, else empty
@property (nonatomic, copy) NSString *routeUID;      // v9.9.4: that output's UID (it carries the PC's address), else empty
@property (nonatomic, copy) NSString *peerName;      // v9.9.6: this connection's PC, named by its address, else empty
@property (nonatomic, copy) NSString *peerAddress;   // v9.9.6: the address the tweak read from the connection, else empty
@property (nonatomic, readonly) BOOL canStart;
@property (nonatomic, readonly) BOOL canStop;
@end

typedef NS_ENUM(NSInteger, SPRequestState) {
    SPRequestWaiting = 0,
    SPRequestFinished,
};

/// A Start or Stop request in flight; follow it with -[SPTweakClient pollRequest:].
@interface SPRequest : NSObject
@property (nonatomic, copy, readonly) NSString *action;   // "start" | "stop"
@property (nonatomic, copy, readonly) NSString *nonce;
@property (nonatomic, strong, readonly) NSDate *started;
@property (nonatomic) BOOL relaunched;                    // bluetoothd came back and answered this nonce
@property (nonatomic) BOOL succeeded;
@property (nonatomic, copy, nullable) NSString *resultTitle;
@property (nonatomic, copy, nullable) NSString *resultMessage;
@end

typedef NS_ENUM(NSInteger, SPRoomVerdict) {
    SPRoomNotReported,      // no result yet
    SPRoomDone,             // the expected endpoint slots exist
    SPRoomFailed,           // a result that isn't the expected count for its layout
};

@interface SPTweakClient : NSObject
+ (instancetype)sharedClient;
/// The three output modes, in Settings order: key, title, shortTitle, symbol, detail.
+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes NS_SWIFT_NAME(outputModes());
- (SPStatus *)readStatus NS_SWIFT_NAME(readStatus());
- (BOOL)setOutputMode:(NSString *)mode NS_SWIFT_NAME(setOutputMode(_:));
- (nullable SPRequest *)sendAction:(NSString *)action error:(NSString *_Nullable *_Nullable)error
    NS_SWIFT_NAME(sendAction(_:error:));
- (SPRequestState)pollRequest:(SPRequest *)request NS_SWIFT_NAME(pollRequest(_:));
/// The app's own settings (v9.7), in the app's own preferences, never com.apple.bluetooth's: whether Settings > Update
/// notifications is on, and the last release a notification announced, so each is announced once.
+ (BOOL)updateNotificationsEnabled NS_SWIFT_NAME(updateNotificationsEnabled());
// v9.9.11: AAC-LC (preferred) and SBC are both standard - no codec switches. Removes the AAC experiment's saved
// switches (SpeakaAAC, SpeakaAACSink) once; nothing reads them any more.
+ (void)removeLegacyCodecSettings NS_SWIFT_NAME(removeLegacyCodecSettings());
// v9.9.1: the live endpoint-count result (diag.v81_seven_slots): done only when the count was readable, equals
// what this launch's layout expected (8 dual, 7 single), and agrees with the layout - shared/SessionCheck.h.
+ (SPRoomVerdict)roomVerdict:(NSString *)result NS_SWIFT_NAME(roomVerdict(_:));
+ (void)setUpdateNotificationsEnabled:(BOOL)enabled NS_SWIFT_NAME(setUpdateNotificationsEnabled(_:));
// v9.9.3: when the app or SpeakaUpdateCheck last got an answer from GitHub (Settings shows it).
+ (nullable NSDate *)lastUpdateCheck NS_SWIFT_NAME(lastUpdateCheck());
+ (void)setLastUpdateCheck:(NSDate *)date NS_SWIFT_NAME(setLastUpdateCheck(_:));

/// Uninstall (v9.8): every key Speaka keeps in com.apple.bluetooth (all start with "Speaka", shared/SpeakaShared.h) and the
/// app's own settings. Run once the tweak is removed, so nothing writes them back.
+ (void)removeLeftoverPreferences NS_SWIFT_NAME(removeLeftoverPreferences());
+ (nullable NSString *)notifiedVersion NS_SWIFT_NAME(notifiedVersion());
+ (void)setNotifiedVersion:(nullable NSString *)version NS_SWIFT_NAME(setNotifiedVersion(_:));

@end

NS_ASSUME_NONNULL_END
