# Speaka app

The app for the Speaka tweak, in Caz's design: the same panels, type, spacing and animations, built from Caz's own
SwiftUI code, on Speaka's own background. It offers:

- **one button** that starts or stops the speaker and opens into a log while Bluetooth restarts, like Caz's Clean
  button;
- an **Audio Output** window with the three outputs, and iOS's own output picker for Bluetooth earbuds;
- a **connection line** above the button that says who is connected and where the audio plays;
- **Credits**, with the app's and the tweak's versions and the license of the interface;
- **Update Available**, Caz's updater, which installs a newer release's tweak and app and then resprings.

It is a TrollStore app, published as `Speaka.tipa` and `Speaka.ipa` (the same file), and built separately from the
tweak's `.deb`. The icon and the background are still Speaka's own (`Artwork/make_icon.py`, and `SpeakaBackground` in
`Theme.swift`, which draws the background exactly as the earlier UIKit app did); `tests/test_speaka_app.py` checks
that neither has changed.

## The button

It follows the tweak's state, with the same rules as Settings > Speaka:

| The speaker is | The button reads | Tapping it |
| --- | --- | --- |
| Off, refused, or gone after Bluetooth was turned off and on | **Start** | Restarts Bluetooth with the speaker |
| On, starting, or stuck half-started | **Stop** | Restarts Bluetooth without it, back to normal |
| Unreachable: no tweak, or an older one still running in Bluetooth | **Start**, greyed out | Nothing; the header says why |

There is no confirmation, as with Caz's Clean button: the button's word says what it will do. Tapping it opens the
button into the log (spring 0.75 s, damping 0.9) while the menu shrinks away and the header rises 25 pt:

| Step | While it runs | When it's done |
| --- | --- | --- |
| 1 | Restarting Bluetooth | Bluetooth restarted |
| 2, Start only | Adding the speaker | Speaker added |
| Last line | | Ready for your PC, or Bluetooth back to normal |

The title beside the dot helix reads *Starting* or *Stopping*, then *Done*, or *Stopped* if a step fails. Each finished
line gives a light tap and the last one a success buzz. 1.6 s later the log folds back into the button, which now reads
the other word.

A step that fails says what went wrong (*Bluetooth didn't restart*, *The speaker wasn't added*), and the header keeps the
reason in Caz's warning colour until the speaker's state changes.

## The screen

| Part | Shows |
| --- | --- |
| Header | *Speaka*, *iOS 17.0*, then *by Parv*, or what needs your attention in the warning colour |
| Audio Output | A window with iPhone speaker, Wired headphones or earbuds, and Any device; a gold checkmark on the one in use; then Choose Output Device |
| Credits | A window with the app's and the tweak's versions and Dopamine's license |
| Connection line | *LAPTOP-XXXX · iPhone speaker*, *Waiting: no wired headphones plugged in*, or *Connect from your PC* |
| The button | Start or Stop, as above |

Update Available sits where Caz has it, 20 pt above the button, and slides up 25 pt as it fades in. The connection line
takes the same place, or the line above it, 10 pt higher, while Update Available is showing. Both hide while the log is
open, and the connection line also while the speaker is off.

## Updates

Once per launch the app reads Speaka's releases from GitHub, ignoring drafts and pre-releases (so the old rolling nightly
too). A release carries the tweak's `.deb` and the app's `.tipa` with the same version number. The app compares it with
its own version and with the tweak's, which it reads from dpkg's database (`/var/jb/var/lib/dpkg/status`).

**Update Available** shows when either is behind. The changelog lists every newer release's notes, which are its commit
messages, and **Update** then:

