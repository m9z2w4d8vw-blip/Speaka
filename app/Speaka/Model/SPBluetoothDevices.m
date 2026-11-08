//
//  SPBluetoothDevices.m
//  Speaka
//
//  See SPBluetoothDevices.h. Every BluetoothManager call is checked first; nothing here is needed for the speaker.
//

#import "SPBluetoothDevices.h"
#import "SPConnectedDevice.h"
#import "SPTweakClient.h"
#import <objc/message.h>
#include <sys/stat.h>
#include "shared/DeviceRoster.h"

static NSString *const SPConnectLogPath = @"/tmp/FTABHarvest/SpeakaConnect.log";

// The notifications BluetoothManager posted up to iOS 16, by name. Which arrive on 17.0 is logged (the first of each),
// so a device test shows what this iOS sends; one that never comes just leaves its event to the next poll.
static NSString *const SPNoteAvailability = @"BluetoothAvailabilityChangedNotification";
static NSString *const SPNotePower = @"BluetoothPowerChangedNotification";
static NSString *const SPNoteDiscovered = @"BluetoothDeviceDiscoveredNotification";
static NSString *const SPNoteUpdated = @"BluetoothDeviceUpdatedNotification";
static NSString *const SPNoteRemoved = @"BluetoothDeviceRemovedNotification";
static NSString *const SPNoteConnected = @"BluetoothDeviceConnectSuccessNotification";
static NSString *const SPNoteConnectFailed = @"BluetoothDeviceConnectFailedNotification";
static NSString *const SPNoteDisconnected = @"BluetoothDeviceDisconnectSuccessNotification";
static NSString *const SPNoteConnectionStatus = @"BluetoothConnectionStatusChangedNotification";

static id SPBTSend(id target, SEL selector)
{
    if (!target || ![target respondsToSelector:selector]) return nil;
    return ((id (*)(id, SEL))objc_msgSend)(target, selector);
}

static BOOL SPBTBool(id target, SEL selector, BOOL fallback)
{
    if (!target || ![target respondsToSelector:selector]) return fallback;
    return ((BOOL (*)(id, SEL))objc_msgSend)(target, selector);
}

static NSString *SPBTString(id value)
{
    return [value isKindOfClass:[NSString class]] ? value : @"";
}

static id SPBTManager(void)
{
    Class managerClass = SPBluetoothManagerClass();
    return managerClass ? SPBTSend((id)managerClass, @selector(sharedInstance)) : nil;
}

static double SPNow(void) { return [NSDate date].timeIntervalSince1970; }

/// "24:B2:...": the address BluetoothManager or the history gives, the way shared/DeviceRoster.h keys it; nil if none.
static NSString *SPKey(NSString *address)
{
    char normal[SP_ADDR_LEN];
    return sp_address_normalize(address.UTF8String, normal) ? [NSString stringWithUTF8String:normal] : nil;
}

@interface SPDeviceRow ()
@property (nonatomic, readwrite, copy) NSString *address;
@property (nonatomic, readwrite, copy) NSString *name;
@property (nonatomic, readwrite) SPDeviceKind kind;
@property (nonatomic, readwrite) SPDeviceState state;
@property (nonatomic, readwrite) SPDeviceSection section;
@property (nonatomic, readwrite, nullable) NSDate *lastConnected;
@property (nonatomic, readwrite) BOOL paired;
@property (nonatomic, readwrite) BOOL canConnect;
@property (nonatomic, readwrite, copy, nullable) NSString *failure;
@end

@implementation SPDeviceRow
@end

/// A device this scan saw: what was read from it, and the object itself - only to recognise it again, never kept past a
/// change in Bluetooth's availability (a bluetoothd restart makes it stale).
@interface SPSighting : NSObject
@property (nonatomic, copy) NSString *name;
@property (nonatomic) unsigned rawClass;
@property (nonatomic) double lastSeen;
@end

@implementation SPSighting
@end

@interface SPBluetoothDevices ()
- (void)log:(NSString *)format, ... NS_FORMAT_FUNCTION(1, 2);
@end

@implementation SPBluetoothDevices {
    NSMutableDictionary<NSString *, SPSighting *> *_seen;          // address -> sighting, this scan session
    NSMutableDictionary<NSString *, NSNumber *> *_connecting;      // address -> when it was asked (Unix time)
    NSMutableDictionary<NSString *, NSArray *> *_failures;         // address -> @[reason, until (Unix time)]
    NSMutableSet<NSString *> *_arrived;                            // notification names already logged
    NSUInteger _scanGeneration;
    BOOL _loggedInterface;
    BOOL _haveLooked;
    double _unavailableSince;
}

