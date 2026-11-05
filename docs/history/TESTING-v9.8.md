# Testing v9.8: Caz's menu, and hold to uninstall

## What changed

**The menu** has four rows, like Caz's: Settings, Audio Output, Uninstall, Credits. The panel and its rows were
already Caz's code, so with a fourth row they land exactly where Caz's do (spec page 5): 73 pt rows, a 330 x 302 pt
panel at y 306.7 on an iPhone 13, icons at 19 pt Medium, titles at 17 pt, separators 10 pt in. Uninstall is an action,
like Caz's Scan, so it has no chevron.

**Uninstall** acts only when held for 4 seconds:

| You | It |
| --- | --- |
| Tap it | Nothing (the fill starts and runs straight back) |
| Hold it | A fill in the warning colour sweeps across the row over 4 s; the row reads *Keep holding to uninstall* |
| Let go early | The fill runs back; nothing happens |
| Hold 4 s | A warning haptic; the row shows a spinner and *Uninstalling...*, greyed to 45%; everything else locks |

Then, with no second prompt:

1. `speakahelper remove-tweak`, as root: `dpkg -r com.parv.speaka` (its remove script stops the audio helper), then
   bluetoothd is killed so the removed tweak unloads, and Settings is closed so Settings > Speaka goes. Then Speaka's
   files: in `/tmp/FTABHarvest` only names starting with *Speaka* or *BTPS* (the folder goes only if that empties
   it), and `/var/mobile/Library/Caches/Speaka`.
2. The app removes every key starting with *Speaka* from Bluetooth's preferences (all of Speaka's do; a test checks
   every key the tweak and Settings use), and its own settings.
3. `speakahelper uninstall-app` starts `finish-uninstall` in its own session, which runs
   `trollstorehelper uninstall com.parv.speaka`. Speaka closes and its icon goes.

If dpkg fails, nothing else is removed: Bluetooth isn't restarted, no files go, and the header says why (*The tweak
didn't uninstall (dpkg exited with N)*). If the tweak is already gone, it goes straight to the rest.

Helper 1.3. Files: `app/RootHelper/main.c`, `app/Speaka/{MainComponents,ContentView,SpeakaModel}.swift`,
`app/Speaka/Model/SPTweakClient.[hm]`, `tests/test_v98_uninstall.py` (6 tests), and three assertions in
`tests/test_speaka_app.py`.

## On the phone

1. **The menu.** Four rows; Uninstall has a trash icon and no chevron. Compare with Caz side by side: the panel's top
   and bottom, the rows and the separators should line up.
2. **A tap.** Tap Uninstall: the row starts to fill and runs back, and nothing else happens.
3. **Let go early.** Hold for about 2 seconds and let go: *Keep holding to uninstall* while held, then the fill runs
   back and the title returns.
4. **The whole thing**, when you're ready to reinstall afterwards. Hold for 4 seconds: a haptic, *Uninstalling...*
   with a spinner. Bluetooth restarts (anything connected drops for a few seconds), then Speaka closes and its icon
   disappears. Then check:
   - Settings: no Speaka page.
   - `dpkg -l | grep speaka` (in a terminal or Filza) shows nothing.
   - `/tmp/FTABHarvest` has no `Speaka*` or `BTPS*` files.
5. **Reinstall** from the latest release's `.tipa` with TrollStore, then tap **Install Tweak** in the app.

If it stops with a message in the header, nothing past that step was removed: screenshot it, then hold again.
