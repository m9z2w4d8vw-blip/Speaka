//
//  SPConnectedDevice.h
//  Speaka
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@interface SPConnectedDevice : NSObject

/// Best effort: the name of the PC streaming to Speaka, read through the private BluetoothManager framework. In order:
/// a computer iOS reports as connected; the one computer paired with this iPhone, since a PC must be paired to stream
/// and iOS doesn't always count Speaka's connection as one of its own; the one phone connected. Returns nil when none
/// of those is certain, or the framework is unavailable or not ready yet; callers then say "Your PC". Call on the
/// main thread.
+ (nullable NSString *)connectedComputerName NS_SWIFT_NAME(connectedComputerName());

/// One line on what BluetoothManager reports (how many devices, which are connected, which are computers), shown in
/// Credits, so a missing name can be explained from a screenshot. Call on the main thread.
+ (NSString *)lookupSummary NS_SWIFT_NAME(lookupSummary());

@end

NS_ASSUME_NONNULL_END
