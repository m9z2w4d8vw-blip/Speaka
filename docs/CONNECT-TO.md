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

## Connecting from the iPhone

### Bluetooth link versus Speaka's audio

They're different, and only the second plays sound:

| Layer | Proved by | Log line |
| --- | --- | --- |
| Bluetooth link | `BluetoothDeviceConnectSuccessNotification`, or iOS listing the device as connected | `LINK ... up ... (a link, not audio)` in SpeakaConnect.log |
| A2DP set up | The computer configures Speaka's Sink (AVDTP SET_CONFIGURATION) | `V113_AVDTP from=pc SET_CONFIGURATION command`, `V105_PEER address=...`; `AUDIO ... set up` in SpeakaConnect.log |
| Stream started | AVDTP START | `V113_AVDTP from=pc START command`, `V88_STREAM active=1` |
| Media arriving | Packets on the media channel | `V893_RX ... forwarded=N` with N > 0 |
| Audio playing | Decoded PCM rendered by the helper | `V881_PCM ... nonzero=...` |
| Stream paused or ended | AVDTP SUSPEND, CLOSE or ABORT; `from=` says who | `V113_AVDTP from=pc SUSPEND command`, `V88_STREAM active=0` |

A connection notification proves only the first row. The page shows **Connected** only from Speaka's own stream.

### Why connecting from the iPhone worked only sometimes

Traced in the iOS 17.0 binaries (Ghidra) and matched to the device logs:

| Step | Where | What it does |
| --- | --- | --- |
| `-[BluetoothDevice connect]` | BluetoothManager `0x1f4ba9110` | `[[BluetoothManager sharedInstance] connectDevice:self]` |
| `-[BluetoothManager connectDevice:]` | `0x1f4bae94c` | `mov w3,#0xffffd7bf`, then `connectDevice:withServices:` |
| `connectDevice:withServices:` | `0x1f4bae814` | `BTDeviceConnectServices(device, mask)`, MobileBluetooth's `kCBMsgIdDeviceConnectServicesMsg` to bluetoothd |
| The services | bluetoothd `0x100440678` | names the bits: `0x1` Handsfree, `0x8` Remote (AVRCP), `0x10` A2DP, ... |
| Connect request | `0x100357360`, `0x1003d12ac`, `0x10043e128` | queues the request per device; a duplicate is ignored, a new mask merged |
| A2DP from iOS | `0x100373ec8` | sets "we initiated" (`strb #1, [x0,#0x268]` at `0x100373ef4`) if the device advertises Audio Sink `0x110B`, then connects AVDTP |
| Discovery | `0x100379034` | flag set: `AVDTP_Discover` at once; flag clear (the computer connected): waits **2000 ms** (`0x100379610`), "for incoming device" |
| Endpoints | `0x10037a3b0`, `0x1003b1a48` | keeps only audio **Sink** endpoints that aren't in use |
| None | `0x10037423c` | "Couldn't find any valid stream endpoint IDs - disconnecting": closes the media channel if a stream exists, then AVDTP |

So `connect`, with A2DP in its mask, made iOS start A2DP itself as a Source. It found the computer's endpoints, which are Sources (the 20:37 trace: `DISCOVER` from the iPhone, answered `52:src 54:src 55:src`), and disconnected A2DP. AVRCP dropped 0.35 s later. Speaka's audio comes only when the computer configures Speaka's Sink. That happened only when the computer did so before iOS gave up (the 20:40:50 success). When the computer connects first, iOS waits 2 s for it, which is why that path is reliable.

**The fix:** the page connects with `connectDevice:withServices:` and iOS's own mask without A2DP: `0xFFFFD7BF & ~0x10 = 0xFFFFD7AF` (`shared/DeviceRoster.h`, `sp_connect_services`). AVRCP, Handsfree and the rest are unchanged. iOS no longer starts A2DP, so the computer's A2DP setup isn't raced or torn down, and it follows the same path as when the computer connects first. The page also ends a scan before connecting, and sends one request per device at a time. Each attempt ends as audio, a link only (no stream 15 s after the link), or no link (20 s), each logged. Still to confirm on the device: whether Windows sets up the stream by itself after the iPhone links, or only once Speaka is chosen as its output.

### When the stream stops

At 20:36:52 the stream stopped on the computer's request: `RX ... sig=9 by=pc`, AVDTP SUSPEND of SEID 7, after about 9 s of silent media (`nonzero=0`, about 1.5 KB every 2 s). That's Windows pausing an idle stream; CLOSE followed at 20:36:57, also from the computer. The stops at 20:40:56 and 20:41:11 have no full trace. No new configuration followed the 20:40:56 stop (one `V105_PEER`, at 20:40:50), so the 20:40:59 start reused the same stream. That points to a pause, not iOS closing it, but only the `V113_AVDTP` lines of a new trial can say who sent it.


## Scanning

