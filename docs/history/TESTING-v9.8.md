# Testing v9.8: Caz's menu, and hold to uninstall

## What changed

**The menu** has four rows, like Caz's: Settings, Audio Output, Uninstall, Credits. The panel and its rows were
already Caz's code, so with a fourth row they land exactly where Caz's do (spec page 5): 73 pt rows, a 330 x 302 pt
panel at y 306.7 on an iPhone 13, icons at 19 pt Medium, titles at 17 pt, separators 10 pt in. Uninstall is an action,
like Caz's Scan, so it has no chevron.

**Uninstall** acts only when held for 4 seconds:

| You | It |
| --- | --- |
| Tap it | Nothing (the edge starts across and runs straight back) |
| Hold it | A gray fill sweeps from left to right over 4 s, and what it has covered dims to 45%, like a disabled row; it still reads *Uninstall* |
| Let go early | The edge runs back; nothing happens |
| Hold 4 s | A warning haptic; the whole row has the disabled look, with a spinner and *Uninstalling...*; everything else locks |

Then, with no second prompt:

1. `speakahelper remove-tweak`, as root: `dpkg -r com.parv.speaka` (its remove script stops the audio helper), then
   bluetoothd is killed so the removed tweak unloads, and Settings is closed so Settings > Speaka goes. Then Speaka's
   files: in `/tmp/FTABHarvest` only names starting with *Speaka* or *BTPS* (the folder goes only if that empties
   it), and `/var/mobile/Library/Caches/Speaka`.
2. The app removes every key starting with *Speaka* from Bluetooth's preferences (all of Speaka's do; a test checks
   every key the tweak and Settings use), and its own settings.
3. `speakahelper uninstall-app` starts `finish-uninstall` in its own session, which runs
   `trollstorehelper uninstall com.parv.speaka`. Speaka closes and its icon goes.

If dpkg fails, nothing else is removed: Bluetooth isn't restarted, no files go, and the header says why (*The tweak
didn't uninstall (dpkg exited with N)*). If the tweak is already gone, it goes straight to the rest.

Helper 1.3. Files: `app/RootHelper/main.c`, `app/Speaka/{MainComponents,ContentView,SpeakaModel}.swift`,
`app/Speaka/Model/SPTweakClient.[hm]`, `tests/test_v98_uninstall.py` (6 tests), and three assertions in
`tests/test_speaka_app.py`.

## After the first test on the phone

- **Settings** is a size smaller: rows 44 pt (were 50) with 12 pt corners, text at 15 pt (Caz's size for Settings
  rows), the switch at 85%, 10 pt between rows. Update notifications has no description any more; a line shows only
  if iOS refused permission, like Kill Bluetooth's line, which shows only if it failed. Both lines are 12 pt.
- **While uninstalling**, the header no longer warns *The tweak isn't installed yet*: removing it is the point. The
  header reads *by Parv*, and neither the connection line nor Install Tweak appears, until Speaka closes. If the
  uninstall fails, the reason shows as before.
- **The hold** no longer sweeps a red fill or says *Keep holding to uninstall*. Picked from ten mockups
  (interpretation 6, *fill and text together*): a gray fill (white at 10%) sweeps across the row from left to right
  over the 4 s, and everything it has covered dims to 45%, a disabled row's level, while the rest stays white. It reads
  *Uninstall* throughout, and letting go runs the edge back. Once the uninstall starts, the whole row keeps that
  disabled look, with the spinner and *Uninstalling...*. One animatable shape draws the fill and cuts the row's two
  copies (white and dimmed), so the three never drift apart.

## Cleanup, step 1: dead code removed

Nothing that runs changed. What went:

- **The pre-libsbc audio chain:** `SBCSink.mm`, `sbc_decode.c`, `BTPSMedia.c`, `BTPSV75RTP.c` and the 100 KB
  `BTPSV75Fixture.c`. Nothing called `SBCSink.mm`; the rest only fed the v7.5 test. Playback has used libsbc in the
  helper since v8.8.
- **The v7.3-v7.6 manual tests:** `BTPSV74Tone.c`, `BTPSIPCProtocol.c`, the probes in `BTPSDaemonProbe.c` (it keeps
  only the loopback connect the audio relay uses), the helper's `--daemon`, `--sbc` and `--sbc-play` modes and
  `BTPSV76Decode.c`. They needed the helper started by hand over SSH, which the package never does.
- **In Settings > Speaka:** the Experiments switches, the debug log's five test buttons (with `BTPSAudioProbe` and
  `BTPSIPCSender`), the log export's sections about those tests, and the controller's Start, Stop, Session and
  output code, unused since v9.7. The page now has Status and Debug log only.
- **Loaded into bluetoothd:** AVFoundation, AudioToolbox and CoreAudio no longer are; only the removed decoder used
  them.
- **Tools and tests:** `decode_rtp_sbc.py`, `triage_log.py` and `sdp_record.py`, the tests for everything above, and
  one fixture. The ffmpeg comparison moved onto the live decoder (`tests/test_v88_runtime.py`), which is stricter than
  before: it now checks the decoder that plays.

The phone's own names didn't change: preference and status keys, `/tmp/FTABHarvest`, the helper's path and port.

## Cleanup, step 2, part one: the tweak and shared files

