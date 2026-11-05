# v9.2: the app takes Caz's look

Builds on v9.1. Only the app changes: the tweak, the audio helper and Settings > Speaka are the same, so Bluetooth
still reports `diag.v89.build: v9.1.0-one-tap-output-select`. The app becomes version 2.0.

## What changed

The app is now SwiftUI built from Caz's own code, so it looks and moves like Caz.

| | App 1.2 (v9.1) | App 2.0 (v9.2) |
| --- | --- | --- |
| Look | Speaka's violet backdrop and its own panels | Caz's Sunshine Sweep backdrop, panels, type and spacing |
| Start and Stop | Two buttons side by side, each behind a confirmation | One button that reads Start or Stop and opens into a log, like Caz's Clean button |
| Who is connected | A scrolling banner under the header | A line above the button, where Caz shows Update Available |
| Audio output | A list that unfolds inside the menu | An Audio Output window, with Choose Output Device |
| Credits | A card | A window in Caz's style, with the app's and the tweak's versions |
| Build | Theos | Caz's: XcodeGen and `xcodebuild`, signed with `ldid` |

Unchanged: the icon (the same files, byte for byte), the bundle ID `com.parv.speaka`, the two entitlements, and the
Objective-C that talks to the tweak (`SPTweakClient`), which keeps the Settings page's rules.

## The button

| The speaker is | The button reads |
| --- | --- |
| Off, refused, or gone after Bluetooth was turned off and on | **Start** |
| On, starting, or stuck half-started | **Stop**, which restarts Bluetooth back to normal |
| Unreachable: no tweak, or an older one still running in Bluetooth | **Start**, greyed out, with the reason in the header |

There is no confirmation any more, as with Caz's Clean button. Tapping restarts Bluetooth straight away, which briefly
drops AirPods, a watch and other devices; they reconnect by themselves.

The log it opens into:

| Step | While it runs | When it's done | If it fails |
| --- | --- | --- | --- |
| 1 | Restarting Bluetooth | Bluetooth restarted | Bluetooth didn't restart, or Bluetooth wasn't restarted (refused, with the tweak's reason) |
| 2, Start only | Adding the speaker | Speaker added | The speaker wasn't added, or No speaker yet (after 45 s) |
| Last line | | Ready for your PC, or Bluetooth back to normal | |

The timings and the meaning of each outcome are SPTweakClient's, shared with Settings: 25 s for Bluetooth to come back,
45 s in all for the speaker to register.

## Device test

1. Install `Speaka.tipa` from the new nightly with TrollStore. The `.deb` doesn't need reinstalling for this release.
2. Open Speaka. Expect Caz's yellow backdrop, the header (*Speaka*, *iOS 17.0*, *by Parv*), the menu (Audio Output,
   Credits) and the button reading **Start**. The title is in SF Rounded Bold until the Fonarto picture is added.
3. Tap **Start**. The button should open into the log: *Restarting Bluetooth*, then *Bluetooth restarted*, *Adding the
   speaker*, *Speaker added* and *Ready for your PC*, with the title going from *Starting* to *Done*, a light tap per
   line and a buzz at the end. 1.6 s later it folds back, reading **Stop**.
4. Connect the PC and play. The line above the button should read *LAPTOP-... · iPhone speaker*.
5. Open **Audio Output**, switch to Wired headphones and back, then tap outside the window to close it.
6. Tap **Stop**. Expect *Restarting Bluetooth*, *Bluetooth restarted*, then *Bluetooth back to normal*, and the button
   back to **Start**.
7. Start again, then turn Bluetooth off and on. The header should say the speaker was removed, and the button should
   read **Start**.

If anything looks off, a screen recording shows it best, the way the 23:33 one did for v9.1.

## Apply and build

Copy the ZIP's contents over your clone of the repository, keeping the folder paths. Copying doesn't delete files, so
**also delete these from the clone**, all of them from the earlier UIKit app:

```text
app/Speaka/UI/                  (the whole folder)
app/Speaka/main.m
app/Speaka/SPAppDelegate.h
app/Speaka/SPAppDelegate.m
app/Speaka/SPSceneDelegate.h
app/Speaka/SPSceneDelegate.m
```

If one is left behind, `tests/test_speaka_app.py` names it and fails CI until it's gone. The app's project excludes
those paths anyway, so a leftover `main.m` can't clash with SwiftUI's `@main`.

These change or are new:

```text
.github/workflows/build.yml
.gitignore
README.md
app/                            (rewritten: README, Makefile, project.yml, Resources/Info.plist, scripts/, Speaka/)
docs/history/TESTING-v9.2.md    (new)
tests/test_speaka_app.py
```

## Validation

Host run: **219 tests: 214 passed, 3 skipped, 1 expected legacy-decoder failure, and 1 failure that needs the libsbc
download** (`test_v88_runtime`, unchanged; CI fetches libsbc). The same under AddressSanitizer and UBSan.

- The app's tests were rewritten for SwiftUI. Besides the protocol and Settings-page checks kept from before, they
  check the Swift the way a compiler would catch the usual first-build mistakes: balanced code, the right imports,
  every type and model member that's used being defined, and every Objective-C method being called by its pinned
  Swift name. Nine planted mistakes, from a missing import to a second big button, were each caught.
- Caz's numbers are pinned: colours and opacities, the 30 pt margins, the 73 pt rows, the 60 pt button, the sheet's
  top at 26% of the height plus 35 pt, the springs, the 1.6 s fold-back, and Caz's 1290 × 2796 backdrop.

There is no Apple compiler on the host, so the GitHub build is the Swift's first real compile. The shared pieces (the
theme, the menu, the button, the log, the spinner, the dot helix and the window card) are Caz's code, which already
compiles with the same Xcode; the new code is the model, the main screen and the two windows.
