# Speaka app

The app for the Speaka tweak, in Caz's design: the same backdrop, panels, type, spacing and animations, built from
Caz's own SwiftUI code. It offers:

- **one button** that starts or stops the speaker and opens into a log while Bluetooth restarts, like Caz's Clean
  button;
- an **Audio Output** window with the three outputs, and iOS's own output picker for Bluetooth earbuds;
- a **connection line** above the button that says who is connected and where the audio plays;
- **Credits**, with the app's and the tweak's versions and the license of the interface.

It is a TrollStore app, published as `Speaka.tipa` and `Speaka.ipa` (the same file), and built separately from the
tweak's `.deb`. The icon is still Speaka's own (`Artwork/make_icon.py`); `tests/test_speaka_app.py` checks that it
hasn't changed.

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

The connection line sits where Caz shows Update Available, 20 pt above the button, and slides up as it fades in. It is
hidden while the speaker is off and while the log is open.

## How it talks to the tweak

Exactly like Settings > Speaka, through the `com.apple.bluetooth` preferences and the Darwin notifications in
[`BTPSShared.h`](../BTPSShared.h). This needs two entitlements, `platform-application` and
`com.apple.private.security.no-sandbox`, and nothing else.

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

This is how Caz is built. XcodeGen generates `Speaka.xcodeproj` from `project.yml`, `xcodebuild` compiles it, and
`scripts/package.sh` signs it with `Speaka.entitlements` using `ldid` and zips it. You need macOS with Xcode, plus
`brew install xcodegen ldid`.

CI builds it in its own job, **Build the Speaka app**, and uploads both files as the `Speaka-app` artifact. The
release attaches them next to the tweak's `.deb`. If the app fails to build, the release goes out without it rather
than not at all.

There is no Apple compiler on the host, so `tests/test_speaka_app.py` checks the Swift before CI does: balanced code,
imports, every type and model member that is used being defined, and every Objective-C method being called by its
pinned Swift name.

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
| `Speaka/Theme.swift` | `Theme.swift`, code unchanged | Colours, metrics, the backdrop and the title's font |
| `Speaka/Windows.swift` | `Windows.swift` (the window card) | The Audio Output and Credits windows |
| `Speaka/SpeakaModel.swift` | after `CleanerModel.swift` | State, polling, and Start or Stop as log lines |
| `Speaka/Model/SPTweakClient`, `SPConnectedDevice` | | The tweak's protocol and the PC's name, in Objective-C |
| `Speaka/Assets.xcassets` | `Backdrop.imageset` | Caz's Sunshine Sweep backdrop |
| `Resources/` | | `Info.plist`, the icons (unchanged) and `LICENSE-Dopamine.txt` |
| `Artwork/` | | The 1024 px icon and `make_icon.py`, which draws it |
| `project.yml`, `Makefile`, `scripts/package.sh` | `project.yml`, `scripts/package.sh` | The build |
| `scripts/make_wordmark.py` | `scripts/make_wordmark.py` | Draws the title |

Caz's interface is adapted from [Dopamine](https://github.com/opa334/Dopamine) by Lars Fröder (opa334) and its
contributors, under the MIT License. The license ships in the app (`Resources/LICENSE-Dopamine.txt`, shown under
**Credits**), and every adapted file names its origin in its header.
