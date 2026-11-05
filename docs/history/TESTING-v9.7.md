# Speaka v9.7: update notifications, a plainer Settings, and Settings > Speaka without what the app does

## In the app

- **Kill Bluetooth** is just the button now: no heading or explanation. While it runs it reads *Killing
  Bluetooth...*; a line appears under it only if it failed. What it does hasn't changed (`killall -9 bluetoothd`,
  through the root helper).
- **Update notifications**, a switch under it. Turning it on asks iOS for permission to notify; if that's refused, it
  stays off and says where to allow it. With it on:
  - **while Speaka is open**, it checks GitHub every 2 minutes, so Update Available appears within 2 minutes of a
    release;
  - **in the background**, Speaka asks iOS to wake it every 2 minutes to check. iOS decides when that actually
    happens: often much less often, and never once Speaka has been swiped away from the app switcher. That's iOS's
    rule for every app, so the switch's line says it.
  - A release is announced once, when it's ready: newer than the app and carrying `Speaka.tipa`. The notification
    reads *Speaka 9.7.N is ready*.

## In Settings > Speaka

- **Bluetooth speaker** (Start, Stop and restore normal Bluetooth, Session) and **Audio output** (Play audio on, Choose
  output device...) are gone: the app has all of them, through the same protocol, and nothing in the app used this
  page. The page's code behind those rows is left as it was, unused: with the Session row gone, the 2 s refresh finds
  nothing to refresh.
- **Experiments** holds all five research switches under one header, instead of one header each.
- **Status** and **Debug log** are as they were.

## The first push

Run 68 built everything: the `.deb`, the app carrying it, both artifacts uploaded. Then publishing failed on GitHub's
side: `gh release create` makes a draft, uploads the files into it and publishes it; one upload was retried and
refused (*ReleaseAsset.name already exists*), and deleting the half-made draft got *HTTP 500*. So `v9.7.68` was left as
an unpublished draft, which the updater ignores. Publishing now has three tries, 30 s apart; each first deletes the
drafts GitHub Actions left behind (never anyone else's), and a re-run that finds a draft publishes it. The next run
also deletes run 68's draft.

## Apply and build

Copy the zip's contents over your clone (or let `Update-Speaka.ps1` do it). Nothing needs deleting. The changed files:

```text
control                          (Version: 9.7)
speakaprefs/Resources/Root.plist
app/project.yml                  (MARKETING_VERSION: 9.7)
app/Resources/Info.plist         (UIBackgroundModes fetch; BGTaskSchedulerPermittedIdentifiers)
app/Speaka/UpdateNotifications.swift   (new)
app/Speaka/SpeakaApp.swift, SpeakaUpdater.swift, SpeakaModel.swift, ContentView.swift, Windows.swift
app/Speaka/Model/SPTweakClient.h, SPTweakClient.m
tests/test_settings_page.py, test_speaka_app.py, test_v89_session_output.py, test_v894_wired_output.py
.github/workflows/build.yml      (after run 68: publishing retries and clears leftover drafts)
README.md, app/README.md, docs/history/TESTING-v9.7.md
```

## On the phone

1. Push and let **Update Available** install `v9.7.<run>`.
2. **Settings** in the app: **Kill Bluetooth**, just the button, then the **Update notifications** switch. Turn it on
   and allow notifications.
3. Leave Speaka open, push any small change, and wait: within about 2 minutes of the release being published,
   **Update Available** should appear.
4. For the background: go home without swiping Speaka away, and push again. A notification may take a while; iOS
   chooses when to wake Speaka.
5. **Settings > Speaka** should show Status, Debug log and one Experiments section with all five switches.
