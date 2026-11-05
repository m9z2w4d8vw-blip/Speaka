"""The Speaka companion app (app/): a SwiftUI app in Caz's style, on Speaka's own background, with Caz's updater for
both the app and the tweak. It must speak exactly the tweak's protocol, agree with the Settings page, keep Speaka's own
icon and background, and package correctly for TrollStore.

Swift is first compiled by CI (there is no Apple compiler on the host), so the structural checks below are the only
ones that run before that: balanced code, imports, every type and model member that is used being defined, and every
Objective-C method being called by the Swift name its header pins down."""
import hashlib
import os
import pathlib
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SWIFT = sorted((APP / "Speaka").rglob("*.swift"))
OBJC = sorted((APP / "Speaka").rglob("*.[hm]"))
SOURCES = OBJC + SWIFT

MODEL = APP / "Speaka/SpeakaModel.swift"
CONTENT = APP / "Speaka/ContentView.swift"
THEME = APP / "Speaka/Theme.swift"
PARTS = APP / "Speaka/MainComponents.swift"
WINDOWS = APP / "Speaka/Windows.swift"
UPDATER = APP / "Speaka/SpeakaUpdater.swift"
UPDATE_VIEWS = APP / "Speaka/UpdateViews.swift"
HELPER = APP / "RootHelper/main.c"
CLIENT_H = APP / "Speaka/Model/SPTweakClient.h"
CLIENT_M = APP / "Speaka/Model/SPTweakClient.m"
DEVICE_H = APP / "Speaka/Model/SPConnectedDevice.h"

# Speaka's background, as v9.1 drew it (SPTheme.m): x and y as fractions of the size drawn (the screen, 200 pt taller),
# radius as a fraction of its longer side, colour, alpha.
GLOWS = [("0.10", "0.12", "0.62", "5B2BB5", "0.95"), ("0.95", "0.30", "0.45", "2B4FD8", "0.60"),
         ("0.90", "0.92", "0.60", "12B3A6", "0.80"), ("0.05", "0.88", "0.42", "B0307A", "0.45")]

# Speaka keeps its own icon for now: the leading 16 hex digits of each file's SHA-256, as shipped in v9.1.
# When the icon changes on purpose, update these.
ICON_DIGESTS = {
    "Resources/AppIcon60x60@2x.png": "4fd52814abcf3aac",
    "Resources/AppIcon60x60@3x.png": "ed999310b8c16b3c",
    "Resources/AppIcon76x76@2x~ipad.png": "4a0f8f99c1e4f768",
    "Resources/AppIcon83.5x83.5@2x~ipad.png": "ec993513bf336c1a",
    "Artwork/AppIcon-1024.png": "b3816e5299b13b6f",
}


