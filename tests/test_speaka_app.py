"""The Speaka companion app (app/): a SwiftUI app in Caz's style. It must speak exactly the tweak's protocol, agree
with the Settings page, keep Speaka's own icon, and package correctly for TrollStore.

Swift is first compiled by CI (there is no Apple compiler on the host), so the structural checks below are the only
ones that run before that: balanced code, imports, every type and model member that is used being defined, and every
Objective-C method being called by the Swift name its header pins down."""
import hashlib
import pathlib
import plistlib
import re
import struct
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
CLIENT_H = APP / "Speaka/Model/SPTweakClient.h"
CLIENT_M = APP / "Speaka/Model/SPTweakClient.m"
DEVICE_H = APP / "Speaka/Model/SPConnectedDevice.h"

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
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return re.sub(r'@?"(?:\\.|[^"\\\n])*"', '""', src)


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

    # ------------------------------------------------------------------ Caz's look
    def test_the_look_is_cazs(self):
        theme = read(THEME)
        for line in ("static let menu = Color(red: 63 / 255.0, green: 63 / 255.0, blue: 63 / 255.0).opacity(114 / 255.0)",
                     "static let window = Color(red: 76 / 255.0, green: 76 / 255.0, blue: 76 / 255.0).opacity(153 / 255.0)",
                     "static let accent = Color(red: 255 / 255.0, green: 178 / 255.0, blue: 30 / 255.0)",
                     "static let separator = Color(white: 0.6).opacity(0.3)",
                     "static let secondary = Color.white.opacity(0.8)",
                     "static let foreground = Color.white.opacity(0.75)",
                     "static let failure = Color(red: 1.0, green: 0.62, blue: 0.56)",
                     "static let padding: CGFloat = 30", "static let innerPadding: CGFloat = 20",
                     "static let innerPaddingTiny: CGFloat = 10", "static let innerTopPadding: CGFloat = 5",
                     "if isSmall(height) { return 52 }", "return isHomeButtonDevice ? 65 : 73",
                     ".opacity(configuration.isPressed ? 0.45 : 1)",
                     ".animation(.easeOut(duration: 0.15), value: configuration.isPressed)",
                     'Image("Backdrop")', ".opacity(dimmed ? 0.4 : 1)"):
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
        backdrop = APP / "Speaka/Assets.xcassets/Backdrop.imageset"
        self.assertEqual(jpeg_size(backdrop / "Backdrop.jpg"), (1290, 2796))    # Caz's Sunshine Sweep
        self.assertIn('"filename" : "Backdrop.jpg"', read(backdrop / "Contents.json"))

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
        self.assertRegex(info["CFBundleShortVersionString"], r"^\d+\.\d+$")
        self.assertIn("UILaunchScreen", info)
        self.assertEqual(info["UIUserInterfaceStyle"], "Dark")
        # SwiftUI's own app lifecycle: no scene delegate class to name, and the minimum iOS comes from project.yml.
        self.assertEqual(info["UIApplicationSceneManifest"], {"UIApplicationSupportsMultipleScenes": False})
        self.assertNotIn("MinimumOSVersion", info)
        self.assertIn("@main\nstruct SpeakaApp: App {", read(APP / "Speaka/SpeakaApp.swift"))

    def test_entitlements_are_only_what_the_app_needs(self):
        entitlements = plistlib.loads((APP / "Speaka.entitlements").read_bytes())
        self.assertEqual(entitlements, {"platform-application": True, "com.apple.private.security.no-sandbox": True})

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
        # The only pictures are Speaka's own asset catalog entries: Caz's backdrop and the wordmark. Never Dopamine's.
        named = set()
        for source in SWIFT:
            for pair in re.findall(r'\bImage\("([^"]+)"\)|UIImage\(named: "([^"]+)"\)', read(source)):
                named |= {name for name in pair if name}
        self.assertEqual(named, {"Backdrop", "Wordmark"})

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
        self.assertEqual(imported, ["Model/SPTweakClient.h", "Model/SPConnectedDevice.h"])
        for name in imported:
            self.assertTrue((APP / "Speaka" / name).is_file(), name)
        make = read(APP / "Makefile")
        self.assertIn("tipa: app\n\tbash scripts/package.sh", make)
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
        # The .ipa is the same package under its other name, made after the .tipa.
        self.assertLess(package.index("zip -qry ../packages/Speaka.tipa Payload"),
                        package.index("cp packages/Speaka.tipa packages/Speaka.ipa"))
        # XcodeGen compiles everything under app/Speaka: no source may live anywhere else in app/.
        for path in APP.rglob("*"):
            if path.suffix in {".swift", ".m", ".h"}:
                self.assertEqual(path.relative_to(APP).parts[0], "Speaka", path)

    def test_the_uikit_app_is_gone(self):
        """v9.2 replaced the UIKit app. Copying a new version over a clone doesn't delete files, so name the leftovers."""
        leftovers = [path.relative_to(ROOT).as_posix() for pattern in ("UI", "main.m", "SPAppDelegate.*", "SPSceneDelegate.*")
                     for path in (APP / "Speaka").glob(pattern)]
        self.assertEqual(leftovers, [], "left over from the UIKit app (v9.1): delete these in your clone")
        project = read(APP / "project.yml")                     # and even if they linger, they're never built
        for pattern in ('"UI/**"', "main.m", '"SPAppDelegate.*"', '"SPSceneDelegate.*"'):
            self.assertIn(f"          - {pattern}\n", project)

    def test_workflow_builds_the_app_without_risking_the_tweak(self):
        workflow = read(ROOT / ".github/workflows/build.yml")
        app_job = workflow.split("\n  app:\n", 1)[1].split("\n  release:\n", 1)[0]
        self.assertIn("brew install xcodegen ldid", app_job)
        self.assertIn("make -C app clean tipa", app_job)
        self.assertNotIn("theos", app_job.lower())
        self.assertIn("name: Speaka-app", app_job)
        upload = app_job.split("actions/upload-artifact", 1)[1]
        self.assertIn("app/packages/Speaka.tipa", upload)
        self.assertIn("app/packages/Speaka.ipa", upload)
        self.assertIn('cmp "$tipa" app/packages/Speaka.ipa', app_job)       # both names, one package
        self.assertIn("com.apple.private.security.no-sandbox", app_job)
        self.assertIn("Assets.car", app_job)                                # the backdrop's asset catalog
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
        # GitHub renders "<file>" as an HTML tag and drops it: the notes must name the real package.
        self.assertNotIn("<file>", release)
        self.assertIn("sudo dpkg -i ${deb_name}", release)
        self.assertIn('if [ -f dist/Speaka.tipa ] && [ -f dist/Speaka.ipa ]; then', release)

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
        for pair in re.findall(r"@interface (\w+)|NS_ENUM\(NSInteger, (\w+)\)", read(CLIENT_H) + read(DEVICE_H)):
            bridged |= {name for name in pair if name}
        framework = {
            # Swift and Foundation
            "Bool", "CGFloat", "CGRect", "CGSize", "Date", "Double", "Int", "String", "UUID", "URL", "Array", "Self", "Void",
            "Equatable", "Identifiable", "NSString", "Bundle", "FileManager", "Timer", "RunLoop", "DispatchQueue",
            # SwiftUI and Combine
            "App", "Scene", "WindowGroup", "View", "ViewBuilder", "Content", "Context", "Text", "Image", "Font",
            "Color", "Button", "ButtonStyle", "Configuration", "VStack", "HStack", "ZStack", "Spacer", "ForEach",
            "List", "Section", "ScrollView", "GeometryReader", "Capsule", "Circle", "Rectangle", "RoundedRectangle",
            "LinearGradient", "StrokeStyle", "TimelineView", "EdgeInsets", "Namespace", "State", "StateObject",
            "ObservedObject", "Environment", "Published", "ObservableObject", "UIViewRepresentable",
            # UIKit, CoreText and AVKit
            "UIFont", "UIImage", "UIDevice", "UIApplication", "UIWindow", "UIWindowScene", "UIView", "UIControl",
            "UIImpactFeedbackGenerator", "UINotificationFeedbackGenerator", "CTFont", "CTFontDescriptor", "CFURL",
            "CTFontManagerRegisterFontsForURL", "CTFontManagerCreateFontDescriptorsFromURL",
            "CTFontDescriptorCopyAttribute", "AVRoutePickerView",
        }
        code = "".join(strip_comments_and_strings(read(s)) for s in SWIFT)
        code = re.sub(r"^import \w+$", "", code, flags=re.M)
        used = set(re.findall(r"(?<![.\w])([A-Z][A-Za-z0-9]*)\b", code))
        self.assertEqual(used - defined - bridged - framework, set(),
                         "names that are neither defined here, bridged from Objective-C, nor listed as framework names")
        # Every model member the views use exists in SpeakaModel.
        model = strip_comments_and_strings(read(MODEL))
        members = set(re.findall(r"\b(?:var|let|func)\s+(\w+)", model))
        for source in SWIFT:
            if source == MODEL:
                continue
            code = strip_comments_and_strings(read(source))
            for name in re.findall(r"\bmodel\.(\w+)", code):
                self.assertIn(name, members, f"{source.name} uses model.{name}, which SpeakaModel doesn't have")
            for name in re.findall(r"\bSpeakaModel\.(\w+)", code):
                self.assertIn(name, members | defined, f"{source.name} uses SpeakaModel.{name}")


if __name__ == "__main__":
    unittest.main()
