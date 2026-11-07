# AVRCP absolute volume: evidence and experimental implementation

## Status and scope

The reference `bluetoothd` contains a working **controller-side** absolute-volume implementation for remote
audio devices. Its **target-side** parser understands SetAbsoluteVolume, but the registered target callback
is null; its native target capabilities omit volume-change notifications and its notification handler rejects
event `0x0d`. Reusing the controller sender would send a command in the wrong direction for Speaka's sink.

This change adds a portable target protocol engine, a separate shared-memory volume control, post-decode
PCM gain, and an **off-by-default, build-specific experimental adapter**. The reverse-engineered addresses,
arguments, callback tables and packet construction are supported by static evidence. **No private hook,
native call, threading assumption or Windows/iPhone interoperability is claimed to be live verified.**

The volume being synchronized is **Speaka's software gain**, not iOS system/output volume. An explicit helper
command supplies local changes for notification testing. Hardware buttons and Control Center are not connected
to this layer; the phone's route volume still independently affects loudness. There is no new app slider.

## Inputs and reproducibility

The original binaries are opened read-only. The collector refuses different SHA-256 hashes and writes only
text/JSON in a separate directory. Addresses below are **image-relative offsets**; add `0x100000000` for
`bluetoothd` and `BTAvrcp` unslid VAs. `BTAudioHALPlugin` has image base zero. Runtime offsets use the loaded
Mach-O header as base. These offsets are not portable to another iOS build.

| Binary | Size | LC_UUID | SHA-256 |
| --- | ---: | --- | --- |
| BTAudioHALPlugin | 612784 | `22d918ef-8f6e-3c25-80b6-2306d4c12560` | `5194ccb2c0168bb7a5b70ee039024b194a5497b66dc5fc8fa72e21cb8ede1f68` |
| bluetoothd | 10196720 | `5491ba47-b5ae-3d44-9536-b30161df1e90` | `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6` |
| BTAvrcp | 174800 | `fffba70c-3450-3fd2-8fa9-86c70c83dd99` | `4bd97270734de51a461fd787aac6b76b574df656c5c234817b8bbd8bdb1bfa61` |

```sh
python3 -m pip install -r tools/avrcp-analysis-requirements.txt
python3 tools/inspect_avrcp.py docs/reference-binaries/ios17-iphone13 --output-dir docs/avrcp-evidence
```

The [collector](../tools/inspect_avrcp.py) uses LIEF 1.0.0 for Mach-O sections, chained fixups, imports and
`LC_FUNCTION_STARTS`, and Capstone 5.0.9 for arm64e instructions. Function boundaries are checked against
`LC_FUNCTION_STARTS`; the [manifest](avrcp-evidence/manifest.json) records whole-function hashes and entry bytes.
Annotated excerpts retain instruction bytes, call sites and fixup-backed pointers. ADRP/ADD/LDR annotations use
bounded straight-line propagation, not a complete dataflow analysis. Function labels are analyst descriptions,
except selectors recovered from Objective-C method metadata and explicitly noted assertion strings.

## bluetoothd: target path needed by Speaka

See [annotated disassembly](avrcp-evidence/bluetoothd.txt).

| Entry / data offset | Identification | Instruction and structural evidence |
| --- | --- | --- |
| `0x54f5c0` | AVRCP registration | At `0x54f65c` the descriptor is `0x9369e0`; `0x54f670` calls registration at `0xe1a98`. Descriptor `+0x10` resolves through a chained fixup to metadata callbacks at `0x936d78`. |
| `0xe0ff4` | Target vendor parser/dispatcher | Reads the AV/C frame, validates SIG company `0x001958` at `0xe12ac`, reads PDU at `0xe1280`, compares `0x50` at `0xe136c` and loads its one-byte parameter at `0xe1434`. It contains an inlined UnmarshalCommand assertion; that string alone is not its function name. |
| `0x936d78 + 0xc0` | SetAbsoluteVolume callback slot | PDU `0x50` selects the slot at `0xe15d0`; **the slot is zero in the file**. Null leads to `0xe188c`, status `0xce5`, then the rejection response path at `0xe161c`. A populated callback receives peer in `x0`, label in `w1`, volume in `w2`. |
| `0x551cf4` | Native target GetCapabilities | Capability 3 selects seven bytes at `0x779498`: `01 02 08 09 0a 0b 0c`; count 7 is passed to `0xdff50`. No `0x0d`. |
| `0x55b940` → `0x552cf8` | Native target RegisterNotification | Chained callback `0x936d78 + 0x58` points to the thunk. Handler compares event to `0x0c` at `0x552d7c`; higher values take rejection path `0x552e90`. Thus `0x0d` is not implemented here. |
| `0xdecac` | Simple metadata reply | Builds SIG metadata payload and an AV/C response, then calls `0xe28e4`. |
| `0xe28e4` → `0xdd580` | Target response and serializer | Response entry checks response bit 3. Serializer packs subunit type/id, copies operands, and calls AVCTP at `0xcc0f4` with profile `0x110e`, the transaction label, and response direction. The operand bytes are copied before return, including the transient-send queue path. |

