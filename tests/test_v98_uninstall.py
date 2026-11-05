"""v9.8: the menu is Caz's four rows, and Uninstall, held for 4 s, removes the tweak, Speaka's leftovers, then the app."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app/Speaka"
HELPER = ROOT / "app/RootHelper/main.c"


def read(path):
    return pathlib.Path(path).read_text()


def block(source, start, end="\n}\n"):
    i = source.index(start)
    return source[i:source.index(end, i) + len(end)]


class Menu(unittest.TestCase):
    def test_four_rows_in_order(self):
        items = block(read(APP / "ContentView.swift"), "    private var menuItems: [MenuItem] {", "\n    }\n")
        ids = re.findall(r'MenuItem\(id: "(\w+)"', items)
        self.assertEqual(ids, ["settings", "output", "uninstall", "credits"])
        rows = dict(zip(ids, re.split(r'MenuItem\(id: "\w+"', items)[1:]))
        self.assertIn("let locked = model.uninstalling", items)
        for name in ("settings", "output", "credits"):
            self.assertIn("chevron: true, enabled: !locked", rows[name])      # locked once Uninstall starts
        uninstall = rows["uninstall"]
        self.assertIn('title: "Uninstall", symbol: "trash"', uninstall)
        self.assertNotIn("chevron", uninstall)                                  # an action, like Caz's Scan
        self.assertIn("enabled: model.request == nil && !updater.isBusy", uninstall)
        self.assertIn("holdSeconds: 4, busy: locked) { model.uninstall() }", uninstall)

    def test_panel_keeps_cazs_measurements(self):
        """Caz's spec: 73 pt rows (65 with a Home button, 52 on small screens), 20 pt sides, 5 pt top and bottom, 1 pt
        separators 10 pt in, 14 pt continuous corners. Four rows make Caz's 302 pt panel."""
        components, theme = read(APP / "MainComponents.swift"), read(APP / "Theme.swift")
        menu = block(components, "struct ActionMenu: View {")
        for part in (".padding(.horizontal, innerPadding)", ".padding(.vertical, Metrics.innerTopPadding)",
                     "RoundedRectangle(cornerRadius: 14, style: .continuous)",
                     ".frame(height: 1)\n                            .padding(.horizontal, 10)",
                     "if index < items.count - 1 {"):
            self.assertIn(part, menu)
        for part in ("static let innerPadding: CGFloat = 20", "static let innerTopPadding: CGFloat = 5",
                     "if isSmall(height) { return 52 }", "return isHomeButtonDevice ? 65 : 73"):
            self.assertIn(part, theme)
        self.assertIn("rowHeight: Metrics.actionHeight(size.height)", read(APP / "ContentView.swift"))

    def test_hold_row(self):
        components = read(APP / "MainComponents.swift")
        row = block(components, "struct HoldMenuRow: View {")
        plain = block(components, "struct MenuRow: View {")
        # Laid out exactly like the other rows.
        for part in ("HStack(spacing: 10)", ".font(.system(size: 19, weight: .medium))", ".font(.system(size: 17))",
                     ".lineLimit(1)", ".minimumScaleFactor(0.6)", ".foregroundStyle(Theme.text)",
                     ".padding(.horizontal, 12)", ".frame(height: height)", ".contentShape(Rectangle())"):
            self.assertIn(part, row)
            self.assertIn(part, plain)
        # Only a hold acts: no button, no press-dim (45% means busy), and a fill that shows how long is left.
        self.assertNotIn("Button {", row)
        self.assertNotIn("MenuButtonStyle", row)
        self.assertIn(".onLongPressGesture(minimumDuration: seconds, maximumDistance: 40) {", row)
        self.assertIn("private var seconds: Double { item.holdSeconds ?? 4 }", row)
        self.assertIn("pressing ? Animation.linear(duration: seconds) : Animation.easeOut(duration: 0.25)", row)
        self.assertIn(".frame(width: holding ? geo.size.width : 0)", row)
        self.assertIn(".fill(Theme.failure.opacity(0.3))", row)
        self.assertIn('return holding ? "Keep holding to uninstall" : item.title', row)
        self.assertIn("UINotificationFeedbackGenerator().notificationOccurred(.warning)\n            item.action()", row)
        # Then it greys out, saying so, with a spinner where the icon was.
        self.assertIn('if item.busy { return "Uninstalling..." }', row)
        self.assertIn("if item.busy {\n                    Spinner()", row)
        self.assertIn(".disabled(!item.enabled || item.busy)", row)
        self.assertIn(".opacity(item.enabled && !item.busy ? 1 : 0.45)", row)
        self.assertIn("if item.holdSeconds != nil {\n                        HoldMenuRow(item: item, height: rowHeight)",
                      block(components, "struct ActionMenu: View {"))
        item = block(components, "struct MenuItem: Identifiable {")
        self.assertIn("var holdSeconds: Double? = nil", item)
        self.assertIn("var busy = false", item)


