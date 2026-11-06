# Changelog

Newest first. Since v9.3 every push to `main` is a release, numbered from `control`'s version and the build's run
number (9.8.74 is a push of 9.8), so the versions below are the ones where something changed. Each release's full
notes, with its device-test steps, are in the git history (`docs/history/`, before v9.8's cleanup). How each part of
`bluetoothd` was found is in [BLUETOOTHD.md](BLUETOOTHD.md).

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