+ (instancetype)shared
{
    static SPBluetoothDevices *shared;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ shared = [[SPBluetoothDevices alloc] initShared]; });
    return shared;
}

- (instancetype)initShared
{
    if ((self = [super init])) {
        _seen = [NSMutableDictionary dictionary];
        _connecting = [NSMutableDictionary dictionary];
        _failures = [NSMutableDictionary dictionary];
        _arrived = [NSMutableSet set];
        _availability = SPBluetoothStarting;
        _availabilityDetail = @"";
        NSNotificationCenter *center = [NSNotificationCenter defaultCenter];
        for (NSString *name in @[SPNoteAvailability, SPNotePower, SPNoteDiscovered, SPNoteUpdated, SPNoteRemoved,
                                 SPNoteConnected, SPNoteConnectFailed, SPNoteDisconnected, SPNoteConnectionStatus])
            [center addObserver:self selector:@selector(bluetoothNotification:) name:name object:nil];
        [self refresh];
    }
    return self;
}

#pragma mark - The log a device test reads

- (void)log:(NSString *)format, ...
{
    va_list args;
    va_start(args, format);
    NSString *line = [[NSString alloc] initWithFormat:format arguments:args];
    va_end(args);
    mkdir("/tmp/FTABHarvest", 0777);
    struct stat st;
    const char *path = SPConnectLogPath.fileSystemRepresentation;
    FILE *file = fopen(path, stat(path, &st) == 0 && st.st_size > 64 * 1024 ? "w" : "a");
    if (!file) return;
    NSDateFormatter *when = [[NSDateFormatter alloc] init];
    when.dateFormat = @"yyyy-MM-dd HH:mm:ss";
    fprintf(file, "%s %s\n", [when stringFromDate:[NSDate date]].UTF8String, line.UTF8String ?: "");
    fclose(file);
}

/// Once Bluetooth answers: which of the methods the page can use this iOS's BluetoothManager has.
- (void)logInterface:(id)manager
{
    NSMutableArray<NSString *> *have = [NSMutableArray array], *missing = [NSMutableArray array];
    for (NSString *name in @[@"available", @"powered", @"enabled", @"pairedDevices", @"connectedDevices",
                             @"setDeviceScanningEnabled:", @"deviceScanningEnabled", @"deviceScanningInProgress",
                             @"scanForServices:", @"scanForConnectableDevices:", @"connectDevice:",
                             @"connectDevice:withServices:", @"disconnectDevice:"])
        [([manager respondsToSelector:NSSelectorFromString(name)] ? have : missing) addObject:name];
    id paired = SPBTSend(manager, @selector(pairedDevices));
    id device = [paired isKindOfClass:[NSArray class]] ? [(NSArray *)paired firstObject] : nil;
    NSMutableArray<NSString *> *deviceHas = [NSMutableArray array];
    for (NSString *name in @[@"name", @"address", @"majorClass", @"paired", @"connected", @"connect",
                             @"connectWithServices:", @"disconnect"])
        if ([device respondsToSelector:NSSelectorFromString(name)]) [deviceHas addObject:name];
    [self log:@"INTERFACE BluetoothManager has: %@; lacks: %@; a paired device (%@) has: %@",
              [have componentsJoinedByString:@" "], missing.count ? [missing componentsJoinedByString:@" "] : @"none",
              device ? NSStringFromClass([device class]) : @"none paired", [deviceHas componentsJoinedByString:@" "]];
}

#pragma mark - State

