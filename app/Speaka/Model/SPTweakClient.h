//
//  SPTweakClient.h
//  Speaka
//
//  Talks to the Speaka tweak exactly like Settings > Speaka does: through the com.apple.bluetooth
//  preferences and the Darwin notifications defined in BTPSShared.h at the repository root.
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
@property (nonatomic, copy) NSDictionary<NSString *, id> *diagnostics; // the tweak's whole status block, as published
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

@interface SPTweakClient : NSObject
+ (instancetype)sharedClient;
/// The three output modes, in Settings order: key, title, shortTitle, symbol, detail.
+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes NS_SWIFT_NAME(outputModes());
- (SPStatus *)readStatus NS_SWIFT_NAME(readStatus());
- (BOOL)setOutputMode:(NSString *)mode NS_SWIFT_NAME(setOutputMode(_:));
- (nullable SPRequest *)sendAction:(NSString *)action error:(NSString *_Nullable *_Nullable)error
    NS_SWIFT_NAME(sendAction(_:error:));
- (SPRequestState)pollRequest:(SPRequest *)request NS_SWIFT_NAME(pollRequest(_:));
@end

NS_ASSUME_NONNULL_END