A scan starts when the page opens, unless Speaka is playing or waiting for audio: discovery takes radio time from an
active A2DP stream and can make it stutter. Rescan always scans, because you asked. Each scan ends by itself after 12
s, and closing the page or leaving the app stops it. Nothing scans in the background. When `bluetoothd` restarts (Start
and Stop restart it), the page drops what the scan saw, abandons an attempt in flight (logged), and reads everything
again. Connect ends a running scan before it asks iOS to connect. In `bluetoothd`, a connect request doesn't stop discovery:
its only side call turns the denylist off (`0x10041d828`). So the page does it, which takes the scan out of every
attempt. The 20:37 failure is explained without the scan, but the scan-running trials test it.

## What was verified, and how

| How | What |
| --- | --- |
| Binary analysis | The entitlement gate and its levels (table above), in the iOS 17.0 `bluetoothd` in the repository |
| Host tests | `shared/DeviceRoster.h` compiled as C99 and GNU11 (also under AddressSanitizer and UBSan) and run: identity, history, renaming, bounds, states, freshness, timeouts, ordering, no headphones (`tests/test_app.py`, `ConnectTo`) |
| Source checks | The tweak records only at a verified connection; every BluetoothManager call is checked first; scans are bounded and stopped; nothing native is kept across a `bluetoothd` restart; Connect only for paired devices |
| CI | Builds the app with Xcode (the Swift and Objective-C can't be compiled off a Mac) |
| Binary analysis (Ghidra) | How `connect` reaches bluetoothd, the service bits, the A2DP initiator, the 2 s grace for an incoming connection, the Sink-only endpoint filter and the disconnect (the table above) |
| Host tests | The connect mask, the attempt state machine (request, link, audio, link only, no link, duplicates, late notifications) and the AVDTP trace on the logs' own PDUs, as C99 and GNU11 (`ConnectReliability`) |
| Device | Confirmed earlier: BluetoothManager answers, scans find the laptop, connection notifications arrive, history is saved. Still needed: the trials above with this release |

## Device test

Install the release and tap Start. Keep the Connect to page open during each attempt: it's what logs the attempt's
outcome.

```powershell
$Phone = "mobile@10.0.0.128"
# One attempt's outcome, from the page (request, link, audio or not):
ssh $Phone 'grep -E "CONNECT|LINK|AUDIO|SCAN" /tmp/FTABHarvest/SpeakaConnect.log | tail -n 12'
# The same attempt in bluetoothd: who sent which AVDTP signal, the stream, the media and the PCM:
ssh $Phone 'grep -E "V113_AVDTP|V105_PEER|V88_STREAM|AVRCP_VOLUME (engaged|reset)" /tmp/FTABHarvest/Speaka.log | tail -n 30'
ssh $Phone 'grep -E "V893_RX|V881_PCM" /tmp/FTABHarvest/Speaka.log | tail -n 6'
# The complete logs, for anything that needs the whole sequence:
$Dir = "$env:USERPROFILE\Desktop\SpeakaTrial-$(Get-Date -Format yyyyMMdd-HHmmss)"; New-Item -ItemType Directory $Dir | Out-Null
scp "${Phone}:/tmp/FTABHarvest/Speaka.log" "$Dir\"; scp "${Phone}:/tmp/FTABHarvest/SpeakaConnect.log" "$Dir\"
```

### Controlled trials

Change one thing at a time. Before each app attempt, disconnect the iPhone from the laptop without unpairing (Windows
*Bluetooth & devices*, the iPhone's *...*, *Disconnect*), and wait 10 s.

1. **Windows-initiated control (3 attempts).** On the laptop, choose Speaka as the output. Expected: `V113_AVDTP
   from=pc DISCOVER`, `SET_CONFIGURATION`, `OPEN`, `START`, then `V893_RX` and `V881_PCM nonzero`.
2. **App, no scan (5 attempts).** Open Connect to, wait for *Searching* to finish, tap Connect once.
3. **App, scan running (5 attempts).** Tap Rescan, then Connect within 3 s. The log should show `stopped the scan
   first`.
4. **App, Speaka not the laptop's output (2 attempts).** Choose the laptop's own speakers first. This tells whether
   Windows sets up the stream only for its chosen output.

For each attempt, record:
- the link: `LINK ... up`;
- A2DP configured: `V105_PEER`, or `AUDIO ... set up`;
- `V88_STREAM active=1`;
- `V893_RX forwarded > 0`;
- `V881_PCM nonzero > 0`;
- at least 30 s of audible playback;
- the time from the request to the `AUDIO` line;
- any stop, and its `from=`.

With the fix, an app attempt should show **no** `V113_AVDTP from=iphone DISCOVER command`. If one appears, iOS still
started A2DP itself.

What the older lines in SpeakaConnect.log mean:

- `AVAILABILITY ready`: Bluetooth answers the app (the entitlement).
- `INTERFACE BluetoothManager has: ...`: the methods this iOS has; the fix needs `connectDevice:withServices:`.
- `NOTIFICATION ... arrives`: which notifications iOS sends.
- `SEEN 24:B2:... "LAPTOP-..." class 0x...`: a device the scan found.
