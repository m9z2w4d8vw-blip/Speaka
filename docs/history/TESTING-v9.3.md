# v9.3: Speaka's own background, and updates from the app

Builds on v9.2. Three things change:

| | v9.2 | v9.3 |
| --- | --- | --- |
| Background | Caz's yellow backdrop | Speaka's own again: deep indigo with violet, blue, teal and magenta glows, drawn exactly as the UIKit app drew it |
| Accent | Caz's gold | Speaka's teal (#12B3A6), for the checkmark in Audio Output |
| Updates | By hand | **Update Available** in the app installs the newer tweak and app, then resprings |
| Releases | A rolling nightly pre-release | A normal release for every push to `main`, named after its version |

Everything else from v9.2 stays: Caz's panels, type and spacing, and the one Start/Stop button that opens into a log.
The tweak, the audio helper and Settings > Speaka are unchanged.

## Updates

Once per launch the app reads Speaka's releases from GitHub, and it compares the newest with its own version and with
the tweak's, read from dpkg's database. When either is behind, **Update Available** slides up above the button, in
Caz's place; the connection line moves up a line while it shows. Tapping it opens the changelog, and **Update**:

1. downloads the newer `.deb` and `.tipa`;
2. installs the `.deb` with dpkg and waits. Its install script restarts Bluetooth, as installing it by hand does;
3. installs the `.tipa` with TrollStore, which closes Speaka;
4. resprings, if the tweak was updated.

Steps 3 and 4 run in the update helper's own session, so they finish after Speaka has closed. If dpkg fails, nothing
else happens and the window says why. If only the app is newer, it works like Caz: no respring, and TrollStore's own
link if its installer fails.

Strictly, the tweak needs only the Bluetooth restart, which its install script already does. The respring also closes
Settings and the app, so both open in their new versions.

The app gains one entitlement, `com.apple.private.persona-mgmt`, which lets it start the helper as root, and the helper
has Caz's helper's entitlements.

## Releases and versions

Every push to `main` now publishes `v<version>`, marked latest, with the `.deb`, `Speaka.tipa`, `Speaka.ipa` and
`SHA256SUMS`. The notes are the push's commit messages, and they become the app's changelog, so a clear first line in
each commit message reads well there. The old `nightly` release is deleted on the first such push.

The `.deb` and the app carry the same number: `control`'s `Version`, now `9.3`, plus the run number, for example
`9.3.140` (`tools/release_version.sh`). Your installed tweak is `0.1.0.<run>` from the nightly era, which counts as older,
so the first time, the app offers to update it.

## Device test

1. Push, and wait for the run to finish. On the Releases page, expect `v9.3.<run>` marked **Latest** with all four
   files, and no `nightly`.
2. Install that release's `Speaka.tipa` with TrollStore, by hand this once.
3. Open Speaka. Expect the indigo background with its glows, not yellow. **Credits** should read
   *App 9.3.<run> · tweak 0.1.0.<run>*.
4. **Update Available** should show, for the tweak. Tap it, then **Update**. Expect *Downloading update...*, then
   *Installing update...* with *Bluetooth restarts, then your iPhone resprings*, then a respring.
5. Open Speaka again. **Credits** should read *App 9.3.<run> · tweak 9.3.<run>*, with no Update Available. Settings >
   Speaka should open, and **Start** should work as before.
6. Push any other commit. When its release is out, open Speaka: **Update Available** should show again. **Update**
   should install both, so Speaka closes, then the iPhone resprings. Afterwards both versions are the new number.

If something fails, the window shows the helper's message. The last step also writes to
`/tmp/FTABHarvest/SpeakaUpdate.log`.

## Apply and build

Copy the ZIP's contents over your clone of the repository, keeping the folder paths. Copying doesn't delete files, so
**also delete these from the clone**:

```text
app/Speaka/Assets.xcassets/     (the whole folder: Caz's yellow backdrop, from v9.2)
```

If you're coming straight from v9.1, also delete the UIKit app's files listed in
[TESTING-v9.2](TESTING-v9.2.md#apply-and-build). If anything is left behind, `tests/test_speaka_app.py` names it and fails
CI until it's gone.

These change or are new:

```text
.github/workflows/build.yml
README.md
app/                            (README, Makefile, project.yml, Speaka.entitlements, Resources/Info.plist,
                                 scripts/, Speaka/, and RootHelper/, which is new)
control                         (Version: 9.3)
docs/history/TESTING-v9.3.md    (new)
tests/test_speaka_app.py
tools/release_version.sh        (new)
```

## Validation

Host run: **222 tests: 217 passed, 3 skipped, 1 expected legacy-decoder failure, and 1 failure that needs the libsbc
download** (`test_v88_runtime`, unchanged; CI fetches libsbc). The same under AddressSanitizer and UBSan.

- The update helper compiles on the host, in its device form and in its test mode, with warnings as errors. The test
  mode runs every path on a fake phone's files: both parts, only the tweak, only the app, dpkg failing, TrollStore
  failing, no dpkg, no TrollStore, and files that are links, outside `/var/mobile` or climbing out with `..`. Each
  checks what would run and in what order: dpkg before anything else, the app before the respring, and nothing after a
  failed dpkg.
- The background is checked against v9.1's own numbers: the base colour, the four glows' places, sizes, colours and
  strengths, and drawing 200 pt taller than the screen.
- The version script is run for a push, a tag and a bad tag.

There is no Apple compiler on the host, so the GitHub build is the first real compile of the new Swift. The update
screens and the root spawn are Caz's code, which already compiles with the same Xcode.
