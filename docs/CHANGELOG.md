# Changelog

Newest first. Since v9.3 every push to `main` is a release, numbered from `control`'s version and the build's run
number (9.8.74 is a push of 9.8), so the versions below are the ones where something changed. Each release's full
notes, with its device-test steps, are in the git history (`docs/history/`, before v9.8's cleanup). How each part of
`bluetoothd` was found is in [BLUETOOTHD.md](BLUETOOTHD.md).

## Unreleased - experimental AAC-LC (behind the SpeakaAAC switch, off by default)

Groundwork for receiving AAC-LC as well as SBC. SBC is unchanged and remains the only codec used until the native
blocker below is resolved; the AAC paths only run when the **SpeakaAAC** preference is on, and the helper always
falls back to SBC if AAC setup fails.

**What is implemented and tested (pure protocol and decoding, from the public A2DP AAC and MPEG-4 specs):**
- `shared/AACConfig.c/.h`: the AAC Codec Specific Information - building Speaka's Sink capability (AAC-LC, 44.1/48
  kHz, mono/stereo), parsing a negotiated configuration, the MPEG-4 AudioSpecificConfig and sampling-frequency index.
- `shared/LATM.c/.h`: the A2DP AAC payload is an MPEG-4 LATM AudioMuxElement, not a bare access unit. This parses
  the AudioMuxElement (StreamMuxConfig with an inline AAC-LC AudioSpecificConfig, PayloadLengthInfo, PayloadMux) and
  extracts the AAC access unit. Validated against ffmpeg: 25/25 access units from an ffmpeg LATM stream come out
  byte-identical to ffmpeg's own ADTS encode of the same audio.
- `audiohelper/AACDecoder.c/.h`: an AAC-LC decoder over AudioToolbox's public `AudioConverter` API. It depacketizes
  the LATM element to the access unit, ADTS-wraps THAT, and decodes. (The first version wrongly ADTS-wrapped the raw
  LATM payload, which would decode to noise; corrected after review.)
- `audiohelper/CodecDecoder.c/.h`: one handle over the SBC and AAC decoders; reads the codec from the ring header.
  It falls back to SBC only for a session the SRC negotiated as SBC. If AAC is negotiated but cannot be decoded
  (AAC off, bad config, converter failure), it goes to a silent DISABLED state rather than feeding AAC/LATM to the
  SBC decoder (which would produce noise). A mid-stream AAC decode failure drops the frame; it is never SBC-misread.
- The shared ring gained a `codec` field and `codec_config` in its **header** (not the packet), so the packet ABI is
  unchanged and an older helper still reads a session as SBC. `AudioRelaySetCodec` publishes it; the CONFIGURE hook
  calls it (with SBC today).
- `sink_build_aac_codec_record` builds the AVDTP media-codec capability record for AAC-LC.
- `tests/test_aac.py`: tests for the config parsing, the capability record, the ASC, the ADTS framing, the LATM
  depacketizer (cross-checked against ffmpeg fixtures), and the DISABLED fallback. All of `test_audio.py`'s SBC
  tests still pass unchanged.

**Blockers - what AAC still needs, and why it is not done here:**
- *Registering a second (AAC) endpoint in `bluetoothd`.* The SBC Sink is registered by writing recovered native
  structures at specific offsets in one exact `bluetoothd` build. A second, AAC endpoint needs the same native
  layout for AAC, which would require disassembling Apple's signed binaries to recover. That reverse engineering is
  out of scope, so no AAC SEP is registered and Windows will keep negotiating SBC. This is isolated behind
  `sink_build_aac_codec_record` (the record is built; nothing advertises it).
- *Confirming the decode on a device.* The LATM depacketizer is verified on the host against ffmpeg, but
  `AACDecoder.c`'s AudioConverter stage is compiled by CI and never run (no Apple toolchain on the host), so AAC ->
  PCM is unproven until a device test. A packet capture can confirm which codec was negotiated, but not the native
  endpoint layout.

On-device steps and how to read the codec line: [AAC-TESTING.md](AAC-TESTING.md).

Later in this cycle:
- **Decoder fix:** AudioConverter now gets each bare access unit with an ESDS magic cookie. The previous build sent
  ADTS-headed packets to a raw-AAC converter, which Apple's decoder reads as corrupt data.
- **`BTPSAudioHelper --aac-selftest`:** decodes an embedded fixture through the real decoder on the phone and prints
  JSON (`tools/device_test`). It replaces the standalone test binary, which the phone killed at launch (exit 137).
- **The codec at CONFIGURE** is read from the PC's SET_CONFIGURATION instead of assumed; fragmented LATM elements
  are reassembled; an in-band configuration change rebuilds the converter.