1. downloads the `.deb`, then the `.tipa`, whichever are newer, behind Caz's progress ring;
2. hands both to the update helper, `speakahelper`, which runs as root (TrollStore's persona spawn, as in Caz);
3. the helper installs the `.deb` with dpkg and waits. Its install script restarts Bluetooth. If dpkg fails, the
   window says so and nothing else happens;
4. then, in its own session so it outlives the app, the helper installs the `.tipa` with TrollStore's installer, which
   closes Speaka, and resprings.

With only a newer app, it works exactly like Caz: TrollStore's installer, no respring, and if that fails, TrollStore's
own link (`apple-magnifier://install?url=`), then the releases page.

| Helper exit code | Meaning |
| --- | --- |
| 0 | Done; the last step is running |
| 3 | Not running as root (the app wasn't installed with TrollStore) |
| 4 | A downloaded file is missing or outside `/var/mobile` |
| 5, 6, 7 | TrollStore's installer wasn't found, couldn't start, or failed |
| 8, 9 | dpkg failed, or wasn't found at `/var/jb/usr/bin/dpkg` |
| 10 | The last step couldn't be started |

The last step writes to `/tmp/FTABHarvest/SpeakaUpdate.log`.

## How it talks to the tweak

Exactly like Settings > Speaka, through the `com.apple.bluetooth` preferences and the Darwin notifications in
[`BTPSShared.h`](../BTPSShared.h). This needs two entitlements, `platform-application` and
`com.apple.private.security.no-sandbox`. The third, `com.apple.private.persona-mgmt`, lets the app start its update
helper as root. The helper has Caz's helper's entitlements (`RootHelper/entitlements.plist`).

The protocol and its rules are Objective-C, in `Speaka/Model/SPTweakClient`, unchanged from the earlier UIKit app, so
they stay in step with the Settings page. The SwiftUI screens call it through `Speaka/Speaka-Bridging-Header.h`. Its
header spells out each method's Swift name with `NS_SWIFT_NAME`, because Swift would otherwise import `pollRequest:` as
`poll(_:)`.

| Action | What the app does |
| --- | --- |
| Start / Stop | Writes `{action, nonce, time}` to `SpeakaV89SessionRequest`, posts `com.parv.speaka.v89.session`, then follows `diag.v89.session` until the tweak answers that nonce |
| Status | Reads `SpeakaStatus` and `SpeakaV89HelperStatus` every 0.5 s, with the same rules as the Settings page |
| Audio output | Writes `SpeakaV89OutputMode`, then posts `com.parv.speaka.v89.output`; the helper switches mid-song |
| PC name | Best effort, from the private BluetoothManager framework; otherwise the line says "Your PC" |

## Build

```sh
make -C app tipa      # app/packages/Speaka.tipa and Speaka.ipa (the same file)
```

This is how Caz is built. XcodeGen generates `Speaka.xcodeproj` from `project.yml`, `xcodebuild` compiles it, clang
builds the update helper from `RootHelper/main.c`, and `scripts/package.sh` puts the helper in the app, signs both
with their entitlements using `ldid`, and zips them. You need macOS with Xcode, plus `brew install xcodegen ldid`.
The version is the release's, which CI passes as `VERSION`; a local build uses `control`'s.

CI builds it in its own job, **Build the Speaka app**, and uploads both files as the `Speaka-app` artifact. The
release attaches them next to the tweak's `.deb`. If the app fails to build, the release goes out without it rather
than not at all.

There is no Apple compiler on the host, so `tests/test_speaka_app.py` checks the Swift before CI does: balanced code,
imports, every type and model member that is used being defined, and every Objective-C method being called by its
pinned Swift name. The helper does compile on the host: the tests build it in a test mode and run every update path on
a fake phone's files, checking what it would run and in what order.

## The title

Caz's title is a picture of the word drawn in Fonarto, not live text. Speaka's works the same way:

```sh
python3 app/scripts/make_wordmark.py --font /path/to/FonartoRegular.ttf
```

This draws "Speaka" into `Speaka/Assets.xcassets/Wordmark.imageset`. Until that picture exists, the app writes
"Speaka" in SF Rounded Bold in the same place. [Fonarto](https://www.fontspace.com/fonarto-2-font-f104632) is by
Locomotype (Arwan Sutanto) and is free for personal and commercial use. Only the picture is shipped, not the font.

## Files

| File | From Caz | Role |
| --- | --- | --- |
| `Speaka/SpeakaApp.swift` | `CazApp.swift` | App entry point |
| `Speaka/ContentView.swift` | `ContentView.swift` | The main screen: header, menu, the button and the log it opens into |
| `Speaka/MainComponents.swift` | `MainComponents.swift` | Header, title, menu rows, the button, the connection line, the log, spinner and dot helix |
| `Speaka/Theme.swift` | `Theme.swift` | Colours and metrics (Caz's, with Speaka's teal), Speaka's background, the title's font |
| `Speaka/Windows.swift` | `Windows.swift` (the window card) | The Audio Output and Credits windows |
| `Speaka/SpeakaModel.swift` | after `CleanerModel.swift` | State, polling, and Start or Stop as log lines |
| `Speaka/SpeakaUpdater.swift` | `UpdateModel.swift` | Releases, versions, downloads and the hand-off to the helper |
| `Speaka/UpdateViews.swift` | `UpdateViews.swift` | Update Available, the changelog, the progress ring |
| `Speaka/Model/SPTweakClient`, `SPConnectedDevice` | | The tweak's protocol and the PC's name, in Objective-C |
| `Speaka/Model/SPRootSpawn` | `RootSpawn` | Starts the update helper as root |
| `RootHelper/` | `RootHelper/` (much smaller) | `speakahelper`: dpkg, TrollStore's installer and the respring |
| `Resources/` | | `Info.plist`, the icons (unchanged) and `LICENSE-Dopamine.txt` |
| `Artwork/` | | The 1024 px icon and `make_icon.py`, which draws it |
| `project.yml`, `Makefile`, `scripts/package.sh` | `project.yml`, `scripts/package.sh` | The build |
| `scripts/make_wordmark.py` | `scripts/make_wordmark.py` | Draws the title |

Caz's interface is adapted from [Dopamine](https://github.com/opa334/Dopamine) by Lars Fröder (opa334) and its
contributors, under the MIT License. The license ships in the app (`Resources/LICENSE-Dopamine.txt`, shown under
**Credits**), and every adapted file names its origin in its header.
