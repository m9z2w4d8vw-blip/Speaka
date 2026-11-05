# Speaka app

The app for the Speaka tweak, in Caz's design: the same panels, type, spacing and animations, built from Caz's own
SwiftUI code, on Speaka's own background. It offers:

- **one button** that starts or stops the speaker and opens into a log while Bluetooth restarts, like Caz's Clean
  button;
- a **Settings** window with **Kill Bluetooth** (`killall -9 bluetoothd`, through the root helper) and **Update
  notifications**;
- an **Audio Output** window with the three outputs, and iOS's own output picker for Bluetooth earbuds;
- a **connection line** above the button that says who is connected and where the audio plays;
- **Uninstall**: held for 4 seconds, it removes the tweak, Speaka's leftovers and the app, with no second prompt;
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

Each step is read from a status the tweak publishes from inside `bluetoothd` (`Speaka/SessionSteps.swift`), in the order
the tweak goes through them:

| Step | While it runs | When it's done | If it fails | Read from |
| --- | --- | --- | --- | --- |
| 1 | Restarting Bluetooth | Bluetooth restarted | Bluetooth didn't restart | the request's nonce in `diag.v89.session` |
| 2, Start | Arming the speaker | Speaker armed | Speaker not armed | `diag.v82.trial` |
| 3, Start | Installing the hooks | Hooks installed | A hook wasn't installed | each hook's own status |
| 4, Start | Registering Bluetooth's endpoints | Bluetooth's endpoints registered | Bluetooth's endpoints failed | `diag.native_source_ok` (6) |
| 5, Start | Making room for the speaker | Room made for the speaker | No room for the speaker | `diag.v81_seven_slots` (7 slots) |
| 6, Start | Preparing the audio path | Audio path ready | Audio path not ready | `diag.audio_receive` |
| 7, Start | Adding the speaker | Speaker added | Speaker not added | `diag.v82.sep` (SEID 7) |
| 8, Start | Advertising the speaker | Speaker advertised | Speaker not advertised | `diag.v82.sdp` |
| 2, Stop | Switching the speaker off | Speaker switched off | Speaker still on | `result=` in `diag.v89.session` |
| Last line | | Ready for your PC, or Bluetooth back to normal | | |

Bluetooth arms the speaker and installs the hooks as it launches, so steps 2 and 3 finish right after the restart;
steps 4 to 8 follow once Bluetooth is powered on and registers its endpoints. The log moves on one step per poll
(0.5 s), so it reads one line at a time even when Bluetooth reports several at once. The whole request has 45 s, as in
Settings > Speaka; a step still waiting then fails with its own message (step 4's says to check that Bluetooth is on).

The log's column is as wide as the widest line a Start or a Stop could show, the same for both and fixed for the
app's life, so lines never shift sideways when a longer one appears or a running line (20 pt) finishes (18 pt), and a
line sits in the same place in a Start's log as in a Stop's.

The title beside the dot helix reads *Starting* or *Stopping*, then *Done*, or *Stopped* if a step fails. Each finished
line gives a light tap and the last one a success buzz. 1.6 s later the log folds back into the button, which now reads
the other word. After a failure the log stays up for 6 s, so you can see which step it was, and the header keeps the
step and the tweak's reason in Caz's warning colour until the speaker's state changes, for example
*Speaker not added: ROLLED BACK: Sink registration returned 0x68; 6 entries restored*.

## The screen

| Part | Shows |
| --- | --- |
| Header | *Speaka*, *iOS 17.0*, then *by Parv*, or what needs your attention in the warning colour |
| Settings | **Kill Bluetooth**, just the button: `speakahelper kill-bluetooth` kills bluetoothd as `killall -9` does, and launchd starts it again without the speaker. Greyed out during a Start or Stop; a line shows only if it failed. Then **Update notifications**, a switch, with no description; a line shows only if iOS refused permission, saying where to allow it. Rows 44 pt with 15 pt text and the switch at 85% |
| Audio Output | A window with iPhone speaker, Wired headphones or earbuds, and Any device; a gold checkmark on the one in use; then Choose Output Device |
| Uninstall | An action row without a chevron, like Caz's Scan. A tap does nothing; held, a gray fill (white at 10%) sweeps from the left edge to the right over 4 seconds, and everything it has covered dims to 45%, a disabled row's level, while the rest stays white; the title stays *Uninstall*, and letting go runs the edge back. One animatable shape (`HoldPortion`) draws the fill and cuts the row's two copies, white and dimmed, so they move together. At 4 seconds, a warning haptic; the whole row keeps the disabled look with a spinner and *Uninstalling...*, and every other row and the button lock. Until Speaka closes, the header shows no warning, and neither the connection line nor Install Tweak appears. `speakahelper remove-tweak` runs `dpkg -r com.parv.speaka`, kills bluetoothd so the tweak unloads, closes Settings, and deletes Speaka's files in `/tmp/FTABHarvest` (only names starting with *Speaka* or *BTPS*) and its downloads folder. Then the app removes every *Speaka* key from Bluetooth's preferences and its own settings, and `speakahelper uninstall-app` starts `finish-uninstall` in its own session, which runs `trollstorehelper uninstall com.parv.speaka`. If removing the tweak fails, nothing else is removed and the header shows dpkg's reason |
| Credits | A window with the app's and the tweak's versions, a line on what iOS's Bluetooth list reports, and Dopamine's license |
| Connection line | *LAPTOP-XXXX · connected* once the PC sets up the stream and while it's paused, *LAPTOP-XXXX · iPhone speaker* while it plays, *Waiting: no wired headphones plugged in*, or *Connect from your PC* while nothing is connected. How the app knows it's connected, and the PC's name, are under [How it talks to the tweak](#how-it-talks-to-the-tweak) |
| The button | Start or Stop, as above |

Update Available sits where Caz has it, 20 pt above the button, and slides up 25 pt as it fades in. The connection line
takes the same place, or the line above it, 10 pt higher, while Update Available is showing. Both hide while the log is
open, and the connection line also while the speaker is off.

## Updates

Once per launch the app reads Speaka's releases from GitHub, ignoring drafts and pre-releases (so the old rolling nightly
too). A release carries the tweak's `.deb` and the app's `.tipa` with the same version number. The app compares it with
its own version and with the tweak's, which it reads from dpkg's database (`/var/jb/var/lib/dpkg/status`).