def read(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def strip_comments_and_strings(src):
    """Code with comments removed and every string or character literal emptied, read in one pass in source order, so
    a "//" inside a string (https://...) is never taken for a comment. Line breaks are kept."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("\n" * src.count("\n", i, end))
            i = end
        elif src[i] in "\"'" or src.startswith('@"', i):
            if src[i] == "@":
                i += 1
            quote, j = src[i], i + 1
            while j < n and src[j] != quote and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append(quote * 2)
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def png_size(path):
    data = pathlib.Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", data[16:24])


def jpeg_size(path):
    data = pathlib.Path(path).read_bytes()
    assert data[:2] == b"\xff\xd8", path
    at = 2
    while at + 9 < len(data):
        marker, length = data[at + 1], struct.unpack(">H", data[at + 2:at + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):                       # start of frame: height, then width
            height, width = struct.unpack(">HH", data[at + 5:at + 9])
            return width, height
        at += 2 + length
    raise AssertionError(f"{path}: no JPEG frame header")


def block(src, opener):
    """The text of the brace block that `opener` opens (`opener` must end with '{')."""
    assert opener.endswith("{"), opener
    start = src.index(opener) + len(opener)
    depth = 1
    for at in range(start, len(src)):
        depth += {"{": 1, "}": -1}.get(src[at], 0)
        if depth == 0:
            return src[start:at]
    raise AssertionError(f"unbalanced block after {opener!r}")


class SpeakaApp(unittest.TestCase):
    # ------------------------------------------------------------------ protocol
    def test_app_speaks_the_tweaks_protocol(self):
        client = read(CLIENT_M)
        self.assertIn('#import "BTPSShared.h"', client)
        for name in ("@BTPS_KEY_V89_REQUEST", "BTPS_NOTIFY_V89_SESSION", "@BTPS_KEY_STATUS", "@BTPS_STATUS_V89_SESSION",
                     "@BTPS_KEY_V89_HELPER", "@BTPS_KEY_V89_OUTPUT_MODE", "BTPS_NOTIFY_V89_OUTPUT", "BTPS_PREFS_DOMAIN"):
            self.assertIn(name, client)
        send = client.split("- (SPRequest *)sendAction:", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('@{ @"action": action, @"nonce": nonce, @"time": @([NSDate date].timeIntervalSince1970) }', send)
        self.assertLess(send.index("SPWritePref(@BTPS_KEY_V89_REQUEST, request)"), send.index("notify_post(BTPS_NOTIFY_V89_SESSION)"))
        self.assertIn("SPWritePref(@BTPS_KEY_V89_REQUEST, nil);", send)   # withdrawn if the notification fails
        # Names come from BTPSShared.h only: no hand-typed keys or notification names anywhere in the app.
        for source in SOURCES:
            text = read(source)
            self.assertNotRegex(text, r'@?"Speaka[A-Z]\w*"', source.name)
            self.assertNotIn('"com.parv.speaka.', text, source.name)
            self.assertNotIn('"com.apple.bluetooth"', text, source.name)
        # The Swift screens never touch preferences or notifications themselves: SPTweakClient does.
        for source in SWIFT:
            code = strip_comments_and_strings(read(source))
            for call in ("CFPreferences", "notify_post", "UserDefaults"):
                self.assertNotIn(call, code, f"{source.name} calls {call}; go through SPTweakClient")

    def test_session_rules_match_the_settings_page(self):
        client = read(CLIENT_M)
        settings = read(ROOT / "speakaprefs/BTPSRootListController.m")
        bad = r'@\[ (@"REFUSED", @"ROLLED BACK", @"UNSAFE", @"FAILED", @"SDP publish failed") \]'
        self.assertEqual(re.search(bad, client).group(1), re.search(bad, settings).group(1))
        for rule in ('[sep hasPrefix:@"LOST"]', '[trial hasPrefix:@"ARMED"]', '[trial hasPrefix:@"REFUSED"]',
                     '[sep containsString:@"register=0"]', '[sep containsString:@"SEID=7"]',
                     'status[@"diag.v89.build"]', '[session containsString:@"result=REFUSED"]'):
            self.assertIn(rule, client)
            self.assertIn(rule, settings)
        self.assertIn("> 20.0)", client)                       # helper freshness, BTPSHelperStatus(20.0)
        self.assertIn("BTPSHelperStatus(20.0)", settings)
        self.assertIn("request.relaunched ? 45.0 : 25.0", client)
        self.assertIn("self.pendingRelaunched ? 45.0 : 25.0", settings)

    def test_output_modes_match_the_settings_page(self):
        plist = plistlib.loads((ROOT / "speakaprefs/Resources/Root.plist").read_bytes())
        item = next(i for i in plist["items"] if i.get("key") == "SpeakaV89OutputMode")
        client = read(CLIENT_M)
        modes = client.split("+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes", 1)[1].split("];", 1)[0]
        keys = re.findall(r'@"key": @"([^"]+)"', modes)
        titles = re.findall(r'@"title": @"([^"]+)"', modes)
        short = re.findall(r'@"shortTitle": @"([^"]+)"', modes)
        self.assertEqual(keys, item["validValues"])
        self.assertEqual(titles, item["validTitles"])
        self.assertEqual(short, item["shortTitles"])
        for symbol in re.findall(r'@"symbol": @"([^"]+)"', modes):
            self.assertRegex(symbol, r"^[a-z0-9.]+$")
        # The Audio Output window lists exactly these, from the client, and the model stores the choice through it.
        model = read(MODEL)
        self.assertIn("SPTweakClient.outputModes().map", model)
        self.assertIn("ForEach(SpeakaModel.outputModes)", read(WINDOWS))
        self.assertIn("client.setOutputMode(mode)", block(model, "func setOutputMode(_ mode: String) {"))

    # ------------------------------------------------------------------ Swift <-> Objective-C
    def test_swift_calls_the_objective_c_by_its_pinned_names(self):
        headers = read(CLIENT_H) + read(DEVICE_H)
        pinned = set(re.findall(r"NS_SWIFT_NAME\((\w+)\(", headers))
        self.assertEqual(pinned, {"outputModes", "readStatus", "setOutputMode", "sendAction", "pollRequest",
                                  "connectedComputerName"})
        # Every method the client declares, apart from the unused singleton, has its Swift name pinned.
        interface = read(CLIENT_H).split("@interface SPTweakClient : NSObject", 1)[1].split("@end", 1)[0]
        for declaration in re.findall(r"^[-+][^;]+;", interface, re.M):
            if "sharedClient" not in declaration:
                self.assertIn("NS_SWIFT_NAME(", declaration, declaration)
        code = "".join(strip_comments_and_strings(read(s)) for s in SWIFT)
        for receiver in ("client", "SPTweakClient", "SPConnectedDevice"):
            for name in re.findall(rf"\b{receiver}\.(\w+)\(", code):
                self.assertIn(name, pinned, f"Swift calls {receiver}.{name}(), which no NS_SWIFT_NAME declares")
        # Properties read from SPStatus (status.) and SPRequest (pending.) exist in the header.
        properties = set(re.findall(r"@property \([^)]*\) [\w <>*]+?\*?(\w+);", read(CLIENT_H)))
        for owner in ("status", "pending"):
            names = set(re.findall(rf"\b{owner}\.(\w+)\b(?!\()", code))
            self.assertTrue(names, owner)
            self.assertLessEqual(names, properties, f"{owner}.{names - properties} aren't declared in SPTweakClient.h")
        # Enum cases as Swift imports them: the common prefix is dropped and the first letter lowered.
        def cases(enum, prefix):
            body = read(CLIENT_H).split(f"typedef NS_ENUM(NSInteger, {enum})", 1)[1].split("};", 1)[0]
            names = re.findall(rf"^\s*({prefix}\w+)", body, re.M)
            return {n[len(prefix)].lower() + n[len(prefix) + 1:] for n in names}
        sessions = cases("SPSession", "SPSession")
        self.assertEqual(sessions, {"noTweak", "oldTweak", "off", "starting", "on", "lost", "failed", "refused"})
        self.assertEqual(cases("SPRequestState", "SPRequest"), {"waiting", "finished"})
        model = strip_comments_and_strings(read(MODEL))
        used = set()
        for switch in re.finditer(r"switch snapshot\.session \{", model):
            for label in re.findall(r"case ([^:]+):", block(model[switch.start():], "switch snapshot.session {")):
                used |= set(re.findall(r"\.(\w+)", label))
        used |= set(re.findall(r"session (?:==|!=) \.(\w+)", model))
        used |= set(re.findall(r"SPSession = \.(\w+)", model))
        self.assertGreaterEqual(len(used), 6)
        self.assertLessEqual(used, sessions, f"unknown SPSession cases: {used - sessions}")
        self.assertIn("state == .finished", model)

    # ------------------------------------------------------------------ one button
    def test_one_button_starts_and_stops(self):
        content = read(CONTENT)
        self.assertEqual(strip_comments_and_strings(content).count("ActionButton("), 1, "the main screen has one big button")
        button = content.split("ActionButton(title: model.buttonTitle,", 1)[1].split(".matchedGeometryEffect", 1)[0]
        self.assertIn("symbol: model.buttonSymbol", button)
        self.assertIn("enabled: model.canPress", button)
        self.assertIn("model.toggle()", button)
        for old in ("startButton", "stopButton", "SPBigButton", "SPMarqueeView", "SPOutputDropdown"):
            self.assertNotIn(old, "".join(read(s) for s in SOURCES))

        model = read(MODEL)
        self.assertIn("case .on, .starting, .failed:\n            return true", block(model, "var buttonStops: Bool {"))
        self.assertIn('var buttonTitle: String { buttonStops ? "Stop" : "Start" }', model)
        self.assertIn('var buttonSymbol: String { buttonStops ? "stop" : "play" }', model)
        press = block(model, "var canPress: Bool {")
        self.assertIn("guard request == nil else { return false }", press)
        self.assertIn("case .noTweak, .oldTweak:\n            return false", press)

        # Tapping opens the log first, then sends the request; each step of the restart gets a line.
        toggle = block(model, "func toggle() {")
        self.assertIn('let action = buttonStops ? "stop" : "start"', toggle)
        self.assertLess(toggle.index("sheetVisible = true"), toggle.index("client.sendAction(action, error: &error)"))
        self.assertIn('startLyric("Restarting Bluetooth")', toggle)
        relaunched = block(model, "private func relaunched(_ pending: SPRequest) {")
        self.assertIn('finishLyric("Bluetooth restarted")', relaunched)
        self.assertIn('startLyric("Adding the speaker")', relaunched)
        finish = block(model, "private func finish(_ pending: SPRequest) {")
        self.assertLess(finish.index('finishLyric("Speaker added")'), finish.index('finishLyrics(success: "Ready for your PC"'))
        self.assertIn('finishLyrics(success: "Bluetooth back to normal"', finish)
        self.assertIn("collapseSoon()", finish)
        self.assertIn("relaunched(pending)", block(model, "private func tick() {"))
        # A failure is told apart by SPTweakClient's own result titles.
        failure = block(model, "static func failure(for pending: SPRequest) -> (line: String, problem: String?) {")
        titles = set(re.findall(r'title == "([^"]+)"', failure))
        self.assertEqual(titles, {"Still waiting"})
        for title in titles:
            self.assertIn(f'title:@"{title}"', read(CLIENT_M))

    # ------------------------------------------------------------------ updates
    def test_updater_installs_the_tweak_then_the_app_then_resprings(self):
        updater = read(UPDATER)
        self.assertIn('URL(string: "https://api.github.com/repos/m9z2w4d8vw-blip/Speaka/releases")!', updater)
        self.assertIn("if release.draft || release.prerelease { return nil }", updater)   # skips the old nightly too
        self.assertIn('debURL: link(in: release.assets, ending: ".deb")', updater)
        self.assertIn('tipaURL: link(in: release.assets, ending: ".tipa") ?? link(in: release.assets, ending: ".ipa")',
                      updater)
        # What's installed: this app's version, and the tweak's from dpkg's database.
        package = re.search(r"^Package: (\S+)$", read(ROOT / "control"), re.M).group(1)
        self.assertIn(f'static let packageID = "{package}"', updater)
        self.assertIn('"/var/jb/var/lib/dpkg/status"', updater)
        self.assertIn('installed = line.hasSuffix(" installed")', updater)
        self.assertIn("self.choose(from: releases, app: Self.versionParts(Bundle.main.shortVersion), package: package)",
                      updater)
        choose = block(updater, "func choose(from releases: [SpeakaRelease], app: [Int], package: [Int]?) {")
        self.assertIn("Self.compare(release.version, app) > 0", choose)
        self.assertIn("package.map({ Self.compare(release.version, $0) > 0 }) ?? true", choose)
        # The .deb is downloaded first and handed over as --deb, the .tipa as --tipa.
        start = block(updater, "func startDownload() {")
        self.assertLess(start.index('"Speaka-update.deb"'), start.index('"Speaka-update.tipa"'))
        self.assertIn('var args = ["install-update"]', updater)
        self.assertIn('args += [file.pathExtension.lowercased() == "deb" ? "--deb" : "--tipa", file.path]', updater)
        # Only an app-only update falls back to TrollStore's link; a failed .deb is reported and nothing else happens.
        done = block(updater, "private func installed(status: Int32, errors: [String], withTweak: Bool) {")
        self.assertLess(done.index("if withTweak {"), done.index("openInTrollStore()"))
        self.assertIn('"apple-magnifier://install?url="', updater)
        self.assertIn("var resprings: Bool { deb != nil }", updater)
        views = read(UPDATE_VIEWS)
        self.assertIn('updater.resprings ? "Bluetooth restarts, then your iPhone resprings"', views)
        self.assertIn('"Speaka will close when it\'s done"', views)
        self.assertNotIn("Caz will", views)
        content = read(CONTENT)
        self.assertIn('UpdateLinkButton(title: "Update Available", symbol: "arrow.down.circle")', content)
        self.assertIn("if window == .update && updater.isBusy { return }", content)
        self.assertIn("updater.check()", content)
        # Update Available keeps Caz's place; the connection line moves up a line while it shows, so they never overlap.
        self.assertIn(".offset(y: -(30 + linkGap))\n", content)
        self.assertIn(".offset(y: -(30 + linkGap) - (updater.isAvailable ? 40 : 0))", content)

    def test_update_helper(self):
        """The root helper, compiled for the host in its test mode, on a fake phone: what it would run, and in what order."""
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not cc:
            self.skipTest("no C compiler")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            # The device code compiles here too, with warnings as errors, so a slip there can't wait for CI.
            for name, flags in (("device", []), ("test", ["-DSPEAKA_TEST"])):
                build = subprocess.run([cc, "-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", *flags,
                                        "-o", str(tmp / name), str(HELPER)], capture_output=True, text=True)
                self.assertEqual(build.returncode, 0, build.stderr)
            phone = tmp / "phone"
            caches = phone / "var/mobile/Library/Caches"
            caches.mkdir(parents=True)
            (phone / "var/jb/usr/bin").mkdir(parents=True)
            (phone / "var/jb/usr/bin/dpkg").write_text("")
            box = phone / "private/var/containers/Bundle/Application/ABC"
            (box / "TrollStore.app").mkdir(parents=True)
            (box / "TrollStore.app/trollstorehelper").write_text("")
            (box / ".com.apple.mobile_container_manager.metadata.plist").write_text("com.opa334.TrollStore\n")
            deb, tipa = caches / "Speaka-update.deb", caches / "Speaka-update.tipa"

            def run(*args, fail=None):
                deb.write_text("deb")
                tipa.write_text("tipa")
                env = {"SPEAKA_ROOT": str(phone), "PATH": os.environ.get("PATH", "")}
                if fail:
                    env["SPEAKA_TEST_FAIL"] = fail
                result = subprocess.run([str(tmp / "test"), *args], capture_output=True, text=True, env=env)
                return result.returncode, result.stdout

            def arguments(text):
                return re.findall(r"\(test\)   (.+)", text)

            # Both: dpkg first, waited for; then, in its own session, TrollStore's installer and the respring.
            code, out = run("install-update", "--deb", str(deb), "--tipa", str(tipa))
            self.assertEqual(code, 0, out)
            self.assertLess(out.index("would run " + str(phone / "var/jb/usr/bin/dpkg")), out.index("would start"))
            self.assertEqual(arguments(out.split("would start", 1)[0]), ["-i", str(deb)])
            finish = out.split("would start", 1)[1]
            self.assertIn("in its own session", finish.splitlines()[0])
            self.assertEqual(arguments(finish), ["finish-update", "--tipa", str(tipa), "--respring"])
            # The last step: the app, then the respring.
            code, out = run("finish-update", "--tipa", str(tipa), "--respring")
            self.assertEqual(code, 0, out)
            self.assertLess(out.index("trollstorehelper in its own session"), out.index("would kill SpringBoard"))
            # Only the tweak: dpkg, then a respring, no TrollStore.
            code, out = run("install-update", "--deb", str(deb))
            self.assertEqual(code, 0, out)
            self.assertEqual(arguments(out.split("would start", 1)[1]), ["finish-update", "--respring"])
            # Only the app: Caz's way. TrollStore's installer is waited for, and nothing resprings.
            code, out = run("install-update", "--tipa", str(tipa))
            self.assertEqual(code, 0, out)
            self.assertIn("trollstorehelper in its own session", out)
            self.assertNotIn("SpringBoard", out)
            self.assertNotIn("would start", out)
            # dpkg fails: nothing else runs and nothing resprings.
            code, out = run("install-update", "--deb", str(deb), "--tipa", str(tipa), fail="dpkg")
            self.assertEqual(code, 8, out)
            self.assertNotIn("would start", out)
            self.assertNotIn("trollstorehelper", out)
            # TrollStore's installer fails: code 7, so the app falls back to TrollStore's link.
            self.assertEqual(run("install-update", "--tipa", str(tipa), fail="trollstorehelper")[0], 7)
            # Update files must be regular files inside /var/mobile, with the right extension.
            for bad in ("/tmp/elsewhere.deb", str(caches) + "/../../../../etc/x.deb", str(caches / "Speaka-update.txt")):
                self.assertEqual(run("install-update", "--deb", bad)[0], 4, bad)
            (caches / "link.deb").symlink_to(deb)
            self.assertEqual(run("install-update", "--deb", str(caches / "link.deb"))[0], 4)
            self.assertEqual(run("install-update")[0], 2)
            (phone / "var/jb/usr/bin/dpkg").unlink()
            self.assertEqual(run("install-update", "--deb", str(deb))[0], 9)
            shutil.rmtree(box)
            self.assertEqual(run("install-update", "--tipa", str(tipa))[0], 5)
        # dpkg gets the jailbreak's folders on PATH: the app starts the helper with an empty environment, and the
        # package's install script calls launchctl and killall by name.
        source = read(HELPER)
        self.assertIn('"PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:/usr/bin:/bin:/usr/sbin:/sbin"', source)
        self.assertIn("int code = run_and_wait(dpkg, argv, k_dpkg_env, 0);", source)
        self.assertIn("signal(SIGPIPE, SIG_IGN);", source)
        self.assertIn('if (resprings) respring();', source)

    # ------------------------------------------------------------------ Caz's look
    def test_the_look_is_cazs_on_speakas_background(self):
        theme = read(THEME)
        for line in ("static let menu = Color(red: 63 / 255.0, green: 63 / 255.0, blue: 63 / 255.0).opacity(114 / 255.0)",
                     "static let window = Color(red: 76 / 255.0, green: 76 / 255.0, blue: 76 / 255.0).opacity(153 / 255.0)",
                     "static let separator = Color(white: 0.6).opacity(0.3)",
                     "static let secondary = Color.white.opacity(0.8)",
                     "static let foreground = Color.white.opacity(0.75)",
                     "static let failure = Color(red: 1.0, green: 0.62, blue: 0.56)",
                     "static let padding: CGFloat = 30", "static let innerPadding: CGFloat = 20",
                     "static let innerPaddingTiny: CGFloat = 10", "static let innerTopPadding: CGFloat = 5",
                     "if isSmall(height) { return 52 }", "return isHomeButtonDevice ? 65 : 73",
                     ".opacity(configuration.isPressed ? 0.45 : 1)",
                     ".animation(.easeOut(duration: 0.15), value: configuration.isPressed)",
                     ".opacity(dimmed ? 0.4 : 1)"):
            self.assertIn(line, theme)
        parts = read(PARTS)
        for line in (".background(Theme.menu, in: RoundedRectangle(cornerRadius: 14, style: .continuous))",
                     ".opacity(enabled ? 1 : 0.7)", "ForEach(lines.suffix(14))", ".frame(height: 40)",
                     ".font(.system(size: line.status == .running ? 20 : 18))",
                     ".opacity(line.status == .running ? 0.9 : 0.5)", ".init(color: .black, location: 0.45)",
                     ".trim(from: 0, to: 0.72)", "StrokeStyle(lineWidth: 2.5, lineCap: .round)", "let period = 2.3",
                     ".frame(width: 30, height: 12)", ".font(.system(size: 18, weight: .regular))"):
            self.assertIn(line, parts)
        content = read(CONTENT)
        for line in ("let top = size.height * 0.26 + 35", ".frame(width: size.width, height: visible + 100)",
                     ".animation(.spring(response: 0.75, dampingFraction: 0.9), value: model.sheetVisible)",
                     ".animation(.spring(response: 0.6, dampingFraction: 0.9), value: window)",
                     ".animation(.spring(response: 0.5, dampingFraction: 0.9), value: model.statusLine != nil)",
                     ".transition(.offset(y: 25).combined(with: .opacity))", ".scaleEffect(expanded ? 0.6 : 1)",
                     ".offset(y: expanded ? -75 : 0)", ".offset(y: expanded ? -25 : 0)", ".frame(height: 60)",
                     "(Metrics.isHomeButtonDevice ? 0.78 : 0.73)",
                     ".transition(.scale(scale: 0.7).combined(with: .opacity))"):
            self.assertIn(line, content)
        model = read(MODEL)
        self.assertIn("DispatchQueue.main.asyncAfter(deadline: .now() + 1.6)", model)
        self.assertIn('var headerSubtitle: String { "iOS \\(UIDevice.current.systemVersion)" }', model)
        self.assertIn('var headerDetail: String { headerWarning ?? "by Parv" }', model)
        self.assertIn(".background(Theme.window, in: RoundedRectangle(cornerRadius: 16, style: .continuous))", read(WINDOWS))
        # Speaka's own background, exactly as v9.1 drew it, and its teal: not Caz's yellow backdrop or gold.
        glows = re.findall(r"glow\(context, space, CGPoint\(x: w \* ([\d.]+), y: h \* ([\d.]+)\), m \* ([\d.]+), "
                           r"0x([0-9A-F]{6}), ([\d.]+)\)", theme)
        self.assertEqual(glows, GLOWS)
        for line in ("UIColor(red: 0x0D / 255.0, green: 0x0A / 255.0, blue: 0x24 / 255.0, alpha: 1).setFill()",
                     "let components: [CGFloat] = [r, g, b, alpha, r, g, b, alpha * 0.45, r, g, b, 0.0]",
                     "let locations: [CGFloat] = [0.0, 0.45, 1.0]", "format.scale = 1",
                     "let size = CGSize(width: max(screen.width, 1), height: max(screen.height, 1) + 200)",
                     ".frame(width: size.width, height: size.height + 200)",
                     "static let accent = Color(red: 18 / 255.0, green: 179 / 255.0, blue: 166 / 255.0)"):
            self.assertIn(line, theme)
        self.assertIn("Backdrop(dimmed: window != nil, size: geo.size)", content)
        code = "".join(read(source) for source in SWIFT)
        self.assertNotIn('Image("Backdrop")', code)
        self.assertNotIn("green: 178 / 255.0, blue: 30", code)            # Caz's gold

    def test_the_icon_is_unchanged(self):
        for name, digest in ICON_DIGESTS.items():
            self.assertEqual(hashlib.sha256((APP / name).read_bytes()).hexdigest()[:16], digest,
                             f"{name} changed; update ICON_DIGESTS only when the icon changes on purpose")
        sizes = {"AppIcon60x60@2x.png": 120, "AppIcon60x60@3x.png": 180,
                 "AppIcon76x76@2x~ipad.png": 152, "AppIcon83.5x83.5@2x~ipad.png": 167}
        for name, pixels in sizes.items():
            self.assertEqual(png_size(APP / "Resources" / name), (pixels, pixels), name)
        self.assertEqual(png_size(APP / "Artwork/AppIcon-1024.png"), (1024, 1024))
        info = plistlib.loads((APP / "Resources/Info.plist").read_bytes())
        for icons in (info["CFBundleIcons"], info["CFBundleIcons~ipad"]):
            for base in icons["CFBundlePrimaryIcon"]["CFBundleIconFiles"]:
                self.assertTrue(any(n.startswith(base + "@") for n in sizes), base)
        # The icon comes from those files, so the asset catalog has none and actool is told not to look for one.
        self.assertFalse((APP / "Speaka/Assets.xcassets/AppIcon.appiconset").exists())
        self.assertIn('ASSETCATALOG_COMPILER_APPICON_NAME: ""', read(APP / "project.yml"))

    # ------------------------------------------------------------------ bundle
    def test_bundle_contents(self):
        info = plistlib.loads((APP / "Resources/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleExecutable"], "Speaka")
        self.assertEqual(info["CFBundleIdentifier"], "com.parv.speaka")
        self.assertEqual(info["CFBundleDisplayName"], "Speaka")
        # The version is the release's, the same as the tweak's: CI passes it, project.yml has control's for local builds.
        self.assertEqual(info["CFBundleShortVersionString"], "$(MARKETING_VERSION)")
        self.assertEqual(info["CFBundleVersion"], "$(CURRENT_PROJECT_VERSION)")
        base = re.search(r"^Version: (\S+)$", read(ROOT / "control"), re.M).group(1)
        self.assertIn(f'MARKETING_VERSION: "{base}"', read(APP / "project.yml"))
        self.assertIn("UILaunchScreen", info)
        self.assertEqual(info["UIUserInterfaceStyle"], "Dark")
        # SwiftUI's own app lifecycle: no scene delegate class to name, and the minimum iOS comes from project.yml.
        self.assertEqual(info["UIApplicationSceneManifest"], {"UIApplicationSupportsMultipleScenes": False})
        self.assertNotIn("MinimumOSVersion", info)
        self.assertIn("@main\nstruct SpeakaApp: App {", read(APP / "Speaka/SpeakaApp.swift"))

    def test_entitlements_are_only_what_the_app_needs(self):
        entitlements = plistlib.loads((APP / "Speaka.entitlements").read_bytes())
        # persona-mgmt is what lets the app start its update helper as root (SPRootSpawn.m), as in Caz.
        self.assertEqual(entitlements, {"platform-application": True, "com.apple.private.security.no-sandbox": True,
                                        "com.apple.private.persona-mgmt": True})
        helper = plistlib.loads((APP / "RootHelper/entitlements.plist").read_bytes())
        self.assertEqual(helper, {"platform-application": True, "com.apple.private.security.no-sandbox": True,
                                  "com.apple.private.security.container-required": False,
                                  "com.apple.private.security.storage.AppBundles": True,
                                  "com.apple.private.security.storage.AppDataContainers": True,
                                  "com.apple.private.persona-mgmt": True})

    def test_dopamine_ui_is_credited_under_its_license(self):
        license_text = read(APP / "Resources/LICENSE-Dopamine.txt")
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2023-2024 Lars Fröder (opa334)", license_text)
        self.assertIn("The above copyright notice and this permission notice shall be included", license_text)
        adapted = [s for s in SWIFT if "Dopamine's" in read(s)]
        self.assertGreaterEqual(len(adapted), 5)
        for source in adapted:
            self.assertIn("Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).",
                          read(source), source.name)
        self.assertIn('Bundle.main.path(forResource: "LICENSE-Dopamine", ofType: "txt")', read(WINDOWS))
        # The only picture is the title's, in Speaka's own asset catalog once it's drawn. Never Dopamine's or Caz's.
        named = set()
        for source in SWIFT:
            for pair in re.findall(r'\bImage\("([^"]+)"\)|UIImage\(named: "([^"]+)"\)', read(source)):
                named |= {name for name in pair if name}
        self.assertEqual(named, {"Wordmark"})

    def test_wordmark_falls_back_until_the_picture_exists(self):
        parts = read(PARTS)
        self.assertIn('if let image = UIImage(named: "Wordmark") {', parts)
        self.assertIn('return Text("Speaka")', parts)
        script = read(APP / "scripts/make_wordmark.py")
        self.assertIn('parser.add_argument("--text", default="Speaka")', script)
        self.assertIn('"Assets.xcassets", "Wordmark.imageset"', script)
        wordmark = APP / "Speaka/Assets.xcassets/Wordmark.imageset"
        if wordmark.exists():                                   # drawn in Fonarto by app/scripts/make_wordmark.py
            for name, height in (("Wordmark.png", 40), ("Wordmark@2x.png", 80), ("Wordmark@3x.png", 120)):
                self.assertEqual(png_size(wordmark / name)[1], height, name)

    # ------------------------------------------------------------------ build
    def test_project_builds_like_caz(self):
        project = read(APP / "project.yml")
        for line in ("name: Speaka", "  Speaka:\n    type: application\n    platform: iOS",
                     "      - path: Speaka\n", "      - path: Resources\n        excludes:\n          - Info.plist",
                     "PRODUCT_NAME: Speaka", "PRODUCT_BUNDLE_IDENTIFIER: com.parv.speaka",
                     "INFOPLIST_FILE: Resources/Info.plist", "SWIFT_OBJC_BRIDGING_HEADER: Speaka/Speaka-Bridging-Header.h",
                     'HEADER_SEARCH_PATHS: "$(SRCROOT)/.."', 'IPHONEOS_DEPLOYMENT_TARGET: "16.0"',
                     'CODE_SIGNING_ALLOWED: "NO"'):
            self.assertIn(line, project)
        self.assertTrue((APP / "Resources/Info.plist").is_file())
        self.assertTrue((ROOT / "BTPSShared.h").is_file())       # what HEADER_SEARCH_PATHS is for
        bridge = read(APP / "Speaka/Speaka-Bridging-Header.h")
        imported = re.findall(r'#import "([^"]+)"', bridge)
        self.assertEqual(imported, ["Model/SPTweakClient.h", "Model/SPConnectedDevice.h", "Model/SPRootSpawn.h"])
        for name in imported:
            self.assertTrue((APP / "Speaka" / name).is_file(), name)
        make = read(APP / "Makefile")
        self.assertIn("tipa: app helper\n\tbash scripts/package.sh", make)
        helper_rule = make.split("\nhelper:\n", 1)[1].split("\n\n", 1)[0]
        for flag in ("xcrun --sdk iphoneos clang -arch arm64 -mios-version-min=16.0", "-framework CoreFoundation",
                     "-o build/speakahelper RootHelper/main.c"):
            self.assertIn(flag, helper_rule)
        self.assertIn('MARKETING_VERSION="$(VERSION)" CURRENT_PROJECT_VERSION="$(BUILD)"', make)
        self.assertIn("VERSION ?= $(shell sed -n 's/^Version:[[:space:]]*//p' ../control | head -n 1)", make)
        self.assertIn("ipa: tipa", make)
        app_rule = make.split("\napp:\n", 1)[1].split("\n\n", 1)[0]
        self.assertLess(app_rule.index("xcodegen generate"), app_rule.index("xcodebuild"))
        for flag in ("-project Speaka.xcodeproj", "-scheme Speaka", "-configuration Release", "-sdk iphoneos",
                     "-derivedDataPath $(DERIVED)", "CODE_SIGNING_ALLOWED=NO"):
            self.assertIn(flag, app_rule)
        self.assertIn("DERIVED = build/DerivedData", make)
        for line in make.splitlines():                           # make needs tabs before recipe lines
            if line.startswith(("xcode", "bash", "rm ")):
                self.fail(f"recipe line without a tab: {line}")
        package = read(APP / "scripts/package.sh")
        self.assertIn('APP="build/DerivedData/Build/Products/Release-iphoneos/Speaka.app"', package)
        self.assertIn('ldid -SSpeaka.entitlements "$APP/Speaka"', package)
        self.assertIn('cp build/speakahelper "$APP/speakahelper"', package)
        self.assertIn('ldid -SRootHelper/entitlements.plist "$APP/speakahelper"', package)
        # The app starts the helper by this name, from its own bundle.
        self.assertIn('Bundle.main.bundlePath + "/speakahelper"', read(UPDATER))
        # The .ipa is the same package under its other name, made after the .tipa.
        self.assertLess(package.index("zip -qry ../packages/Speaka.tipa Payload"),
                        package.index("cp packages/Speaka.tipa packages/Speaka.ipa"))
        # XcodeGen compiles everything under app/Speaka, and clang the helper in app/RootHelper: nothing else in app/.
        for path in APP.rglob("*"):
            if path.suffix in {".swift", ".m", ".h", ".c"}:
                self.assertIn(path.relative_to(APP).parts[0], {"Speaka", "RootHelper"}, path)
        self.assertEqual(sorted(p.name for p in (APP / "RootHelper").iterdir()), ["entitlements.plist", "main.c"])

    def test_leftovers_are_gone(self):
        """Copying a new version over a clone doesn't delete files, so name what earlier versions left behind."""
        leftovers = [path.relative_to(ROOT).as_posix() for pattern in ("UI", "main.m", "SPAppDelegate.*", "SPSceneDelegate.*")
                     for path in (APP / "Speaka").glob(pattern)]
        self.assertEqual(leftovers, [], "left over from the UIKit app (v9.1): delete these in your clone")
        backdrop = APP / "Speaka/Assets.xcassets/Backdrop.imageset"
        self.assertFalse(backdrop.exists(), "app/Speaka/Assets.xcassets/Backdrop.imageset is Caz's yellow backdrop, "
                                            "left over from v9.2: delete it in your clone")
        project = read(APP / "project.yml")                     # and even if they linger, they're never built
        for pattern in ('"UI/**"', "main.m", '"SPAppDelegate.*"', '"SPSceneDelegate.*"'):
            self.assertIn(f"          - {pattern}\n", project)

    def test_workflow_builds_the_app_without_risking_the_tweak(self):
        workflow = read(ROOT / ".github/workflows/build.yml")
        app_job = workflow.split("\n  app:\n", 1)[1].split("\n  release:\n", 1)[0]
        self.assertIn("brew install xcodegen ldid", app_job)
        self.assertIn("bash tools/release_version.sh", app_job)
        self.assertIn('make -C app clean tipa VERSION="${{ steps.version.outputs.version }}" BUILD="$GITHUB_RUN_NUMBER"',
                      app_job)
        self.assertNotIn("theos", app_job.lower())
        self.assertIn("name: Speaka-app", app_job)
        upload = app_job.split("actions/upload-artifact", 1)[1]
        self.assertIn("app/packages/Speaka.tipa", upload)
        self.assertIn("app/packages/Speaka.ipa", upload)
        self.assertIn('cmp "$tipa" app/packages/Speaka.ipa', app_job)       # both names, one package
        self.assertIn("for f in Speaka speakahelper Info.plist", app_job)
        for entitlement in ("com.apple.private.security.no-sandbox", "com.apple.private.persona-mgmt"):
            self.assertIn(entitlement, app_job)
        self.assertIn('ldid -e "$bundle/speakahelper"', app_job)
        self.assertIn('[ "$shown" = "${{ steps.version.outputs.version }}" ]', app_job)   # the app says the release's number
        build_job = workflow.split("\n  build:\n", 1)[1].split("\n  app:\n", 1)[0]
        self.assertIn('version="$(bash tools/release_version.sh)"', build_job)
        release = workflow.split("\n  release:\n", 1)[1]
        self.assertIn("needs: [build, app]\n", release)
        # The release is gated on the tweak only: an app failure must never hold back the .deb.
        gate = next(line for line in release.splitlines() if line.startswith("    if:"))
        self.assertIn("!cancelled()", gate)
        self.assertIn("needs.build.result == 'success'", gate)
        self.assertNotIn("needs.app", gate)
        add_app = release.split("- name: Add the Speaka app\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("if: needs.app.result == 'success'", add_app)
        self.assertIn("name: Speaka-app", add_app)
        sums = release.split("- name: Checksums for everything published\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("files=( *.deb *.tipa *.ipa )", sums)
        self.assertIn("shopt -s nullglob", sums)                       # no .tipa must not break the checksums
        self.assertLess(release.index("- name: Checksums for everything published"), release.index("- name: Publish"))
        # Normal releases, which the updater reads: v<version>, the latest, never a pre-release, notes from the commits.
        publish = release.split("- name: Publish\n", 1)[1]
        self.assertIn('tag="v${VERSION}"', publish)
        self.assertIn("--latest", publish)
        self.assertNotIn("--prerelease", publish)
        self.assertIn("COMMITS_JSON: ${{ toJSON(github.event.commits) }}", publish)
        self.assertIn('notes="$(jq -r \'.[]? | "- " + (.message | split("\\n")[0])\'', publish)
        self.assertIn("gh release delete nightly --cleanup-tag --yes", publish)   # the old rolling build is retired
        self.assertIn('if [ -f dist/Speaka.tipa ] && [ -f dist/Speaka.ipa ]; then', publish)
        # GitHub renders "<file>" as an HTML tag and drops it.
        self.assertNotIn("<file>", release)

    def test_release_versions(self):
        """One number for the tweak and the app: control's Version plus the run number, or a pushed tag's."""
        def version(**env):
            run = subprocess.run(["bash", str(ROOT / "tools/release_version.sh")], capture_output=True, text=True,
                                 env={**{k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}, **env})
            return run.returncode, run.stdout.strip()
        base = re.search(r"^Version: (\S+)$", read(ROOT / "control"), re.M).group(1)
        self.assertRegex(base, r"^\d+\.\d+$")
        self.assertEqual(version(GITHUB_RUN_NUMBER="57"), (0, f"{base}.57"))
        self.assertEqual(version(GITHUB_REF="refs/tags/v9.4.0", GITHUB_REF_NAME="v9.4.0", GITHUB_RUN_NUMBER="58"),
                         (0, "9.4.0"))
        self.assertEqual(version(GITHUB_REF="refs/tags/vbeta", GITHUB_REF_NAME="vbeta")[0], 1)
        self.assertEqual(version(GITHUB_REF="refs/heads/main", GITHUB_RUN_NUMBER="3"), (0, f"{base}.3"))

    def test_everything_is_called_speaka(self):
        control = read(ROOT / "control")
        self.assertIn("Package: com.parv.speaka\n", control)
        self.assertIn("Name: Speaka\n", control)
        self.assertIn("TWEAK_NAME = Speaka\n", read(ROOT / "Makefile"))
        self.assertTrue((ROOT / "Speaka.plist").is_file())                   # the injection filter for Speaka.dylib
        entry = plistlib.loads((ROOT / "layout/Library/PreferenceLoader/Preferences/SpeakaPrefs.plist").read_bytes())
        self.assertEqual(entry["entry"]["label"], "Speaka")
        self.assertEqual(entry["entry"]["bundle"], "SpeakaPrefs")
        self.assertIn("BUNDLE_NAME = SpeakaPrefs", read(ROOT / "speakaprefs/Makefile"))
        root = plistlib.loads((ROOT / "speakaprefs/Resources/Root.plist").read_bytes())
        self.assertEqual(root["title"], "Speaka")
        job = plistlib.loads((ROOT / "layout/Library/LaunchDaemons/com.parv.speaka.audio.plist").read_bytes())
        self.assertEqual(job["Label"], "com.parv.speaka.audio")
        for name, pixels in (("icon.png", 29), ("icon@2x.png", 58), ("icon@3x.png", 87)):
            self.assertEqual(png_size(ROOT / "speakaprefs/Resources" / name), (pixels, pixels))
        for line in read(ROOT / "layout/DEBIAN/postinst").splitlines():
            if line.strip().startswith("echo '"):
                self.assertTrue(line.strip().startswith("echo 'Speaka: "), line)
        workflow = read(ROOT / ".github/workflows/build.yml")
        self.assertIn('cp "$deb" "dist/Speaka_${name#*_}"', workflow)
        self.assertEqual(workflow.count("name: Speaka-deb\n"), 2)            # uploaded by build, downloaded by release

    def test_the_former_name_appears_nowhere(self):
        """Neither file contents nor paths contain the project's former name. Only its SHA-256 is stored here, so this
        file does not contain it either: every 11-letter run that starts with b and ends with k is checked."""
        digest = "40fe101ac2bc16ab238f8426f9059ef2a21ad8b0067acb562d40a7e2b65c6d90"
        def hits(text):
            text = text.lower()
            for i in range(len(text) - 10):
                if text[i] == "b" and text[i + 10] == "k" and \
                        hashlib.sha256(text[i:i + 11].encode()).hexdigest() == digest:
                    yield i
        skip = {".git", "__pycache__", ".theos", "packages", "obj", "build"}
        for path in ROOT.rglob("*"):
            rel = path.relative_to(ROOT)
            if skip.intersection(rel.parts):
                continue
            self.assertFalse(any(hits(rel.as_posix())), f"path {rel}")
            if path.is_file():
                data = path.read_bytes()
                text = data.decode("latin-1")                            # binary-safe; the name is plain ASCII
                found = next(hits(text), None)
                self.assertIsNone(found, f"{rel}: the former name at offset {found}")

    # ------------------------------------------------------------------ structure
    def test_objective_c_is_structurally_sound(self):
        include_dirs = [APP / d for d in ("Speaka", "Speaka/Model")] + [ROOT]
        for source in OBJC:
            raw = read(source)
            code = strip_comments_and_strings(raw)
            depth = {"(": 0, "[": 0, "{": 0}
            closing = {")": "(", "]": "[", "}": "{"}
            for ch in code:
                if ch in depth:
                    depth[ch] += 1
                elif ch in closing:
                    depth[closing[ch]] -= 1
                    self.assertGreaterEqual(depth[closing[ch]], 0, f"{source.name}: unbalanced {ch}")
            self.assertEqual(depth, {"(": 0, "[": 0, "{": 0}, source.name)
            opens = len(re.findall(r"^\s*@(?:interface|implementation|protocol)\b(?!\s+\w+\s*;)", code, re.M))
            self.assertEqual(opens, len(re.findall(r"^\s*@end\b", code, re.M)), source.name)
            for name in re.findall(r'^\s*#\s*(?:import|include)\s+"([^"]+)"', raw, re.M):
                self.assertTrue(any((d / name).exists() for d in [source.parent] + include_dirs),
                                f"{source.name}: #import \"{name}\" does not resolve")
            if source.suffix == ".m" and source.with_suffix(".h").exists():
                self.assertIn(f'#import "{source.with_suffix(".h").name}"', raw, source.name)

    def test_swift_is_structurally_sound(self):
        defined = set()
        for source in SWIFT:
            raw = read(source)
            code = strip_comments_and_strings(raw)
            depth = {"(": 0, "[": 0, "{": 0}
            closing = {")": "(", "]": "[", "}": "{"}
            for ch in code:
                if ch in depth:
                    depth[ch] += 1
                elif ch in closing:
                    depth[closing[ch]] -= 1
                    self.assertGreaterEqual(depth[closing[ch]], 0, f"{source.name}: unbalanced {ch}")
            self.assertEqual(depth, {"(": 0, "[": 0, "{": 0}, source.name)
            defined |= set(re.findall(r"\b(?:struct|class|enum|protocol|typealias)\s+([A-Z]\w*)", code))
            imports = set(re.findall(r"^import (\w+)", raw, re.M))
            # What each framework provides, so no file uses it without importing it.
            for pattern, frameworks in ((r"\bObservableObject\b|@Published", {"Combine"}),
                                        (r"\bAVRoutePickerView\b", {"AVKit"}),
                                        (r"\bUI(?:Impact|Notification)FeedbackGenerator\b|\bUIDevice\b|\bUIImage\b|"
                                         r"\bUIControl\b|\bUIApplication\b|\bUIWindow", {"UIKit"}),
                                        (r"\bCTFont", {"CoreText"}),
                                        (r"\bView\b|\bColor\b|\bText\(", {"SwiftUI"})):
                if re.search(pattern, code):
                    self.assertTrue(imports & frameworks, f"{source.name} uses {pattern} without importing {frameworks}")
            # Every view has a body.
            for match in re.finditer(r"struct (\w+)(?:<[^>]+>)?: View \{", code):
                self.assertRegex(block(code[match.start():], match.group(0)), r"var body: some View", match.group(1))
        # Every capitalised name is defined here, bridged from Objective-C, or one of the framework names below.
        bridged = set()
        headers = read(CLIENT_H) + read(DEVICE_H) + read(APP / "Speaka/Model/SPRootSpawn.h")
        for pair in re.findall(r"@interface (\w+)|NS_ENUM\(NSInteger, (\w+)\)", headers):
            bridged |= {name for name in pair if name}
        bridged |= set(re.findall(r"^int (\w+)\(", headers, re.M))     # C functions, such as SpeakaSpawnRoot
        self.assertIn("SpeakaSpawnRoot", bridged)
        framework = {
            # Swift and Foundation
            "Bool", "CGFloat", "CGRect", "CGSize", "Date", "Double", "Int", "String", "UUID", "URL", "Array", "Self", "Void",
            "Int32", "UInt32", "Equatable", "Identifiable", "Decodable", "NSString", "Data", "AttributedString", "Bundle",
            "FileManager", "Timer", "RunLoop", "DispatchQueue", "URLRequest", "URLSession", "URLSessionDownloadTask",
            "HTTPURLResponse", "JSONDecoder",
            # CoreGraphics
            "CGPoint", "CGContext", "CGColorSpace", "CGGradient", "CGColorSpaceCreateDeviceRGB",
            # SwiftUI and Combine
            "App", "Scene", "WindowGroup", "View", "ViewBuilder", "Content", "Context", "Text", "Image", "Font",
            "Color", "Button", "ButtonStyle", "Configuration", "VStack", "HStack", "ZStack", "Spacer", "ForEach",
            "List", "Section", "ScrollView", "GeometryReader", "Capsule", "Circle", "Rectangle", "RoundedRectangle",
            "LinearGradient", "StrokeStyle", "TimelineView", "EdgeInsets", "Namespace", "State", "StateObject",
            "ObservedObject", "Environment", "Published", "ObservableObject", "UIViewRepresentable",
            # UIKit, CoreText and AVKit
            "UIFont", "UIImage", "UIDevice", "UIApplication", "UIWindow", "UIWindowScene", "UIView", "UIControl", "UIColor",
            "UIGraphicsImageRenderer", "UIGraphicsImageRendererFormat",
            "UIImpactFeedbackGenerator", "UINotificationFeedbackGenerator", "CTFont", "CTFontDescriptor", "CFURL",
            "CTFontManagerRegisterFontsForURL", "CTFontManagerCreateFontDescriptorsFromURL",
            "CTFontDescriptorCopyAttribute", "AVRoutePickerView",
        }
        code = "".join(strip_comments_and_strings(read(s)) for s in SWIFT)
        code = re.sub(r"^import \w+$", "", code, flags=re.M)
        used = set(re.findall(r"(?<![.\w])([A-Z][A-Za-z0-9]*)\b", code))
        self.assertEqual(used - defined - bridged - framework, set(),
                         "names that are neither defined here, bridged from Objective-C, nor listed as framework names")
        # Every model and updater member the views use exists.
        for owner, path, type_name in (("model", MODEL, "SpeakaModel"), ("updater", UPDATER, "SpeakaUpdater")):
            members = set(re.findall(r"\b(?:var|let|func|case)\s+(\w+)", strip_comments_and_strings(read(path))))
            for source in SWIFT:
                if source == path:
                    continue
                code = strip_comments_and_strings(read(source))
                for name in re.findall(rf"\b{owner}\.(\w+)", code):
                    self.assertIn(name, members, f"{source.name} uses {owner}.{name}, which {type_name} doesn't have")
                for name in re.findall(rf"\b{type_name}\.(\w+)", code):
                    self.assertIn(name, members | defined, f"{source.name} uses {type_name}.{name}")


if __name__ == "__main__":
    unittest.main()
