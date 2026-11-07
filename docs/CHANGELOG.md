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
- **CI fix (run 114720996133):** the startup fixtures were named *.log, which .gitignore drops, so they were never
  committed and CI's checkout failed two tests. They are .txt now, and a test fails if any file the build or tests
  need would be ignored by git.
- **Exclusive audible playback (v9.8.92 Test D):** v9.8.92's mixing session let TikTok be heard alongside Speaka,
  which isn't wanted - but it also proved the route and the PC's stream stay intact while another app plays (no
  interruption, no route change, no phone-initiated START or SUSPEND). v9.8.91's non-mixing session silenced TikTok
  only by losing its session, and the phone suspended the PC's stream when Speaka took it back. The session now stays
  mixing (it can't be taken) and, when another app's non-mixing audio starts (secondaryAudioShouldBeSilencedHint),
  activates once without MixWithOthers - which makes iOS interrupt that app - then mixes again. It never goes inactive,
  so the route never leaves Speaka's output; an app that takes the audio straight back meets a doubling backoff (to
  8 s), never a loop. Calls and alarms still interrupt as before. Unverified on the device: that iOS honours the
  re-activation of an active session (V103_EXCLUSIVE shows each one) and reports the hint to a daemon (V103_OTHER_AUDIO).
- **The PC's name, from the connection (Test-92):** every one of Test-92's 21 routes was the speaker, so the route
  never names the PC while Speaka holds it (and v9.8.92's V102_PC_ROUTE never fired), and BluetoothManager reported
  "not ready". The tweak now reads bluetoothd's own paired-device records when the PC configures the speaker (and 10 s
  and 60 s later) and names the device seen most recently - only if no other was seen within 10 s of it - published as
  diag.v104.peer, which the app shows first. A new connection forgets the last name before looking, so a failed lookup
  shows "Your PC", never the wrong PC. Credits now gives BluetoothManager's lastInitError. Unverified: where iOS 17
  keeps those records (preferences or the system-group container: V104_PEER says which, or that none were found).
- **Startup, still unresolved (Test-92):** Test A's PC-started stream at 16.8 s after launch was slow until ~42 s
  after launch - the sixth measurement putting the end there - and nothing Speaka did coincides with the recovery.
  Test B was confounded (94.7 s after launch: past the window, and with mixing iPhone audio never reached the PC), so
  the phone-initiated-start idea stays untested. tools/startup_report.py turns any Speaka.log into per-stream rows (who
  started it, how long after launch, first rate, when full rate began) for the A-D matrix.
- **Startup throttle, narrowed (v9.8.91 device test):** across the v9.8.85-v9.8.91 logs, every audio stream the PC
  started within ~42 s of a bluetoothd launch, with no phone-initiated START or SUSPEND before it, arrived at a third of
  real time (9 of 9); every stream in that window started by Apple's own A2DP code, or after it had signalled, ran at
  full rate (4 of 4) - one 18 s after launch. So the slow window is phone-side and cleared by native streaming, not a
  fixed post-launch period and not Windows (my v9.8.87 entry was wrong about that). Speaka's receive path does no
  per-packet work. v9.8.87's always-held session also removed the phone-initiated signalling that used to clear it by
  accident. Every Start restarts bluetoothd, and Windows reconnects 7-15 s later, inside the window. Not fixed: that
  needs bluetoothd internals. Changed: a cold stream's first starvation verdict comes after 400 ms (below 70%), so a
  starved start is held before its first sound instead of after a second of choppy audio; every START/SUSPEND line
  carries `since_launch_ms`, so the next log tests the hypothesis directly.
- **Pause/resume latency (same test):** ~280 ms of each resume was Speaka's - a full 0.25 s prime every time, then
  Windows' four leading silent frames (14:52:14). A warm resume (a connection that has played 2 s at real time) now
  primes 64 ms, doubling after an underrun, and drops leading digital silence. Model with the measured packet gaps:
  245 -> 48 ms from the first audio packet to sound, no underruns, no stale audio, never gated.
