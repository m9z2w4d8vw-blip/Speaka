# Speaka v9.4: updates that install, and every step of Start

Two reports from the first device test of v9.3: Update Available showed, but **Update** failed with
*The operation couldn't be completed. Operation not permitted*. And Start's log said only *Adding the speaker*, so a
failure couldn't say which step it was.

## Why the update failed

That message is iOS's text for a raw POSIX error (EPERM), not one of the update helper's messages, which all say what
they were installing. So it came from the app's download step, before the helper ever ran. That step used a download
task, which first writes a temporary file inside the app's own data folder, and then moved the file into the app's
caches folder, which is in the same place. An unsandboxed TrollStore app can't write its data folder without
`com.apple.private.security.storage.AppDataContainers`. Caz has that entitlement; Speaka doesn't, and shouldn't: it would
also move the app's view of `com.apple.bluetooth`'s preferences, which is how the app talks to the tweak, into that
folder.

This is worked out from the message and the code, not watched on the phone, so v9.4 removes every step that could have
failed that way:

| | v9.3 | v9.4 |
| --- | --- | --- |
| Download | A download task, with a temporary file in the app's data folder | A data task into memory (the files are under 1 MB), no cache |
| Saved to | The app's caches folder, inside its data folder | `/var/mobile/Library/Caches/Speaka`, or the tweak's `/tmp/FTABHarvest` if that can't be written |
| Update helper takes files from | `/var/mobile` | `/var/mobile` and `/tmp/FTABHarvest`, still regular files with the right extension and no `..` |
| A failure says | The error's text | The step and the file (*Downloading the tweak*, *Saving the app*), the error's text, domain and code, and for saving every folder tried |

While it downloads, the window says which file: *Downloading the tweak, 1 of 2*.

## Every step of Start

After Bluetooth restarts, each step is read from a status the tweak publishes from inside `bluetoothd`
(`app/Speaka/SessionSteps.swift`):

1. Restarting Bluetooth
2. Arming the speaker
3. Installing the hooks
4. Registering Bluetooth's endpoints
5. Making room for the speaker
6. Preparing the audio path
7. Adding the speaker
8. Advertising the speaker
9. Ready for your PC

