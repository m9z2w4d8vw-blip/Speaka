# Speaka v9.5: one .tipa installs the app and the tweak

Until now a release had two files to install: the tweak's `.deb` with a package manager, then `Speaka.tipa` with
TrollStore. From v9.5, `Speaka.tipa` carries the tweak as well, so it's the only file anyone needs.

## What changed

| | v9.4 | v9.5 |
| --- | --- | --- |
| A first install | The `.deb` with Sileo or dpkg, then the `.tipa` with TrollStore | The `.tipa` with TrollStore, then **Install Tweak** in the app |
| The app without the tweak | *The Speaka tweak isn't running. Install its package* | *The tweak isn't installed yet. Tap Install Tweak.*, with **Install Tweak** where Update Available sits |
| The app with the tweak | As before | As before, whatever the tweak's version |
| Updating both | Download the `.deb` and the `.tipa`; dpkg, then TrollStore, then a respring | Download the `.tipa` only; TrollStore, then the tweak inside the new app, then a respring |
| Updating only the tweak | Download the `.deb` | Install the tweak this app already carries; nothing to download |
| A release | `.deb`, `.tipa`, `.ipa`, `SHA256SUMS` | The same; the `.tipa` and `.ipa` now carry the `.deb` |

**Install Tweak** shows only while dpkg doesn't list the tweak, the app carries one, and the jailbreak's dpkg exists.
Without a jailbreak, the header says Speaka's tweak needs one instead. If the jailbreak isn't active (after a reboot,
before running it again), dpkg can't start, and the window says to run the jailbreak again.

## How it works

- **CI** builds the app after the tweak now, from the tweak's own `Speaka-deb` artifact, and `scripts/package.sh`
  copies it into the app as `Speaka.app/Speaka.deb` before signing. The app job then checks the app carries exactly
  that file, and that its name has the release's version. So the carried tweak is always the app's own version.
- **The update helper** has one new option, `--tweak-from-app`. It finds the installed Speaka.app through its bundle
  container, the same way it finds TrollStore's installer, copies `Speaka.deb` from it into a new folder under
  `/var/tmp` that only root can write, and installs that copy with dpkg. The app's own copy is never touched: only
  TrollStore writes inside an app's bundle, and a link there is refused.
  - `install-update --tweak-from-app`: Install Tweak, or only the tweak behind. dpkg, then the respring.
  - `install-update --tipa <file> --tweak-from-app`: both behind. Everything happens in the last step, in its own
    session so it outlives Speaka: TrollStore's installer, then the tweak from the app it just installed, then the
    respring. If TrollStore's installer fails, no tweak is installed, and the respring still resets the screen.
- **The app's updater** downloads only the `.tipa` when the app is behind, and passes `--tweak-from-app` when the tweak
  is behind too. With only the tweak behind, it installs the one this app carries, which is at least as new as the
  release's. A build that carries no tweak (a local one without `DEB`) downloads the `.deb`, as before.

The standalone `.deb` stays in every release, for installing by hand, and so that v9.4's updater, which downloads the
`.deb` and the `.tipa`, can update itself into v9.5.

Deleting the app doesn't remove the tweak yet. That's the watcher we discussed, which goes into the `.deb`'s install
scripts, so it comes in a later release, once this one has worked on the phone.

## Apply and build

Copy the zip's contents over your clone (or let `Update-Speaka.ps1` do it). Nothing needs deleting. The changed files:

```text
control                          (Version: 9.5)
.github/workflows/build.yml      (the app job: after the tweak, carrying its .deb)
app/project.yml                  (MARKETING_VERSION: 9.5)
app/Makefile, app/scripts/package.sh
app/RootHelper/main.c            (--tweak-from-app; a clearer message when dpkg can't start)
app/Speaka/SpeakaUpdater.swift, UpdateViews.swift, ContentView.swift, SpeakaModel.swift
tests/test_speaka_app.py
README.md, app/README.md, docs/history/TESTING-v9.5.md
```

## On the phone

1. Push, and wait for the run. Expect release `v9.5.<run>` marked Latest with the `.deb`, `Speaka.tipa`, `Speaka.ipa`
   and `SHA256SUMS`, and in the app job's log, *Carries the tweak: Speaka_9.5.<run>_iphoneos-arm64.deb*.
2. In the v9.4 app, tap **Update Available**, then **Update**. This is still v9.4's updater, so it downloads both files,
   installs the tweak and then the app, and resprings. **Credits** should then read *App 9.5.<run> · tweak 9.5.<run>*.
3. **Install Tweak.** Remove the tweak with Sileo (or `sudo apt remove com.parv.speaka`), then open Speaka. The header
   should say *The tweak isn't installed yet. Tap Install Tweak.*, and **Install Tweak** should sit above the button.
   Tap it: *Installing the tweak...*, Bluetooth restarts, the iPhone resprings, and Credits shows the tweak again.
4. **A one-download update.** On the next release after this one, tap **Update Available**, then **Update**. It should
   download only the app, then TrollStore installs it, the tweak inside it is installed, and the iPhone resprings, with
   both versions in Credits matching the new release.
5. If a step fails after Speaka has closed, the last step's output is in `/tmp/FTABHarvest/SpeakaUpdate.log`.

## Host run

The new and changed tests check that the app carries the release's `.deb` (packaging, Makefile, workflow), that Install
Tweak shows only while the tweak is missing, the new download order, and the helper's new flows on a fake phone:
Install Tweak from a private copy that's then removed, the app's own copy left alone, the last step's order
(TrollStore, then dpkg, then the respring), no tweak when TrollStore's installer fails, and a missing or linked carried
tweak refused. As before, the Swift couldn't be compiled here; the helper was, both ways, with warnings as errors.
