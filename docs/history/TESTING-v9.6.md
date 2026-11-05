# Speaka v9.6: Settings, the PC's name, and paused is still connected

v9.5 worked on the phone: one `.tipa` installed the app and the tweak. Three things were left, and the 10:22 debug log
explained two of them.

## Kill Bluetooth, in a new Settings window

**Settings** now sits above **Audio Output**, with one button, **Kill Bluetooth**. It's what the README has said to
run over SSH when Bluetooth misbehaves, `killall -9 bluetoothd`, from the app. bluetoothd runs as root, so the app asks
its root helper (`speakahelper kill-bluetooth`, which sends SIGKILL, as `-9` does). launchd starts Bluetooth again at
once, without the speaker: everything connected drops for a moment, and the speaker is off until **Start**. The
button is greyed out while a Start or Stop is running, and says how it went underneath. Start's *Bluetooth didn't
restart* message now points to it too.

## The PC's name

Still *Your PC*. The log's helper status had the answer:

```text
Helper status: idle (media_idle) mode=headphones outputs=LAPTOP-UGIVBIA5 [BluetoothA2DPOutput]
```

iOS publishes the streaming PC as a Bluetooth audio device, and while Speaka isn't playing, it routes the phone's
audio to it. So the audio route knows the PC's name, while iOS's Bluetooth list, which v9.4 and v9.5 read, gives the
app nothing at all (even a Windows *LAPTOP-* name in it would have been found). The app now takes the name of a
Bluetooth output in the helper's route, or in its own, with a Windows default name (*DESKTOP-…*, *LAPTOP-…*) first,
and keeps it while the PC stays connected, because the route only shows the PC while nothing else is playing. The
Bluetooth list is still asked first, in case it ever answers.

One case it can't tell apart: with the PC renamed to something that isn't a Windows default, and other Bluetooth
headphones connected and playing when the PC connects, the headphones' name could be taken for the PC's.

## Paused is still connected

Pausing on the PC showed *Connect from your PC*. In the log, the pause is the PC's AVDTP SUSPEND
(`V882_CONTROL direction=RX ... sig=9 active=0 by=pc`): the stream is paused, not closed, and the PC is still
connected. The app only counted *playing* and *waiting* as connected. Now:

- **The tweak** reports the PC's side of the stream as `diag.v95.peer`: *configured* when the PC sets up the speaker's
  stream, *streaming* when a START is accepted, *paused* on SUSPEND, *closed* on CLOSE or ABORT. A status, nothing
  else; the stream itself is handled as before.
- **The app** counts the PC as connected while the helper plays, waits or idles (it idles, still attached, while
  paused), or while the tweak says *configured*, *streaming* or *paused*, until the stream is closed or the helper's
  session ends. The line then reads *LAPTOP-UGIVBIA5 · connected*.

A link that drops without the PC closing the stream (out of range, say) leaves the last state, so the line can say
*connected* until the next Start or Stop. Windows closes the stream when it disconnects normally.

## Apply and build

Copy the zip's contents over your clone (or let `Update-Speaka.ps1` do it). Nothing needs deleting. The changed files:

```text
control                          (Version: 9.6)
Tweak.xm                         (diag.v95.peer; build tag v9.6.0)
app/project.yml                  (MARKETING_VERSION: 9.6)
app/RootHelper/main.c            (kill-bluetooth)
app/Speaka/Model/SPTweakClient.h, SPTweakClient.m
app/Speaka/SpeakaModel.swift, ContentView.swift, Windows.swift
tests/test_speaka_app.py, test_v89_session_output.py, test_v91_output_hold.py
README.md, app/README.md, docs/history/TESTING-v9.6.md
```

`test_v89_session_output.py` runs the real `btps_v88_note_signal` and AVDTP parser on actual packets, and now also
checks the PC's status: *streaming* after an accepted START, *paused* after the PC's SUSPEND, *closed* after CLOSE and
after ABORT, and untouched by another device's traffic.

## On the phone

1. Push, wait for release `v9.6.<run>`, then **Update Available** in the app. This is v9.5's updater, so it's the first
   one-download update: the `.tipa`, then the tweak inside it, then a respring. **Credits** should read
   *App 9.6.<run> · tweak 9.6.<run>*.
2. **Settings** should sit above Audio Output. Tap **Kill Bluetooth**: devices drop and reconnect, the line under the
   button says Bluetooth restarted, and the main screen shows the speaker off. Tap **Start**.
3. Connect from the PC without playing: *LAPTOP-UGIVBIA5 · connected*. Play: *LAPTOP-UGIVBIA5 · Headphones (wired)*
   or similar. Pause on the PC: back to *LAPTOP-UGIVBIA5 · connected*, not *Connect from your PC*.
4. If the name is still *Your PC*, send a debug log saved right then: its *Helper status* line shows the route.