- **TikTok and other apps (same test):** TikTok's session interrupted Speaka's, and iOS sent its audio to the PC -
  Apple's A2DP code started the PC's stream at 14:52:32.736 and suspended it when TikTok stopped (14:52:38.536). The
  session now mixes with other apps in every output mode (never a Bluetooth option, so Speaka's own output can't reach
  the PC); if another app still pulls the shared route away, Speaka falls back to exclusive playback once. Unverified:
  whether iOS keeps the shared route on Speaka's output while another app mixes in - `V102_MIX fallback` says if not.
- **The PC's name (same build):** the app named the PC from the audio route, which lists it only while nothing holds
  the route; since v9.8.87 the helper holds it permanently, hence "Your PC". The helper now captures the PC from the
  route before its own activation and on every route change (before and after), and the app matches the port's
  address with BluetoothManager's devices, never naming headphones as the PC (shared/PeerName.h).
- **Health lease during the input gate (v9.8.90 regression, fixed):** gated on a starved input, the output renders
  nothing but its queue still runs, so the helper reported "playing" with a frozen render count. The lease lapsed
  1.5 s into every gated stretch (12:05:55.870 -> 12:05:57.443 `render_age_ms=1516`, again 12:07:07 -> 12:07:08), the
  idle guard passed, and Apple's A2DP code suspended the stream ~10 s later - while packets were still arriving and
  decoding. Holding now covers the gate as well as an interruption (OutputPolicy.h `health_holding`): the helper
  reports decoded frames, so the lease lasts exactly as long as the stream is received and decoded, and still lapses
  within the normal 1.5 s when input or decoding stops. The lease now treats a switch between the two counters as a
  new baseline rather than progress, so alternating states can't fake health. `V882_HEALTH` logs `holding=`.
- **Startup stutter, measured (v9.8.89 device test):** the first ~26 s after a Start arrive at 13-27 packets/s
  instead of 47, in order - contiguous RTP sequence numbers, timestamps +1024 each, forwarded == decoded - so nothing
  is lost at Speaka's interception point; the packets are delivered late. The window ends ~42.5 s after bluetoothd
  launches (v9.8.87: 42.7 s, v9.8.89: 42.5 s) and appears identically in v9.8.85, whose helper never activated any
  audio session; a resume inside an established connection runs at full rate from its first packet. v9.9.1's 0.25 s
  jitter buffer filled, played and re-primed 26 times in that window. The helper now measures the decoded PCM rate
  (1 s windows; starved below 80% of real time, healthy again at 92%) and holds the output silent while the input
  can't sustain it, keeping the newest 0.25 s, then plays at once and continuously when it can. Silence is reported by
  cause (gated / priming / underrun / busy) with the measured input rate. This makes the window coherent; it does
  not shorten it - that needs the cause upstream of Speaka (the next device test isolates it).
- **Interruptions (same test):** at 11:05:31 another app took the audio and kept playing; v9.9.1 waited 8.8 s for an
  "ended" that never came, then reactivated - and Apple's A2DP code suspended the PC's stream (11:05:39.816), because
  the idle guard's lease needs rendered frames and had lapsed 1.5 s in. The queue is now paused, not disposed (v9.9.1
  also cleared the PCM, though its comment said otherwise); the helper reports "holding" with decode progress, which
  keeps the lease while it consumes the stream; and the interruption resumes only by its own rules - at once on
  "ended" or when the other app's audio stops, else a takeover after 3 s that doubles (to 24 s) if the app takes it
  straight back. A START or a route change can no longer resume an interrupted output early (v9.9.1's own_now could).
- **AAC startup stutter (v9.8.87 device test):** the first ~27 s of AAC were choppy, then smooth. The logs show why -
  every packet that arrived was forwarded and decoded (forwarded==decoded, no errors), but only ~24-30 arrived per
  second when 48 kHz needs ~47. (This entry first blamed Windows ramping its transmit rate; the v9.8.89 entry
  above shows that unsupported - Windows resumes at full rate inside a connection - and what the data does show.)
  Nothing in Speaka's decode path was the bottleneck, so no buffer can conjure the missing frames. The render callback no longer starts on the
  first frame and splices silence between the sparse real ones; it fills a 0.25 s PCM jitter buffer, then plays, and
  re-primes if it ever drains - turning a long stutter into one short wait, then smooth from the first audible moment.
  The arrival deficit itself is upstream of Speaka's decode path, visible in the log as `V881_RENDER priming=/reprimes=`.
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