class Uninstall(unittest.TestCase):
    def test_model_order(self):
        model = read(APP / "SpeakaModel.swift")
        flow = block(model, "    func uninstall() {", "\n    }\n")
        steps = ['SpeakaSpawnRoot(helper, ["remove-tweak"])', "SPTweakClient.removeLeftoverPreferences()",
                 'SpeakaSpawnRoot(helper, ["uninstall-app"])']
        at = [flow.index(step) for step in steps]
        self.assertEqual(at, sorted(at))
        self.assertIn("if status == 0 {\n                SPTweakClient.removeLeftoverPreferences()", flow)
        self.assertIn("guard !uninstalling, request == nil else { return }", flow)
        self.assertIn("uninstalling = true", flow)
        self.assertIn("guard status != 0 else { return }", flow)              # TrollStore closes Speaka
        self.assertIn("self.uninstalling = false\n                self.problem = reason", flow)
        self.assertIn('let reason = errors.last ?? fallback', flow)
        self.assertIn("guard request == nil, !uninstalling else { return false }", model)   # no Start meanwhile

    def test_leftover_preferences(self):
        header, client = read(APP / "Model/SPTweakClient.h"), read(APP / "Model/SPTweakClient.m")
        self.assertIn("+ (void)removeLeftoverPreferences NS_SWIFT_NAME(removeLeftoverPreferences());", header)
        impl = block(client, "+ (void)removeLeftoverPreferences\n{")
        for part in ("CFStringRef domain = CFSTR(BTPS_PREFS_DOMAIN);",
                     "CFPreferencesCopyKeyList(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)",
                     'if ([key hasPrefix:@"Speaka"])', "CFRelease(keys);",
                     "CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);",
                     "removePersistentDomainForName:app"):
            self.assertIn(part, impl)
        # The prefix finds every key: the tweak's, the helper's and the Settings pane's all start with "Speaka".
        keys = re.findall(r'#define BTPS_KEY_\w+\s+"([^"]+)"', read(ROOT / "BTPSShared.h"))
        self.assertGreater(len(keys), 10)
        pane = re.findall(r"<key>key</key>\s*<string>([^<]+)</string>", read(ROOT / "speakaprefs/Resources/Root.plist"))
        self.assertTrue(pane)
        for key in keys + pane:
            self.assertTrue(key.startswith("Speaka"), key)

    def test_helper(self):
        """The helper's three uninstall commands on a fake phone, compiled in its test mode."""
        source = read(HELPER)
        for command, function in (("remove-tweak", "do_remove_tweak"), ("uninstall-app", "do_uninstall_app"),
                                  ("finish-uninstall", "do_finish_uninstall")):
            self.assertIn(f'if (strcmp(argv[1], "{command}") == 0) return {function}();', source)
        self.assertIn('#define SPEAKA_HELPER_VERSION "1.3"', source)
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not cc:
            self.skipTest("no C compiler")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            for name, flags in (("device.o", ["-c"]), ("test", ["-DSPEAKA_TEST"])):
                build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", *flags,
                                        "-o", str(tmp / name), str(HELPER)], capture_output=True, text=True)
                self.assertEqual(build.returncode, 0, build.stderr)
            phone = tmp / "phone"
            (phone / "var/jb/usr/bin").mkdir(parents=True)
            (phone / "var/jb/usr/bin/dpkg").write_text("")
            info = phone / "var/jb/var/lib/dpkg/info"
            info.mkdir(parents=True)
            box = phone / "private/var/containers/Bundle/Application/ABC"
            (box / "TrollStore.app").mkdir(parents=True)
            (box / "TrollStore.app/trollstorehelper").write_text("")
            (box / ".com.apple.mobile_container_manager.metadata.plist").write_text("com.opa334.TrollStore\n")
            logs, caches = phone / "tmp/FTABHarvest", phone / "var/mobile/Library/Caches/Speaka"
            ours = ["Speaka.log", "SpeakaUpdate.log", "BTPSAudioHelper-v88.log", "BTPS-v88-123-abc.shm"]

            def seed():
                (info / "com.parv.speaka.list").write_text("")
                logs.mkdir(parents=True, exist_ok=True)
                caches.mkdir(parents=True, exist_ok=True)
                for name in ours + ["notes.txt"]:
                    (logs / name).write_text("x")
                (caches / "Speaka-update.tipa").write_text("x")

            def run(*args, fail=None):
                env = {"SPEAKA_ROOT": str(phone), "PATH": os.environ.get("PATH", "")}
                if fail:
                    env["SPEAKA_TEST_FAIL"] = fail
                result = subprocess.run([str(tmp / "test"), *args], capture_output=True, text=True, env=env)
                return result.returncode, result.stdout, re.findall(r"\(test\)   (.+)", result.stdout)

            seed()
            code, out, args = run("remove-tweak")
            self.assertEqual(code, 0, out)
            self.assertEqual(args, ["-r", "com.parv.speaka"])
            self.assertIn("would kill bluetoothd and Preferences", out)
            self.assertEqual(sorted(p.name for p in logs.iterdir()), ["notes.txt"])   # never anyone else's files
            self.assertFalse(caches.exists())

            (info / "com.parv.speaka.list").unlink()                                   # already gone: no dpkg
            code, out, args = run("remove-tweak")
            self.assertEqual((code, args), (0, []), out)
            self.assertIn("The tweak isn't installed", out)

            seed()
            code, out, _ = run("remove-tweak", fail="dpkg")
            self.assertEqual(code, 8, out)
            self.assertIn("Nothing else was removed.", out)
            self.assertNotIn("would kill", out)
            self.assertEqual(len(list(logs.iterdir())), 5)
            self.assertTrue((caches / "Speaka-update.tipa").exists())

            code, out, args = run("uninstall-app")
            self.assertEqual((code, args), (0, ["finish-uninstall"]), out)
            self.assertIn("in its own session, output to /dev/null", out)

            code, out, args = run("finish-uninstall")
            self.assertEqual((code, args), (0, ["uninstall", "com.parv.speaka"]), out)
            code, out, _ = run("finish-uninstall", fail="trollstorehelper")
            self.assertEqual(code, 7, out)

            shutil.rmtree(box)
            code, out, _ = run("uninstall-app")
            self.assertEqual(code, 5, out)
            self.assertIn("TrollStore's installer wasn't found", out)


if __name__ == "__main__":
    unittest.main()