Stop has *Restarting Bluetooth*, *Switching the speaker off*, then *Bluetooth back to normal*. The full table, with
what each step reads, is in [app/README.md](../../app/README.md#the-button).

A step that fails keeps its line with the failed mark, for example *Speaker not added*. The log stays up for 6 s instead
of 1.6 s, and the header keeps the step and the tweak's own reason afterwards. A step still waiting when the 45 s run out
fails with its own message; step 4's says to check that Bluetooth is on.

## What changed in the tweak

The steps need the tweak to report each one, and promptly, so it changed in two small ways:

- **Statuses reach the app within a second.** `BTPSSetStatus` and `BTPSLogNow` marked the status changed but never
  scheduled the copy to the preferences the app reads, so a step could wait for an unrelated log line or for the 60 s
  heartbeat. Both now schedule it, coalesced to once a second. Only setup and connection events change statuses; the
  audio path never does, so playback adds no writes.
- **Every refusal on the way to the speaker sets a status.** Several only wrote the debug log, which left the app
  waiting with no reason: the seven-slot check before the audio callback, the audio callback's own checks, a null
  audio context, the audio table's counts and a failed allocation. They now set `REFUSED: …`. A missing prerequisite
  before the one attempt is used sets `WAITING: …` instead, because nothing is used up and the next Bluetooth power-on
  tries again, so neither the app nor Settings counts it as a failure.

No check changed, and nothing else in the tweak did. Its build tag is `v9.4.0-one-tap-output-select`.

## The first push

Run 62 built everything: the new Swift compiled, and the app passed its package checks as `9.4.62`. Then GitHub's
artifact service timed out uploading the app (*Failed to CreateArtifact: Unable to make request: ETIMEDOUT*), so the
release step got only the `.deb`, and `v9.4.62` went out without `Speaka.tipa`. That's by design: an app failure never
holds back the tweak's release. Nothing in the code was wrong, but two things in the workflow made it worse than it
needed to be:

| | Before | Now |
| --- | --- | --- |
| A failed upload | Failed the job at once | Is tried once more, 30 s later, and only a second failure fails the job (both jobs) |
| Re-running a run whose release exists | Replaced the files but kept the notes, which said the app was missing | Replaces the notes too, since the updater shows them as the changelog |

## After the first device test

The steps worked on the phone. Two things to fix:

- **The connection line said *Your PC*.** The app read the PC's name only from iOS's list of connected devices, and
  only took it when the device said it was a computer or was the only one connected. iOS doesn't always count Speaka's
  connection as one of its own, so now, if no computer is reported as connected, the app takes the only computer
  paired with the iPhone (a PC has to be paired to stream), and then the only phone connected. The old rule, the
  only device of any kind, is gone: it could have named a watch. **Credits** now has a line on what the list
  reports, for example *Bluetooth knows 4 devices. Connected: LAPTOP-ABC. Computers: LAPTOP-ABC.*, so if the name is
  still missing, a screenshot of that line says why.
- **The log moved sideways.** Its lines sat in a column as wide as the widest line so far, centered, and the running
  line is drawn larger (20 pt) than a finished one (18 pt). So while *Registering Bluetooth's endpoints*, the longest
  running line, was showing, the column widened and every line moved left, then moved back when it finished. Now the
  column is sized before the first line, to the widest line this Start or Stop could show, so it never moves; it's
  still centered.

Both are in the app only (`SPConnectedDevice`, `MainComponents.swift`, `SpeakaModel.swift`, `Windows.swift`); the
tweak didn't change. This is also the first update the app can install for itself: the next release's Update
Available installs the `.deb` and the `.tipa`, then resprings.

## Apply and build

Copy the zip's contents over your clone. Nothing needs deleting. The changed files:

```text
control                          (Version: 9.4)
BTPSLog.c, Tweak.xm
app/project.yml                  (MARKETING_VERSION: 9.4)
app/RootHelper/main.c
app/Speaka/SessionSteps.swift    (new)
app/Speaka/SpeakaModel.swift, SpeakaUpdater.swift, UpdateViews.swift
app/Speaka/Model/SPTweakClient.h, SPTweakClient.m
tests/test_speaka_app.py
.github/workflows/build.yml      (the upload retry and the notes on a re-run)
README.md, app/README.md, docs/history/TESTING-v9.4.md
```

## On the phone

1. Push, and wait for the run. Expect release `v9.4.<run>` marked Latest, with the `.deb`, `Speaka.tipa`,
   `Speaka.ipa` and `SHA256SUMS`.
2. **Install that release's `Speaka.tipa` by hand with TrollStore.** v9.3's updater can't download, so it can't update
   itself this once.
3. Open Speaka. **Update Available** offers the tweak. Tap it, then **Update**: the window says *Downloading the tweak*,
   then Bluetooth restarts and the iPhone resprings. If it fails, the window now says which step, which file and the
   error's code; a screenshot of that is all I need.
4. Tap **Start**. Expect the nine lines, the last few a second or two after Bluetooth restarts.
5. To see a failure, turn Bluetooth off in **Settings > Bluetooth** (Control Center's switch only disconnects
   accessories and leaves Bluetooth on), then tap **Start**. *Registering Bluetooth's endpoints* waits and, 45 s after
   the tap, fails with *Check that Bluetooth is on*, and the log stays up for 6 s. Turn Bluetooth back on: the speaker
   should then be added, and the header's message clears once it is.
6. Tap **Stop**: *Restarting Bluetooth*, *Switching the speaker off*, *Bluetooth back to normal*.
7. After the fixes above: Start again and watch the lines; none should move sideways. While the PC plays, the
   connection line should name it. If it still says *Your PC*, open **Credits** and send a screenshot of the line
   under the versions.

## Host run

226 tests. Here: 221 pass, 3 skipped (no libsbc, no `bluetoothd` binary, no idle-guard binary), 1 expected failure
(decoder fidelity), and `test_v88_runtime` fails without the libsbc download that CI does. The new tests check that every
status key and prefix a step reads is one the tweak really writes, that every final refusal on the way to the speaker
sets a status, and that statuses are flushed within a second.

The Swift couldn't be compiled here (no Swift toolchain), so GitHub's build is its first compile. The changed C in
`BTPSLog.c` and `Tweak.xm` was compile-checked here with warnings as errors and format checking on every new status.
