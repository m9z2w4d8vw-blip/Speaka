# bluetoothd: what Speaka hooks, and how each part was found

Speaka supports exactly one `bluetoothd`, the one from the iOS 17.0 iPhone it was developed on:

| | |
| --- | --- |
| Size | 10,196,720 bytes, arm64e |
| SHA-256 | `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6` |
| LC_UUID | `5491BA47-B5AE-3D44-9536-B30161DF1E90` |

Everything here comes from disassembling that binary (LLVM's `llvm-objdump`) and from the debug logs of device tests.
Its offsets are in `tweak/BluetoothdBuild.h` and `tweak/Hooks.xm`; porting to another build means finding each one
again, and `tools/inspect_bluetoothd.py` collects the evidence for that.

These are the research notes as they were written, oldest first. They use the names of the time, and some describe
switches and test buttons that have since been removed (the Experiments switches and the debug log's test buttons went
in v9.8). The files they mention are now:

| In these notes | Now |
| --- | --- |
| `Tweak.xm` | `tweak/Hooks.xm` |
| `BTPSBuild.h` | `tweak/BluetoothdBuild.h` |
| `BTPSV77Sink`, `BTPSV78Capacity`, `BTPSV79Geometry`, `BTPSV81Config`, `BTPSV82SDP`, `BTPSDiagnostics` | `tweak/SinkEndpoint.c` |
| `BTPSV88Control` | `tweak/StreamControl.c` |
| `BTPSV88Daemon` | `tweak/AudioRelay.c` |
| `BTPSV88Shared` | `shared/AudioRing.c` |
| `BTPSShared.h` | `shared/SpeakaShared.h` |
| `btpsaudiohelper/` | `audiohelper/` |

1. [Static disassembly landmarks (v5)](#1-static-disassembly-landmarks-v5)
2. [The plan for Sink discovery and incoming audio (v6)](#2-the-plan-for-sink-discovery-and-incoming-audio-v6)
3. [Instrumenting SDP record adds (v7)](#3-instrumenting-sdp-record-adds-v7)
4. [The receive side: RTP packets to stereo PCM (v7.1)](#4-the-receive-side-rtp-packets-to-stereo-pcm-v71)
5. [AVDTP_RegisterMultipoint, and where error 0x68 comes from (v7.8)](#5-avdtp_registermultipoint-and-where-error-0x68-comes-from-v78)
6. [Where the six-entry AVDTP table comes from (v7.9)](#6-where-the-six-entry-avdtp-table-comes-from-v79)
7. [The configuration lookup's timing (v8.0)](#7-the-configuration-lookups-timing-v80)
8. [The seven-slot copy (v8.1)](#8-the-seven-slot-copy-v81)
9. [Offsets for live Sink signalling (v8.2)](#9-offsets-for-live-sink-signalling-v82)
10. [The audio-context expansion (v8.3)](#10-the-audio-context-expansion-v83)

## 1. Static disassembly landmarks (v5)

All addresses are **image-relative**; add `0x100000000` for the unslid Mach-O VM addresses of this binary.

- `0x0CECF0`: AVDTP_RegisterMultipoint (verified PAC instruction, 4-word signature).
- `0x0CED14`: `mov x19, x5` retains sixth parameter.
- `0x0CEDD4..0x0CEDDC`: validate x5, caps pointer and number of caps.
- `0x0CEDE4..0x0CEE08`: load endpoint table state; `state+0x60` carries the number of slots considered by the loop.
- `0x0CEE30..0x0CEE38`: advance candidate by `#0x28` (40-byte entry).
- `0x0CEE14`: load status 0x68 for failed registration.
- `0x0CEEE8`: `strb w8, [x19]`, demonstrating **one-byte** SEID output via sixth parameter.
- `0x0AB990..0x0AB99C`: passes x0=sp and x1 to `bl 0x1001E8E78` near error message “OI_SDPDB_AddServiceRecord failed”; exact private ABI needs additional call-site tracing.
- `0x44E170`: writes class UUID `0x110A` for Source; directly switching it to `0x110B` would remove an existing Source advertisement and is not an acceptable sink-only implementation.

**Critical limitation:** this does not demonstrate how much memory was allocated for the endpoint array, whether the SDP “add” routine is initialized at an arbitrary hook call, or how to publish an audio playback path. Do not change capacity fields blindly or call unknown ABI pointers.

### Additional SDP ABI evidence from disassembly (not a complete API contract)

At `image+0x0AB888`, the daemon constructs a 32-byte temporary SDP record descriptor:

- `sp+0x00`: pointer to attribute array (`0x10091A078` in the unslid image).
- `sp+0x08`: 16-bit attribute count = 3.
- `sp+0x10`: pointer loaded from the data structure at `0x10096DD78`.
- `sp+0x18`: 16-bit count loaded from `0x10096DD80`.

It later calls `0x1001E8E78` with `x0=sp`, `x1=out_handle_pointer` at `image+0x0AB998`. The callee allocates an `0x50`-byte internal node, copies the pointer/count pairs into fields at `+0x10/+0x18` and `+0x20/+0x28`, and writes a 32-bit record handle through x1 if non-NULL. It retains pointers, rather than deep-copying the attribute array in its entry path, making record lifetime/ownership a critical issue. It returns zero on apparent success and error codes on failures.

The callee scans the first array's 24-byte attribute descriptors to locate ServiceClassIDList (`0x0001`). This supports (but does not definitively prove) the provisional signature `OI_STATUS AddServiceRecord(const RecordDescriptor *record, uint32_t *handle)`. Before using this in a tweak, verify the attribute data-element structure and a known-good native Source publisher; a wire-format SDP sequence cannot simply be supplied as `RecordDescriptor`.

## 2. The plan for Sink discovery and incoming audio (v6)

### On-device evidence from the user's v5 debug log

- Tested `bluetoothd` image UUID `5491BA47-B5AE-3D44-9536-B30161DF1E90`; all five `pacibsp` entrypoint hooks installed and returned to original functions.
- Six native AVDTP **Source** endpoint registrations returned 0; assigned SEIDs 1..6.
- Capability headers from those calls: `07 00 00 04 00` (SBC), `07 00 02 06 00` (AAC), `07 00 ff 0e 00` (vendor-specific), each occurring twice. The codec identifier is byte `+2`; byte `+4` is zero in these captures and was incorrectly decoded by v5.
- `advertise_A2DP_source` was called twice; these observations do **not** mean Audio Sink UUID `0x110B` is published.
- The project has not registered or published a Sink in v5/v6. Headphone playback from iPhone works in the user's test after restarting `bluetoothd`.
- In-process probes of `AURemoteIOServer`, `AudioQueueServer`, `AudioComponentRegistrar` returned bootstrap error 1100 (sandbox denied). The `AudioSession` probe was reachable; this does not prove usable playback.

### Static evidence from the supplied iOS 17 bluetoothd binary

- The `AVDTP_RegisterMultipoint` entry is image+`0x0CECF0`; disassembly at `0x0CEEE8` performs a byte store of SEID (`strb w8, [x19]`) through argument x5.
- Its slot-search path advances by `0x28` bytes and loads `0x68` on one registration-failure path. v4's failure after interleaving Sinks is consistent with exhaustion, but *slot count and meaning of every error code need further runtime proof*.
- Near image+`0x0AB990`, code prepares x0=sp, x1=pointer and calls image+`0x1E8E78`. The same branch logs `OI_SDPDB_AddServiceRecord failed` if nonzero. This is a **candidate** private record-add function. The memory descriptor layout and owner lifetimes are not sufficiently verified to call it safely.
- Existing Source advertising at image+`0x44DF14` constructs data involving UUID `0x110A`. Do not replace `0x110A` with `0x110B`; that would degrade existing Source advertising.

### Implementation gates (in order)

1. **Protect the baseline**: observe six native Source registrations and verify normal headphones work on every build. No new endpoint calls until a free slot or a safe registered Source trade-off is demonstrated.
2. **Reverse SDP builder**: inspect multiple confirmed caller sites of the potential AddServiceRecord routine. Determine exact record and attribute representation, callback/release semantics, service lifetime, and error status values before adding any registration code.
3. **Prove available AVDTP Sink SEP**: a Windows SDP record alone is not enough; Windows must also find a Sink SEP that can accept configuration. Explore safe capacity strategy offline; never patch an array length alone.
4. **Publish SDP**: use a confirmed live record API with `Audio Sink 0x110B`, audio profile `0x110D`, L2CAP PSM `0x0019` and an AVDTP protocol descriptor. Verify from Windows/another peer. Only enable behind an explicit *new* experimental gate after separate fail-safe tests.
5. **Receive audio**: trace AVDTP/L2CAP media callbacks and packet boundaries; validate RTP sequence number/CSRC/extension/header length and SBC frame count, negotiate supported codecs, queue and decode frames without changing outgoing Bluetooth.
6. **Play PCM in a permitted audio process**: because bluetoothd is sandbox-restricted for several audio services, design an IPC boundary to a trusted helper with access to the output path. Do not add unverified AVAudioEngine startup code directly to bluetoothd.

v6 implements only Gate 1 (more detailed observation) and offline SDP data-element generation tests. Its `tools/sdp_record.py` creates wire-format SDP elements, **not** the private OI record descriptor required by the suspected `AddServiceRecord` routine. The existence of source-level `SBCSink.mm` and `sbc_decode.c` does not mean the media path is wired or tested on iOS.

## 3. Instrumenting SDP record adds (v7)

### Inputs and provenance

Analyzed `bluetoothd` from the user device: 10,196,720 bytes, SHA256 `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6`, UUID `5491BA47-B5AE-3D44-9536-B30161DF1E90`. These findings apply **only** to this binary.

### New direct-call evidence

Disassembly contains **20 direct calls** to unslid `0x1001E8E78` (image+`0x1E8E78`):

```
0x0093a88, 0x00a58fc, 0x00ab4b8, 0x00ab998, 0x00cb0b0,
0x00e1d14, 0x00e8044, 0x00ee324, 0x00f4a64, 0x00fd3f0,
0x00fee28, 0x0100cb0, 0x0216f70, 0x02175bc, 0x03ccf10,
0x0479998, 0x058f904, 0x058f944, 0x068575c, 0x068579c
```

The `0x00AB998` caller branches to a `...SDPDB_AddServiceRecord failed` logger on nonzero return. The call is plausibly the SDP database service record-add routine, but **no external source validates its ABI for other builds**.

### Input descriptor inference from actual instructions

Entry `0x1E8E78` has words `D503237F A9BD57F6 A9014FF4 A9027BFD`. It reads x0 as a descriptor, x1 as an optional 32-bit record-handle output pointer.

* The callee copies x0+0x00 pointer and x0+0x08 count (16-bit) into its internal 0x50-byte node.
* It likewise copies x0+0x10 pointer and x0+0x18 count into the node.
* It examines the first descriptor array with a 0x18-byte **stride** (24 bytes, not 32). For each entry it reads attr ID at offset +0 (u16), type at +8 (u8), and for ServiceClassIDList attr ID 0x0001 with type 0x06 it reads pointer +16 followed by u16 at that pointer+8. The u16 is a provisional class-ID candidate, not a full SDP attribute parser.
* If a record handle output pointer is non-null, it stores a u32. It returns zero on its success branch; other codes are not documented.
* The callee appears to keep attribute array pointers in the database node. Therefore any *future* synthetic registration must provide persistent memory and understand ownership. v7 does not allocate or add a record.

### v7 safe diagnostic design

1. Exactly five v6 hooks remain in the original table. The optional sixth hook is excluded from the five-hook summary and OFF by default.
2. The optional hook requires BOTH the known image UUID and all four exact prologue words. No fallbacks based on partial matches.
3. Input inspection uses the existing failure-safe `mach_vm_read` helper, strict pointer bounds, a cap of eight attributes per descriptor list, and reduced logging after the first 24 calls.
4. Original function is called **once and exactly once** with the two original pointers untouched. v7 never calls AVDTP registration, SDP registration, audio service creation, or media decode paths.
5. If an unknown build is encountered, the optional probe is refused. If runtime behavior worsens, disable the separate Settings toggle and restart `bluetoothd` once; restore v6 if required.

**Expected v7 result:** determine which kinds of SDP service records Apple publishes normally and the format of the existing Source record metadata, before considering a live Sink registration. Windows-to-iPhone Bluetooth audio remains not implemented.

## 4. The receive side: RTP packets to stereo PCM (v7.1)

This is **new executable functionality** beyond v6 diagnostics. It is also deliberately
**not a working Windows-to-iPhone Bluetooth path**: the private incoming AVDTP media
callback has not been safely identified/hooked; Apple's Sink endpoint and SDP record
have not been registered. Audio services cannot safely be started from `bluetoothd`
under the observed sandbox restrictions.

### Code actually implemented

1. `BTPSMedia.h/c`: accepts a complete RTP v2 packet with an A2DP SBC payload and
   checks version, CSRC count, extension length, padding, SBC media frame count,
   frame boundaries, bitpool bounds and consistent channel/sample-rate settings.
   Fragmented SBC media is explicitly rejected, as is extra unparsed data.
2. `sbc_decode.c`: corrected the previously incorrect 3-byte SBC header accounting
   to include the mandatory fourth CRC byte both in frame-length calculations and
   bitstream positioning. This decoder's CRC check and bit-exact audio fidelity are
   **NOT verified**, so synthesized audio quality must not yet be trusted.
3. `BTPSMedia.c`'s `btps_media_decode_rtp_sbc`: decodes SBC frames into caller-owned,
   stereo-interleaved 16-bit PCM using the project's existing research decoder. PCM
   capacity is validated before any frame is decoded.
4. `SBCSink.mm`: `speaka_on_rtp_packet()` can feed such decoded PCM into the
   existing ring buffer, conditional on the audio engine having been started in an
   output-authorized process. **No daemon hook calls it yet**.
5. `tools/decode_rtp_sbc.py`: host-side offline converter of *one complete raw RTP
   packet* to a WAV for test data. This is not a PCAP/L2CAP capture parser. C compiler
   required; output is only an experimental decoding check.
6. `tests/test_media_packet.py`: compiles the real C code and tests framing, bounds,
   malformed data, extensions, padding, declared packet frame count and decoder
   output count.

### Next implementation gates

* Test optional SDP observer against the actual device and collect the exported
  `SDP_PROBE` logs without altering existing service records.
* Reconstruct the private service record registration ABI and lifetime rules from
  several callers. Identify a safe way to register a *real* A2DP Sink SEP without
  exhausting the six existing Source slots.
* Only after an actual remote peer sees Audio Sink SDP `0x110B` and can negotiate
  SetConfiguration, trace the incoming AVDTP/L2CAP **media** callbacks and route raw
  complete packets to this parser.
* Move decoded PCM through an explicit, bounded IPC channel to an audio-output
  process permitted to use the iPhone speaker. `bluetoothd` bootstrap probes have
  denied several audio services; direct `AVAudioEngine` inside it is not a viable
  assumption.
* Add CRC validation, codec fixture tests with known/reference output, sequence-loss
  handling, fragmentation/reassembly, jitter buffering and lifecycle recovery.

### Security notes

* Passive observer OFF by default; exact daemon UUID and first-four-instruction
  guard for the optional SDP hook. Existing hooks remain pass-through.
* No new AVDTP Sink endpoint is allocated in this build; no SDP advertising altered.
* No audio capture or cloud network upload occurs. iCloud export is a user-selected
  Files document picker in Settings.app; the tweak never gains iCloud permissions.

## 5. AVDTP_RegisterMultipoint, and where error 0x68 comes from (v7.8)

**Evidence:** `bluetoothd` executable copied from this iPhone, SHA-256 `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6`, UUID `5491BA47-B5AE-3D44-9536-B30161DF1E90`; exact `arm64e` instructions disassembled with LLVM `llvm-objdump -d --start-address=0x1000cecf0 --stop-address=0x1000cef28 bluetoothd`.

`AVDTP_RegisterMultipoint` begins at image offset `0xCECF0` and receives endpoint media type `x0`, role `x1`, capability pointer `x2`, count `w3`, endpoint count `w4`, one-byte SEID output `x5`. Other callers' arguments are not exhaustively characterized.

The relevant instructions are:

| image offset | ARM64e instruction | Interpretation |
|---|---|---|
| `0xCEDE0` | `adrp x25, 0x100976000` | address private global page (hex offset is `0xCEDE0`) |
| `0xCEDE4` | `ldr x8, [x25, #0x8b0]` | load context pointer from image+`0x9768b0` |
| `0xCEDE8` | `ldrb w26, [x8, #0x64]` | next SEID candidate cursor |
| `0xCEE04` | `ldr x12, [x8]` | pointer to endpoint table |
| `0xCEE08` | `ldrb w10, [x8, #0x60]` | current slot-entry count |
| `0xCEE14` | `mov w0, #0x68` | preset fallback return code |
| `0xCEE24` | `ldrb w15, [x14]` | existing SEID at entry+`0x19` |
| `0xCEE34` | `add x14, x14, #0x28` | loop stride is 40 bytes |
| `0xCEE54` | `ldurb w14, [x27, #-5]` | occupied indicator (same entry+`0x19`) |
| `0xCEE60` | `add x27, x27, #0x28` | step to next endpoint |
| `0xCEE68` | `b.lo 0x1000cee54` | keep scanning while within entry count |
| `0xCEE78` | `cmp w9, #0xfe` | bound attempts over candidate SEIDs |
| `0xCEE80` | `b.lo 0x1000cee18` | retry candidate / scan |
| `0xCEE84` | `b 0x1000ceeec` | exit with default `0x68` when no slot selected |
| `0xCEE9C` | ... | successful free-slot path |
| `0xCEEB4` | `mov w0, #0` | success return |
| `0xCEEB8` | `sturb w26, [x27, #-5]` | set endpoint SEID in formerly free slot |
| `0xCEEE8` | `strb w8, [x19]` | copy 1-byte assigned SEID to caller |

**Inference (strong, not yet runtime-confirmed):** `0x68` occurs after the endpoint-registration search fails to identify a usable free slot; with six successful Apple Source entries it is strongly consistent with an occupied fixed-size table. An alternative is that the SEID search fails for another reason. `0x68` is *not* proof that a specific Apple public API reports “capacity exhausted.”

The v7.8 test snapshots image+`0x9768b0` and **only reads** the context's endpoint count and an owned local copy of up to 32 entries. It uses the byte at `entry+0x19` as the occupancy marker. `slots=6 occupied=6 free=0` would confirm this table was full at the time of the snapshot.

### Alternative approaches (not yet implemented)

- Reuse one of the existing Source slots for a Sink: dangerous unless Apple's Source initialization and outgoing audio expectations can tolerate losing that slot. A role change alone is insufficient and could break headphones.
- Expand the table: current allocation ownership, memory layout, absolute pointer references and other loops must be traced before any allocation/count modification. Merely setting count to 7 would cause an out-of-bounds write.
- Use an independently managed AVDTP/L2CAP profile stack: would need access to the already owned Bluetooth controller/transport, incoming signaling and media channels, without competing with `bluetoothd`. Feasibility unproven.

**Avoid repeating the v7.7 opt-in registration**: it already returned 0x68; v7.8 removes the switch and compiles out its side effect. Do not enable Legacy Sink or SDP advertisement.

## 6. Where the six-entry AVDTP table comes from (v7.9)

**Evidence:** User-provided iOS 17.0 `bluetoothd`, SHA-256
`e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6`, UUID
`5491BA47-B5AE-3D44-9536-B30161DF1E90`. Analyzed with
`llvm-objdump --disassemble --start-address=0x1000ce3f8 --stop-address=0x1000ce550 bluetoothd`
and `--start-address=0x1001f5f20 --stop-address=0x1001f5f68`.

### What static disassembly actually proves

The initializer around image offset `0xCE3F8` obtains **configuration index 3** and allocates:

| image offset | instruction | consequence |
|---|---|---|
| `0xCE3F8` | `mov w0, #3` | Select configuration index 3 |
| `0xCE3FC` | `bl 0x1001f5f20` | Receive pointer to configuration data in x0 |
| `0xCE408` | `mov w0,#0x68` | Request a 104-byte private context object |
| `0xCE40C` | `bl 0x1000aa09c` | Private wrapper around allocator |
| `0xCE414` | `str x0,[x27,#0x8b0]` | Save context pointer in private global |
| `0xCE41C` | `ldrb w8,[x23]` | Read endpoint-table count from config byte zero |
| `0xCE420`, `0xCE424` | `add x8,x8,x8,lsl #2`; `lsl x0,x8,#3` | Compute **count × 40 bytes** |
| `0xCE428` | `bl 0x1000aa09c` | Allocate endpoint array via private wrapper |
| `0xCE434` | `str x24,[x0]` | Save endpoint-array pointer to context+0 |
| `0xCE448`–`0xCE464` | initialization loop | Zero each endpoint's byte +0x19 (SEID slot marker) |
| `0xCE468`–`0xCE474` | second count, `x8 + x8*8`, `<<4` | Request config byte 1 × **144 bytes** |
| `0xCE480` | `str x8,[x0,#8]` | Save second allocation pointer to context+8 |
| `0xCE4D0`–`0xCE4D8` | config byte 2 × **64 bytes** | Request third allocation |
| `0xCE4E8` | `str x8,[x0,#0x10]` | Save third allocation pointer to context+16 |
| `0xCE4F4` | `strb w8,[x0,#0x60]` | Persist config byte 0 as the **live endpoint count** |
| `0xCE4F8` | `ldurh w8,[x23,#1]` | Fetch config bytes 1 and 2 |
| `0xCE4FC` | `sturh w8,[x0,#0x61]` | Persist other two live counts |

The getter at image+`0x1F5F20` uses a pointer table at image+`0x984a48`; for index 3,
`0x984a48 + 3*8 = 0x984a60` is the configuration pointer entry.
The private allocator wrapper at image+`0xAA09C` forwards to an allocator abstraction. The
actual heap ownership, memory-zone behavior and lifetime still need analysis. **An allocation
request size is NOT proof of the allocator's physical usable capacity.**

### New on-device read-only check

After six normal Source endpoint registrations, v7.9:

1. Verifies the exact build UUID and twelve exact instructions across initializer/getter.
2. Reads, without calling, the configuration pointer at image+`0x984a60`.
3. Reads only three config bytes, three count bytes at context+`0x60`, and the three table pointers.
4. Compares configured and live counts; reports requested allocation byte lengths (40/144/64 strides), and whether required table pointers exist.
5. Leaves all allocations, pointers, private configuration, SEIDs, SDP and registered endpoints **unchanged**.

A possible log looks like:

```
V79_ALLOC RESULT config=(6,?,?) live=(6,?,?) request_bytes=(240,?,?) match=yes missing_mask=0; READ ONLY
```

The extra counts are unknown until the iPhone runs the audit. The example is *not* a device result.

### Implications and next research

A carefully placed intervention at initialization might supply a larger configuration count,
causing the daemon to allocate and initialize a bigger endpoint array in its existing code.
That would be structurally preferable to increasing only context+`0x60` at runtime.
However we must first trace (a) where configuration block 3 originates, (b) whether it is
immutable/shared with other components, (c) the initialization/error cleanup paths and
(d) whether any parallel count / slot assumptions elsewhere prevent expansion.

**Do NOT yet patch config byte 0 or install a hook on either the config getter or allocator.**
We have only established the allocation arithmetic, not the safety or lifecycle of a new count.
No new SDP record or audio receive mechanism is implemented in v7.9. The new module performs no allocation and makes no private initializer calls.

## 7. The configuration lookup's timing (v8.0)

### Purpose

v7.9 observed `config=(6,3,3)` and `live=(6,3,3)`. This update tests whether
`bluetoothd`'s config-getter call for index 3 happens after the tweak loads and
whether the call returns the same pointer the existing read-only table audit saw.
**No memory changes, no new Sink endpoint, no SDP publishing, no Bluetooth receiving.**

### Testing

1. Apply the small patch in the correct GitHub folders, commit, and ensure the
   Actions rootless package build succeeds. Preserve working v7.9 `.deb`.
2. Install through Sileo. First leave **Legacy Sink OFF**, **SDP trace OFF**, and
   **Trace AVDTP config lookup (advanced) OFF**. Confirm 5 original hooks, 6
   native Source registrations, and Bluetooth headphone playback works.
3. Optionally enable ONLY **Trace AVDTP config lookup (advanced)**. This adds a
   read-only pass-through hook on an undocumented routine; it is experimental.
4. Restart Bluetooth once from PowerShell (update IP if it changed):

   `ssh -t mobile@10.5.82.212 "sudo killall -9 bluetoothd"`

5. Check the log for `V80_CFG_GET installed` and ideally
   `V80_CFG_GET index=3 ... bytes=(6,3,3) matches_table=yes`.
   Continue to require `V78_TABLE slots=6 occupied=6` and
   `V79_ALLOC config=(6,3,3) live=(6,3,3)`.
6. Confirm real headphone playback still works. Export the log to iCloud Drive.
7. Turn the advanced config observer OFF and restart `bluetoothd` one more time.

`NO_INDEX3_CALL_OBSERVED` is not proof the function is never used; it may have
been called before the hook loaded. Also, an observed index-3 lookup does not,
by itself, prove that the caller was the AVDTP constructor; other parts of the
daemon can request configuration index 3. The hook logs at most four index-3 calls,
leaving all pointers and return values unchanged. This test **does not** establish
that safely patching configuration is possible.

### Recovery

If there is instability, toggle the observer OFF, restart bluetoothd once,
and restore v7.9 if necessary. Do NOT enable Legacy Sink. Do NOT enable the
v7.7 extra endpoint trial.

## 8. The seven-slot copy (v8.1)

**This experimental code is not verified by an iOS device test.** It intercepts an undocumented initialization function, and may crash/restart `bluetoothd`. Keep a working v8.0 .deb and SSH access.

1. Upload updated files, keeping their paths including `BTPSV81Config.c/.h`, through your GitHub repo. Confirm Actions builds successfully; do not install on build failure.
2. Install .deb. Confirm `v8.1-one-shot-seven-slot` in Debug log.
3. With **Arm seven slots for ONE restart OFF**, keep **Legacy Sink OFF**, **Trace SDP OFF**, **Trace AVDTP config lookup OFF**. Restart bluetoothd if needed and verify 5 hooks, six native Sources, 6/6 endpoint slots and normal headphone sound. Export this baseline log.
4. Only after confirming baseline, switch **Arm seven slots for ONE restart ON**; leave other switches OFF.
5. Run `ssh -t mobile@10.5.82.212 "sudo killall -9 bluetoothd"` in Windows PowerShell (IP may change).
6. Open Debug log and look for `V81_CFG ARM CONSUMED` and `V81_CFG SHADOW`, then `V78_TABLE RESULT slots=7 occupied=6 free=1`, `V79_ALLOC RESULT config=(6,3,3) live=(7,3,3)` (expected mismatch because original config pointer remains untouched). Verify headphones connect AND audibly play.
7. Verify Settings switch has reset to OFF. **Do not re-arm immediately.** Export the log and send it for review.
8. Return to normal baseline with one more `bluetoothd` restart (arm should stay OFF). Confirm table returns to 6/6 and headphones work.

**Stop/rollback:** If Bluetooth is unstable, ensure the one-shot switch is OFF and restart `bluetoothd`. If it continues to fail, restore the v8.0 package through Sileo / safe mode and reboot-jailbreak as required. Never enable Legacy Sink. A seven-slot readout alone proves allocation capacity only; it does not mean an extra Sink can register or Windows can send audio.

**Design specifics:** Exact `bluetoothd` UUID and initializer signatures checked before installing getter hook. A one-shot preference is cleared and synchronized before trial. The getter may return a static aligned 32-byte copy of the original immutable index-3 config with only byte 0 changed 6→7, only on its first validated index-3 access. The original config, allocator, and native registrations are never modified. Active getter hook is excluded from the late signature scan only if its original instruction signatures were verified before installation; all other initializer instruction checks remain live.

**Scope:** This build makes no Sink service announcement and has no Sink registration code. Bluetooth receive still does not exist.

## 9. Offsets for live Sink signalling (v8.2)

SHA256 `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6`

LC_UUID `5491BA47-B5AE-3D44-9536-B30161DF1E90`

All addresses below refer to image base 0x100000000; offsets are arithmetic differences.

| Target | Entry RVA | Verified entry words | Proven role |
|---|---:|---|---|
| AVDTP_RegisterMultipoint | CE CF0 | d503237f d102c3ff a9056ffc a90667fa | Existing Apple Source registrar; six native Source calls demonstrated |
| AVDTP config getter | 1F5F20 | d503237f a9be4ff4 a9017bfd 910043fd | Existing v8.1 single-restart seven-slot allocation shadow |
| OI_SDPDB_AddServiceRecord candidate | 1E8E78 | d503237f a9bd57f6 a9014ff4 a9027bfd | Existing SDP record-add hook; retains attribute pointer arrays |
| AVDTP signaling RX dispatcher | D3B08 | d503237f d10203ff a90557f6 a9064ff4 | Caller passes CID, packet pointer, length; dispatches signaling IDs 1..13 |

### Relevant instructions from the uploaded image

```
1000ceed0: sturb w23, [x27, #-0x3]  ; endpoint type from x1
1000ceee8: strb w8, [x19]           ; one-byte assigned SEID output x5

1001e8ea4: ldr x8, [x21]
1001e8ea8: str x8, [x0, #0x10]      ; retained first attr array
1001e8eac: ldrh w8, [x21, #0x8]   ; count
1001e8eb4: ldr x8, [x21, #0x10]
1001e8eb8: str x8, [x0, #0x20]     ; retained second attr array
1001e8ebc: ldrh w8, [x21, #0x18]  ; count

1000d3b38: cbz w2, 0x1000d3f14
1000d3b3c: mov x19, x0
1000d3b40: str x1, [sp, #0x20]
1000d3b44: strh w2, [sp, #0x28]
1000d3c64: sub w16, w9, #0x1
1000d3c68: cmp w16, #0xc
1000d3c8c: br x16
```

Source SDP descriptor: caller `0x1000CB0B0` to `0x1001E8E78`.
Static Source attributes image+`0x91ABD0`, protocol sequence +`0x91AC30`, A2DP profile sequence +`0x91AC50`; primitives +`0x773678...`. Source IDs `0x0001`, `0x0004`, `0x0009`, `0x0311`, service-name `0x0100`. The Sink candidate uses Source-derived private element layout (24-byte attrs/16-byte elements) with `0x110B`, PSM `0x0019`, profile `0x110D`, feature value `0x0001`, name `Audio Sink`.

### Binary validation (not an on-device test)

```
python3 - <<'PY'
from pathlib import Path
import hashlib,struct
b=Path('bluetoothd').read_bytes()
assert hashlib.sha256(b).hexdigest()=='e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6'
for off in [0xCECF0,0x1F5F20,0x1E8E78,0xD3B08]:
 print(hex(off),[hex(x) for x in struct.unpack_from('<4I',b,off)])
PY
```

No TX signaling-response hook, internal SEP state-transition hook, or authenticated incoming RTP media hook is claimed. Those are pending further binary validation. This v8.2 trial cannot independently prove **PASS** or **KILL-NATIVE-SINK-ACP**; peer-side SDP and response checks are mandatory to establish signaling outcomes.

## 10. The audio-context expansion (v8.3)

All evidence is from the user-provided `/mnt/data/files/bluetoothd` (10,196,720 bytes), SHA-256 `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6`, LC_UUID `5491BA47-B5AE-3D44-9536-B30161DF1E90`. Tool: LLVM 17 `llvm-objdump -d --start-address=X --stop-address=Y bluetoothd`. Image base is 0x100000000. This binary is not shipped in the repository.

### Audio-table constructor and zero initialization

```
1003b39d8: mov   x19, x0
1003b39e4: str   w8, [x0]             ; NOTE: context+0, NOT entry+0
1003b39ec: mov   w0, #0x6             ; six entries
1003b39f0: mov   w1, #0x20            ; 32 bytes each
1003b3a04: bl    0x10075bea0         ; auth stub for malloc_type_calloc
1003b3a08: str   x0, [x19, #0x78]   ; table pointer
1003b3a0c: str   wzr, [x0, #0xc]     ; first entry+0xc
1003b3a14: ldr   d0, [x8, #0x320]  ; static template starts SEID=1
1003b3a18: stur  d0, [x0, #0x4]    ; entry+4..+b
1003b3a20: str   w21, [x0, #0x1c]  ; numCaps=3
1003b3a3c: bl    0x10075bea0         ; calloc 3 x 16-byte capabilities
1003b3a44: str   x0, [x8, #0x10]   ; entry+0x10 capability pointer
1003b3a4c: strb  w20, [x0]          ; CAPABILITY+0, not entry+0
1003b3aa8: strb  w20, [x8, #0x18] ; entry+0x18=1
```

Stub `0x10075bea0` is an authenticated bind through address `0x1009154d0` with chained-fixup word `c00900000000029a` -> import ordinal 666 -> `_malloc_type_calloc` (libSystem). Therefore the six-entry array is zero-initialized. No separate nonzero store to **parent** entry+0 identified in the native constructor. Keep new entry+0 as zero. Flag entry+0x18 is written explicitly to 1. The precise semantic meaning of this flag remains unknown.

### Registration-loop return is the intended interception boundary

```
100372bd4: add  x19, x0, #0x118
100372bd8: mov  x0, x19
100372bdc: bl   0x1003b39b4   ; construct higher-level audio table
...
100372e48: mov  x0, x19
100372e4c: bl   0x1003b4128   ; register native Source SEPs and Source SDP
```

Prologue signature for +0x3B4128:

```
1003b4128: d503237f  pacibsp
1003b412c: d10103ff  sub sp, sp, #0x40
1003b4130: a90157f6  stp x22, x21, [sp, #0x10]
1003b4134: a9024ff4  stp x20, x19, [sp, #0x20]
```

The native Source loop reads the audio count and registers each with TSEP=0:

```
1003b4150: ldr w8, [x19, #0x68]
1003b4160: ldr x8, [x19, #0x78]
1003b4164: add x5, x8, x22
1003b4168: ldr x2, [x5], #-0xc ; capability pointer, SEID out pointer at +4
1003b416c: mov w0, #0
1003b4170: mov w1, #0             ; native SOURCE TSEP
1003b4174: mov w3, #3
1003b4178: mov w4, #1
1003b417c: bl 0x1000cecf0
```

The function then invokes `0x1000cb008` to add Apple's native Source SDP record before it returns. Thus the new high-level entry MUST be installed only after the original `+0x3B4128` returns; otherwise the unmodified loop might register an accidental seventh Source.

### Entry layout at 32-byte stride

| Offset | Meaning | Evidence |
|---|---|---|
| +0x00..03 | zero from calloc in native constructor; further semantics unresolved | calloc import as above; no parent entry+0 nonzero store in examined constructor |
| +0x04..07 | SEID preseeded then used for lookup | `0x1003b3a18` and `0x1003b4418: ldr w10,[x9,#4]` |
| +0x08..0B | zero from template in native initial entries | static template `01 00 00 00 00 00 00 00` at `0x100778320` |
| +0x0C..0F | state initialized to zero | `1003b3a0c: str wzr,[x0,#0xc]` |
| +0x10..17 | pointer to 3 x 16-byte capability records | `1003b3a44: str x0,[x8,#0x10]` |
| +0x18 | byte initialized to 1; purpose not proven | `1003b3aa8: strb w20,[x8,#0x18]` with `w20=1` |
| +0x19..1B | remains zero from calloc on first native entry | no independent semantics proven |
| +0x1C..1F | numCaps=3 | `1003b3a20: str w21,[x0,#0x1c]` |

Codec capability pointer at second 16-byte entry+8 is the **audio context** `x19`, not the four SBC configuration bytes used by the *different*, low-level AVDTP SEP representation:

```
1003b3a90: str x19, [x8, #0x18]   ; cap_array+0x18
```

v8.3 duplicates a validated native SBC capability array for the new higher-level entry and uses the previously owned low-level Sink SEP capability array for `AVDTP_RegisterMultipoint`.

### Count/pointer consumers and teardown

```
1003b4028: stp w21, w21, [x19,#0x68]   ; two count fields
1003b4408: ldr w8,[x20,#0x68]          ; lookup count
1003b4414: ldr x9,[x20,#0x78]          ; lookup array
1003b4418: ldr w10,[x9,#4]             ; matching SEID
1003b4424: add x9,x9,#0x20             ; stride
1003b4298: ldr w8,[x19,#0x68]          ; deregistration loop
1003b42a8: ldr x8,[x19,#0x78]
1003b42b0: bl 0x1000cef28              ; deregister SEID
1003b3970: ldr x8,[x19,#0x78]          ; cleanup traversal
1003b3974: ldr x0,[x8,x21]             ; per-entry capability pointer
1003b3978: bl 0x10075bc70             ; free
1003b3990: ldr x0,[x19,#0x78]          ; current parent table
1003b3994: bl 0x10075bc70             ; free
```

The destructor frees the **currently published** table and its per-entry capability arrays. On successful swap, v8.3 deliberately retains the old 192-byte *parent* block but does NOT duplicate or free its six children; the copied pointers in the new array remain their owners. This avoids immediate dangling aliases to old entries, but **does not prove safe concurrency/ownership for every possible cached pointer**.

### Experiment limitations

- Original low-level seven-slot init remains the v8.1 one-shot feature. v8.3 adds a second high-level seven-entry table only with BOTH v8.2 one-shot arms enabled.
- This is not a verified safe API; `+0x18` flag semantics, possible pointer caching, synchronization, other users of +0x6C, and later codec initialization remain risks.
- A successful allocation, SDP add or handler entry does NOT demonstrate live A2DP Sink reception.
- The v8.3 post-hook is allowed only on the exact UUID and exact four-instruction signature.
- `KILL-NATIVE-SINK-ACP` remains untriggered until a role-exclusion branch is proven. A protocol REJECT remains diagnostic evidence, not an automatic kill.