- (void)refresh
{
    Class managerClass = SPBluetoothManagerClass();
    id manager = SPBTManager();
    SPBluetoothAvailability availability;
    NSString *detail;
    if (!managerClass || !manager) {
        availability = SPBluetoothUnavailable;
        detail = @"This iOS has no BluetoothManager.";
    } else if (!SPBTBool(manager, @selector(available), NO)) {
        int initError = [managerClass respondsToSelector:@selector(lastInitError)]
            ? ((int (*)(id, SEL))objc_msgSend)((id)managerClass, @selector(lastInitError)) : -1;
        if (_unavailableSince == 0) _unavailableSince = SPNow();
        // A few seconds is Bluetooth starting (or restarting, after Start or Stop). Longer is bluetoothd not serving
        // Speaka - what an unentitled client gets.
        availability = SPNow() - _unavailableSince < 10 ? SPBluetoothStarting : SPBluetoothUnavailable;
        detail = [NSString stringWithFormat:@"Bluetooth isn't answering Speaka (available NO, lastInitError %d).", initError];
    } else {
        _unavailableSince = 0;
        BOOL powered = [manager respondsToSelector:@selector(powered)] ? SPBTBool(manager, @selector(powered), YES)
                                                                       : SPBTBool(manager, @selector(enabled), YES);
        availability = powered ? SPBluetoothReady : SPBluetoothOff;
        detail = powered ? @"" : @"Bluetooth is off.";
    }
    if (!_haveLooked || availability != _availability) {
        static NSString *const names[] = {@"unavailable", @"starting", @"off", @"ready"};
        [self log:@"AVAILABILITY %@ %@", names[availability], detail];
        if (availability != SPBluetoothReady) {
            // bluetoothd restarted, or Bluetooth went off: what this scan saw and what was in flight are stale.
            [_seen removeAllObjects];
            [_connecting removeAllObjects];
            _scanning = NO;
            _scanGeneration++;
        }
    }
    _haveLooked = YES;
    _availability = availability;
    _availabilityDetail = detail;
    if (availability == SPBluetoothReady && !_loggedInterface) {
        _loggedInterface = YES;
        [self logInterface:manager];
    }
}

- (BOOL)canScan
{
    return _availability == SPBluetoothReady && [SPBTManager() respondsToSelector:@selector(setDeviceScanningEnabled:)];
}

#pragma mark - Scanning

- (void)startScanningFor:(NSTimeInterval)seconds
{
    [self refresh];
    if (!self.canScan) {
        [self log:@"SCAN not started: %@", _availability == SPBluetoothReady ? @"no setDeviceScanningEnabled:" : _availabilityDetail];
        return;
    }
    id manager = SPBTManager();
    BOOL services = [manager respondsToSelector:@selector(scanForServices:)];
    if (services) ((void (*)(id, SEL, unsigned int))objc_msgSend)(manager, @selector(scanForServices:), 0xFFFFFFFFu);
    ((void (*)(id, SEL, BOOL))objc_msgSend)(manager, @selector(setDeviceScanningEnabled:), YES);
    _scanning = YES;
    NSUInteger generation = ++_scanGeneration;
    [self log:@"SCAN started for %.0f s%@", seconds, services ? @" (all services)" : @""];
    __weak SPBluetoothDevices *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(seconds * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        SPBluetoothDevices *strongSelf = weakSelf;
        if (strongSelf && strongSelf->_scanGeneration == generation) [strongSelf stopScanning];
    });
}

- (void)stopScanning
{
    if (!_scanning) return;
    _scanning = NO;
    _scanGeneration++;
    id manager = SPBTManager();
    if ([manager respondsToSelector:@selector(setDeviceScanningEnabled:)])
        ((void (*)(id, SEL, BOOL))objc_msgSend)(manager, @selector(setDeviceScanningEnabled:), NO);
    [self log:@"SCAN stopped: %lu device%@ seen", (unsigned long)_seen.count, _seen.count == 1 ? @"" : @"s"];
}

#pragma mark - Notifications