The adapter intercepts `0xe0ff4` before the missing target callback. It consumes only volume commands and
events-supported capabilities for the peer associated with the active Speaka configuration. It delegates
other peers, other PDUs, other notifications and unavailable-helper cases to the original implementation.
It does not populate Apple's callback table or patch native data.

The recovered frame layout used at the receive/send boundary is:

| Byte offset | Meaning |
| ---: | --- |
| 0, 1, 2, 3 | AV/C ctype/response, subunit type, subunit id, opcode (each one byte) |
| 8 | Operand pointer (64-bit) |
| 16 | Operand count (16-bit) |
| total 24 | Includes native padding; compile-time layout assertions are present |

Metadata operands begin `00 19 58 <PDU> <packet-type> <length-big-endian> <parameters>`.
The implemented exchanges are:

| Request | Response / behavior |
| --- | --- |
| STATUS `0x10`, capability 3 | STABLE `0x0c`; preserves the seven native events and appends `0x0d` |
| CONTROL `0x50`, one volume byte | ACCEPTED `0x09` with applied value `0..127`; bit 7 is reserved and ignored |
| NOTIFY `0x31`, event `0x0d` + four interval bytes | INTERIM `0x0f` with current volume; retains this transaction label |
| Local Speaka gain change | CHANGED `0x0d` on the retained label, once; Windows must register again |
| Remote SetAbsoluteVolume | Updates gain and replies, without completing the outstanding notification |

Malformed handled requests are rejected; unrelated traffic stays native. Failed local CHANGED sends are
retried while the subscription is valid. Remote changes cancel pending local echoes. Disconnect/reset clears
subscription state. A successful response submission means native transport accepted the call, not that Windows
received it or that a sample has reached the speaker.

## bluetoothd: existing controller path and native audio bridge

| Function offset | Evidence |
| --- | --- |
| `0x559fcc` | Converts scalar float to integer using multiplier 127 (`0x42fe0000`, then `fcvtzs` at `0x55a1ac`); sends through `0x93fe0` at `0x55a258`. |
| `0x93fe0` | Constructs `00 19 58 50 00 00 01 <volume>` as a CONTROL command and calls the controller send path `0x93e30`. This is not the missing target setter. |
| `0xddc64` | Constructs RegisterNotification PDU `0x31`, ctype 3 and event parameter. |
| `0xddef8` | Assertion identifies `OI_AVRCPController_UnmarshalMetaResponse`; compares PDU `0x50` at `0xde048` and masks returned volume with `0x7f` at `0xde068`. |
| `0x5593e4` | Distinguishes PDU `0x31` and event `0x0d`; CHANGED `0x0d` at `0x559bfc` triggers re-registration through `0xddc64`. Also registers after receiving capabilities. |
| `0x37ec98` → `0x26f1b8` → `0x26fca4` | Remote-volume bridge to native audio-device property: wrapper supplies `kBTAudioMsgPropertyVolumeScalar`; float is wrapped with `CFNumberCreate` and delivered to native property-update functions. |
| `0x380fe8` → `0x3810dc` | Native audio volumeDidUpdate schedules a block which calls the controller scalar setter. |

