//
//  SPConnectedDevice.h
//  Speaka
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// BluetoothManager's class, loaded once with the main queue for its callbacks; Nil when the framework isn't there.
/// Shared by SPConnectedDevice and SPBluetoothDevices.
Class _Nullable SPBluetoothManagerClass(void);

@interface SPConnectedDevice : NSObject

/// The name of a computer iOS's Bluetooth list reports as connected (or, failing that, of the one phone connected),
/// read through the private BluetoothManager framework. nil when there's none, or the framework is unavailable or not
/// ready yet. This is what lets the main screen name the PC as soon as it connects. Call on the main thread.
+ (nullable NSString *)connectedComputerName NS_SWIFT_NAME(connectedComputerName());

/// The name of the one computer paired with this iPhone, connected or not, or nil if there isn't exactly one. A PC has
/// to be paired to stream, so while one is streaming this names it even when iOS doesn't list it as connected.
/// Call on the main thread.
+ (nullable NSString *)pairedComputerName NS_SWIFT_NAME(pairedComputerName());
/// v9.9.4: the name of the device whose Bluetooth address the audio route's PC port carries (its UID) - the device
/// actually streaming - unless it is headphones or a speaker. nil when there's no such device. Call on the main thread.
+ (nullable NSString *)computerNameForRouteUID:(NSString *)uid NS_SWIFT_NAME(computerName(forRouteUID:));
/// v9.9.4: YES if that UID's device is headphones or a speaker - then the route's Bluetooth output isn't the PC, and
/// its name must not be shown for it either (shared/PeerName.h, peer_choose_name). Call on the main thread.
+ (BOOL)isAudioDeviceForRouteUID:(NSString *)uid NS_SWIFT_NAME(isAudioDevice(forRouteUID:));

/// One line on what BluetoothManager reports, each device with its class as reported, shown in Credits so a missing
/// name can be explained from a screenshot. Call on the main thread.
+ (NSString *)lookupSummary NS_SWIFT_NAME(lookupSummary());

@end

NS_ASSUME_NONNULL_END
