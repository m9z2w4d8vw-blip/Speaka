# Speaka

Speaka turns a jailbroken iPhone into a Bluetooth speaker for a Windows PC. The PC streams its audio to the iPhone over
Bluetooth (A2DP), and the iPhone plays it through its speaker or wired headphones. A companion app starts and stops
the speaker, connects to your computer, and keeps Speaka up to date.

## Features

- **A2DP Sink in iOS's own Bluetooth daemon.** Windows sees the iPhone as a speaker and streams to it.
- **AAC-LC preferred, SBC as the fallback.** The computer picks the codec; Speaka decodes whichever it chose.
- **Plays through the iPhone's speaker or wired headphones**, chosen in **Audio Output**.
- **Connect from either side.** Connect from Windows as with any speaker, or from the iPhone with **Connect to**.
- **Connect to** lists the computers that streamed to Speaka, the paired ones, and those nearby.
- **The connected computer's name and status** show above the main button.
- **Volume in both directions:** Windows' slider changes the iPhone's volume, and the iPhone's buttons move Windows'
  slider (AVRCP absolute volume).
- **A SwiftUI app** with Start and Stop, Connect to, Audio Output, Settings and Uninstall.
- **Updates:** a notification when a new release is out, and one tap to install the tweak and the app together.

## Requirements

| | Tested | Also configured for |
| --- | --- | --- |
| iPhone | iPhone 13 (arm64e), iOS 17.0 | — |
| Jailbreak | Dopamine (rootless), with ElleKit and PreferenceLoader; TrollStore for the app | — |
| Computer | Windows 11 | Any A2DP source using AAC or SBC (untested) |
| Build targets | — | App: iOS 16.0 or later. Tweak and helper: iOS 15.0 or later, arm64 and arm64e |

The deployment targets are what the code compiles for, not what has been tested. The tweak works on exactly one
`bluetoothd` build, the one in iOS 17.0 on the iPhone 13, and refuses to hook any other.

<details>
<summary><b>The <code>bluetoothd</code> build Speaka checks for</b></summary>

<br>

| | |
| --- | --- |
| Size | 10,196,720 bytes, arm64e |
| SHA-256 | `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6` |
| LC_UUID | `5491BA47-B5AE-3D44-9536-B30161DF1E90` |

The tweak hooks functions by address, and addresses only hold for one build. Before hooking, it checks the daemon's
LC_UUID and the first instructions of every function it touches; on any mismatch it logs why and hooks nothing.

</details>

## Install

1. Install **`Speaka.tipa`** from the [latest release](https://github.com/m9z2w4d8vw-blip/Speaka/releases/latest) with
   TrollStore.
2. Open Speaka and tap **Install Tweak**. The app carries the tweak and installs it; there's nothing else to download.

The tweak can also be installed on its own: the release's `Speaka_<version>_iphoneos-arm64.deb`, with Sileo or
`dpkg -i`. The app is still how you start, stop and connect.

## Using Speaka

1. **Open Speaka and tap Start.** Start restarts Bluetooth so the speaker loads; your other Bluetooth devices
   reconnect by themselves. The log under the button ends with *Ready for your PC*.
2. **Pair the computer once,** from Windows: *Settings > Bluetooth & devices > Add device > Bluetooth*, then the iPhone.
3. **Connect.** From Windows, choose the iPhone as the output. From the iPhone, open **Connect to** and tap **Connect**
   on the computer.
4. **Make Speaka the computer's output** if Windows didn't switch to it, and play something.
5. **Change the volume** on either device; the other follows.
6. **Choose where it plays** in **Audio Output**: the iPhone's speaker or wired headphones. It takes effect at once.
7. **Tap Stop** to turn the speaker off. Bluetooth restarts without it.

The speaker stays on until Bluetooth next restarts. After turning Bluetooth off and on, or after a reboot, tap Start
again.

**Settings > Speaka**, in iOS's Settings, shows the same status: whether the tweak is loaded, and the session's state.

The app's **Settings** also has **Kill Bluetooth**, which restarts Bluetooth at once without the speaker (the same as
`killall -9 bluetoothd`), **Update notifications**, **Check for Updates**, and **Credits**, with the installed versions
and licenses.

## Updates and removal

- **Update Available** appears above the main button when a newer release exists. Tapping it installs the tweak and
  the app from that release, then resprings.
- **Update notifications**, in the app's Settings, check every 15 minutes and post a notification for each new
  release. **Check for Updates** checks immediately.
- **To remove Speaka**, hold **Uninstall** in the app for 4 seconds. It removes the tweak, Speaka's files and logs,
  and then the app. Removing the tweak alone: `dpkg -r com.parv.speaka`.

## How it works