Nothing that runs changed; the files and names did.

- **The tweak** is in `tweak/`: `Hooks.xm` (was `Tweak.xm`), `BluetoothdBuild.h` (was `BTPSBuild.h`),
  `SinkEndpoint.c` (v7.7, v7.8, v7.9, v8.1 and v8.2, with `BTPSDiagnostics.c`), `StreamControl.c` (v8.8 control),
  `AudioRelay.c` (v8.8 daemon, with the connect function), `Session.c` (v8.9), `Log.c` (with `BTPSCore.c`) and
  `Tweak.h`, which replaces 13 headers. 31 files became 8.
- **Shared** files are in `shared/`: `SpeakaShared.h` (was `BTPSShared.h`) and `AudioRing.c/.h` (was `BTPSV88Shared`).
  The root helper uses `SpeakaShared.h` for the log folder instead of its own copy.
- **Names**: 255 BTPS- and version-tagged names in code have functional ones, for example `BTPSV88Prepare` is
  `AudioRelayPrepare`, `g_v82_armed` is `g_sink_armed` and `BTPS_KEY_V81_SEVEN_ONCE` is
  `SPEAKA_KEY_SEVEN_SLOTS_ONCE`. Every string the phone keeps has its old value: the rename was checked against each
  preference key, notification name, status key, path and the helper's binary name.
- **Removed**: the notification names of the v7.4 and v7.5 probes, and seven status writes nothing read
  (`experiments`, `experiment.sink_register`, `diag.stage`, `diag.v77_sink_trial`, `diag.v82.media`).
- `Hooks.xm` opens with what it does today, not the v2-v7 history.
- `Update-Speaka.ps1` recognises the repository by its `Makefile` and its `control` file's package, since
  `Tweak.xm` moved.

## Cleanup, step 2, part two: the audio helper and the Settings page

Nothing that runs changed; the files and names did.

- **The helper** is in `audiohelper/` (was `btpsaudiohelper/`): `AudioHelper.m` (the stream code with its `main`),
  `SBCDecoder.c/.h` (was `BTPSV88Decode`) and `OutputPolicy.h` (`BTPSV89Output.h` and `BTPSV91Hold.h`, where to play
  and when to let go). Its usage line goes through the timestamped log like every other line. It's still installed
  as `/var/jb/usr/bin/BTPSAudioHelper`, the name launchd starts.
- **Settings** is in `settings/` (was `speakaprefs/`): `SettingsPage.m` and `DebugLog.h/.m`, six files becoming
  three. The classes are `SpeakaSettingsController`, `SpeakaLogViewController` and `SpeakaLogReader`; the bundle's
  `Info.plist` and the PreferenceLoader entry name the first, and changed with it. The bundle is still
  `SpeakaPrefs.bundle`, identifier `com.parv.speakaprefs`.
- **Names**: 54 more, for example `btps_v91_may_start` is `hold_may_start` and `BTPSV88Decoder` is `SBCDecoder`.
- `tools/fetch_google_sbc.sh`, `.gitignore` and the workflow use `audiohelper/vendor_sbc`.

## Cleanup, step 3: the tests

The 25 test files are 6, one per part of Speaka, each in sections titled by what they check:

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_tweak.py` | 68 | the build check, the hooks, the speaker endpoint's helpers |
| `tests/test_audio.py` | 27 | the ring, stream control, the relay, the decoder, playback and output policy |
| `tests/test_start_stop.py` | 12 | Start and Stop |
| `tests/test_settings_page.py` | 11 | Settings > Speaka and saving the log |
| `tests/test_app.py` | 37 | the app, its updater, Uninstall and the root helper |
| `tests/test_build.py` | 11 | the repository layout and the libsbc fetch |

The same 166 tests as before: the merge listed every test before and after and refused to write unless they
matched. Only version-tagged class names changed (`V88Runtime` is `AudioRuntime`, `TrialTest` is `SevenSlots`).
The C harness is `tests/audio_runtime.c` (was `v88_runtime.c`), and the two recordings are
`tests/fixtures/recorded_258_frames.sbc` and its ffmpeg decode, `recorded_258_frames_ffmpeg.wav`.

## On the phone

1. **The menu.** Four rows; Uninstall has a trash icon and no chevron. Compare with Caz side by side: the panel's top
   and bottom, the rows and the separators should line up.
2. **A tap.** Tap Uninstall: the edge barely starts and runs straight back, and nothing else happens.
3. **Let go early.** Hold for about 2 seconds and let go: the fill covers about half the row, with *Unins* dimmed and
   the rest still white, reading *Uninstall* the whole time; then the edge runs back.
4. **The whole thing**, when you're ready to reinstall afterwards. Hold for 4 seconds: a haptic, *Uninstalling...*
   with a spinner. Bluetooth restarts (anything connected drops for a few seconds), then Speaka closes and its icon
   disappears. Then check:
   - Settings: no Speaka page.
   - `dpkg -l | grep speaka` (in a terminal or Filza) shows nothing.
   - `/tmp/FTABHarvest` has no `Speaka*` or `BTPS*` files.
5. **Reinstall** from the latest release's `.tipa` with TrollStore, then tap **Install Tweak** in the app.

If it stops with a message in the header, nothing past that step was removed: screenshot it, then hold again.