Calling this controller setter for Windows would issue an outgoing target command instead of handling Windows'
incoming target request. The experiment therefore keeps these functions untouched.

## BTAudioHALPlugin and BTAvrcp

In [BTAudioHALPlugin](avrcp-evidence/BTAudioHALPlugin.txt), `0xfd34` creates an XPC double under
`kBTAudioMsgPropertyVolumeScalar`, builds an argument dictionary and passes message id **3** to `0xf808`.
That function builds an envelope with `kBTAudioMsgId` and `kBTAudioMsgArgs`, includes the audio object's string
identifier, and calls `xpc_connection_send_message` at `0xf8b0`. This proves an object-addressed native audio
property interface, not a callable global iOS speaker-volume setter.

The property dispatcher `0x6550` compares the scalar key at `0x6584`, reads the double on the `0x685c` path
and handles `absoluteVolume` as a boolean on the `0x69a0` path. A2DP functions `0x2ff70` and `0x2d7d8`
connect incoming/outgoing native device-volume updates. Speaka's independent AudioQueue is not instantiated
as this native A2DP output device. Injecting into this HAL path would not establish control over Speaka's PCM.

[BTAvrcp](avrcp-evidence/BTAvrcp.txt) has actual Objective-C method metadata. Its `handleMsg:` at `0x71c8`
reads XPC `kMsgId`/`kMsgArgs`, constructs a selector and dispatches it. The recovered handlers concern
MediaRemote playback, now-playing/library/browsing. `handleRegisterForChangesMsg:replyBlock:` at `0x5f14`
accesses now-playing information and invokes a reply block; this is not evidence of AVRCP volume subscription
handling. No dedicated volume selector was found in the recovered method lists. That absence alone does not
prove the executable can never participate in volume; the concrete target/parser evidence above determines
where this experiment integrates. No BTAvrcp or HAL hook is installed.

## Integration and runtime gates

- `shared/VolumeControl.*`: standalone C protocol state, peer/epoch isolation, callbacks for applying/sending,
  and signed-16-bit PCM gain. No Apple frameworks or hook dependency.
- `shared/VolumeMailbox.*`: optional 40-byte sidecar with daemon PID/token identity, checked ownership,
  permissions and size; atomic gain and helper heartbeat. Existing AudioRing ABI/handshake is unchanged.
- `tweak/VolumeControl.m`: opt-in native adapter. Requires Speaka's exact UUID check plus four-instruction
  fingerprints of both hooks, response function and lock/unlock, native event bytes and the null target slot.
  Missing/mismatched prerequisites disable the experiment. Function pointers are arm64e authenticated.
- Active peer association reads the existing learned `SpeakaPeerPath` against the accepted sink SetConfig owner.
  It does not change peer learning, device names or their publication. BD_ADDR formatter `0x4108f8` loads bytes
  0 through 5 in canonical text order, supporting the address comparison used here.
- Accepted sink configuration binds a fresh epoch; CLOSE, ABORT and sink loss reset it; SUSPEND retains it.
  AVRCP teardown thunk `0x55b2f4` forwards to `0x550eb0`, which removes native per-peer state. Its hook ABI
  remains a static candidate requiring live confirmation.
- The local-change timer enters native stack lock `0xa042c` / unlock `0xa0528`, the same mutex used by native
  outgoing AVRCP through guards `0x247c10`/`0x247c48`. Both functions demonstrably use the same pthread mutex.
  Receive-thread lock ownership, reentrancy and callback lifetime still require live checking.
- `audiohelper/AudioHelper.m` multiplies already-decoded stereo PCM immediately before AudioQueue enqueue.
  Gain 127 leaves every sample untouched; 0 produces exact digital silence. AAC/SBC parsers, decoders, stream
  format, endpoint registration, startup logic and AVAudioSession activation/ownership are not changed.

Off mode adds no volume hooks, timer or sidecar; the helper's missing-sidecar path has unity gain. Observe mode
logs a bounded sample of relevant requests and always delegates them to native code. Experimental mode requires
both a bound peer and a recent helper heartbeat before consuming requests. This guards availability but is not
an audio-output acknowledgement: AudioQueue buffering delays audible changes. The linear gain has no smoothing;
large changes may click and need listening tests. Sidecars follow the existing temporary ring-file lifetime.