| Part | Where | What it does |
| --- | --- | --- |
| Tweak | `tweak/`, loaded into `bluetoothd` | Registers the AAC and SBC Sink endpoints and advertises them, follows the computer's AVDTP signaling, hands the encoded audio to the helper, answers AVRCP volume, records which computer connected, and runs Start and Stop |
| Audio helper | `audiohelper/`, `SpeakaAudioHelper` | Started by launchd (`com.parv.speaka.audio`) as the stream begins: decodes AAC or SBC, plays it through the chosen output, and owns the audio session |
| Shared code | `shared/` | The audio ring between tweak and helper, the AAC transport, the AVRCP volume engine, the Connect to device roster, and the keys both sides use |
| App | `app/` | The SwiftUI app; `app/RootHelper/` installs updates and removes Speaka as root |
| Settings page | `settings/` | Settings > Speaka |
| Update checker | `updatecheck/`, `SpeakaUpdateCheck` | Checks for releases every 15 minutes and notifies as Speaka |

The details are in [docs/](docs/):

- [BLUETOOTHD.md](docs/BLUETOOTHD.md): what the tweak hooks, and how each part was found.
- [CONNECT-TO.md](docs/CONNECT-TO.md): discovery, history, and connecting from the iPhone.
- [AVRCP-VOLUME.md](docs/AVRCP-VOLUME.md): two-way volume.
- [AAC-TESTING.md](docs/AAC-TESTING.md): AAC on the device.
- [CHANGELOG.md](docs/CHANGELOG.md): every release.

## Logs

Everything logs to `/tmp/FTABHarvest/` on the phone:

| File | Written by |
| --- | --- |
| `Speaka.log` | The tweak, in `bluetoothd` |
| `SpeakaAudioHelper.log` | The audio helper |
| `SpeakaConnect.log` | The app's Connect to page |
| `SpeakaUpdateCheck.log` | The update checker |

```powershell
scp mobile@<iphone-ip>:/tmp/FTABHarvest/Speaka.log "$env:USERPROFILE\Downloads\"
```

## Repository layout

```
├── tweak/          the tweak in bluetoothd (Hooks.xm and the Sink, stream, relay, session, peer and volume code)
├── audiohelper/    the audio helper: decoders, output policy and the AAC self-test
├── shared/         code and definitions used by more than one part
├── app/            the SwiftUI app (XcodeGen project.yml), its root helper and packaging scripts
├── settings/       the Settings > Speaka page
├── updatecheck/    the background update checker
├── layout/         installed files: launchd jobs and package scripts
├── tests/          host tests (Python unittest, C harnesses compiled on the host)
├── tools/          build helpers, device tests and analysis scripts
└── docs/           technical documentation, evidence and the reference iOS binaries
```

## Building and testing

GitHub Actions builds every push to `main` and publishes it as a release (`.github/workflows/build.yml`). The release
contains `Speaka.tipa` (TrollStore), `Speaka.ipa`, `Speaka_<version>_iphoneos-arm64.deb` (rootless, arm64 and arm64e)
and `SHA256SUMS`. The version is `control`'s, plus the build's run number.

To build locally:

```sh
tools/fetch_google_sbc.sh                       # Google libsbc, into audiohelper/vendor_sbc/
make package FINALPACKAGE=1                     # the tweak and helper .deb (Theos, rootless)
make -C app                                     # Speaka.tipa (XcodeGen + xcodebuild, on macOS)
python3 -m unittest discover -s tests           # the host tests
SPEAKA_SANITIZE=1 python3 -m unittest discover -s tests   # with AddressSanitizer and UBSan where tests compile C
```

`make package SPEAKA_WITH_SETTINGS=0` builds without the Settings page. Without `libsbc`, the one test that decodes
real SBC fails; CI fetches it.

## Limitations

- **One tested setup:** iPhone 13 on iOS 17.0 with Dopamine, and Windows 11. Other devices, iOS versions and computers
  are untested, and other `bluetoothd` builds are refused.
- **New computers are paired from the computer.** Connect to connects computers that are already paired.
- **Windows decides the output.** After connecting from the iPhone, Windows sets up the audio itself. If it doesn't,
  choose Speaka as its output.
- **Playback is controlled from the computer;** the iPhone has no media controls.
- **The speaker lasts until Bluetooth restarts.** Tap Start again after Bluetooth was turned off and on, or a reboot.

## Credits and licenses

- **Google [libsbc](https://github.com/google/libsbc)** (Apache-2.0) decodes SBC. It's fetched at build time at commit
  `6e505650145c9973d08a0bdd5e5f5e1914305e40`, isn't committed, and its license is installed with the package.
- **The app's interface is adapted from [Dopamine](https://github.com/opa334/Dopamine)** by Lars Fröder (opa334) and
  its contributors, under the MIT License. The license ships in the app, under **Settings > Credits**, and in
  `app/Resources/LICENSE-Dopamine.txt`.
- **The app's title** is a picture of "Speaka" drawn in [Fonarto](https://www.fontspace.com/fonarto-2-font-f104632)
  Regular (`app/scripts/make_wordmark.py`). Fonarto is by Locomotype and free for personal and commercial use; only
  the picture ships, not the font.
- Built with [Theos](https://theos.dev) and [XcodeGen](https://github.com/yonaskolb/XcodeGen); runs on Dopamine with
  ElleKit and PreferenceLoader.