- (void)bluetoothNotification:(NSNotification *)note
{
    if (![NSThread isMainThread]) {
        dispatch_async(dispatch_get_main_queue(), ^{ [self bluetoothNotification:note]; });
        return;
    }
    if (![_arrived containsObject:note.name]) {
        [_arrived addObject:note.name];
        [self log:@"NOTIFICATION %@ arrives (object %@)", note.name, note.object ? NSStringFromClass([note.object class]) : @"none"];
    }
    if ([note.name isEqualToString:SPNoteAvailability] || [note.name isEqualToString:SPNotePower]) {
        [self refresh];
        return;
    }
    id device = note.object;
    NSString *key = [device respondsToSelector:@selector(address)] ? SPKey(SPBTString(SPBTSend(device, @selector(address)))) : nil;
    if (!key) return;
    double now = SPNow();
    if ([note.name isEqualToString:SPNoteDiscovered] || [note.name isEqualToString:SPNoteUpdated]) {
        if (!_scanning && !_seen[key]) return;                    // only what this scan finds
        SPSighting *sighting = _seen[key];
        BOOL first = sighting == nil;
        if (first) {
            sighting = [[SPSighting alloc] init];
            sighting.name = @"";
            _seen[key] = sighting;
        }
        NSString *name = SPBTString(SPBTSend(device, @selector(name)));
        if (name.length) sighting.name = name;
        if ([device respondsToSelector:@selector(majorClass)])
            sighting.rawClass = ((unsigned int (*)(id, SEL))objc_msgSend)(device, @selector(majorClass));
        sighting.lastSeen = now;
        if (first) [self log:@"SEEN %@ \"%@\" class %#x", key, sighting.name, sighting.rawClass];
    } else if ([note.name isEqualToString:SPNoteRemoved]) {
        [_seen removeObjectForKey:key];
    } else if ([note.name isEqualToString:SPNoteConnected]) {
        if (_connecting[key]) [self log:@"CONNECT %@ succeeded (notification)", key];
        [_connecting removeObjectForKey:key];
    } else if ([note.name isEqualToString:SPNoteConnectFailed]) {
        if (_connecting[key]) {
            [_connecting removeObjectForKey:key];
            _failures[key] = @[@"iOS couldn't connect it.", @(now + 15)];
            [self log:@"CONNECT %@ failed (notification)", key];
        }
    }
}

#pragma mark - Connecting

- (SPConnectOutcome)connectAddress:(NSString *)address
{
    [self refresh];
    if (_availability != SPBluetoothReady) return SPConnectBluetoothNotReady;
    NSString *key = SPKey(address);
    if (!key) return SPConnectNotFound;
    id manager = SPBTManager();
    id device = nil;
    id paired = SPBTSend(manager, @selector(pairedDevices));
    if ([paired isKindOfClass:[NSArray class]])
        for (id candidate in (NSArray *)paired)
            if ([SPKey(SPBTString(SPBTSend(candidate, @selector(address)))) isEqualToString:key]) { device = candidate; break; }
    if (!device) return _seen[key] ? SPConnectNotPaired : SPConnectNotFound;
    if (SPBTBool(device, @selector(connected), NO)) return SPConnectAlreadyConnected;
    NSString *how = nil;
    if ([device respondsToSelector:@selector(connect)]) {
        ((void (*)(id, SEL))objc_msgSend)(device, @selector(connect));
        how = @"-[BluetoothDevice connect]";
    } else if ([manager respondsToSelector:@selector(connectDevice:)]) {
        ((void (*)(id, SEL, id))objc_msgSend)(manager, @selector(connectDevice:), device);
        how = @"-[BluetoothManager connectDevice:]";
    }
    if (!how) {
        [self log:@"CONNECT %@ not possible: no connect method", key];
        return SPConnectUnsupported;
    }
    _connecting[key] = @(SPNow());
    [_failures removeObjectForKey:key];
    [self log:@"CONNECT %@ requested with %@", key, how];
    return SPConnectRequested;
}

#pragma mark - Rows

static void SPAddList(sp_roster *roster, id manager, SEL list, BOOL linked)
{
    id devices = SPBTSend(manager, list);
    if (![devices isKindOfClass:[NSArray class]]) return;
    for (id device in (NSArray *)devices) {
        sp_device *d = sp_roster_upsert(roster, SPBTString(SPBTSend(device, @selector(address))).UTF8String);
        if (!d) continue;
        sp_device_name(d, SPBTString(SPBTSend(device, @selector(name))).UTF8String, SP_NAME_IOS);
        if ([device respondsToSelector:@selector(majorClass)])
            sp_device_class(d, ((unsigned int (*)(id, SEL))objc_msgSend)(device, @selector(majorClass)));
        if (list == @selector(pairedDevices) || SPBTBool(device, @selector(paired), NO)) d->paired = 1;
        if (linked || SPBTBool(device, @selector(connected), NO)) d->linked = 1;
    }
}

