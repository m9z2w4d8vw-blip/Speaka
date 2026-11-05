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
STEPS = APP / "Speaka/SessionSteps.swift"
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

    def test_output_modes_match_the_helper(self):
        """The app's three outputs are the ones the helper understands (Settings no longer lists them, v9.7)."""
        client = read(CLIENT_M)
        modes = client.split("+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes", 1)[1].split("];", 1)[0]
        keys = re.findall(r'@"key": @"([^"]+)"', modes)
        self.assertEqual(keys, ["speaker", "headphones", "any"])
        self.assertEqual(len(re.findall(r'@"title": @"([^"]+)"', modes)), 3)
        self.assertEqual(len(re.findall(r'@"shortTitle": @"([^"]+)"', modes)), 3)
        helper = read(ROOT / "btpsaudiohelper/BTPSV89Output.h")
        self.assertIn('return mode == BTPS_V89_MODE_HEADPHONES ? "headphones" : mode == BTPS_V89_MODE_ANY ? "any" : "speaker";',
                      helper)
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
                                  "connectedComputerName", "pairedComputerName", "lookupSummary",
                                  "updateNotificationsEnabled", "setUpdateNotificationsEnabled", "notifiedVersion",
                                  "setNotifiedVersion", "removeLeftoverPreferences"})
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
        properties = set(re.findall(r"@property \([^)]*\) [\w <>*,]+?\*?(\w+);", read(CLIENT_H)))
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
        self.assertIn("guard request == nil, !uninstalling else { return false }", press)   # v9.8: not while uninstalling
        self.assertIn("case .noTweak, .oldTweak:\n            return false", press)

        # Tapping opens the log first, then sends the request; each step of the restart gets a line.
        toggle = block(model, "func toggle() {")
        self.assertIn('let action = buttonStops ? "stop" : "start"', toggle)
        self.assertLess(toggle.index("sheetVisible = true"), toggle.index("client.sendAction(action, error: &error)"))
        self.assertIn('startLyric("Restarting Bluetooth")', toggle)
        relaunched = block(model, "private func relaunched(_ pending: SPRequest) {")
        self.assertIn('finishLyric("Bluetooth restarted")', relaunched)
        self.assertIn('steps = pending.action == "start" ? SessionStep.start : SessionStep.stop', relaunched)
        self.assertIn("startLyric(steps[0].running)", relaunched)
        # After the restart the model reads the tweak's statuses itself, one step per poll.
        tick = block(model, "private func tick() {")
        self.assertLess(tick.index("refresh()"), tick.index("advance(pending)"))
        self.assertLess(tick.index("advance(pending)"), tick.index("client.pollRequest(pending)"))
        self.assertIn("relaunched(pending)", tick)
        self.assertIn("self.status = status.diagnostics.compactMapValues { $0 as? String }", model)
        advance = block(model, "private func advance(_ pending: SPRequest) {")
        for line in ("switch step.check(status) {", "finishLyric(step.done)", "startLyric(steps[stepIndex].running)",
                     "complete(pending)", 'stop(line: step.failed, problem: "\\(step.failed): \\(reason)")',
                     "Date().timeIntervalSince(pending.started) > SessionStep.timeout"):
            self.assertIn(line, advance)
        self.assertEqual(advance.count("stepIndex += 1"), 1)   # at most one step per poll
        complete = block(model, "private func complete(_ pending: SPRequest) {")
        self.assertIn('finishLyrics(success: "Ready for your PC", symbol: "hifispeaker")', complete)
        self.assertIn('finishLyrics(success: "Bluetooth back to normal"', complete)
        self.assertIn("collapseSoon()", complete)
        # A failed log stays up long enough to read which step failed.
        self.assertIn("collapseSoon(after: Self.failedLogSeconds)", block(model, "private func stop(line: String, problem: String) {"))
        self.assertIn("static let failedLogSeconds: TimeInterval = 6", model)
        # Before the restart, a failure is told apart by SPTweakClient's own result titles.
        failure = block(model, "static func failure(for pending: SPRequest) -> (line: String, problem: String) {")
        titles = set(re.findall(r'resultTitle == "([^"]+)"', failure))
        self.assertEqual(titles, {"Still waiting"})
        for title in titles:
            self.assertIn(f'title:@"{title}"', read(CLIENT_M))
        self.assertEqual(re.search(r"static let timeout: TimeInterval = (\d+)", read(STEPS)).group(1), "45")
        self.assertIn("elapsed > (request.relaunched ? 45.0 : 25.0)", read(CLIENT_M))

    def test_start_steps_read_what_the_tweak_publishes(self):
        """Every status key and prefix a step reads is one the tweak really writes, so the log can't drift from it."""
        steps = read(STEPS)
        start = re.search(r"static let start: \[SessionStep\] = \[([^\]]+)\]", steps).group(1)
        self.assertEqual([n.strip() for n in start.split(",")],
                         ["arm", "hooks", "endpoints", "room", "audioPath", "speaker", "advertising"])
        self.assertIn("static let stop: [SessionStep] = [switchOff]", steps)
        tweak = read(ROOT / "Tweak.xm") + read(ROOT / "BTPSV89Session.c")
        # Statuses as the tweak writes them: the key, and the format's fixed text before its first conversion.
        written = {}
        for call in re.finditer(r'BTPSSetStatus\(\s*"([\w.]+)"\s*,(.*?)\);', tweak, re.S):
            written.setdefault(call.group(1), []).extend(re.findall(r'"([^"]*)"', call.group(2)))
        written.setdefault("diag.v89.session", []).append("%s nonce=%s pid=%d result=%s")
        keys = set(re.findall(r'status\["([\w.]+)"', steps)) | set(re.findall(r'\("(diag\.[\w.]+)", \[', steps))
        self.assertGreaterEqual(len(keys), 12)
        for key in keys:
            self.assertIn(key, written, f"no BTPSSetStatus writes {key}")

        def matches(prefix, key):
            for fmt in written[key]:
                fixed = fmt.split("%", 1)[0]
                if fixed.startswith(prefix) or (prefix.startswith(fixed) and "%" in fmt):
                    return True
            return False

        # Prefixes each step checks, by key.
        checks = {
            "diag.v82.trial": ["ARMED", "REFUSED", "OFF"],
            "diag.v81_seven_slots": ["ONE SHOT result: live_slots=", "ONE SHOT result:", "REFUSED"],
            "diag.v80_config_lookup": ["ARMED", "OBSERVED", "NO_INDEX3"],
            "diag.v82.rx": ["installed"], "diag.v82.tx": ["installed", "TX capture active"],
            "diag.v86.media": ["V87 ARMED"], "diag.v882.idle_guard": ["v8.8.2"], "diag.sdp_probe": ["installed"],
            "diag.audio_receive": ["v8.8 callback ready", "REFUSED"],
            "diag.v83.audio_table": ["WAITING", "REFUSED", "ROLLED BACK", "UNSAFE", "FAILED", "SDP publish failed",
                                     "REFUSED: required", "REFUSED: registration-loop", "FAILED: registration-loop"],
            "diag.v82.sep": ["LOCAL register=0 SEID=", "LOST"],
            "diag.v82.sdp": ["LOCAL add="],
        }
        for key, prefixes in checks.items():
            for prefix in prefixes:
                self.assertTrue(matches(prefix, key), f"the tweak never writes {key} starting {prefix!r}")
            for prefix in prefixes:
                self.assertIn(f'"{prefix}', steps.replace("live_slots=7 ", "live_slots=").replace("SEID=7", "SEID="),
                              f"{prefix!r} is listed here but no step checks it")
        # The results the steps read from the session line are the ones BTPSV89SessionReport writes.
        report = read(ROOT / "BTPSV89Session.c")
        for result in ("ARMED", "REFUSED", "OFF", "STILL_ARMED"):
            self.assertIn(f'"{result}"', report)
        for result in ("result=REFUSED", "result=OFF", "result=STILL_ARMED"):
            self.assertIn(f'"{result}"', steps)
        # A zero SDP result prints as "0" (%#llx), and SEID 7 is the speaker's.
        self.assertIn('BTPSSetStatus("diag.v82.sdp", "LOCAL add=%#llx handle=%#x;', read(ROOT / "Tweak.xm"))
        self.assertIn('sdp.hasPrefix("LOCAL add=0 ")', steps)
        self.assertIn('sep.hasPrefix("LOCAL register=0 SEID=7")', steps)
        # Six native Sources: the step accepts the count the tweak writes as "%u".
        self.assertIn('BTPSSetStatus("diag.native_source_ok", "%u", sources);', read(ROOT / "Tweak.xm"))

    def test_the_tweak_reports_every_step_promptly(self):
        """Every final refusal on the way to the speaker sets a status, and statuses reach the app within a second."""
        tweak = read(ROOT / "Tweak.xm")
        attempt = block(tweak, "static void btps_v83_attempt_post_source(uint64_t audio_context)\n{")
        claim = attempt.index("__sync_bool_compare_and_swap(&g_v82_sink_attempted, 0, 1)")
        # Before the one attempt is claimed, a missing prerequisite is WAITING: the next power-on tries again.
        self.assertIn('BTPSSetStatus("diag.v83.audio_table", "WAITING: %s", missing);', attempt[:claim])
        self.assertNotRegex(attempt[:claim], r'BTPSSetStatus\([^;]*"REFUSED')
        # After it (its own line returns quietly: someone else made the attempt), every return says why.
        after = attempt[attempt.index("return;", claim) + len("return;"):].split("reject_before_swap:", 1)[0]
        stretches = after.split("return;")[:-1]
        self.assertGreaterEqual(len(stretches), 7)
        for stretch in stretches:
            self.assertIn("BTPSSetStatus(", stretch, "a return without a status, after: " + stretch[-200:])
        callback = block(tweak, "static int btps_v88_install_callback(uint64_t context)\n{")
        self.assertEqual(callback.count("return 0;"), callback.count('BTPSSetStatus("diag.audio_receive","REFUSED'))
        self.assertRegex(tweak, r'#define BTPS_V89_BUILD_TAG "v9\.6\.0-one-tap-output-select"')
        log = read(ROOT / "BTPSLog.c")
        flush = block(log, "static void schedule_flush(void)\n{")
        self.assertIn("if (!g_queue) return;", flush)
        self.assertIn("if (!__sync_lock_test_and_set(&g_flush_pending, 1))", flush)
        self.assertIn("1LL * NSEC_PER_SEC", flush)
        status = block(log, "void BTPSSetStatus(const char *key, const char *fmt, ...)\n{")
        self.assertLess(status.index("pthread_mutex_unlock(&g_lock);"), status.index("schedule_flush();"))
        common = block(log, "static void log_common(int sync, const char *fmt, va_list ap)\n{")
        self.assertIn("if (inited) schedule_flush();", common)   # BTPSLogNow too, not only BTPSLog
        self.assertEqual(log.count("dispatch_after_f(dispatch_time(DISPATCH_TIME_NOW, 1LL * NSEC_PER_SEC)"), 1)

    def test_the_log_never_moves_sideways(self):
        """The log's column is sized to every line it could show before its first, so no line shifts when a longer one
        appears or a running line shrinks (it did while "Registering Bluetooth's endpoints" ran)."""
        components = read(ROOT / "app/Speaka/MainComponents.swift")
        log = block(components, "struct LyricsLog: View {")
        self.assertIn("let texts: [LyricText]", log)
        body = block(log, "var body: some View {")
        self.assertTrue(body.lstrip().startswith("ZStack(alignment: .bottomLeading) {"), body[:80])
        hidden = body.split("ZStack(alignment: .leading) {", 1)[1].split(".hidden()", 1)[0]
        self.assertIn("ForEach(texts, id: \\.self) { text in", hidden)
        self.assertIn("LyricRow.measure(text)", hidden)
        self.assertIn("VStack(alignment: .leading, spacing: 0) {", body)
        self.assertIn("LyricsLog(lines: model.lyrics, texts: model.logTexts,", components)
        # The measuring row has the real row's geometry exactly.
        row = block(components, "struct LyricRow: View {")
        measure = block(components, "static func measure(_ text: LyricText) -> some View {")
        for shared in ("HStack(spacing: 15) {", ".frame(width: 30, height: 30)", ".lineLimit(1)", ".minimumScaleFactor(0.6)"):
            self.assertIn(shared, row)
            self.assertIn(shared, measure)
        self.assertIn(".font(.system(size: line.status == .running ? 20 : 18))", row)
        self.assertIn(".font(.system(size: text.running ? 20 : 18))", measure)
        # Start's and Stop's lines together, fixed for the model's life: both logs get the same column, so a line sits
        # in the same place in either (it didn't when each was sized from its own lines). Every line written is there.
        model = read(MODEL)
        self.assertIn("let logTexts = SpeakaModel.possibleLines()", model)
        self.assertNotIn("logTexts =", block(model, "func toggle() {"))
        possible = block(model, "static func possibleLines() -> [LyricText] {")
        self.assertIn("for step in SessionStep.start + SessionStep.stop {", possible)
        for final in ("Ready for your PC", "Bluetooth back to normal"):
            self.assertIn(f'"{final}"', possible)
        for kind in ("running", "done", "failed"):
            self.assertIn(f"step.{kind}", possible)
        written = set(re.findall(r'(?:startLyric|finishLyric)\("([^"]+)"\)', model))
        written |= set(re.findall(r'fail\("([^"]+)",', model))
        written |= set(re.findall(r'finishLyrics\(success: "([^"]+)"', model))
        failure = block(model, "static func failure(for pending: SPRequest) -> (line: String, problem: String) {")
        written |= set(re.findall(r'return \("([^"]+)",', failure))
        written |= set(re.findall(r'^\s+return \("([^"]+)",$', failure, re.M))
        self.assertGreaterEqual(len(written), 7, written)
        for text in written:
            self.assertIn(f'"{text}"', possible, f"the log can show {text!r}, but its column isn't sized for it")

    def test_the_pcs_name_shows_as_soon_as_it_connects(self):
        """The PC is named from iOS's Bluetooth list once it's connected, before any audio, whatever form its device class
        comes in; while it streams, the one paired computer names it even if iOS doesn't list it as connected."""
        source = read(ROOT / "app/Speaka/Model/SPConnectedDevice.m")
        for selector in ("sharedInstance", "available", "connectedDevices", "pairedDevices", "name", "address",
                         "majorClass", "connected"):
            self.assertIn(f"@selector({selector})", source)
        self.assertEqual(source.count("objc_msgSend)"), 3)          # every private call goes through these three
        self.assertIn("if (!target || ![target respondsToSelector:selector])", source)
        # The class in any form: index 1, masked 0x100, with the minor class 0x10C, or the whole Class of Device.
        self.assertIn("return value >= 0x100 ? (value >> 8) & 0x1F : value & 0x1F;", source)
        major = lambda v: (v >> 8) & 0x1F if v >= 0x100 else v & 0x1F
        for value in (1, 0x100, 0x10C, 0x2A010C):
            self.assertEqual(major(value), 1, hex(value))
        for value in (2, 0x200, 0x20C):
            self.assertEqual(major(value), 2, hex(value))
        self.assertEqual(major(0x240404), 4)
        self.assertNotIn("== SPMajorClassComputerMasked", source)    # v9.4's exact match, which missed the PC
        self.assertIn('[upper hasPrefix:@"DESKTOP-"] || [upper hasPrefix:@"LAPTOP-"]', source)   # Windows' own names
        connected = block(source, "+ (NSString *)connectedComputerName\n{")
        self.assertIn("if (device.computer && device.connected)", connected)
        self.assertIn("return phones == 1 ? phone.name : nil;", connected)
        self.assertNotIn("computers == 1", connected)              # being paired doesn't make it connected
        paired = block(source, "+ (NSString *)pairedComputerName\n{")
        self.assertIn("return computers == 1 ? computer.name : nil;", paired)
        for method in (connected, paired):
            self.assertIn("@catch (NSException *exception)", method)
        # Credits lists each device with its class exactly as reported, so a wrong guess shows in a screenshot.
        self.assertIn("Text(SPConnectedDevice.lookupSummary())", read(WINDOWS))
        self.assertIn('@"Bluetooth knows %lu devices: %@."', source)
        self.assertIn('@"class %#x"', source)
        # The model looks it up whenever the speaker is on, and names the PC before audio once it's connected.
        model = read(MODEL)
        lookup = model.split("// The PC's name is looked up", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("if next.session == .on {", lookup)
        self.assertNotIn("next.receiving", lookup)
        self.assertIn("Date().timeIntervalSince($0) > 3.0", lookup)
        self.assertIn("SPConnectedDevice.connectedComputerName()", lookup)
        self.assertIn("SPConnectedDevice.pairedComputerName()", lookup)
        line = block(model, "var statusLine: StatusLine? {")
        self.assertLess(line.index('text: "\\(who) · connected"'), line.index('text: "Connect from your PC"'))
        self.assertIn("let name = computerName ?? routePCName ?? (snapshot.receiving ? pairedName : nil)", line)
        self.assertLess(line.index('let who = name ?? "Your PC"'), line.index("guard snapshot.receiving else {"))

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
        # v9.5: one .tipa carries the tweak. A newer app is the only download, and its tweak comes from inside it; with
        # only the tweak behind, this app's own carried tweak is installed; a build carrying none downloads the .deb.
        start = block(updater, "func startDownload() {")
        app_part = start.index('Part(url: url, name: "Speaka-update.tipa", label: "the app")')
        self.assertLess(app_part, start.index("tweakFromApp = deb != nil"))
        self.assertLess(start.index("} else if deb != nil {"), start.index("if Self.carriedTweak != nil {"))
        self.assertLess(start.index("if Self.carriedTweak != nil {"),
                        start.index('Part(url: url, name: "Speaka-update.deb", label: "the tweak")'))
        self.assertIn('args.append("--tweak-from-app")', updater)
        self.assertIn("let withTweak = tweakFromApp || files.contains", updater)
        self.assertIn('static var carriedTweak: String? { Bundle.main.path(forResource: "Speaka", ofType: "deb") }',
                      updater)
        self.assertIn('var args = ["install-update"]', updater)
        self.assertIn('args += [file.pathExtension.lowercased() == "deb" ? "--deb" : "--tipa", file.path]', updater)
        # v9.4: no download task (its temporary file goes in the app's data folder, which v9.3 couldn't write) and no
        # cache; each file is fetched into memory and written to a folder outside it, the tweak's own as a fallback.
        self.assertIn("private static let session = URLSession(configuration: .ephemeral)", updater)
        self.assertIn("Self.session.dataTask(with: part.url)", updater)
        for gone in (r"\bdownloadTask\b", r"\bmoveItem\b", r"\.cachesDirectory\b",
                     r"URLSession\.shared\.dataTask\(with: part\.url\)"):
            self.assertNotRegex(strip_comments_and_strings(updater), gone)
        self.assertIn('static let downloadFolders = ["/var/mobile/Library/Caches/Speaka", "/tmp/FTABHarvest"]', updater)
        self.assertRegex(read(ROOT / "BTPSShared.h"), r'#define BTPS_LOG_DIR_TMP\s+"/tmp/FTABHarvest"')
        helper = read(HELPER)
        for folder in ("/var/mobile/", "/tmp/FTABHarvest/", "/private/var/tmp/FTABHarvest/"):
            self.assertIn(f'"{folder}"', helper)
        # Every failure says which step and which file, and the error's domain and code.
        self.assertIn('problem = "Downloading \\(part.label): \\(Self.describe(error))"', updater)
        self.assertIn('if file == nil { problem = "Saving \\(part.label): \\(saving.problem)" }', updater)
        self.assertIn('var text = "\\(ns.localizedDescription) (\\(ns.domain) \\(ns.code)"', updater)
        self.assertIn('Part(url: url, name: "Speaka-update.deb", label: "the tweak")', updater)
        self.assertIn('Part(url: url, name: "Speaka-update.tipa", label: "the app")', updater)
        self.assertIn('"Downloading \\(part.label), \\(partsDone + 1) of \\(partCount)"', updater)
        self.assertIn('updater.activity.isEmpty ? "Please wait while files finish downloading" : updater.activity',
                      read(UPDATE_VIEWS))
        # Only an app-only update falls back to TrollStore's link; a failed .deb is reported and nothing else happens.
        done = block(updater, "private func installed(status: Int32, errors: [String], withTweak: Bool) {")
        self.assertLess(done.index("if withTweak {"), done.index("openInTrollStore()"))
        self.assertIn('"apple-magnifier://install?url="', updater)
        self.assertIn("var resprings: Bool { deb != nil || installingTweak }", updater)
        views = read(UPDATE_VIEWS)
        self.assertIn('updater.resprings ? "Bluetooth restarts, then your iPhone resprings"', views)
        self.assertIn('"Speaka will close when it\'s done"', views)
        self.assertNotIn("Caz will", views)
        content = read(CONTENT)
        self.assertIn('UpdateLinkButton(title: updater.tweakMissing ? "Install Tweak" : "Update Available",', content)
        self.assertIn("if window == .update && updater.isBusy { return }", content)
        self.assertIn("updater.check()", content)
        # Update Available keeps Caz's place; the connection line moves up a line while it shows, so they never overlap.
        self.assertIn(".offset(y: -(30 + linkGap))\n", content)
        self.assertIn(".offset(y: -(30 + linkGap) - (updater.showsLink ? 40 : 0))", content)

    def test_a_paused_pc_stays_connected_and_named(self):
        """v9.6: pausing is still connected, and the PC is named from the audio route, where iOS shows it."""
        tweak = read(ROOT / "Tweak.xm")
        config = block(tweak, "static uint64_t hook_setconfigind(uint64_t a0,uint64_t a1,uint64_t a2,uint64_t a3,\n"
                              "    uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7)\n{")
        self.assertIn('BTPSSetStatus("diag.v95.peer","configured: the PC set up the speaker\'s stream");', config)
        signal = block(tweak, "static void btps_v88_note_signal(unsigned direction,uint64_t cid,const uint8_t *p,size_t size)\n{")
        self.assertIn('BTPSSetStatus("diag.v95.peer","%s",change==1?"streaming":(p[1]&63u)==9u?"paused":"closed");',
                      signal)
        self.assertLess(signal.index("if(change>=0) {"), signal.index('"diag.v95.peer"'))   # only our stream's
        # The parser it relies on: SUSPEND (9), CLOSE (8) and ABORT (10) give 0, an accepted START gives 1.
        parser = read(ROOT / "BTPSV88Control.c")
        self.assertIn("if(sig!=7 && sig!=8 && sig!=9 && sig!=10) return -1;", parser)
        self.assertIn("return sig==7?-1:0;", parser)
        self.assertIn("if(msg==2 && length==2) return sig==7?1:0;", parser)
        # The client: attached helper (playing, waiting or idle, i.e. paused) or the tweak's status, unless the helper's
        # session ended; the name from the helper's route, else the app's own, a Windows default name first.
        client = read(CLIENT_M)
        read_status = block(client, "- (SPStatus *)readStatus\n{")
        for text in ('[state isEqualToString:@"idle"]', '[state isEqualToString:@"stopped"]',
                     '[peer hasPrefix:@"paused"]', "s.connected = s.session == SPSessionOn && (helperAttached || (peerUp && !helperEnded));",
                     'SPBluetoothOutputName(helper[@"outputs"])', "SPBluetoothOutputName(SPOwnRouteOutputs())"):
            self.assertIn(text, read_status)
        name = block(client, "static NSString *SPBluetoothOutputName(id outputs)\n{")
        for port in ("AVAudioSessionPortBluetoothA2DP", "AVAudioSessionPortBluetoothHFP", "AVAudioSessionPortBluetoothLE"):
            self.assertIn(port, name)
        self.assertIn('if ([upper hasPrefix:@"DESKTOP-"] || [upper hasPrefix:@"LAPTOP-"]) return name;', name)
        header = read(CLIENT_H)
        self.assertIn("@property (nonatomic) BOOL connected;", header)
        self.assertIn("@property (nonatomic, copy) NSString *routeName;", header)
        # The model keeps the route's name while the PC stays connected, and says "connected" while it's paused.
        model = read(MODEL)
        keep = model.split("// The PC's name from the audio route, kept while it stays connected.", 1)[1].split("// A problem", 1)[0]
        self.assertIn("if next.session == .on && next.connected {", keep)
        self.assertIn("Self.isWindowsName(seen)", keep)
        self.assertIn("routePCName = nil", keep)
        line = block(model, "var statusLine: StatusLine? {")
        self.assertIn("if snapshot.connected || computerName != nil {", line)

    def test_settings_kills_bluetooth(self):
        """v9.6: Settings, above Audio Output, has Kill Bluetooth: the README's killall -9 bluetoothd, through the helper."""
        content = read(CONTENT)
        menu = block(content, "private var menuItems: [MenuItem] {")
        self.assertLess(menu.index('MenuItem(id: "settings", title: "Settings", symbol: "gearshape",'),
                        menu.index('MenuItem(id: "output", title: "Audio Output"'))
        self.assertIn("case settings, output, credits, update", content)
        self.assertIn("case .settings:\n            SettingsWindow(model: model)", content)
        windows = read(WINDOWS)
        settings = block(windows, "struct SettingsWindow: View {")
        self.assertIn('WindowCard(title: "Settings") {', settings)
        self.assertIn("model.killBluetooth()", settings)
        self.assertIn('model.killingBluetooth ? "Killing Bluetooth..." : "Kill Bluetooth"', settings)
        self.assertIn(".disabled(busy)", settings)                 # not during a Start or Stop
        # v9.7: just the button. A line shows only if it failed.
        for fluff in ("Stops Bluetooth at once", 'Text("Kill Bluetooth")\n'):
            self.assertNotIn(fluff, settings)
        self.assertIn("if let failure = model.killResult {", settings)
        self.assertIn('self.killResult = status == 0 ? nil :', read(MODEL))
        model = read(MODEL)
        kill = block(model, "func killBluetooth() {")
        self.assertIn("guard !killingBluetooth, request == nil else { return }", kill)
        self.assertIn('SpeakaSpawnRoot(helper, ["kill-bluetooth"])', kill)
        self.assertIn("killall -9 bluetoothd", read(ROOT / "README.md"))
        helper = read(HELPER)
        command = block(helper, "static int do_kill_bluetooth(void) {")
        self.assertIn('kill_named("bluetoothd")', command)
        self.assertIn("kill(pid, SIGKILL)", block(helper, "static int kill_named(const char *name) {"))   # as -9 does
        self.assertIn('if (strcmp(argv[1], "kill-bluetooth") == 0) return do_kill_bluetooth();', helper)

    def test_update_notifications(self):
        """v9.7: Settings > Update notifications checks GitHub every 2 minutes while Speaka is open, asks iOS for the
        same in the background, and announces each ready release (one that carries Speaka.tipa) once."""
        notes = read(APP / "Speaka/UpdateNotifications.swift")
        self.assertIn('static let taskID = "speaka.update-check"', notes)
        self.assertIn("static let interval: TimeInterval = 120", notes)
        info = plistlib.loads((APP / "Resources/Info.plist").read_bytes())
        self.assertEqual(info["UIBackgroundModes"], ["fetch"])
        self.assertEqual(info["BGTaskSchedulerPermittedIdentifiers"], ["speaka.update-check"])
        self.assertIn(".backgroundTask(.appRefresh(UpdateNotifications.taskID)) {\n"
                      "            await UpdateNotifications.backgroundCheck()", read(APP / "Speaka/SpeakaApp.swift"))
        background = block(notes, "static func backgroundCheck() async {")
        self.assertLess(background.index("UpdateNotifications.schedule()"), background.index("await UpdateNotifications.check()"))
        self.assertIn("guard SPTweakClient.updateNotificationsEnabled() else { return }", background)
        check = block(notes, "static func check() async {")
        self.assertIn("releases.filter { $0.tipaURL != nil }", check)            # ready: it carries the app
        self.assertIn("SpeakaUpdater.compare(newest.version, app) > 0", check)    # newer than this app
        self.assertIn("SPTweakClient.notifiedVersion() != newest.name", check)    # announced once
        self.assertLess(check.index("try await UNUserNotificationCenter.current().add(note)"),
                        check.index("SPTweakClient.setNotifiedVersion(newest.name)"))
        enable = block(notes, "static func setEnabled(_ on: Bool, completion: @escaping (Bool) -> Void) {")
        self.assertIn("requestAuthorization(options: [.alert, .sound])", enable)
        self.assertIn("SPTweakClient.setUpdateNotificationsEnabled(granted)", enable)
        self.assertIn("BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: UpdateNotifications.taskID)", enable)
        # While open: a timer on the updater (a timer made in a view's body would restart at every redraw).
        updater = read(UPDATER)
        self.assertIn("func check(again: Bool = false) {\n        guard !checking, again || !checked else { return }", updater)
        watch = block(updater, "func watch(_ on: Bool) {")
        self.assertIn("Timer.scheduledTimer(withTimeInterval: UpdateNotifications.interval, repeats: true)", watch)
        self.assertIn("self?.check(again: true)", watch)
        content = read(CONTENT)
        self.assertIn("updater.watch(model.updateNotifications && phase == .active)", content)
        self.assertIn("if phase == .background && model.updateNotifications {\n                UpdateNotifications.schedule()",
                      content)
        self.assertNotIn("Timer.publish", content)
        # The switch, in Settings under Kill Bluetooth; the app's own settings, stored by the client.
        settings = block(read(WINDOWS), "struct SettingsWindow: View {")
        self.assertLess(settings.index("model.killBluetooth()"),
                        settings.index('Toggle("Update notifications", isOn: Binding(get: { model.updateNotifications },'))
        self.assertIn("set: { model.setUpdateNotifications($0) }", settings)
        client = read(CLIENT_M)
        self.assertIn('static NSString *const SPUpdateNotificationsKey = @"updateNotifications";', client)
        self.assertIn("[[NSUserDefaults standardUserDefaults] boolForKey:SPUpdateNotificationsKey]", client)

    def test_one_tipa_carries_the_tweak(self):
        """v9.5: Speaka.tipa carries the release's .deb, and Install Tweak shows only while the tweak isn't installed."""
        package = read(APP / "scripts/package.sh")
        carry = package.index('cp "$DEB" "$APP/Speaka.deb"')
        self.assertLess(carry, package.index('ldid -SSpeaka.entitlements "$APP/Speaka"'))   # in the bundle before the zip
        self.assertLess(carry, package.index("zip -qry"))
        self.assertIn('rm -f "$APP/Speaka.deb"', package)                  # no DEB, no stale tweak from a last build
        makefile = read(APP / "Makefile")
        self.assertIn("DEB ?=\n", makefile)
        self.assertIn('\tDEB="$(DEB)" bash scripts/package.sh\n', makefile)
        self.assertIn('#define SPEAKA_CARRIED_DEB "Speaka.app/Speaka.deb"', read(HELPER))
        updater = read(UPDATER)
        # Install Tweak: only when dpkg doesn't list the tweak, this app carries one, and there's a jailbreak.
        refresh = block(updater, "func refreshTweak() {")
        for condition in ("Self.installedPackageVersion() == nil", "Self.carriedTweak != nil",
                          "FileManager.default.fileExists(atPath: Self.dpkgPath)"):
            self.assertIn(condition, refresh)
        self.assertIn('static let dpkgPath = "/var/jb/usr/bin/dpkg"', updater)
        install = block(updater, "func installTweak() {")
        self.assertIn("guard tweakMissing, !isBusy else { return }", install)
        self.assertIn("install([], tweakFromApp: true)", install)           # nothing to download
        self.assertIn("var showsLink: Bool { tweakMissing || isAvailable }", updater)
        self.assertIn("init() {\n        refreshTweak()\n    }", updater)
        content = read(CONTENT)
        self.assertIn("if updater.showsLink && !model.uninstalling {", content)   # v9.8: not while uninstalling
        tap = content.split('UpdateLinkButton(title: updater.tweakMissing ? "Install Tweak" : "Update Available",', 1)[1]
        tap = tap.split("window = .update", 1)[0]
        self.assertIn("if updater.tweakMissing {\n                            updater.installTweak()", tap)
        self.assertIn("updater.refreshTweak()", content.split(".onChange(of: scenePhase) { phase in", 1)[1].split("}\n        }", 1)[0])
        views = read(UPDATE_VIEWS)
        self.assertIn('updater.installingTweak ? "Installing the tweak..." : "Installing update..."', views)
        model = read(MODEL)
        self.assertIn('return "The tweak isn\'t installed yet. Tap Install Tweak."', model)
        self.assertIn("let missing = SpeakaUpdater.installedPackageVersion() == nil", model)

    def test_update_helper(self):
        """The root helper, compiled for the host in its test mode, on a fake phone: what it would run, and in what order."""
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not cc:
            self.skipTest("no C compiler")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            # The device code is compiled too, with warnings as errors but not linked (on macOS it would need
            # CoreFoundation), so a slip there can't wait for the app's build. The test build uses no frameworks.
            for name, flags in (("device.o", ["-c"]), ("test", ["-DSPEAKA_TEST"])):
                build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", *flags,
                                        "-o", str(tmp / name), str(HELPER)], capture_output=True, text=True)
                self.assertEqual(build.returncode, 0, build.stderr)
            phone = tmp / "phone"
            caches = phone / "var/mobile/Library/Caches"
            caches.mkdir(parents=True)
            (phone / "var/jb/usr/bin").mkdir(parents=True)
            (phone / "var/jb/usr/bin/dpkg").write_text("")
            (phone / "var/tmp").mkdir(parents=True)
            speaka = phone / "private/var/containers/Bundle/Application/SPK"
            (speaka / "Speaka.app").mkdir(parents=True)
            (speaka / ".com.apple.mobile_container_manager.metadata.plist").write_text("com.parv.speaka\n")
            carried = speaka / "Speaka.app/Speaka.deb"
            carried.write_text("the tweak this app carries")
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
            # v9.5, Install Tweak: the tweak inside the installed Speaka.app, from a copy in a folder of root's own,
            # which goes afterwards; the app's own copy stays. Then the respring.
            code, out = run("install-update", "--tweak-from-app")
            self.assertEqual(code, 0, out)
            before = out.split("would start", 1)[0]
            self.assertEqual(arguments(before)[0], "-i")
            copy = arguments(before)[1]
            self.assertTrue(copy.startswith(str(phone / "var/tmp/speaka-tweak.")) and copy.endswith("/Speaka.deb"), copy)
            self.assertEqual(carried.read_text(), "the tweak this app carries")
            self.assertEqual(list((phone / "var/tmp").iterdir()), [])
            self.assertEqual(arguments(out.split("would start", 1)[1]), ["finish-update", "--respring"])
            # v9.5, an update: the new app's tweak can only be installed once TrollStore has installed the app, so it
            # all happens in the last step, in its own session: TrollStore's installer, the tweak, the respring.
            code, out = run("install-update", "--tipa", str(tipa), "--tweak-from-app")
            self.assertEqual(code, 0, out)
            self.assertNotIn("dpkg", out.split("would start", 1)[0])
            self.assertEqual(arguments(out.split("would start", 1)[1]),
                             ["finish-update", "--tipa", str(tipa), "--tweak-from-app", "--respring"])
            code, out = run("finish-update", "--tipa", str(tipa), "--tweak-from-app", "--respring")
            self.assertEqual(code, 0, out)
            self.assertLess(out.index("trollstorehelper in its own session"), out.index("dpkg"))
            self.assertLess(out.index("dpkg"), out.index("would kill SpringBoard"))
            # TrollStore's installer fails: no tweak, but Speaka has closed, so the respring still resets the screen.
            code, out = run("finish-update", "--tipa", str(tipa), "--tweak-from-app", "--respring", fail="trollstorehelper")
            self.assertEqual(code, 7, out)
            self.assertNotIn("dpkg", out)
            self.assertIn("would kill SpringBoard", out)
            # A downloaded tweak and the app's own, both at once, isn't allowed; an app that carries none is reported.
            self.assertEqual(run("install-update", "--deb", str(deb), "--tweak-from-app")[0], 2)
            carried.unlink()
            code, out = run("install-update", "--tweak-from-app")
            self.assertEqual(code, 4, out)
            self.assertIn("The tweak inside Speaka wasn't found", out)
            carried.symlink_to(deb)                             # never a link: only TrollStore writes there
            self.assertEqual(run("install-update", "--tweak-from-app")[0], 4)
            carried.unlink()
            carried.write_text("the tweak this app carries")
            # v9.6, Settings > Kill Bluetooth.
            code, out = run("kill-bluetooth")
            self.assertEqual(code, 0, out)
            self.assertIn("(test) would kill bluetoothd", out)
            # dpkg fails: nothing else runs and nothing resprings.
            code, out = run("install-update", "--deb", str(deb), "--tipa", str(tipa), fail="dpkg")
            self.assertEqual(code, 8, out)
            self.assertNotIn("would start", out)
            self.assertNotIn("trollstorehelper", out)
            # TrollStore's installer fails: code 7, so the app falls back to TrollStore's link.
            self.assertEqual(run("install-update", "--tipa", str(tipa), fail="trollstorehelper")[0], 7)
            # The app's fallback folder, the tweak's own /tmp/FTABHarvest, is accepted too.
            harvest = phone / "tmp/FTABHarvest"
            harvest.mkdir(parents=True)
            (harvest / "Speaka-update.deb").write_text("deb")
            code, out = run("install-update", "--deb", str(harvest / "Speaka-update.deb"))
            self.assertEqual(code, 0, out)
            self.assertEqual(arguments(out.split("would start", 1)[0]), ["-i", str(harvest / "Speaka-update.deb")])
            # Update files must be regular files inside those folders, with the right extension.
            (phone / "tmp/elsewhere").mkdir(parents=True)
            (phone / "tmp/elsewhere/x.deb").write_text("deb")
            for bad in ("/tmp/elsewhere.deb", str(phone / "tmp/elsewhere/x.deb"), str(caches) + "/../../../../etc/x.deb",
                        str(caches / "Speaka-update.txt")):
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
        self.assertIn("private func collapseSoon(after seconds: TimeInterval = 1.6) {\n"
                      "        DispatchQueue.main.asyncAfter(deadline: .now() + seconds)", model)
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
        marketing = re.search(r'MARKETING_VERSION: "([^"]+)"', read(APP / "project.yml")).group(1)
        self.assertRegex(marketing, r"^\d+\.\d+$")
        # CI stamps control with the release's full version before the tests run (9.3 becomes 9.3.60, or a tag's
        # 9.4.0), so project.yml has to match control only while control still holds the plain base.
        stamped = re.search(r"^Version: (\S+)$", read(ROOT / "control"), re.M).group(1)
        if re.fullmatch(r"\d+\.\d+", stamped):
            self.assertEqual(marketing, stamped)
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
        self.assertIn("tipa: app helper\n\tDEB=\"$(DEB)\" bash scripts/package.sh", make)
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
        # v9.5: built after the tweak, carrying exactly the release's .deb.
        self.assertIn("    needs: build\n", app_job)
        fetch = app_job.split("- name: Get the tweak the app carries\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("uses: actions/download-artifact@v8", fetch)
        self.assertIn("name: Speaka-deb", fetch)
        self.assertIn("path: carried", fetch)
        self.assertIn('DEB="$PWD/$deb"', app_job)
        self.assertIn('cmp "$bundle/Speaka.deb" "$carried"', app_job)
        self.assertIn('*"_${{ steps.version.outputs.version }}_"*)', app_job)
        upload = app_job.split("actions/upload-artifact", 1)[1]
        self.assertIn("app/packages/Speaka.tipa", upload)
        self.assertIn("app/packages/Speaka.ipa", upload)
        self.assertIn('cmp "$tipa" app/packages/Speaka.ipa', app_job)       # both names, one package
        self.assertIn("for f in Speaka speakahelper Info.plist Assets.car", app_job)   # Assets.car holds the title
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
        # A re-run of the same version replaces the notes too, which may have said the app was missing (run 62).
        existing = publish.split('if gh release view "$tag" >/dev/null 2>&1; then', 1)[1].split("elif", 1)[0]
        self.assertIn('gh release upload "$tag" dist/* --clobber', existing)
        self.assertIn('gh release edit "$tag" --notes-file notes.md --draft=false || return 1', existing)
        # Publishing retries (run 68 left an unpublished draft): three tries, 30 s apart, each clearing the drafts
        # GitHub Actions left behind, and every gh step returns its own failure, since set -e is off inside `if`.
        function = publish.split("publish() {", 1)[1].split("\n          }\n", 1)[0]
        self.assertIn('select(.draft and .author.login == "github-actions[bot]")', function)
        self.assertIn('gh api -X DELETE "repos/${GH_REPO}/releases/${id}"', function)
        for line in function.splitlines():
            if line.strip().startswith("gh release "):
                self.assertTrue(line.rstrip().endswith("|| return 1"), line)
        self.assertIn("for attempt in 1 2 3; do\n            if publish; then\n              break", publish)
        self.assertIn("sleep 30", publish)
        self.assertIn('echo "::error::Publishing failed three times"', publish)
        # Each upload gets one more try, 30 s after a failure, with the same artifact; only both failing fails the job.
        for job, name in ((build_job, "Speaka-deb"), (app_job, "Speaka-app")):
            first = job.split("        id: upload\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("continue-on-error: true", first)
            self.assertIn(f"name: {name}", first)
            wait = job.split("        id: upload\n", 1)[1].split("\n\n", 2)[1]
            self.assertIn("if: steps.upload.outcome == 'failure'\n        run: sleep 30", wait)
            second = job.split("(second try)\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("if: steps.upload.outcome == 'failure'", second)
            self.assertIn(f"name: {name}", second)
            self.assertIn("overwrite: true", second)
            self.assertNotIn("continue-on-error", second)
            paths = lambda step: step.split("path:", 1)[1].split("if-no-files-found", 1)[0]
            self.assertEqual(paths(first), paths(second))
            self.assertEqual(job.count("actions/upload-artifact@v7"), 2)

    def test_release_versions(self):
        """One number for the tweak and the app: control's Version plus the run number, or a pushed tag's. The script runs
        on a copy with its own control, because CI stamps the real one with the full version before the tests run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "tools").mkdir()
            shutil.copy(ROOT / "tools/release_version.sh", tmp / "tools/release_version.sh")
            (tmp / "control").write_text("Package: com.parv.speaka\nVersion: 9.3\n")

            def version(**env):
                clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
                run = subprocess.run(["bash", str(tmp / "tools/release_version.sh")], capture_output=True, text=True,
                                     env={**clean, **env})
                return run.returncode, run.stdout.strip()

            self.assertEqual(version(GITHUB_RUN_NUMBER="57"), (0, "9.3.57"))
            self.assertEqual(version(GITHUB_REF="refs/heads/main", GITHUB_RUN_NUMBER="3"), (0, "9.3.3"))
            self.assertEqual(version(GITHUB_REF="refs/tags/v9.4.0", GITHUB_REF_NAME="v9.4.0", GITHUB_RUN_NUMBER="58"),
                             (0, "9.4.0"))
            self.assertEqual(version(GITHUB_REF="refs/tags/vbeta", GITHUB_REF_NAME="vbeta")[0], 1)
        # The real control: a plain base here, or the full version once CI has stamped it.
        self.assertRegex(re.search(r"^Version: (\S+)$", read(ROOT / "control"), re.M).group(1), r"^\d+(\.\d+)+$")

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
        # Uploaded by build (and by its second try, if the first timed out), downloaded by the app, which carries it
        # (v9.5), and by release.
        self.assertEqual(workflow.count("name: Speaka-deb\n"), 4)

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
            "FileManager", "Timer", "RunLoop", "DispatchQueue", "URLRequest", "URLSession", "URLSessionTask",
            "HTTPURLResponse", "JSONDecoder", "Error", "NSError", "NSUnderlyingErrorKey", "TimeInterval", "Set",
            "Hashable", "BGTaskScheduler", "BGAppRefreshTaskRequest", "UNUserNotificationCenter",
            "UNMutableNotificationContent", "UNNotificationRequest", "Toggle", "Binding",
            # CoreGraphics
            "CGPoint", "CGContext", "CGColorSpace", "CGGradient", "CGColorSpaceCreateDeviceRGB",
            # SwiftUI and Combine
            "App", "Scene", "WindowGroup", "View", "ViewBuilder", "Content", "Context", "Text", "Image", "Font",
            "Color", "Button", "ButtonStyle", "Configuration", "VStack", "HStack", "ZStack", "Spacer", "ForEach",
            "List", "Section", "ScrollView", "GeometryReader", "Group", "Animation", "Shape", "Path", "Capsule", "Circle", "Rectangle", "RoundedRectangle",
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