## Tests and live validation

```sh
bash tools/fetch_google_sbc.sh
python3 -m unittest discover -s tests -v
BTPS_SANITIZE=1 python3 -m unittest discover -s tests -p test_volume.py -v
```

Host checks exercise real protocol C and shared mappings: all 128 volume values, all 65,536 int16 samples at
every gain, mute/unity, wrong peers/epochs, malformed packets including a deterministic 50,000-input corpus,
notification registration/re-registration, one-shot semantics, apply/send failures and echo suppression.
The existing extracted render callback is exercised with gain and mute, and sink-loss tests check reset.
Existing codec, stream-control, peer-identity and audio-session regression tests remain part of the suite.
Host tests cannot validate Apple private ABIs, actual AAC AudioConverter output or Windows interoperability.

Recorded Linux result: **242 tests run, OK, 2 skipped** (the macOS AudioToolbox tests). Reference-binary gates
ran against the supplied daemon. The portable volume runtime also passed AddressSanitizer and
UndefinedBehaviorSanitizer. Regenerating the evidence produced identical text and manifest files.

Use a build from this branch. Over SSH **as mobile**, first enable observation:

```sh
/var/jb/usr/bin/BTPSAudioHelper --volume-mode observe
```

Tap **Stop**, then **Start** in Speaka; connect Windows and move its volume slider. Collect `AVRCP_VOLUME`
lines from Speaka's debug log. Observe mode is not expected to make volume work. Confirm the peer and received
PDU/ctype/label/count, along with the A2DP configuration and helper heartbeat.

For the experimental native adapter, repeat with:

```sh
/var/jb/usr/bin/BTPSAudioHelper --volume-mode on   # 9.9: the default; only needed after --volume-mode off
```

After Stop/Start and reconnection, verify native installation gates pass, then test Windows slider values,
mute and maximum. With a registered event `0x0d`, issue a local Speaka change:

```sh
/var/jb/usr/bin/BTPSAudioHelper --volume 64
```

The command reports **submitted**, not applied. Confirm `local ... accepted=1`, native send status zero,
Windows slider change, and Windows' subsequent re-registration. Remote SetAbsoluteVolume must not produce
a CHANGED echo. Roll back with `--volume-mode off` followed by Stop/Start.

Outstanding device evidence required before enabling by default:

1. Validate receive/send/teardown ABI, arm64e trampoline behavior, transaction labels and native stack threading
   under repeated changes and disconnects; check for watchdogs, assertions and stale callbacks.
2. Capture actual AVCTP packets showing capabilities, INTERIM, accepted SetAbsoluteVolume, CHANGED and
   re-registration, including Windows mute, reconnect and application/endpoint volume behavior.
3. Resolve capability-discovery timing. Windows may query before A2DP configuration, peer identity or helper
   readiness; those early requests currently receive the native capabilities without `0x0d`. A reconnect may
   not help if Windows caches them. Association before A2DP and any SDP update need live evidence.
4. Check SDP target features. Registration feeds native feature values `0x11`/`0x1d1`, not category-2 bit `0x02`.
   This change does not alter SDP or claim Windows will discover the experimental capability. Confirm the live
   record and Windows requirements before advertising a feature that may not yet be ready.
5. Test SBC and AAC, startup/Stop/Start, track changes, long pauses, Bluetooth off/on, helper crash/relaunch,
   phone locking, competing apps and output changes (speaker, Lightning/wired earbuds). Confirm device names,
   sole session ownership, unity output and recovery remain as before.
6. Decide a separate system-volume integration policy if hardware buttons/Control Center must move Windows'
   slider. Current software gain does not observe or set that independent volume domain.