- **CI:** a separate `device-test` job decodes the fixture with the real AudioToolbox on macOS and builds the phone
  kit; the release doesn't wait for it.
- **AAC-only endpoint (`SpeakaAACSink`, in the app's Settings):** SEID 7 offers AAC-LC instead of SBC, for a first live
  AAC test. Off by default; turning it off and tapping Start restores SBC. Fixed before anyone enabled it:
  v9.8.84 changed only the audio-table entry (the PC would still have been offered SBC) and wrote AAC's 6 codec bytes
  into a 4-byte tail. Both tables now agree, and compile-time checks size both buffers for either codec.
- **Live AAC from Windows (v9.8.85 device test):** Windows negotiated AAC (MPEG-2 AAC-LC, 48 kHz stereo, VBR 256 kbps)
  and streamed it, but every packet failed with -11: the parser accepted only one access unit per packet and only
  the simplest LATM, while Windows packs as many frames as fit (about 3 per packet at low VBR rates, 1 near 256 kbps).
  LATM is now parsed per ISO/IEC 14496-3 (persistent StreamMuxConfig, several frames per element, audioMuxVersion 1,
  otherData), and LOAS, ADTS, RFC 3640, out-of-band LATM and bare access units are accepted too - each only when its
  own length fields account for the whole packet. Every access unit in a packet is decoded. The phone's SUSPENDs
  were the idle guard correctly handing a stream with no PCM back to Apple, and stop once PCM flows. The helper logs
  each stream's first packets (V94_AAC_PKT: transport, frames, first 64 bytes).
- **Owning the output (v9.8.86 device test):** AAC played, but every Windows pause turned into a loop. iOS treats the
  PC as a Bluetooth output, so Apple's A2DP code suspends the stream when the helper activates its session and starts
  it when the helper lets go; the helper let go after 8 s without media, waited 3 s, activated again - 13 phone STARTs
  and 13 phone SUSPENDs in two minutes, with resuming landing in the middle. The helper now owns the output for the
  whole session: the tweak connects it as soon as the PC configures the speaker, it activates before the stream
  starts, keeps its session and queue through pauses and track changes (playing silence), and never lets go for lack
  of media - only at Stop, or 30 s after the PC closes its stream without reopening. Its session is exclusive
  (non-mixing) and asks iOS not to interrupt it for system alerts; when another app takes the audio, the helper takes
  it back within 250 ms (a call or an alarm makes that fail until it ends). Resuming on the PC plays at once: nothing
  is re-initialized.
- **AAC startup stutter (v9.8.87 device test):** the first ~27 s of AAC were choppy, then smooth. The logs show why -
  every packet that arrived was forwarded and decoded (forwarded==decoded, no errors), but only ~24-30 arrived per
  second when 48 kHz needs ~47; Windows' A2DP source ramps its transmit rate over the first seconds. Nothing in
  Speaka was the bottleneck, so no buffer can conjure the missing frames. The render callback no longer starts on the
  first frame and splices silence between the sparse real ones; it fills a 0.25 s PCM jitter buffer, then plays, and
  re-primes if it ever drains - turning a long stutter into one short wait, then smooth from the first audible moment.
  The arrival deficit itself is upstream (Windows), stated in the log as `V881_RENDER priming=/reprimes=`.
- **Interruptions that kept recurring (same test):** v9.8.87 retook the output ~250 ms after every app interruption,
  and each retake's reactivation nudged the other app to interrupt again. The helper now tears the dead queue down
  without a NotifyOthers deactivation, keeps the decoder and buffered PCM, and parks until iOS reports the
  interruption ended (a long backstop still reclaims if it never does), instead of fighting. iOS offers no public way
  for one app to be immune to other media apps, so this makes recovery clean rather than guaranteeing priority.
- **App build:** v9.8.84's app failed to build (an undeclared name in SPTweakClient.m), so that release has only the
  tweak; a new test checks every SP-prefixed name in the app's Objective-C is declared.

## v9.8

- **Caz's four-row menu:** Settings, Audio Output, Uninstall, Credits, at Caz's exact sizes and positions.
- **Uninstall, held for 4 seconds:** removes the tweak with dpkg and restarts Bluetooth, deletes Speaka's logs,
  settings and downloads, then has TrollStore remove the app. A gray fill sweeps across the row while you hold; a tap
  does nothing, and letting go early cancels.
- **Settings** a size smaller, without descriptions under its rows.
- **Publishing** retries when GitHub's release service fails partway (run 68 left 9.7 as a draft).
- **The cleanup:** the dead code went (the pre-libsbc decoder, the v7.3–v7.6 tests and their Settings buttons, the
  Experiments switches); the sources moved into `tweak/`, `shared/`, `audiohelper/` and `settings/`, named for what
  they do; 25 test files became 6; the release and research notes became this file and BLUETOOTHD.md. Nothing that
  runs changed, and every name the phone keeps is the same.

## v9.7

- **Update notifications:** a switch in the app's Settings. The app checks every 2 minutes while it's open, and asks
  iOS to check in the background too; each release is announced once.
- **Kill Bluetooth** is just the button; a line appears only if it fails.
- **Settings > Speaka** no longer has what the app does: Start, Stop and the audio output.

## v9.6

- **Settings, with Kill Bluetooth:** `killall -9 bluetoothd` from the app, through its root helper.
- **The PC's name** comes from the audio route, where iOS lists the streaming PC as a Bluetooth output.
- **Paused is still connected:** the connection line keeps the PC while it pauses.

## v9.5

- **One `.tipa` installs everything:** `Speaka.tipa` carries the tweak's `.deb`, **Install Tweak** installs it, and
  updates are one download.

## v9.4

- **Updates that install:** v9.3's failed with *Operation not permitted*.
- **Start shows every step,** and which one failed, with the tweak's own reason.

## v9.3

- **Speaka's own background** and teal accent again, on Caz's panels.
- **The app updates itself and the tweak** from GitHub releases.
- **A numbered release for every push,** instead of a rolling nightly.

## v9.2

- **The app takes Caz's look,** from Caz's own code: one Start/Stop button that opens into a live log. Only the app
  changed.

## v9.1

- **No more Headphones / PC flicker:** with wired earbuds in, Control Center switched between them and the PC every
  0.7 s and nothing played. The helper now keeps its output through stream changes and never re-activates its audio
  session faster than every 3 s.

## v9.0

- **Renamed to Speaka** throughout, including the package (`com.parv.speaka`) and the names the parts use to find
  each other, so every part had to be updated together.

## v8.9.4

- **Wired earbuds** work without replugging, and **switching outputs** no longer needs the PC to reconnect.

## v8.9.3

- **Logs that cover the whole session** and say where the audio went; the export includes the helper's own log.

## v8.9.2

- **Start reaches the tweak:** it restarts `bluetoothd` from inside, and Bluetooth being turned off and on is
  detected and reported instead of failing silently. Replaced v8.9.1, whose restart approach was wrong.

## v8.9

- **One-tap Start and Stop,** and a choice of audio output, then in Settings > Speaka.

## v8.8.2

- **Native idle-check guard:** `bluetoothd` no longer suspends a stream the helper is playing.

## v8.8.1

- **The iPhone's speaker** as the output route.

## v8.8

- **Continuous audio:** the PC's SBC packets go through shared memory to the separate audio helper, which decodes them
  with Google's libsbc and plays them. The first build where Speaka worked as a speaker.

## v8.7

- **A raw counter at the media handler's entry,** to confirm the PC's media bytes arrive.

## v8.6

- **A receive-only media channel trial:** whether the PC's media bytes reach `bluetoothd`'s incoming L2CAP media
  handler.

## v8.3

- **The post-Source audio context:** `bluetoothd`'s audio table gets room for the Sink after its six Sources.

## v8.2

- **Live native Sink signalling:** the Sink registered as SEID 7 and the Audio Sink SDP record (`0x110B`) published,
  for one armed restart.

## v8.1

- **The one-shot seven-slot copy:** the endpoint table's configuration gets a seventh slot, for one restart.

## v8.0

- **A read-only timing probe** of `bluetoothd`'s AVDTP configuration lookup.

## v7.9

- **A read-only audit** of the endpoint table's allocation geometry.

## v7.8

- **A read-only AVDTP capacity audit,** which found where v7.7's error `0x68` came from. The v7.7 trial was disabled
  for good.

## v7.7

- **A two-phase trial** of registering a Sink after the six native Sources.

## v7.6.1

- **Helper playback diagnostics** and limited gain.

## v7.6

- **The helper decodes a recorded SBC clip** and plays it.

## v7.5

- **`bluetoothd` sends compressed RTP/SBC** (a compiled-in recording) to the helper.

## v7.4

- **`bluetoothd` sends test audio** to the independent helper over loopback.

## v7.3

- **A separate helper process** plays PCM sent from Settings: the first audio outside `bluetoothd`.

## v7.2

- **A verified test sequence.**

## v7.1

- **Save Log to iCloud Drive,** SDP research, and the receive-side media foundation.

## v7

- **Optional observation** of SDP record registration.

## v6

- **A device testing procedure;** diagnostics only.

## v5

- **The first on-device checklist:** observe-only hooks that keep the native Source endpoints and log their SEIDs.
