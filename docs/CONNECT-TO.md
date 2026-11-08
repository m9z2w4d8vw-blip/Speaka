# Connect to

The Connect to page lists the computers that can send audio to Speaka, finds nearby ones, and asks iOS to connect a
paired one. This is how it works, what each state is based on, and what still needs a device to confirm.

## Bluetooth access: the entitlement

The app reads iOS's devices through the private BluetoothManager framework, which talks to `bluetoothd`. Before this
release it never worked: Credits showed *Bluetooth's device list isn't ready yet (available NO, lastInitError 0)*.
The reason is in `bluetoothd` (iOS 17.0, `docs/reference-binaries/ios17-iphone13/bluetoothd`):

| Address | What it does |
| --- | --- |
| `0x10025df44` | `hasEntitlement(client, name)`: `xpc_connection_copy_entitlement_value`, true only for an XPC boolean true |
| `0x10025db18` | A new client gets privilege level 2 if it has `com.apple.bluetooth.internal`... |
| `0x10025db34` | ...else level 1 if it has `com.apple.bluetooth.system`, else 0, stored at `client + 0x50` |
| `0x100261aa4` | The MobileBluetooth message handler: `ldr w8, [x0, #0x50]; cmp w8, #0; b.le` to *"has insufficient entitlements to send MBFramework XPC messages"* |

So every BluetoothManager request from a client without either entitlement is refused, and the app had neither.
`app/Speaka.entitlements` now has `com.apple.bluetooth.system` (level 1, the lower of the two). TrollStore signs the
app with it. The same fix should make the existing PC-name lookup on the home screen work.

## Where each piece of information comes from

`shared/DeviceRoster.h` merges the sources. Each one proves something different, and a row's state comes only from
what proves it. Devices are identified by Bluetooth address, never by name, so a renamed PC stays one row.

| Source | Proves | Written or read by |
| --- | --- | --- |
| History | A verified connection to Speaka, with its time | Written by the tweak only when a computer configures Speaka's A2DP stream (`tweak/PeerIdentity.c`, `SpeakaConnectionHistory`) |
| Paired list | Paired, with its current name and class | BluetoothManager `pairedDevices` |
| Connected list | A Bluetooth link now (not necessarily audio) | BluetoothManager `connectedDevices` |
| Scan | Nearby, while the sighting is under 30 s old | BluetoothManager discovery notifications during a scan |
| Speaka's stream | It's sending audio to Speaka now | The app's existing connected state plus the stream's address, from the tweak |
| Connect request | The app asked iOS, and is waiting | The app, for up to 20 s |

The app's existing name cache (`SpeakaPeerNames`, `shared/PeerName.h`) isn't a history. Its entries are written in
`readStatus`, which runs every 0.5 s while the app is open, with `seen` set to the time of that poll. So `seen` means
"the app last saw this name while open". It misses every connection made while Speaka is closed, and it's refreshed on
every poll. The page uses only its names, iOS's own verified pairs, as a last resort for a row with no other name:
the cache creates no rows, never outranks iOS's current name, and its `seen` is never read.

The history keeps 12 computers for up to 180 days, most recent first. Refreshing or scanning never writes "last
connected". The page starts empty and fills from the next connection after this update. Until then, a computer
paired with the iPhone appears under **Other paired devices**, which is never presented as Speaka history.

| State | Shown when |
| --- | --- |
| Connected | Speaka's own stream is from this address |
| Connecting | The app asked iOS to connect it in the last 20 s |
| Connected to this iPhone | BluetoothManager reports a link, but no audio comes into Speaka |
| Available | A scan saw it in the last 30 s |
| Paired | Paired, not seen or connected now |
| Not available | In the history, but neither paired, seen nor connected now |

Only devices that can send audio are listed: computers and phones (Bluetooth major classes 1 and 2), devices in the
history, and unclassified devices named like a Windows PC (`DESKTOP-`, `LAPTOP-`). Headphones, speakers and other
audio devices, including the iPhone's own output, are never listed as sources.

## Connecting: what the iPhone can start and what it can't