Protocol references: [Bluetooth AVRCP 1.6.3](https://files.bluetooth.com/wp-content/uploads/dlm_uploads/2024/10/AVRCP_v1.6.3.pdf)
and [Microsoft's classic-audio accessory guidelines](https://learn.microsoft.com/en-us/windows-hardware/design/accessory-guidelines/bluetooth-accessory-guidelines/bluetooth-accessory-guidelines-classic-audio).
The latter explicitly requires mute at zero, capabilities/notifications, and no CHANGED echo for a received
SetAbsoluteVolume. These references define intended behavior; the supplied binaries establish the implementation evidence.

## v9.9.8: integration with the stream-map identity, and corrections

Astra's protocol engine, mailbox, PCM gain and evidence are kept. Changes, each from the reference binary (Ghidra on
the user's `Speaka-iOS17` project and this repository's copy):

* **Peer association.** The patch bound the AVRCP peer by reading `SpeakaPeerPath` against SetConfigInd's x0. v9.8.96
  showed x0 is bluetoothd's process-wide manager and replaced the path with the stream-map lookup. The adapter now binds
  the address `peer_identify_stream` returns for the stream at configuration (`PeerIdentity.c` calls
  `SpeakaVolumeConfigure(id.address)`), verified on the device. The address is persisted (`SpeakaAVRCPVolumePeer`), so
  after Start the same PC is bound from the first AVRCP exchange - Windows may ask for capabilities before A2DP
  configures. A reset (close, sink loss, AVRCP disconnect) starts a new epoch and binds that PC again: no stale label or
  subscription survives, and other devices always go to native code.
* **Signatures confirmed.** `+0xe0ff4` is `(peer *, label, frame *)`: its first act on a vendor command is `+0xdc8c8
  (peer, 0)`, which compares a 4-byte and a 2-byte field (a 6-byte address); the frame keeps opcode at +3, operands at
  +8, count at +0x10. PDU `0x50` selects slot `+0xc0` (null: rejected, status `0xce9` in Ghidra's reading); a populated
  callback is called `(peer, label, volume byte)`.
* **SDP: absolute volume is not advertised.** `+0x54f5c0` registers the Target with `+0xe1a98(descriptor, features, 0,
  ...)`, features `0x1d1` or `0x11` (chosen at runtime), and the Controller with features `2`. `+0xe1a98` writes the
  features straight into the Target's SDP SupportedFeatures (once as given, once without bit 8). Neither value has
  Category 2 (bit `0x02`, Monitor/Amplifier, where absolute volume belongs). Experimental mode now hooks `+0xe1a98`
  (fingerprinted; its only caller is `+0x54f670`) and adds `0x02`; observe mode logs the value unchanged. The hook is
  installed from the tweak's constructor, before bluetoothd registers AVRCP. Whether Windows requires the bit is what the
  observation test establishes; Windows may cache a paired device's SDP record until it is removed and paired again.
* **Gates.** Capabilities and notification registration no longer wait for the helper: they need no audio. A
  SetAbsoluteVolume received before the helper runs is stored in the mailbox (created by the tweak at install) and the
  helper plays at it from its first buffer.
* **Mailbox access.** The sidecar follows the existing ring exactly (same directory, created by the tweak `0600`
  `O_EXCL|O_NOFOLLOW`, owner-checked by the helper, which launchd runs as `mobile`); the ring has worked on the device in
  every test, so the same identities apply. Its first live open is logged by the helper.
* **Diagnostics.** `AVRCP_VOLUME sdp target_features=... advertised=...`, `rx ... match= configured= handled=`,
  `set value=... helper_live=`, `tx ... status=`, `reset why=...`, and the helper's `V106_VOLUME gain=... output=`
  when the gain it plays at changes.

Still unverified on the device: the hooks at runtime, the response path, the local-change locking, and Windows'
behaviour (whether it uses absolute volume with this Target, and with or without Category 2).

## v9.9.9: why no `rx` appeared, and the observation points

Device evidence (v9.8.97): experimental mode installed, the Target registration advertised `0x1d3`, the laptop was
bound, the local gain path worked (08:17:33) - and no `AVRCP_VOLUME rx` line of any kind appeared, not even
GetCapabilities (`0x10`), which an AVRCP 1.4+ controller sends to a Target before anything else.

The receive hook is on the right path, and its filter is Apple's own (Ghidra, `bluetoothd`):

* `+0xe1a98` (Target registration) builds `adr x16, +0xe1d9c` / `paciza x16` and stores it at the AVRCP state `+0x28`:
  `+0xe1d9c (peer, label, frame)` is the Target's command callback for every incoming command.
* `+0xe1d9c` sends opcode 0 (VENDOR DEPENDENT) to `+0xe29fc`, which tail-calls `+0xe0ff4` (the hooked dispatcher)
  whenever the Target descriptor's metadata table (`+0x10` -> `0x936d78`) is present - it is. With no Target registered
  it logs "No target registered; rejecting command".
* For PASS THROUGH it rejects `frame[1] != 9 || frame[2] != 0` ("Rejecting PASS THROUGH command directed at subunit
  <%d,%d>"): byte 1 is the subunit type, byte 2 the id, byte 3 the opcode - exactly what `receive()` checks.

So no vendor command reached the Target at all. Either Windows never opened AVRCP to it, or it opened it and sent the
Target nothing. A likely reason for the second: Windows caches a paired device's SDP record, and the laptop was paired
when the Target advertised `0x1d1` (no Category 2); it has to be removed and paired again to read `0x1d3`. Three
read-only hooks, installed in observe and experimental mode alike, now tell these apart in one run:

| Log | Hook | Meaning |
| --- | --- | --- |
| `avctp_incoming peer=` | `+0xccb08`, AVCTP's incoming connection indication | Windows opened AVRCP |
| `avrcp_outgoing peer= result=` | `+0x54f8c4`, "Connecting AVRCP profile to device %{public}s" | iOS opened AVRCP |
| `tg_rx ... opcode= vendor_pdu=` | `+0xe1d9c`, the Target command callback | a command arrived, any kind, before any filter |

Each is fingerprinted (function starts, checked against the reference binary by the tests), logs at most 64 calls
(then every 100th) or 16 connections, and calls the original unconditionally.

## Initial volume

Before Windows engages absolute volume (registers for event `0x0d` or sends SetAbsoluteVolume) it attenuates the stream
itself, so Speaka stays at unity gain: no double attenuation. Once engaged, Windows sends full scale and expects the sink
to apply the level. The level Speaka reports when Windows registers is the last one that PC set (persisted,
`SpeakaAVRCPVolumeLevel`), so Windows adopting it causes no jump; for a PC never seen it is 64 (50%), never 127. If
Windows pushes its own level instead, that is applied. The level Windows had on another output before the connection is
not visible over Bluetooth; matching it would need a Windows-side component that reads the previous endpoint's volume.

## The iPhone's own volume

The iPhone's buttons and Control Center change iOS's output volume, a separate stage after Speaka's gain. Reporting it
to Windows while Windows' slider drives Speaka's gain would put two attenuations in series (and a slider showing one
of them). A single domain needs either Windows to drive iOS's output volume (a private setter, not verified) or the
buttons to drive Speaka's gain instead. This release only observes iOS's volume (`V106_IOS_VOLUME`, the helper,
read-only) to establish that the daemon sees the buttons; nothing is sent or applied from it.

## v9.9.10: one volume - iOS's output volume

**Device evidence (v9.8.98).** The whole incoming chain works: AVCTP from Windows (09:11:35.228), GetCapabilities
answered with `0x0d`, RegisterNotification answered INTERIM 64, then SetAbsoluteVolume 4..127 from 09:11:46, every one
ACCEPTED (status 0) with `V106_VOLUME` following it. Two findings shaped this release:

* **No double attenuation.** From 09:12:05 to 09:12:11 the slider swept the gain from 18 to 123 while the pre-gain decoded
  peak (`V881_PCM`) held at ~18,700-19,250: once engaged, Windows sends full scale. The sink is the only attenuation.
* **Two volumes.** The gain followed Windows, but the phone's buttons, HUD and Control Center show iOS's output volume -
  a separate stage, which the helper saw change (`V106_IOS_VOLUME` 89 -> 95 -> 102 -> 89 -> 76, 09:12:02-04) and nothing
  linked. No CHANGED was ever sent.

**Decision: iOS's output volume is the volume.** Windows' level is set as iOS's output volume, the user's changes go to
Windows, and Speaka's PCM gain stays at unity. The setter is the one Apple's remote-volume daemon uses: `mediaremoted`
binds `AVSystemController` from `Celestial.framework` (chained-fixup import, ordinal 6) and calls
`setActiveCategoryVolumeTo:` (objc stub `+0x1c6c20`, which loads selref `+0x285b38`; call site `+0xc2c10`, the level in
`s0`). The active category's volume is what the hardware buttons control. Reading it back is public
(`AVAudioSession.outputVolume`, already observed on the device).

The policy is pure C (`shared/VolumeControl.c`, `SPVolumeDomain`) and host-tested:

* Windows' levels are coalesced to one set per 50 ms; the newest wins.
* An iOS change within 1 s of a set is that set's own echo and is never reported - Windows dragging its slider can't
  turn into CHANGED responses fighting it. Later changes are the user's: the helper submits them as a local level, and
  the tweak sends CHANGED on Windows' retained label (one-shot; Windows registers again).
* Each set is read back 400 ms later; three ineffective sets in a row fall back to Speaka's gain (`V107_DOMAIN
  domain=speaka`), with no reverse sync in that domain. `BTPSAudioHelper --volume-domain speaka` chooses it directly.
* `V107_LEVEL submitted_peak pre= post= pcm_gain= ios=` reports, every 2 s while playing, the peak actually submitted to
  the AudioQueue before and after Speaka's gain - the audible-attenuation check `V106_VOLUME` alone couldn't give.

**Initial volume.** At connection the phone keeps its level: the helper reports iOS's current volume, so Windows'
slider adopts it (INTERIM if it registers after, CHANGED if before) - no loudness change. If Windows pushes its own level,
iOS follows it. Windows' volume on *another* output before the connection (the laptop's speakers) isn't carried over
Bluetooth; AVRCP only carries Windows' level for this Bluetooth endpoint. Matching the former would need software on
Windows.

**Hook safety.** Every replacement checks its trampoline before calling it. A missing one degrades instead of calling
null: the registration reports failure (bluetoothd logs "Failed to register as an AVRCP Target" and runs without it),
receive and connect return the native "no handler" status, the observers drop the call - each logged once
(`AVRCP_VOLUME FAILED CLOSED`).

Still unverified on the device: that `setActiveCategoryVolumeTo:` works from the helper daemon (the read-back decides,
and falls back if not), the CHANGED path, and Windows' reaction to CHANGED (moving its slider, re-registering).

## 9.9: standard

**Device evidence (9.8.99).** Windows -> iPhone: 188 `V107_IOS_SET` from 10:13:56, every read-back matching (for
example `ios=83/127` after a set to 83), `pcm_gain=127/127` throughout - one attenuation - and no
`domain=speaka` or `FAILED CLOSED`. iPhone -> Windows worked too, but **not through Speaka's CHANGED**: the log has
no `source=user` and no `0x0d` response. Windows' SetAbsoluteVolume commands followed the phone's own changes, which
fits iOS's AVRCP *controller* sending the phone's level to the PC (bluetoothd's controller volume path) and Windows
echoing it back; the echo window then correctly kept Speaka's CHANGED quiet. Inferred from the log's sequence, not
traced. Both paths are kept: Speaka's CHANGED covers a PC whose own reaction differs.

**What changed.** Volume sync needs no setting (`SpeakaVolumeSync` absent = on; the experiment's key is removed).
Initial level: at 10:13:56 the phone (127) was set to 51 - Speaka's saved level, sent as INTERIM when Windows
registered and then mistaken for Windows' choice. The mailbox carries the level's provenance now
(`sp_volume_mailbox_set_from`): only SetAbsoluteVolume is Windows' choice; INTERIM and the iPhone's own changes are not.
At connection the phone keeps its level and reports it, so Windows' slider adopts it - no loudness change. The saved
level is the last one applied, Windows' or the phone's, so the INTERIM usually already matches.

**Limits.** Windows' volume on *another* output before the connection (the laptop's speakers) isn't carried over
Bluetooth: AVRCP carries Windows' level for this endpoint only, and Windows answered Speaka's INTERIM rather than
pushing a level of its own. Matching the laptop's previous level would need software on Windows.

