// DebugLog.h - the debug log: reading it back from every channel the tweak writes to, and the screen that shows it.
#pragma once
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>

/* ---- reading the log back ---- */
// DebugLog.h - reads the debug log back from every channel the tweak writes to (see shared/SpeakaShared.h).

NS_ASSUME_NONNULL_BEGIN

@interface SpeakaLogReader : NSObject
+ (instancetype)shared;

/// Status block + channel report + the log lines. This is what the "Debug log" screen shows.
- (NSString *)fullText;

/// Short values for the rows on the main screen.
- (NSString *)loadedSummary;
- (NSString *)lastEventSummary;
- (NSString *)channelSummary;

/// Removes the log from every channel Settings is allowed to write to. Returns a one-line result for the user.
- (NSString *)clearAll;
@end

NS_ASSUME_NONNULL_END

/* ---- the screen ---- */
// DebugLog.h - the "Debug log" screen: a monospaced, selectable, auto-refreshing text view.

@interface SpeakaLogViewController : UIViewController
@end