**Install Tweak** (v9.5) takes Update Available's place while dpkg doesn't list the tweak, the app carries one
(`Speaka.app/Speaka.deb`, put there by `scripts/package.sh` from CI's `Speaka-deb` artifact; the same version as the
app), and `/var/jb/usr/bin/dpkg` exists. It runs `speakahelper install-update --tweak-from-app`: the helper finds the
installed Speaka.app by its bundle id, copies its `Speaka.deb` into a folder only root can write, installs it with dpkg
(which restarts Bluetooth), and resprings. With the tweak installed, of whatever version, the button doesn't show.

**Update Available** shows when either is behind. The changelog lists every newer release's notes, which are its commit
messages, and **Update** then:

1. if the app is behind, downloads only the new `.tipa` behind Caz's progress ring and saves it in
   `/var/mobile/Library/Caches/Speaka`, or `/tmp/FTABHarvest` if that can't be written. Each file is fetched into
   memory and written out by the app: a download task's temporary file goes inside the app's own data folder, which an
   unsandboxed TrollStore app can't write without `com.apple.private.security.storage.AppDataContainers`, and that
   entitlement would also move the app's view of `com.apple.bluetooth`'s preferences, which the tweak reads, into that
   folder. A failure names the step, the file and the error's domain and code;
2. hands it to the update helper, `speakahelper`, which runs as root (TrollStore's persona spawn, as in Caz), with
   `--tweak-from-app` if the tweak is behind too;
3. in its own session so it outlives the app, the helper installs the `.tipa` with TrollStore's installer, which closes
   Speaka, then the tweak carried by the app it just installed, then resprings;
4. with only the tweak behind, the helper installs the tweak this app already carries, which is at least as new as the
   release's, with nothing to download. A build that carries no tweak downloads the release's `.deb` instead.

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

## Update notifications

**Settings > Update notifications** (v9.7) asks iOS for permission to notify when it's turned on, and stays off if
that's refused. Then:

- **While Speaka is open**, `SpeakaUpdater.watch` runs the release check again every 2 minutes, so Update Available
  appears within 2 minutes of a release.
- **In the background**, Speaka asks iOS to wake it in 2 minutes (`BGAppRefreshTaskRequest`, task
  `speaka.update-check`, listed in `Info.plist` with the `fetch` background mode), and each wake-up asks for the next.
  iOS decides when that actually happens, often much less often than every 2 minutes, and never once Speaka has been
  swiped away from the app switcher.
- **A release counts** once it carries `Speaka.tipa` and is newer than the app. Each is announced once: the last one
  announced is kept in the app's own preferences (through `SPTweakClient`, never `com.apple.bluetooth`'s).

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
| Connected | While the PC streams (the helper plays or waits), while it's paused (the helper idles, still attached), or from when it sets the stream up, by the tweak's `diag.v95.peer` (*configured*, *streaming*, *paused*, *closed*), until it closes the stream or the helper's session ends. A link that drops without closing leaves the last state until bluetoothd restarts |
| PC name | iOS publishes the streaming PC as a Bluetooth audio device, and while the speaker is idle routes the phone's audio to it, so its name is a Bluetooth output in the helper's route, or in the app's own; a Windows *DESKTOP-* or *LAPTOP-* name wins, and the app keeps it while the PC stays connected. Before that, the private BluetoothManager framework; otherwise "Your PC" |

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

