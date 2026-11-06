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
#include "shared/PeerName.h"

// Bluetooth major device classes, by index (bits 8 to 12 of the Class of Device).
static const unsigned int SPMajorComputer = 1;
static const unsigned int SPMajorPhone = 2;

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
@property (nonatomic, copy) NSString *key;     // its address, or its name if it reports none
@property (nonatomic, copy) NSString *address; // its address as reported, or empty
@property (nonatomic) BOOL hasClass;
@property (nonatomic) unsigned int rawClass;   // majorClass exactly as reported, for Credits
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

/// The major class's index, from whichever form majorClass comes in: the plain index (1), the masked major class
/// (0x100), major and minor together (0x10C, a laptop), or the whole Class of Device with its service bits (0x2A010C).
/// v9.4 compared it with 0x100 and 1 only, so a PC reported any other way was never recognised as a computer.
static unsigned int SPMajorIndex(unsigned int value)
{
    return value >= 0x100 ? (value >> 8) & 0x1F : value & 0x1F;
}

/// Windows names a PC DESKTOP-XXXXXXX or LAPTOP-XXXXXXXX until someone renames it.
static BOOL SPLooksLikeWindowsPC(NSString *name)
{
    NSString *upper = name.uppercaseString;
    return [upper hasPrefix:@"DESKTOP-"] || [upper hasPrefix:@"LAPTOP-"];
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
        info.address = [address isKindOfClass:[NSString class]] ? address : @"";
        if ([device respondsToSelector:@selector(majorClass)]) {
            info.hasClass = YES;
            info.rawClass = ((unsigned int (*)(id, SEL))objc_msgSend)(device, @selector(majorClass));
            unsigned int major = SPMajorIndex(info.rawClass);
            info.computer = major == SPMajorComputer;
            info.phone = major == SPMajorPhone;
        }
        if (SPLooksLikeWindowsPC(info.name)) {
            info.computer = YES;
            info.phone = NO;
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
    if (!manager)
        return all;
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
        NSArray<SPBluetoothDevice *> *devices = SPAllDevices(SPManager());
        for (SPBluetoothDevice *device in devices) {
            if (device.computer && device.connected)
                return device.name;
        }
        // Another phone can stream too: exactly one connected. Watches, headphones and the like never count.
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

+ (NSString *)computerNameForRouteUID:(NSString *)uid
{
    @try {
        if (uid.length == 0)
            return nil;
        for (SPBluetoothDevice *device in SPAllDevices(SPManager())) {
            if (device.address.length == 0 || !peer_address_equal(uid.UTF8String, device.address.UTF8String))
                continue;
            // The route's Bluetooth output can be headphones the phone also uses; those are never the PC.
            if (peer_class_is_audio(device.hasClass, SPMajorIndex(device.rawClass)))
                return nil;
            return device.name;
        }
        return nil;
    } @catch (NSException *exception) {
        return nil;
    }
}

+ (BOOL)isAudioDeviceForRouteUID:(NSString *)uid
{
    @try {
        if (uid.length == 0)
            return NO;
        for (SPBluetoothDevice *device in SPAllDevices(SPManager())) {
            if (device.address.length && peer_address_equal(uid.UTF8String, device.address.UTF8String))
                return peer_class_is_audio(device.hasClass, SPMajorIndex(device.rawClass)) ? YES : NO;
        }
        return NO;
    } @catch (NSException *exception) {
        return NO;
    }
}

+ (NSString *)pairedComputerName
{
    @try {
        SPBluetoothDevice *computer = nil;
        NSUInteger computers = 0;
        for (SPBluetoothDevice *device in SPAllDevices(SPManager())) {
            if (device.computer) {
                computer = device;
                computers++;
            }
        }
        return computers == 1 ? computer.name : nil;
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
        BOOL available = SPSendBool(manager, @selector(available), YES);
        NSArray<SPBluetoothDevice *> *devices = SPAllDevices(manager);
        if (devices.count == 0)
            return available ? @"Bluetooth's device list is empty." : @"Bluetooth's device list isn't ready yet.";
        NSMutableArray<NSString *> *parts = [NSMutableArray array];
        for (SPBluetoothDevice *device in devices) {
            NSMutableArray<NSString *> *traits = [NSMutableArray array];
            if (device.connected)
                [traits addObject:@"connected"];
            if (device.computer)
                [traits addObject:@"computer"];
            [traits addObject:device.hasClass ? [NSString stringWithFormat:@"class %#x", device.rawClass] : @"no class"];
            [parts addObject:[NSString stringWithFormat:@"%@ (%@)", device.name, [traits componentsJoinedByString:@", "]]];
            if (parts.count == 8)
                break;
        }
        return [NSString stringWithFormat:@"Bluetooth knows %lu devices: %@.", (unsigned long)devices.count,
                [parts componentsJoinedByString:@"; "]];
    } @catch (NSException *exception) {
        return @"Bluetooth's device list couldn't be read.";
    }
}

@end
