//
//  SPConnectedDevice.h
//  Speaka
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@interface SPConnectedDevice : NSObject

/// The name of a computer iOS's Bluetooth list reports as connected (or, failing that, of the one phone connected),
/// read through the private BluetoothManager framework. nil when there's none, or the framework is unavailable or not
/// ready yet. This is what lets the main screen name the PC as soon as it connects. Call on the main thread.
+ (nullable NSString *)connectedComputerName NS_SWIFT_NAME(connectedComputerName());

/// The name of the one computer paired with this iPhone, connected or not, or nil if there isn't exactly one. A PC has
/// to be paired to stream, so while one is streaming this names it even when iOS doesn't list it as connected.
/// Call on the main thread.
+ (nullable NSString *)pairedComputerName NS_SWIFT_NAME(pairedComputerName());

/// One line on what BluetoothManager reports, each device with its class as reported, shown in Credits so a missing
/// name can be explained from a screenshot. Call on the main thread.
+ (NSString *)lookupSummary NS_SWIFT_NAME(lookupSummary());

@end

NS_ASSUME_NONNULL_END
