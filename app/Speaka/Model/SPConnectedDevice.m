//
//  SPConnectedDevice.m
//  Speaka
//
//  Nothing here is required for the speaker to work: every private call is checked with respondsToSelector:
//  first, and any failure just means the connection line shows "Your PC" instead of the PC's name.
//

#import "SPConnectedDevice.h"
#import <dlfcn.h>
#import <objc/message.h>

// Bluetooth major device classes, as BluetoothDevice may report them: the masked Class of Device value, or its index.
static const unsigned int SPMajorClassComputerMasked = 0x100;
static const unsigned int SPMajorClassComputerIndex = 1;
static const unsigned int SPMajorClassPhoneMasked = 0x200;
static const unsigned int SPMajorClassPhoneIndex = 2;

static Class SPBluetoothManagerClass(void)
{
    static Class managerClass = Nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        dlopen("/System/Library/PrivateFrameworks/BluetoothManager.framework/BluetoothManager", RTLD_LAZY);
        managerClass = NSClassFromString(@"BluetoothManager");
    });
    return managerClass;
}

/// One device as BluetoothManager reports it.
@interface SPBluetoothDevice : NSObject
@property (nonatomic, copy) NSString *name;
@property (nonatomic, copy) NSString *key;   // its address, or its name if it reports none
@property (nonatomic) BOOL computer;
@property (nonatomic) BOOL phone;
@property (nonatomic) BOOL connected;
@end

@implementation SPBluetoothDevice
@end

static id SPSend(id target, SEL selector)
{
    if (!target || ![target respondsToSelector:selector])
        return nil;
    return ((id (*)(id, SEL))objc_msgSend)(target, selector);
}

static BOOL SPSendBool(id target, SEL selector, BOOL fallback)
{
    if (!target || ![target respondsToSelector:selector])
        return fallback;
    return ((BOOL (*)(id, SEL))objc_msgSend)(target, selector);
}

static id SPManager(void)
{
    Class managerClass = SPBluetoothManagerClass();
    return managerClass ? SPSend((id)managerClass, @selector(sharedInstance)) : nil;
}

/// The named devices in one of BluetoothManager's lists.
static NSArray<SPBluetoothDevice *> *SPDevices(id manager, SEL list, BOOL listedAsConnected)
{
    NSMutableArray<SPBluetoothDevice *> *found = [NSMutableArray array];
    id devices = SPSend(manager, list);
    if (![devices isKindOfClass:[NSArray class]])
        return found;
    for (id device in (NSArray *)devices) {
        id name = SPSend(device, @selector(name));
        if (![name isKindOfClass:[NSString class]] || ((NSString *)name).length == 0)
            continue;
        SPBluetoothDevice *info = [[SPBluetoothDevice alloc] init];
        info.name = name;
        id address = SPSend(device, @selector(address));
        info.key = ([address isKindOfClass:[NSString class]] && ((NSString *)address).length > 0) ? address : name;
        if ([device respondsToSelector:@selector(majorClass)]) {
            unsigned int major = ((unsigned int (*)(id, SEL))objc_msgSend)(device, @selector(majorClass));
            info.computer = major == SPMajorClassComputerMasked || major == SPMajorClassComputerIndex;
            info.phone = major == SPMajorClassPhoneMasked || major == SPMajorClassPhoneIndex;
        }
        info.connected = listedAsConnected || SPSendBool(device, @selector(connected), NO);
        [found addObject:info];
    }
    return found;
}

/// The connected devices, then the paired ones, each device once.
static NSArray<SPBluetoothDevice *> *SPAllDevices(id manager)
{
    NSMutableArray<SPBluetoothDevice *> *all = [NSMutableArray array];
    NSMutableDictionary<NSString *, SPBluetoothDevice *> *byKey = [NSMutableDictionary dictionary];
    NSArray<SPBluetoothDevice *> *listed = [SPDevices(manager, @selector(connectedDevices), YES)
        arrayByAddingObjectsFromArray:SPDevices(manager, @selector(pairedDevices), NO)];
    for (SPBluetoothDevice *device in listed) {
        SPBluetoothDevice *known = byKey[device.key];
        if (known) {
            known.connected = known.connected || device.connected;
            continue;
        }
        byKey[device.key] = device;
        [all addObject:device];
    }
    return all;
}

@implementation SPConnectedDevice

+ (NSString *)connectedComputerName
{
    @try {
        id manager = SPManager();
        if (!manager || !SPSendBool(manager, @selector(available), YES))
            return nil;
        NSArray<SPBluetoothDevice *> *devices = SPAllDevices(manager);
        // 1. A computer iOS reports as connected.
        for (SPBluetoothDevice *device in devices) {
            if (device.computer && device.connected)
                return device.name;
        }
        // 2. A PC can only stream to Speaka once it's paired with this iPhone, and iOS doesn't always count Speaka's
        //    connection as one of its own. With exactly one computer paired, it's that one.
        SPBluetoothDevice *computer = nil;
        NSUInteger computers = 0;
        for (SPBluetoothDevice *device in devices) {
            if (device.computer) {
                computer = device;
                computers++;
            }
        }
        if (computers == 1)
            return computer.name;
        // 3. Another phone can stream too: exactly one connected. Watches, headphones and the like never count.
        SPBluetoothDevice *phone = nil;
        NSUInteger phones = 0;
        for (SPBluetoothDevice *device in devices) {
            if (device.phone && device.connected) {
                phone = device;
                phones++;
            }
        }
        return phones == 1 ? phone.name : nil;
    } @catch (NSException *exception) {
        return nil;
    }
}

+ (NSString *)lookupSummary
{
    @try {
        id manager = SPManager();
        if (!manager)
            return @"Bluetooth's device list isn't available to Speaka.";
        if (!SPSendBool(manager, @selector(available), YES))
            return @"Bluetooth's device list isn't ready yet.";
        NSArray<SPBluetoothDevice *> *devices = SPAllDevices(manager);
        NSMutableArray<NSString *> *connected = [NSMutableArray array];
        NSMutableArray<NSString *> *computers = [NSMutableArray array];
        for (SPBluetoothDevice *device in devices) {
            if (device.connected)
                [connected addObject:device.name];
            if (device.computer)
                [computers addObject:device.name];
        }
        return [NSString stringWithFormat:@"Bluetooth knows %lu devices. Connected: %@. Computers: %@.",
                (unsigned long)devices.count,
                connected.count > 0 ? [connected componentsJoinedByString:@", "] : @"none",
                computers.count > 0 ? [computers componentsJoinedByString:@", "] : @"none"];
    } @catch (NSException *exception) {
        return @"Bluetooth's device list couldn't be read.";
    }
}

@end