- (NSArray<SPDeviceRow *> *)rowsWithStreamAddress:(NSString *)streamAddress streaming:(BOOL)streaming
{
    [self refresh];
    double now = SPNow();
    sp_roster *roster = calloc(1, sizeof *roster);
    if (!roster) return @[];
    // Speaka's history: the only source of "last connected" (the tweak writes it at each verified connection).
    for (NSDictionary *entry in [SPTweakClient connectionHistory]) {
        sp_device *d = sp_roster_upsert(roster, SPBTString(entry[@"address"]).UTF8String);
        if (!d) continue;
        d->in_history = 1;
        id last = entry[@"last"];
        d->last_connected = [last isKindOfClass:[NSNumber class]] ? [last doubleValue] : 0;
        sp_device_name(d, SPBTString(entry[@"name"]).UTF8String, SP_NAME_HISTORY);
    }
    id manager = SPBTManager();
    if (_availability == SPBluetoothReady) {
        SPAddList(roster, manager, @selector(pairedDevices), NO);
        SPAddList(roster, manager, @selector(connectedDevices), YES);
    }
    [_seen enumerateKeysAndObjectsUsingBlock:^(NSString *key, SPSighting *sighting, BOOL *stop) {
        sp_device *d = sp_roster_upsert(roster, key.UTF8String);
        if (!d) return;
        d->last_seen = sighting.lastSeen;
        sp_device_name(d, sighting.name.UTF8String, SP_NAME_DISCOVERY);
        sp_device_class(d, sighting.rawClass);
    }];
    if (streaming) {
        sp_device *d = sp_roster_upsert(roster, streamAddress.UTF8String);
        if (d) d->streaming = 1;
    }
    // The app's verified name cache (SPTweakClient, shared/PeerName.h): a name for a row that has none. Its timestamps
    // are when the app last saw a name while open, never a connection, so it makes no rows and no "last connected".
    NSDictionary<NSString *, NSString *> *known = [SPTweakClient verifiedNames];
    for (int i = 0; i < roster->n; i++) {
        sp_device *d = &roster->d[i];
        NSString *name = d->name_rank == SP_NAME_NONE ? known[[NSString stringWithUTF8String:d->address] ?: @""] : nil;
        if (name.length) sp_device_name(d, name.UTF8String, SP_NAME_HISTORY);
    }
    // In-flight connections: proven by the link or Speaka's stream, or failed by the timeout.
    for (NSString *key in _connecting.allKeys) {
        sp_device *d = sp_roster_find(roster, key.UTF8String);
        double started = _connecting[key].doubleValue;
        if (d && (d->linked || d->streaming)) {
            [_connecting removeObjectForKey:key];
            [self log:@"CONNECT %@ succeeded (%@)", key, d->streaming ? @"audio" : @"Bluetooth link"];
        } else if (now - started >= SP_CONNECT_TIMEOUT_S) {
            [_connecting removeObjectForKey:key];
            _failures[key] = @[@"It didn't answer. Is it on, with Bluetooth on?", @(now + 15)];
            [self log:@"CONNECT %@ timed out", key];
        } else if (d) {
            d->connecting = 1;
            d->connect_started = started;
        }
    }
    BOOL canAsk = _availability == SPBluetoothReady;
    NSMutableArray<SPDeviceRow *> *rows = [NSMutableArray array];
    int order[SP_ROSTER_MAX];
    for (int section = SP_SECTION_RECENT; section <= SP_SECTION_PAIRED; section++) {
        int n = sp_roster_order(roster, (sp_section)section, now, order, SP_ROSTER_MAX);
        for (int i = 0; i < n; i++) {
            const sp_device *d = &roster->d[order[i]];
            SPDeviceRow *row = [[SPDeviceRow alloc] init];
            row.address = [NSString stringWithUTF8String:d->address] ?: @"";
            NSString *name = d->name[0] ? [NSString stringWithUTF8String:d->name] : nil;
            row.name = name.length ? name : row.address;
            row.kind = d->major == SP_MAJOR_PHONE ? SPDeviceKindPhone
                : (d->major == SP_MAJOR_COMPUTER || sp_looks_like_windows_pc(d->name) || d->in_history) ? SPDeviceKindComputer
                : SPDeviceKindOther;
            row.state = (SPDeviceState)sp_device_state(d, now);
            row.section = (SPDeviceSection)section;
            row.lastConnected = d->last_connected > 0 ? [NSDate dateWithTimeIntervalSince1970:d->last_connected] : nil;
            row.paired = d->paired != 0;
            row.canConnect = canAsk && d->paired && (row.state == SPDeviceStatePaired || row.state == SPDeviceStateAvailable ||
                                                     row.state == SPDeviceStateUnavailable);
            NSArray *failure = _failures[row.address];
            if (failure && [failure[1] doubleValue] > now) row.failure = failure[0];
            [rows addObject:row];
        }
    }
    free(roster);
    return rows;
}

@end