Caz's title is a picture of the word drawn in Fonarto Regular, not live text, and Speaka's is too, in
`Speaka/Assets.xcassets/Wordmark.imageset`. It was drawn with the same script and the same font file as Caz's: redrawing
"Caz" with them reproduces Caz's title pixel for pixel. To redraw it:

```sh
python3 app/scripts/make_wordmark.py --font /path/to/FonartoRegular.ttf
```

Dopamine's proportions, which both titles use, leave room for Dopamine's own "p" but not quite for Fonarto Regular's:
at Caz's size, "Speaka" would run 1.75 px past the bottom of the 120 px picture. So the script draws a word that doesn't
fit about 1.5% smaller, a pixel clear of the top and bottom, and draws words that fit, like "Caz", exactly as before.

If the picture is ever missing, the app writes "Speaka" in SF Rounded Bold in the same place.
[Fonarto](https://www.fontspace.com/fonarto-2-font-f104632) is by Locomotype (Arwan Sutanto) and is free for personal
and commercial use. Only the picture is shipped, not the font.

## Files

| File | From Caz | Role |
| --- | --- | --- |
| `Speaka/SpeakaApp.swift` | `CazApp.swift` | App entry point |
| `Speaka/ContentView.swift` | `ContentView.swift` | The main screen: header, menu, the button and the log it opens into |
| `Speaka/MainComponents.swift` | `MainComponents.swift` | Header, title, menu rows, the button, the connection line, the log, spinner and dot helix |
| `Speaka/Theme.swift` | `Theme.swift` | Colours and metrics (Caz's, with Speaka's teal), Speaka's background, the title's font |
| `Speaka/Windows.swift` | `Windows.swift` (the window card) | The Audio Output and Credits windows |
| `Speaka/SpeakaModel.swift` | after `CleanerModel.swift` | State, polling, and Start or Stop as log lines |
| `Speaka/SessionSteps.swift` | | The steps after Bluetooth restarts, each read from a status the tweak publishes |
| `Speaka/UpdateNotifications.swift` | | Settings > Update notifications: the check, iOS's background wake-up, the notification |
| `Speaka/SpeakaUpdater.swift` | `UpdateModel.swift` | Releases, versions, downloads and the hand-off to the helper |
| `Speaka/UpdateViews.swift` | `UpdateViews.swift` | Update Available, the changelog, the progress ring |
| `Speaka/Model/SPTweakClient`, `SPConnectedDevice` | | The tweak's protocol and the PC's name, in Objective-C |
| `Speaka/Model/SPRootSpawn` | `RootSpawn` | Starts the update helper as root |
| `RootHelper/` | `RootHelper/` (much smaller) | `speakahelper`: dpkg (a downloaded tweak, or the one the app carries), TrollStore's installer and the respring |
| `Resources/` | | `Info.plist`, the icons (unchanged) and `LICENSE-Dopamine.txt` |
| `Artwork/` | | The 1024 px icon and `make_icon.py`, which draws it |
| `project.yml`, `Makefile`, `scripts/package.sh` | `project.yml`, `scripts/package.sh` | The build |
| `scripts/make_wordmark.py` | `scripts/make_wordmark.py` | Draws the title |

Caz's interface is adapted from [Dopamine](https://github.com/opa334/Dopamine) by Lars Fröder (opa334) and its
contributors, under the MIT License. The license ships in the app (`Resources/LICENSE-Dopamine.txt`, shown under
**Credits**), and every adapted file names its origin in its header.
