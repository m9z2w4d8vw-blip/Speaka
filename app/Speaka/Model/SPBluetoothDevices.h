//
//  SPBluetoothDevices.h
//  Speaka
//
//  The Connect to page's devices (v9.9.12): iOS's paired and connected lists, a bounded scan for nearby devices, and
//  asking iOS to connect a paired one - through the private BluetoothManager framework - merged with Speaka's own
//  connection history and its live A2DP stream by shared/DeviceRoster.h.
//
//  BluetoothManager talks to bluetoothd, which serves its MobileBluetooth messages only to entitled clients: on iOS 17.0
//  (bluetoothd 0x100261aa4) a client below privilege level 1 gets "has insufficient entitlements to send MBFramework
//  XPC messages", and level 1 comes from com.apple.bluetooth.system (0x10025db34), level 2 from
//  com.apple.bluetooth.internal. Speaka.entitlements has com.apple.bluetooth.system for this. Every private call here
//  is checked with respondsToSelector: first; what's missing says so (availabilityDetail, the log), never crashes.
//  What it finds - the methods that exist, the notifications that arrive, each scan and connection - is written to
//  /tmp/FTABHarvest/SpeakaConnect.log.
//
//  Main thread only.
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, SPBluetoothAvailability) {
    SPBluetoothUnavailable,     // BluetoothManager isn't there, or bluetoothd doesn't serve Speaka (availabilityDetail)
    SPBluetoothStarting,        // not ready yet: just opened, or Bluetooth restarting (Start and Stop restart it)
    SPBluetoothOff,             // Bluetooth is off
    SPBluetoothReady,
};

typedef NS_ENUM(NSInteger, SPDeviceState) {          // shared/DeviceRoster.h sp_state
    SPDeviceStateUnavailable,   // not seen, not paired, not connected
    SPDeviceStatePaired,
    SPDeviceStateAvailable,     // this scan saw it, recently
    SPDeviceStateLinked,        // a Bluetooth link, but no audio into Speaka
    SPDeviceStateConnecting,
    SPDeviceStateConnected,     // it's streaming to Speaka (Speaka's own A2DP connection)
};

typedef NS_ENUM(NSInteger, SPDeviceSection) {
    SPDeviceSectionRecent,      // connected to Speaka before (the tweak's history), or streaming now
    SPDeviceSectionNearby,      // seen or linked now, never connected to Speaka
    SPDeviceSectionPaired,      // paired computers and phones that never connected to Speaka
};

typedef NS_ENUM(NSInteger, SPDeviceKind) {
    SPDeviceKindOther,
    SPDeviceKindComputer,
    SPDeviceKindPhone,
};

typedef NS_ENUM(NSInteger, SPConnectOutcome) {
    SPConnectRequested,         // iOS was asked; the row says Connecting until the link or the stream proves it
    SPConnectAlreadyConnected,
    SPConnectNotPaired,         // pair it from the computer first
    SPConnectUnsupported,       // this iOS's BluetoothManager has no way to ask
    SPConnectNotFound,          // not in iOS's paired list any more
    SPConnectBluetoothNotReady,
};

@interface SPDeviceRow : NSObject
@property (nonatomic, readonly, copy) NSString *address;              // its identity: one row per device
@property (nonatomic, readonly, copy) NSString *name;                 // its name, else its address
@property (nonatomic, readonly) SPDeviceKind kind;
@property (nonatomic, readonly) SPDeviceState state;
@property (nonatomic, readonly) SPDeviceSection section;
@property (nonatomic, readonly, nullable) NSDate *lastConnected;      // its last verified Speaka connection, if any
@property (nonatomic, readonly) BOOL paired;
@property (nonatomic, readonly) BOOL canConnect;                      // paired, and iOS can be asked to connect it
@property (nonatomic, readonly, copy, nullable) NSString *failure;    // why the last attempt failed, for a while
@end

@interface SPBluetoothDevices : NSObject

+ (instancetype)shared NS_SWIFT_NAME(shared());

/// Re-reads Bluetooth's state; the page calls it on each refresh.
- (void)refresh;
@property (nonatomic, readonly) SPBluetoothAvailability availability;
@property (nonatomic, readonly, copy) NSString *availabilityDetail;
@property (nonatomic, readonly) BOOL scanning;
@property (nonatomic, readonly) BOOL canScan;

/// A scan for nearby devices that ends by itself after `seconds` (and when the page closes: stopScanning).
- (void)startScanningFor:(NSTimeInterval)seconds NS_SWIFT_NAME(startScanning(for:));
- (void)stopScanning;

/// Asks iOS to connect a paired device: the Bluetooth link. Audio into Speaka still needs the computer to send it.
- (SPConnectOutcome)connectAddress:(NSString *)address NS_SWIFT_NAME(connect(address:));

/// The rows now: Speaka's history, iOS's paired and connected devices, this scan, and Speaka's stream (from
/// `streamAddress` when `streaming`), merged by address (shared/DeviceRoster.h). Only devices that can send audio.
- (NSArray<SPDeviceRow *> *)rowsWithStreamAddress:(NSString *)streamAddress streaming:(BOOL)streaming
    NS_SWIFT_NAME(rows(streamAddress:streaming:));

@end

NS_ASSUME_NONNULL_END