- **The Bluetooth link: the iPhone can start it.** Connect on a paired computer calls `-[BluetoothDevice connect]`
  (or `-[BluetoothManager connectDevice:]`). The row says Connecting until the link or Speaka's stream proves it, or
  says why it failed (iOS's failure notification, or no answer in 20 s).
- **The audio: the computer starts it.** Speaka is an A2DP Sink. The Source, the computer, opens the stream when it
  chooses Speaka as its output, so a link alone isn't audio, and the row says *Connected to this iPhone · choose
  Speaka as its output*. Whether Windows opens the audio by itself after the iPhone makes the link is a device test.
  If it doesn't, starting the audio from the iPhone would mean having the tweak open the AVDTP connection itself, a
  new `bluetoothd` hook.
- **Pairing: done from the computer.** Pairing a new device from the app would mean answering iOS's pairing
  requests (a confirmation code) with methods not yet verified on iOS 17.0. The page doesn't do it, and never
  accepts a request for you. An unpaired computer says *Available · pair it from the computer*.

## Scanning

A scan starts when the page opens, unless Speaka is playing or waiting for audio: discovery takes radio time from an
active A2DP stream and can make it stutter. Rescan always scans, because you asked. Each scan ends by itself after 12
s, and closing the page or leaving the app stops it. Nothing scans in the background. When `bluetoothd` restarts (Start
and Stop restart it), the page drops what the scan saw and what was in flight, and reads everything again.

## What was verified, and how

| How | What |
| --- | --- |
| Binary analysis | The entitlement gate and its levels (table above), in the iOS 17.0 `bluetoothd` in the repository |
| Host tests | `shared/DeviceRoster.h` compiled as C99 and GNU11 (also under AddressSanitizer and UBSan) and run: identity, history, renaming, bounds, states, freshness, timeouts, ordering, no headphones (`tests/test_app.py`, `ConnectTo`) |
| Source checks | The tweak records only at a verified connection; every BluetoothManager call is checked first; scans are bounded and stopped; nothing native is kept across a `bluetoothd` restart; Connect only for paired devices |
| CI | Builds the app with Xcode (the Swift and Objective-C can't be compiled off a Mac) |
| Device | Still needed: everything BluetoothManager does on iOS 17.0 |

## Device test

Install the release, then:

```powershell
$Phone = "mobile@10.5.42.238"
# 1. Open Speaka > Connect to, wait 15 s, then read what this iOS offers and what the scan found:
ssh $Phone 'cat /tmp/FTABHarvest/SpeakaConnect.log'
# 2. With Speaka started, play audio from the laptop, then check the history was recorded:
ssh $Phone 'grep V110_HISTORY /tmp/FTABHarvest/Speaka.log | tail -n 2'
# 3. The stored history itself:
ssh $Phone 'plutil -p /var/mobile/Library/Preferences/com.apple.bluetooth.plist | grep -A 14 SpeakaConnectionHistory'
```

What to look for in `SpeakaConnect.log`:

- `AVAILABILITY ready`: the entitlement worked. `AVAILABILITY unavailable ... available NO` means bluetoothd still
  doesn't serve the app.
- `INTERFACE BluetoothManager has: ...; lacks: ...`: the methods this iOS has. Connect needs `connect` on the device
  (or `connectDevice:`); scanning needs `setDeviceScanningEnabled:`.
- `NOTIFICATION ... arrives`: which notifications iOS sends. Nearby devices need
  `BluetoothDeviceDiscoveredNotification`; without it the scan runs but finds nothing.
- `SEEN 24:B2:... "LAPTOP-..." class 0x...`: a device the scan found. With Windows' *Bluetooth & devices > Add
  device* open, the laptop is discoverable.
- `CONNECT ... requested`, then `succeeded (Bluetooth link)`, `succeeded (audio)`, `failed` or `timed out`.

To test Connect, disconnect the laptop from the iPhone, keep Speaka started, open Connect to and tap Connect on the
laptop. Then check whether Windows starts sending audio on its own, or whether it has to be chosen as the output.
