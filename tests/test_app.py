"""The Speaka app: its screens, its client for the tweak, the updater, Uninstall, and the root helper.

Sections, in order:
  - the app
  - the menu and Uninstall
"""
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


# ---- the app ----
# The Speaka companion app (app/): a SwiftUI app in Caz's style, on Speaka's own background, with Caz's updater for
# both the app and the tweak. It must speak exactly the tweak's protocol, agree with the Settings page, keep Speaka's own
# icon and background, and package correctly for TrollStore.
#
# Swift is first compiled by CI (there is no Apple compiler on the host), so the structural checks below are the only
# ones that run before that: balanced code, imports, every type and model member that is used being defined, and every
# Objective-C method being called by the Swift name its header pins down.

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


GLOWS = [("0.10", "0.12", "0.62", "5B2BB5", "0.95"), ("0.95", "0.30", "0.45", "2B4FD8", "0.60"),
         ("0.90", "0.92", "0.60", "12B3A6", "0.80"), ("0.05", "0.88", "0.42", "B0307A", "0.45")]


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

def objc_declared_names(text):
    """Names an Objective-C header or source declares: macros, classes, protocols, typedefs, enum constants, and
    file-level static functions and constants. Regex-level, for the app's own SP-prefixed names only."""
    names = set(re.findall(r"^\s*#\s*define\s+(\w+)", text, re.M))
    names |= set(re.findall(r"@(?:interface|implementation|protocol)\s+(\w+)", text))
    for group in re.findall(r"@class\s+([\w\s,]+);", text):
        names |= {n.strip() for n in group.split(",") if n.strip()}
    names |= set(re.findall(r"typedef\s+NS_(?:ENUM|OPTIONS|CLOSED_ENUM)\s*\([^,]+,\s*(\w+)\)", text))
    for body in re.findall(r"typedef\s+NS_(?:ENUM|OPTIONS|CLOSED_ENUM)\s*\([^)]*\)\s*\{([^}]*)\}", text):
        names |= set(re.findall(r"^\s*(\w+)\s*(?:=[^,\n]*)?,?", body, re.M))
    names |= set(re.findall(r"typedef\b[^;{]*?\b(\w+)\s*;", text))
    names |= set(re.findall(r"^\s*static\b[^;={(]*?\b(\w+)\s*(?:\(|=|;)", text, re.M))
    return names


def objc_undeclared_sp_names(code_text, declared):
    """SP-prefixed names (the app's own, and the SPEAKA_ macros) used in code but declared nowhere we know of."""
    used = set(re.findall(r"\bSP[A-Z]\w*", strip_comments_and_strings(code_text)))
    return used - declared



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
    def test_objective_c_names_are_all_declared(self):
        # v9.8: the app's Objective-C can't be compiled off a Mac, so an invented name (SPPreferencesDomain, run 114272394413)
        # failed only in CI's app build. Every SP-prefixed name and SPEAKA_ macro the app's Objective-C uses must be
        # declared in its Model headers or sources, or in shared/SpeakaShared.h.
        model = APP / "Speaka/Model"
        declared = set()
        for path in sorted(model.glob("*.h")) + sorted(model.glob("*.m")) + [ROOT / "shared/SpeakaShared.h"]:
            declared |= objc_declared_names(read(path))
        for path in sorted(model.glob("*.m")) + sorted(model.glob("*.h")):
            self.assertEqual(objc_undeclared_sp_names(read(path), declared), set(), path.name)
        # The check itself catches what it is for.
        self.assertEqual(objc_undeclared_sp_names("CFStringRef d = (__bridge CFStringRef)SPPreferencesDomain;", declared),
                         {"SPPreferencesDomain"})
        self.assertEqual(objc_undeclared_sp_names('id v = SPCopyPref(@SPEAKA_KEY_AAC); // SPNotReal', declared), set())

    def test_app_speaks_the_tweaks_protocol(self):
        client = read(CLIENT_M)
        self.assertIn('#import "shared/SpeakaShared.h"', client)
        for name in ("@SPEAKA_KEY_SESSION_REQUEST", "SPEAKA_NOTIFY_SESSION", "@SPEAKA_KEY_STATUS", "@SPEAKA_STATUS_SESSION",
                     "@SPEAKA_KEY_HELPER_STATUS", "@SPEAKA_KEY_OUTPUT_MODE", "SPEAKA_NOTIFY_OUTPUT", "SPEAKA_PREFS_DOMAIN"):
            self.assertIn(name, client)
        send = client.split("- (SPRequest *)sendAction:", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('@{ @"action": action, @"nonce": nonce, @"time": @([NSDate date].timeIntervalSince1970) }', send)
        self.assertLess(send.index("SPWritePref(@SPEAKA_KEY_SESSION_REQUEST, request)"), send.index("notify_post(SPEAKA_NOTIFY_SESSION)"))
        self.assertIn("SPWritePref(@SPEAKA_KEY_SESSION_REQUEST, nil);", send)   # withdrawn if the notification fails
        # Names come from shared/SpeakaShared.h only: no hand-typed keys or notification names anywhere in the app.
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

    def test_session_rules(self):
        # v9.8: the app is the only place that reads the session; these were compared with the Settings page's copy.
        client = read(CLIENT_M)
        self.assertIn('@[ @"REFUSED", @"ROLLED BACK", @"UNSAFE", @"FAILED", @"SDP publish failed" ]', client)
        for rule in ('[sep hasPrefix:@"LOST"]', '[trial hasPrefix:@"ARMED"]', '[trial hasPrefix:@"REFUSED"]',
                     '[sep containsString:@"register=0"]', '[sep containsString:@"SEID=7"]',
                     'status[@"diag.v89.build"]', '[session containsString:@"result=REFUSED"]'):
            self.assertIn(rule, client)
        self.assertIn("> 20.0)", client)                       # the helper's report counts for 20 s
        self.assertIn("request.relaunched ? 45.0 : 25.0", client)

    def test_output_modes_match_the_helper(self):
        """The app's three outputs are the ones the helper understands (Settings no longer lists them, v9.7)."""
        client = read(CLIENT_M)
        modes = client.split("+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes", 1)[1].split("];", 1)[0]
        keys = re.findall(r'@"key": @"([^"]+)"', modes)
        self.assertEqual(keys, ["speaker", "headphones", "any"])
        self.assertEqual(len(re.findall(r'@"title": @"([^"]+)"', modes)), 3)
        self.assertEqual(len(re.findall(r'@"shortTitle": @"([^"]+)"', modes)), 3)
        helper = read(ROOT / "audiohelper/OutputPolicy.h")
        self.assertIn('return mode == OUTPUT_MODE_HEADPHONES ? "headphones" : mode == OUTPUT_MODE_ANY ? "any" : "speaker";',
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
                                  "connectedComputerName", "pairedComputerName", "computerName", "isAudioDevice", "lookupSummary",
                                  "updateNotificationsEnabled", "setUpdateNotificationsEnabled", "notifiedVersion",
                                  "setNotifiedVersion", "removeLeftoverPreferences", "removeLegacyCodecSettings",
                                  "roomVerdict"})
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
        tweak = read(ROOT / "tweak/Hooks.xm") + read(ROOT / "tweak/Session.c")
        # Statuses as the tweak writes them: the key, and the format's fixed text before its first conversion.
        written = {}
        for call in re.finditer(r'SpeakaSetStatus\(\s*"([\w.]+)"\s*,(.*?)\);', tweak, re.S):
            written.setdefault(call.group(1), []).extend(re.findall(r'"([^"]*)"', call.group(2)))
        written.setdefault("diag.v89.session", []).append("%s nonce=%s pid=%d result=%s")
        keys = set(re.findall(r'status\["([\w.]+)"', steps)) | set(re.findall(r'\("(diag\.[\w.]+)", \[', steps))
        self.assertGreaterEqual(len(keys), 12)
        for key in keys:
            self.assertIn(key, written, f"no SpeakaSetStatus writes {key}")

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
                # v9.9.1: the room step's result is judged by shared/SessionCheck.h, which the steps call.
                checked = steps + read(ROOT / "shared/SessionCheck.h")
                self.assertIn(f'"{prefix}', checked.replace("live_slots=7 ", "live_slots=").replace("SEID=7", "SEID="),
                              f"{prefix!r} is listed here but no step checks it")
        # The results the steps read from the session line are the ones SpeakaSessionReport writes.
        report = read(ROOT / "tweak/Session.c")
        for result in ("ARMED", "REFUSED", "OFF", "STILL_ARMED"):
            self.assertIn(f'"{result}"', report)
        for result in ("result=REFUSED", "result=OFF", "result=STILL_ARMED"):
            self.assertIn(f'"{result}"', steps)
        # A zero SDP result prints as "0" (%#llx), and SEID 7 is the speaker's.
        self.assertIn('SpeakaSetStatus("diag.v82.sdp", "LOCAL add=%#llx handle=%#x;', read(ROOT / "tweak/Hooks.xm"))
        self.assertIn('sdp.hasPrefix("LOCAL add=0 ")', steps)
        self.assertIn('sep.hasPrefix("LOCAL register=0 SEID=7")', steps)
        # Six native Sources: the step accepts the count the tweak writes as "%u".
        self.assertIn('SpeakaSetStatus("diag.native_source_ok", "%u", sources);', read(ROOT / "tweak/Hooks.xm"))

    def test_the_tweak_reports_every_step_promptly(self):
        """Every final refusal on the way to the speaker sets a status, and statuses reach the app within a second."""
        tweak = read(ROOT / "tweak/Hooks.xm")
        attempt = block(tweak, "static void register_sink_after_sources(uint64_t audio_context)\n{")
        claim = attempt.index("__sync_bool_compare_and_swap(&g_sink_attempted, 0, 1)")
        # Before the one attempt is claimed, a missing prerequisite is WAITING: the next power-on tries again.
        self.assertIn('SpeakaSetStatus("diag.v83.audio_table", "WAITING: %s", missing);', attempt[:claim])
        self.assertNotRegex(attempt[:claim], r'SpeakaSetStatus\([^;]*"REFUSED')
        # After it (its own line returns quietly: someone else made the attempt), every return says why.
        after = attempt[attempt.index("return;", claim) + len("return;"):].split("reject_before_swap:", 1)[0]
        stretches = after.split("return;")[:-1]
        self.assertGreaterEqual(len(stretches), 7)
        for stretch in stretches:
            self.assertIn("SpeakaSetStatus(", stretch, "a return without a status, after: " + stretch[-200:])
        callback = block(tweak, "static int install_media_callback(uint64_t context)\n{")
        self.assertEqual(callback.count("return 0;"), callback.count('SpeakaSetStatus("diag.audio_receive","REFUSED'))
        self.assertRegex(tweak, r'#define SESSION_BUILD_TAG "v9\.6\.0-one-tap-output-select"')
        log = read(ROOT / "tweak/Log.c")
        flush = block(log, "static void schedule_flush(void)\n{")
        self.assertIn("if (!g_queue) return;", flush)
        self.assertIn("if (!__sync_lock_test_and_set(&g_flush_pending, 1))", flush)
        self.assertIn("1LL * NSEC_PER_SEC", flush)
        status = block(log, "void SpeakaSetStatus(const char *key, const char *fmt, ...)\n{")
        self.assertLess(status.index("pthread_mutex_unlock(&g_lock);"), status.index("schedule_flush();"))
        common = block(log, "static void log_common(int sync, const char *fmt, va_list ap)\n{")
        self.assertIn("if (inited) schedule_flush();", common)   # SpeakaLogNow too, not only SpeakaLogLine
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

    def test_the_pc_is_named_from_its_route_identity(self):
        """Priority 4 (v9.9.4): the app named the PC from the audio route, where iOS lists it whenever nothing else holds
        the route (outputs=LAPTOP-UGIVBIA5 [BluetoothA2DPOutput]). Since v9.8.87 the helper holds the route from the
        moment the PC connects, so the route only showed the speaker and the app showed "Your PC". The helper now captures
        the PC from the route before its own activation and on route changes; the app matches the port's address with
        BluetoothManager's devices. shared/PeerName.h decides, here against realistic routes and device lists."""
        harness = r"""
#include <assert.h>
#include <string.h>
#include "shared/PeerName.h"
int main(void) {
    char a[18];
    /* UIDs as iOS reports Bluetooth ports: the address anywhere in the UID, any case, ':' or '-'. */
    assert(peer_uid_address("78:4F:43:12:34:56-tacl", a) && !strcmp(a, "78:4F:43:12:34:56"));
    assert(peer_uid_address("78:4f:43:12:34:56-tacl", a) && !strcmp(a, "78:4F:43:12:34:56"));
    assert(peer_uid_address("BT-78-4f-43-12-34-56-tsco", a) && !strcmp(a, "78:4F:43:12:34:56"));
    assert(!peer_uid_address("Speaker", a) && !peer_uid_address("", a) && !peer_uid_address(0, a));
    assert(!peer_uid_address("078:4F:43:12:34:56", a) || strcmp(a, "78:4F:43:12:34:56"));   /* not a longer hex run */
    assert(peer_address_equal("78:4f:43:12:34:56-tacl", "78:4F:43:12:34:56"));
    assert(!peer_address_equal("78:4f:43:12:34:56-tacl", "78:4F:43:12:34:57"));
    /* Which route output is the PC. */
    const char *n1[] = {"Speaker", "AirPods Pro", "LAPTOP-UGIVBIA5"};
    const char *t1[] = {"Speaker", "BluetoothA2DPOutput", "BluetoothA2DPOutput"};
    assert(peer_pick_port(n1, t1, 3) == 2);                 /* a Windows default name wins over AirPods */
    const char *n2[] = {"Parv's Workstation"}, *t2[] = {"BluetoothA2DPOutput"};
    assert(peer_pick_port(n2, t2, 1) == 0);                 /* a custom name is fine on its own */
    const char *n3[] = {"Speaker"}, *t3[] = {"Speaker"};
    assert(peer_pick_port(n3, t3, 1) == -1);                /* the speaker alone names nothing */
    /* The order the app shows a name in. */
    peer_names_t n = {.identity="Parv's Workstation", .connected_computer="Other Laptop",
                      .route="Parv's Workstation", .paired_computer="Old PC", .receiving=1};
    assert(!strcmp(peer_choose_name(&n), "Parv's Workstation"));      /* the device streaming, by address, first */
    n.identity = 0;
    assert(!strcmp(peer_choose_name(&n), "Other Laptop"));            /* then a connected computer */
    n.connected_computer = 0;
    assert(!strcmp(peer_choose_name(&n), "Parv's Workstation"));      /* then the route's own name */
    n.route = 0;
    assert(!strcmp(peer_choose_name(&n), "Old PC"));                  /* the one paired computer, while receiving */
    n.receiving = 0;
    assert(peer_choose_name(&n) == 0);                                /* not while idle: "Your PC" / connect prompt */
    /* The route's Bluetooth output is the user's AirPods: neither its identity nor its name is the PC's. */
    peer_names_t buds = {.identity="AirPods Pro", .identity_is_audio=1, .route="AirPods Pro", .receiving=1};
    assert(peer_choose_name(&buds) == 0);
    /* Two computers paired and none listed as connected: the paired guess is withheld (nil), so no wrong name. */
    peer_names_t two = {.receiving=1};
    assert(peer_choose_name(&two) == 0);
    /* A different PC connects: the captured port changes and so does the name - nothing is cached here. */
    peer_names_t first = {.route="DESKTOP-A1", .receiving=1}, second = {.route="DESKTOP-B2", .receiving=1};
    assert(!strcmp(peer_choose_name(&first), "DESKTOP-A1") && !strcmp(peer_choose_name(&second), "DESKTOP-B2"));
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory() as td:
            for lang, name, std in (("c", "peer.c", "-std=gnu11"), ("c++", "peer.cpp", "-std=c++17")):
                src = pathlib.Path(td) / name; exe = pathlib.Path(td) / (name + ".out"); src.write_text(harness)
                # C++: designated initializers that leave fields out are zero, as in C; -Wextra would flag them.
                extra = ["-Wno-missing-field-initializers"] if lang == "c++" else []
                build = subprocess.run([cc if lang == "c" else (shutil.which("c++") or cc), std, "-Wall", "-Wextra",
                                        "-Werror", *extra, "-I" + str(ROOT), str(src), "-o", str(exe)],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stderr)
                ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(ran.returncode, 0, ran.stderr)
        # The helper captures the PC from the route before its first activation and on every change (the route before
        # it, which at its own activation is the system's, and after it), and publishes it.
        helper = read(ROOT / "audiohelper/AudioHelper.m")
        setup = block_until(helper, "    int own_logged=-1;\n", "    for(;;) {")
        self.assertIn('capture_pc_port([AVAudioSession sharedInstance].currentRoute,"before_activation");', setup)
        self.assertIn("forget_pc_port();", setup)
        observer = block_until(helper, "addObserverForName:AVAudioSessionRouteChangeNotification", "}];")
        self.assertIn("note.userInfo[AVAudioSessionRouteChangePreviousRouteKey]", observer)
        self.assertIn('capture_pc_port(previous,"previous_route");', observer)
        capture = block_until(helper, "static void capture_pc_port(", "\n}\n")
        self.assertIn("peer_pick_port(names,types,(unsigned)outputs.count)", capture)
        self.assertIn('@"pc":pc?:@{}', helper)
        self.assertIn('own_released(&own);forget_pc_port();', helper)              # a session that let go forgets it
        # The app reads it, matches it by address, and never names headphones as the PC.
        client = read(ROOT / "app/Speaka/Model/SPTweakClient.m")
        self.assertIn('id pc = helper[@"pc"];', client)
        # v9.9.6: the capture names the route only when it is this connection's device (or the address is unknown).
        self.assertIn("BOOL captureIsConnection = connection.length == 0 || peer_address_equal(connection.UTF8String, captureUID.UTF8String);", client)
        self.assertIn("routeUID = captureUID;", client)
        device = read(ROOT / "app/Speaka/Model/SPConnectedDevice.m")
        for method in ("+ (NSString *)computerNameForRouteUID:(NSString *)uid\n{", "+ (BOOL)isAudioDeviceForRouteUID:(NSString *)uid\n{"):
            body = block(device, method)
            self.assertIn("peer_address_equal(uid.UTF8String, device.address.UTF8String)", body)
            self.assertIn("peer_class_is_audio(device.hasClass, SPMajorIndex(device.rawClass))", body)
            self.assertIn("@catch (NSException *exception)", body)
        model = read(MODEL)
        # Swift's order is peer_choose_name's: the connection's identity (v9.9.5), the route's device by address,
        # a connected computer, the route's name (unless audio), the paired computer while receiving.
        self.assertIn("let name = peer ?? identityName ?? computerName ?? (routeIsAudioDevice ? nil : routePCName) ??\n"
                      "                (snapshot.receiving ? pairedName : nil)", model)
        self.assertIn("SPConnectedDevice.computerName(forRouteUID: next.routeUID)", model)
        self.assertIn("SPConnectedDevice.isAudioDevice(forRouteUID: next.routeUID)", model)

    def test_the_pc_is_named_from_the_connection_itself(self):
        """Priority 1 (v9.9.7). v9.8.95: V105_PATH_NOT_FOUND every time - SetConfigInd's x0 is bluetoothd's process-wide
        manager (loaded from a dispatch_once global; its dump is full of pthread mutexes, 58 54 55 4D), not the
        connection. The connection is the stream handle, which the handler resolves through a std::map<uint16_t, Device *>
        in the manager; the Device holds the peer's address and name. The tweak repeats that lookup with reads at
        configuration time. Here: the walk against a synthetic memory image with a real tree, every failure mode named,
        and the resolver; then the layout checked against bluetoothd's own instructions; then the wiring."""
        harness = r"""
#include <assert.h>
#include <string.h>
#include "shared/PeerName.h"
/* A synthetic address space of separate objects, like bluetoothd's heap. */
typedef struct { uint64_t base; unsigned char bytes[0x200]; } region_t;
static region_t mem[16]; static unsigned nmem;
static unsigned char *obj(uint64_t base) { mem[nmem].base = base; memset(mem[nmem].bytes, 0, 0x200); return mem[nmem++].bytes; }
static int fake_peek(uint64_t a, void *out, size_t n) {
    for (unsigned i = 0; i < nmem; i++)
        if (a >= mem[i].base && a + n <= mem[i].base + 0x200) { memcpy(out, mem[i].bytes + (a - mem[i].base), n); return 1; }
    return 0;                                                       /* unmapped: like vm_read_overwrite failing */
}
static void put64(unsigned char *at, uint64_t v) { memcpy(at, &v, 8); }
static void put16(unsigned char *at, uint16_t v) { memcpy(at, &v, 2); }
static void short_string(unsigned char *at, const char *t) { size_t n = strlen(t); memcpy(at, t, n); at[23] = (unsigned char)n; }
static void long_string(unsigned char *at, uint64_t data, unsigned char *data_obj, const char *t) {
    size_t n = strlen(t); memcpy(data_obj, t, n + 1); put64(at, data); put64(at + 8, n); put64(at + 16, 0x8000000000000030ull);
}
static void address(unsigned char *at, const char *t) { unsigned char b[6]; assert(peer_address_bytes(t, b)); memcpy(at, b, 6); }
#define MANAGER 0x200000000ull
int main(void) {
    /* The manager: std::map at +0x1f8, root (end node's left) at +0x200 - here at its own address so +0x1f8 fits. */
    unsigned char *mgr = obj(MANAGER + 0x100);                     /* covers manager+0x100 .. +0x2ff */
    unsigned char *n2 = obj(0x300000000ull), *n1 = obj(0x300001000ull), *n5 = obj(0x300002000ull);
    unsigned char *laptop = obj(0x400000000ull);                   /* each object spans 0x200: +0x80..+0x130 fit */
    unsigned char *other = obj(0x400010000ull), *other_name = obj(0x400020000ull);
    unsigned char *blank = obj(0x400030000ull);
    put64(mgr + 0x1f8 + 8 - 0x100, 0x300000000ull);                 /* root = handle 2 */
    put16(n2 + 0x20, 2); put64(n2 + 0x0, 0x300001000ull); put64(n2 + 0x8, 0x300002000ull); put64(n2 + 0x28, 0x400010000ull);
    put16(n1 + 0x20, 1); put64(n1 + 0x28, 0x400000000ull);
    put16(n5 + 0x20, 5); put64(n5 + 0x28, 0x400030000ull);
    /* handle 1: the laptop - name in the fallback field, short string; preferred field empty. */
    address(laptop + 0x80, "24:B2:B9:A1:A4:68"); short_string(laptop + 0xf0, "LAPTOP-UGIVBIA5");
    /* handle 2: another computer with a user-assigned name long enough to live out of line. */
    address(other + 0x80, "11:22:33:44:55:66"); short_string(other + 0xf0, "DESKTOP-OTHER");
    long_string(other + 0x118, 0x400020000ull, other_name, "Parv's Living Room Desktop PC");
    /* handle 5: a Device whose address was never set. */
    (void)blank;
    peer_identity_t id; char a[18];
    assert(peer_identify_stream(MANAGER, 1, fake_peek, &id) == PEER_OK);
    peer_address_text(id.address, a);
    assert(!strcmp(a, "24:B2:B9:A1:A4:68") && !strcmp(id.name, "LAPTOP-UGIVBIA5") && id.name_source == 2 && id.nodes == 2);
    assert(peer_identify_stream(MANAGER, 2, fake_peek, &id) == PEER_OK);
    peer_address_text(id.address, a);
    assert(!strcmp(a, "11:22:33:44:55:66") && !strcmp(id.name, "Parv's Living Room Desktop PC") && id.name_source == 1);
    assert(peer_identify_stream(MANAGER, 5, fake_peek, &id) == PEER_ADDRESS_IMPLAUSIBLE);
    assert(peer_identify_stream(MANAGER, 3, fake_peek, &id) == PEER_HANDLE_ABSENT);   /* 2 -> right 5 -> left: none */
    assert(peer_identify_stream(MANAGER, 9, fake_peek, &id) == PEER_HANDLE_ABSENT);
    /* Unreadable memory and an absent handle are different answers. */
    assert(peer_identify_stream(0x700000000ull, 1, fake_peek, &id) == PEER_ROOT_UNREADABLE);
    assert(peer_identify_stream(0, 1, fake_peek, &id) == PEER_NO_MANAGER);
    put64(n2 + 0x0, 0x380000000ull);                                 /* a left child that isn't mapped */
    assert(peer_identify_stream(MANAGER, 1, fake_peek, &id) == PEER_NODE_UNREADABLE);
    put64(n2 + 0x0, 0x300000000ull);                                 /* a cycle: bounded, never a hang */
    assert(peer_identify_stream(MANAGER, 1, fake_peek, &id) == PEER_TOO_DEEP);
    put64(n2 + 0x0, 0x300001000ull);
    put64(mgr + 0x1f8 + 8 - 0x100, 0);
    assert(peer_identify_stream(MANAGER, 1, fake_peek, &id) == PEER_MAP_EMPTY);
    put64(mgr + 0x1f8 + 8 - 0x100, 0x300000000ull);
    /* A name field holding something that isn't a name gives the address without a name. */
    laptop[0xf0] = 0x07;
    assert(peer_identify_stream(MANAGER, 1, fake_peek, &id) == PEER_OK && id.name_source == 0 && !id.name[0]);
    laptop[0xf0] = 'L';

    /* The app's resolution: the connection's own name first; never another device's. */
    peer_cache_t cache; memset(&cache, 0, sizeof cache);
    assert(!strcmp(peer_resolve("24:B2:B9:A1:A4:68", "LAPTOP-UGIVBIA5", "", "", &cache), "LAPTOP-UGIVBIA5"));
    peer_cache_learn(&cache, "24:B2:B9:A1:A4:68", "LAPTOP-UGIVBIA5", 100);
    assert(!strcmp(peer_resolve("24:B2:B9:A1:A4:68", "", "", "", &cache), "LAPTOP-UGIVBIA5"));   /* name read failed */
    assert(peer_resolve("11:22:33:44:55:66", "", "24:B2:B9:A1:A4:68-tacl", "LAPTOP-UGIVBIA5", &cache) == NULL);
    assert(!strcmp(peer_resolve("11:22:33:44:55:66", "DESKTOP-OTHER", "", "", &cache), "DESKTOP-OTHER"));
    assert(!strcmp(peer_resolve(NULL, NULL, "24:B2:B9:A1:A4:68-tacl", "LAPTOP-UGIVBIA5", &cache), "LAPTOP-UGIVBIA5"));
    assert(peer_resolve(NULL, NULL, "", "", &cache) == NULL);
    /* The bounded cache: a renamed PC updates; the least recently seen goes first. */
    peer_cache_learn(&cache, "24:B2:B9:A1:A4:68", "Parv's Laptop", 200);
    assert(cache.n == 1 && !strcmp(peer_cache_lookup(&cache, "24:b2:b9:a1:a4:68"), "Parv's Laptop"));
    for (int i = 0; i < 8; i++) { char uid[32], nm[16]; snprintf(uid, sizeof uid, "10:00:00:00:00:%02X", i);
                                  snprintf(nm, sizeof nm, "PC-%d", i); peer_cache_learn(&cache, uid, nm, 300 + i); }
    assert(cache.n == PEER_CACHE_SIZE && peer_cache_lookup(&cache, "24:B2:B9:A1:A4:68") == NULL);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        with tempfile.TemporaryDirectory() as td:
            for lang, name, std in (("c", "conn.c", "-std=gnu11"), ("c++", "conn.cpp", "-std=c++17")):
                src = pathlib.Path(td) / name; exe = pathlib.Path(td) / (name + ".out"); src.write_text(harness)
                extra = ["-Wno-missing-field-initializers"] if lang == "c++" else []
                build = subprocess.run([cc if lang == "c" else (shutil.which("c++") or cc), std, "-Wall", "-Wextra",
                                        "-Werror", *extra, "-I" + str(ROOT), str(src), "-o", str(exe)],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stderr)
                ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(ran.returncode, 0, ran.stderr)

        # The layout is bluetoothd's own: decode the instructions it uses for each step and compare with PeerName.h.
        import struct
        binary = (ROOT / "docs/reference-binaries/ios17-iphone13/bluetoothd").read_bytes()
        text_vm, text_file = 0x100000000, 0      # __TEXT maps from file offset 0 at 0x100000000 in this thin arm64e binary
        def word(va):
            return struct.unpack_from("<I", binary, text_file + va - text_vm)[0]
        def ldr_imm(w):     # any load, unsigned offset (LDR/LDRH/LDRB/LDRSB...): (size, rt, rn, byte offset)
            size = w >> 30
            self.assertEqual(w & 0x3f000000, 0x39000000, hex(w))
            self.assertNotEqual((w >> 22) & 3, 0, hex(w))                                    # a load, not a store
            return size, w & 31, (w >> 5) & 31, ((w >> 10) & 0xfff) << size
        def add_imm(w):     # ADD (immediate, 64-bit): (rd, rn, imm)
            self.assertEqual(w & 0xffc00000, 0x91000000, hex(w))
            return w & 31, (w >> 5) & 31, (w >> 10) & 0xfff
        header = read(ROOT / "shared/PeerName.h")
        def const(name):
            return int(re.search(r"#define %s (0x[0-9a-f]+)u" % name, header).group(1), 16)
        self.assertEqual(binary[:4], b"\xcf\xfa\xed\xfe")                                   # thin 64-bit Mach-O
        self.assertEqual(word(0x10037ae68), 0xd503237f)                                      # the hooked handler
        # The wrapper hands the handler x0 from a global, and x2 from the closure's handle.
        self.assertEqual(ldr_imm(word(0x100386730))[3], 0xb68)                              # ldr x0, [x8, #0xb68]
        self.assertEqual(ldr_imm(word(0x100386738))[:2] + (ldr_imm(word(0x100386738))[3],), (1, 2, 0xc))   # ldrh w2
        # +0x3730e8: the stream map's root, the node key; +0x374ab8 (map::at): the mapped value.
        self.assertEqual(ldr_imm(word(0x100373104))[3], const("PEER_MGR_STREAM_MAP") + const("PEER_MAP_ROOT"))
        self.assertEqual(ldr_imm(word(0x10037311c))[::3], (1, const("PEER_NODE_KEY")))      # ldrh w11, [x9, #0x20]
        self.assertEqual(ldr_imm(word(0x100374adc))[3], const("PEER_NODE_LEFT"))            # left: [x8, #0]
        self.assertEqual(ldr_imm(word(0x100374af0))[3], const("PEER_NODE_RIGHT"))           # right: [x8, #8]
        self.assertEqual(add_imm(word(0x100374afc))[2], const("PEER_NODE_VALUE"))           # add x0, x8, #0x28
        # The handler formats device+0x80; the formatter reads six bytes and prints them %02X:...
        self.assertEqual(add_imm(word(0x10037af4c))[1:], (28, const("PEER_DEVICE_ADDRESS")))  # add x0, x28, #0x80
        self.assertEqual([ldr_imm(word(0x100410928 + 4 * i))[3] for i in range(6)], [0, 1, 2, 3, 4, 5])
        fmt = binary[0x100837fc0 - text_vm:0x100837fc0 - text_vm + 30].split(b"\0")[0]
        self.assertEqual(fmt, b"%02X:%02X:%02X:%02X:%02X:%02X")
        # The name: device+0x118 if not empty, else device+0xf0.
        self.assertEqual(add_imm(word(0x1003def9c))[2], const("PEER_DEVICE_NAME_PREFERRED"))
        self.assertEqual(add_imm(word(0x1003defb4))[2], const("PEER_DEVICE_NAME_FALLBACK"))
        self.assertEqual(ldr_imm(word(0x1003defa0))[3], const("PEER_DEVICE_NAME_PREFERRED") + 23)   # its size byte
        log = binary.find(b"SetConfigInd setting a2dp stream handle to %d for device %{public}s")
        self.assertGreater(log, 0)

        # The tweak: at CONFIGURE, from the stream handle, before the native handler runs; reads only.
        hooks = read(ROOT / "tweak/Hooks.xm")
        config = block_until(hooks, "static uint64_t hook_setconfigind(", "\n}\n")
        self.assertLess(config.index("SpeakaPeerConnected(a0,(unsigned)a2);"), config.index("return hook_setconfigind_observe("))
        peer = read(ROOT / "tweak/PeerIdentity.c")
        self.assertIn("vm_read_overwrite(mach_task_self()", peer)
        self.assertIn("int st = peer_identify_stream(manager, handle, peek, &id);", peer)
        self.assertIn('SpeakaSetStatus("diag.v105.peer_name", "%s", id.name);', peer)
        self.assertIn('SpeakaLogNow("V105_PEER_UNRESOLVED step=%s', peer)                    # the failing step, by name
        unresolved = peer[peer.index("} else {", peer.index("if (st == PEER_OK)")):]
        self.assertIn('SpeakaSetStatus("diag.v105.peer_name", "%s", "");', unresolved)        # never the last PC's name
        self.assertNotIn("^{", peer)
        self.assertNotIn("peer_scan_for_address", peer + header)                             # the scan is gone
        # The app: the connection's own name first, both verified pairs cached, and the line shows it before audio.
        client = read(ROOT / "app/Speaka/Model/SPTweakClient.m")
        self.assertIn('NSString *connectionName = SPString(status[@"diag.v105.peer_name"]);', client)
        self.assertIn("const char *resolved = peer_resolve(connection.length ? connection.UTF8String : NULL, connectionName.UTF8String,", client)
        model = read(MODEL)
        line = block(model, "var statusLine: StatusLine? {")
        self.assertLess(line.index("let name = peer ??"), line.index('text: "\\(who) · connected"'))

    def test_the_pcs_name_shows_as_soon_as_it_connects(self):
        """The PC is named from iOS's Bluetooth list once it's connected, before any audio, whatever form its device class
        comes in; while it streams, the one paired computer names it even if iOS doesn't list it as connected."""
        source = read(ROOT / "app/Speaka/Model/SPConnectedDevice.m")
        for selector in ("sharedInstance", "available", "connectedDevices", "pairedDevices", "name", "address",
                         "majorClass", "connected"):
            self.assertIn(f"@selector({selector})", source)
        # every private call goes through SPSend/SPSendBool/majorClass, plus v9.9.5's two guarded class calls
        # (setSharedInstanceQueue:, lastInitError)
        self.assertEqual(source.count("objc_msgSend)"), 5)
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
        self.assertIn("let name = peer ?? identityName ?? computerName ?? (routeIsAudioDevice ? nil : routePCName) ??", line)
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
        self.assertRegex(read(ROOT / "shared/SpeakaShared.h"), r'#define SPEAKA_LOG_DIR\s+"/tmp/FTABHarvest"')
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
        tweak = read(ROOT / "tweak/Hooks.xm")
        config = block(tweak, "static uint64_t hook_setconfigind(uint64_t a0,uint64_t a1,uint64_t a2,uint64_t a3,\n"
                              "    uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7)\n{")
        self.assertIn('SpeakaSetStatus("diag.v95.peer","configured: the PC set up the speaker\'s stream");', config)
        signal = block(tweak, "static void note_signal(unsigned direction,uint64_t cid,const uint8_t *p,size_t size)\n{")
        self.assertIn('SpeakaSetStatus("diag.v95.peer","%s",change==1?"streaming":(p[1]&63u)==9u?"paused":"closed");',
                      signal)
        self.assertLess(signal.index("if(change>=0) {"), signal.index('"diag.v95.peer"'))   # only our stream's
        # The parser it relies on: SUSPEND (9), CLOSE (8) and ABORT (10) give 0, an accepted START gives 1.
        parser = read(ROOT / "tweak/StreamControl.c")
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
        self.assertIn("case settings, output, credits, connect, update", content)   # v9.9.2: Connect to
        self.assertIn("case .settings:\n            SettingsWindow(model: model) { self.window = .credits }", content)
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
        self.assertIn('var headerDetail: String { headerWarning ?? codecWarning ?? "by Parv" }', model)
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
        self.assertTrue((ROOT / "shared/SpeakaShared.h").is_file())       # what HEADER_SEARCH_PATHS is for
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
        leftovers = [path.relative_to(ROOT).as_posix() for pattern in ("UI", "AudioHelper.m", "SPAppDelegate.*", "SPSceneDelegate.*")
                     for path in (APP / "Speaka").glob(pattern)]
        self.assertEqual(leftovers, [], "left over from the UIKit app (v9.1): delete these in your clone")
        backdrop = APP / "Speaka/Assets.xcassets/Backdrop.imageset"
        self.assertFalse(backdrop.exists(), "app/Speaka/Assets.xcassets/Backdrop.imageset is Caz's yellow backdrop, "
                                            "left over from v9.2: delete it in your clone")
        project = read(APP / "project.yml")                     # and even if they linger, they're never built
        for pattern in ('"UI/**"', "AudioHelper.m", '"SPAppDelegate.*"', '"SPSceneDelegate.*"'):
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
        self.assertIn("BUNDLE_NAME = SpeakaPrefs", read(ROOT / "settings/Makefile"))
        root = plistlib.loads((ROOT / "settings/Resources/Root.plist").read_bytes())
        self.assertEqual(root["title"], "Speaka")
        job = plistlib.loads((ROOT / "layout/Library/LaunchDaemons/com.parv.speaka.audio.plist").read_bytes())
        self.assertEqual(job["Label"], "com.parv.speaka.audio")
        for name, pixels in (("icon.png", 29), ("icon@2x.png", 58), ("icon@3x.png", 87)):
            self.assertEqual(png_size(ROOT / "settings/Resources" / name), (pixels, pixels))
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
            "List", "Section", "ScrollView", "GeometryReader", "Group", "Animation", "Shape", "Path", "Capsule", "Circle", "Rectangle", "RoundedRectangle", "Divider",
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


# ---- the menu and Uninstall ----
# v9.8: the menu is Caz's four rows, and Uninstall, held for 4 s, removes the tweak, Speaka's leftovers, then the app.

SPEAKA_SOURCES = ROOT / "app/Speaka"


def block_until(source, start, end="\n}\n"):
    i = source.index(start)
    return source[i:source.index(end, i) + len(end)]


class Menu(unittest.TestCase):
    def test_four_rows_in_order(self):
        items = block_until(read(SPEAKA_SOURCES / "ContentView.swift"), "    private var menuItems: [MenuItem] {", "\n    }\n")
        ids = re.findall(r'MenuItem\(id: "(\w+)"', items)
        self.assertEqual(ids, ["settings", "output", "uninstall", "connect"])   # v9.9.2: Credits is in Settings
        rows = dict(zip(ids, re.split(r'MenuItem\(id: "\w+"', items)[1:]))
        self.assertIn("let locked = model.uninstalling", items)
        for name in ("settings", "output", "connect"):
            self.assertIn("chevron: true, enabled: !locked", rows[name])      # locked once Uninstall starts
        uninstall = rows["uninstall"]
        self.assertIn('title: "Uninstall", symbol: "trash"', uninstall)
        self.assertNotIn("chevron", uninstall)                                  # an action, like Caz's Scan
        self.assertIn("enabled: model.request == nil && !updater.isBusy", uninstall)
        self.assertIn("holdSeconds: 4, busy: locked) { model.uninstall() }", uninstall)

    def test_panel_keeps_cazs_measurements(self):
        """Caz's spec: 73 pt rows (65 with a Home button, 52 on small screens), 20 pt sides, 5 pt top and bottom, 1 pt
        separators 10 pt in, 14 pt continuous corners. Four rows make Caz's 302 pt panel."""
        components, theme = read(SPEAKA_SOURCES / "MainComponents.swift"), read(SPEAKA_SOURCES / "Theme.swift")
        menu = block_until(components, "struct ActionMenu: View {")
        for part in (".padding(.horizontal, innerPadding)", ".padding(.vertical, Metrics.innerTopPadding)",
                     "RoundedRectangle(cornerRadius: 14, style: .continuous)",
                     ".frame(height: 1)\n                            .padding(.horizontal, 10)",
                     "if index < items.count - 1 {"):
            self.assertIn(part, menu)
        for part in ("static let innerPadding: CGFloat = 20", "static let innerTopPadding: CGFloat = 5",
                     "if isSmall(height) { return 52 }", "return isHomeButtonDevice ? 65 : 73"):
            self.assertIn(part, theme)
        self.assertIn("rowHeight: Metrics.actionHeight(size.height)", read(SPEAKA_SOURCES / "ContentView.swift"))

    def test_hold_row(self):
        components = read(SPEAKA_SOURCES / "MainComponents.swift")
        row = block_until(components, "struct HoldMenuRow: View {")
        plain = block_until(components, "struct MenuRow: View {")
        # Laid out exactly like the other rows.
        for part in ("HStack(spacing: 10)", ".font(.system(size: 19, weight: .medium))", ".font(.system(size: 17))",
                     ".lineLimit(1)", ".minimumScaleFactor(0.6)", ".foregroundStyle(Theme.text)",
                     ".padding(.horizontal, 12)", ".frame(height: height)", ".contentShape(Rectangle())"):
            self.assertIn(part, row)
            self.assertIn(part, plain)
        # Only a hold acts: no button and no press-dim. Interpretation 6 of the mockups: a gray fill sweeps from the
        # left over the hold, and everything it has covered dims to Caz's disabled-row 45%, so the passed part looks
        # disabled while the rest is still white. The title stays Uninstall all the way; letting go runs the edge back.
        self.assertNotIn("Button {", row)
        self.assertNotIn("MenuButtonStyle", row)
        self.assertIn(".onLongPressGesture(minimumDuration: seconds, maximumDistance: 40) {", row)
        self.assertIn("private var seconds: Double { item.holdSeconds ?? 4 }", row)
        self.assertIn("@State private var progress: CGFloat = 0", row)
        self.assertIn("pressing ? Animation.linear(duration: seconds) : Animation.easeOut(duration: 0.25)", row)
        self.assertIn("progress = pressing ? 1 : 0", row)
        self.assertIn("static let dimmed = 0.45", row)
        self.assertIn(".opacity(item.enabled ? 1 : 0.45)", plain)                  # the same as a disabled row
        self.assertIn("static let cover = Color.white.opacity(0.10)", row)
        # One animatable shape draws the fill and cuts both copies of the row, so all three move together.
        stack = row[row.index("        ZStack {\n            HoldPortion"):]
        self.assertIn("HoldPortion(progress: shown).fill(Self.cover)\n"
                      "            content.mask { HoldPortion(progress: shown, leading: false) }\n"
                      "            content.opacity(Self.dimmed).mask { HoldPortion(progress: shown) }", stack)
        shape = block_until(components, "struct HoldPortion: Shape {")
        self.assertIn("var animatableData: CGFloat {\n        get { progress }\n        set { progress = newValue }", shape)
        self.assertIn("let edge = rect.width * min(max(progress, 0), 1)", shape)
        self.assertIn("CGRect(x: rect.minX, y: rect.minY, width: edge, height: rect.height)", shape)
        self.assertIn("CGRect(x: rect.minX + edge, y: rect.minY, width: rect.width - edge, height: rect.height)", shape)
        for gone in ("Keep holding", "GeometryReader", "Theme.failure", "greyedOut", "holding"):
            self.assertNotIn(gone, row)
        self.assertIn('Text(item.busy ? "Uninstalling..." : item.title)', row)   # only once it's under way
        self.assertIn("UINotificationFeedbackGenerator().notificationOccurred(.warning)\n            item.action()", row)
        # While it runs: the whole row stays covered, with a spinner where the icon was. Disabled otherwise: 45%.
        self.assertIn("private var shown: CGFloat { item.busy ? 1 : progress }", row)
        self.assertIn("if item.busy {\n                    Spinner()", row)
        self.assertIn(".opacity(item.enabled || item.busy ? 1 : Self.dimmed)", row)
        self.assertIn(".disabled(!item.enabled || item.busy)", row)
        self.assertIn(".accessibilityElement(children: .ignore)", row)            # drawn twice, heard once
        self.assertIn("if item.holdSeconds != nil {\n                        HoldMenuRow(item: item, height: rowHeight)",
                      block_until(components, "struct ActionMenu: View {"))
        item = block_until(components, "struct MenuItem: Identifiable {")
        self.assertIn("var holdSeconds: Double? = nil", item)
        self.assertIn("var busy = false", item)


class SettingsWindow(unittest.TestCase):
    def test_smaller_and_without_descriptions(self):
        windows = read(SPEAKA_SOURCES / "Windows.swift")
        settings = block_until(windows, "struct SettingsWindow: View {")
        # v9.9.2: Kill Bluetooth (and Credits) are Dopamine's Actions buttons (WindowActionButton); the switch row
        # keeps Speaka's 44 pt, 12 pt continuous, 15 pt medium row.
        self.assertEqual(settings.count(".frame(height: 44)"), 1)
        self.assertEqual(settings.count("RoundedRectangle(cornerRadius: 12, style: .continuous)"), 1)
        self.assertEqual(settings.count(".font(.system(size: 15, weight: .medium))"), 1)
        self.assertNotIn("Experimental AAC", settings)
        self.assertIn("VStack(spacing: 10)", settings)
        self.assertIn(".scaleEffect(0.85)\n                        .frame(width: 51 * 0.85, height: 31 * 0.85)", settings)
        for gone in ("Every 2 minutes", ".frame(height: 50)", "size: 17", "size: 13"):
            self.assertNotIn(gone, settings)
        # A line only when something failed, in the warning colour at Caz's 12 pt for footers.
        self.assertIn("if let failure = model.killResult {", settings)
        self.assertIn('if model.notificationsRefused {\n                    Text("Allow notifications', settings)
        self.assertEqual(settings.count(".font(.system(size: 12))\n                        .foregroundStyle(Theme.failure)"), 2)
        self.assertNotIn("Color.white.opacity(0.5)", settings)


class Uninstall(unittest.TestCase):
    def test_nothing_about_the_tweak_while_it_goes(self):
        model = read(SPEAKA_SOURCES / "SpeakaModel.swift")
        warning = block_until(model, "    var headerWarning: String? {", "\n    }\n")
        self.assertLess(warning.index("if uninstalling {\n            return nil\n        }"), warning.index("if let problem {"))
        self.assertIn("guard request == nil, !uninstalling else { return nil }", block_until(model, "    var statusLine: StatusLine? {", "\n    }\n"))
        self.assertIn("if updater.showsLink && !model.uninstalling {", read(SPEAKA_SOURCES / "ContentView.swift"))
        # A failed uninstall clears the flag before setting the reason, so the reason shows.
        flow = block_until(model, "    func uninstall() {", "\n    }\n")
        self.assertIn("self.uninstalling = false\n                self.problem = reason", flow)

    def test_model_order(self):
        model = read(SPEAKA_SOURCES / "SpeakaModel.swift")
        flow = block_until(model, "    func uninstall() {", "\n    }\n")
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
        header, client = read(SPEAKA_SOURCES / "Model/SPTweakClient.h"), read(SPEAKA_SOURCES / "Model/SPTweakClient.m")
        self.assertIn("+ (void)removeLeftoverPreferences NS_SWIFT_NAME(removeLeftoverPreferences());", header)
        impl = block_until(client, "+ (void)removeLeftoverPreferences\n{")
        for part in ("CFStringRef domain = CFSTR(SPEAKA_PREFS_DOMAIN);",
                     "CFPreferencesCopyKeyList(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)",
                     'if ([key hasPrefix:@"Speaka"])', "CFRelease(keys);",
                     "CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);",
                     "removePersistentDomainForName:app"):
            self.assertIn(part, impl)
        # The prefix finds every key: the tweak's and the helper's all start with "Speaka", and the Settings page
        # (since v9.8) stores none.
        keys = re.findall(r'#define SPEAKA_KEY_\w+\s+"([^"]+)"', read(ROOT / "shared/SpeakaShared.h"))
        self.assertGreater(len(keys), 10)
        pane = re.findall(r"<key>key</key>\s*<string>([^<]+)</string>", read(ROOT / "settings/Resources/Root.plist"))
        self.assertEqual(pane, [])
        for key in keys:
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


class CodecWarning(unittest.TestCase):
    """v9.9.11: the app warns "Using SBC audio instead of AAC." while the stream playing now was negotiated as SBC -
    the codec the helper is decoding, never what was advertised or a setting - in the header's warning line, under the
    volume warning when both apply, and gone the moment playback stops, the PC disconnects or the codec is unknown."""

    def model(self):
        return read(SPEAKA_SOURCES / "SpeakaModel.swift")

    def test_the_route_from_the_decoder_to_the_app(self):
        helper = read(ROOT / "audiohelper/AudioHelper.m")
        # The helper publishes the decoder's codec for an active generation only, and clears it on stop and per session.
        self.assertIn('@"codec":@(g_active_codec)', helper)
        self.assertIn('g_active_codec=(current&1u)?(decoder.codec==A2DP_CODEC_AAC?"AAC":decoder.codec==A2DP_CODEC_SBC?"SBC":""):"";', helper)
        stop = helper.split("static void stop_output(", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(stop.index('g_active_codec="";'), stop.index('publish_status(@"idle"'))
        session = helper.split("CodecDecoder decoder;codec_decoder_begin(&decoder,s,g_aac_enabled,hlog_line);", 1)[1][:600]
        self.assertIn('g_active_codec="";', session)                          # a new helper session starts unknown
        client = read(CLIENT_M)
        self.assertIn('NSString *codec = SPString(helper[@"codec"]);', client)
        self.assertIn('s.activeCodec = (s.session == SPSessionOn && s.playing &&\n'
                      '                     ([codec isEqualToString:@"AAC"] || [codec isEqualToString:@"SBC"])) ? codec : @"";', client)
        # Stale helper status is dropped before any of this (20 s), so the codec can't outlive the stream.
        self.assertLess(client.index("if (helper && [NSDate date].timeIntervalSince1970 - time > 20.0)"),
                        client.index('NSString *codec = SPString(helper[@"codec"]);'))
        self.assertIn("next.activeCodec = status.activeCodec", self.model())

    def test_where_and_when_it_shows(self):
        model = self.model()
        warning = model.split("    var codecWarning: String? {", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn('guard request == nil, !uninstalling, snapshot.session == .on, snapshot.playing,\n'
                      '              snapshot.activeCodec == "SBC" else { return nil }', warning)
        self.assertIn('return "Using SBC audio instead of AAC."', warning)
        self.assertIn('var headerDetail: String { headerWarning ?? codecWarning ?? "by Parv" }', model)
        self.assertIn("var headerDetailIsWarning: Bool { headerWarning != nil || codecWarning != nil }", model)
        self.assertIn("var headerSecondWarning: String? { headerWarning != nil ? codecWarning : nil }", model)
        # The volume warning (and every error) keeps the first line; their order is unchanged.
        header = model.split("    var headerWarning: String? {", 1)[1].split("\n    }\n", 1)[0]
        self.assertLess(header.index("if let problem {"), header.index("The volume is at 0%. Turn it up while audio plays."))
        self.assertNotIn("SBC", header)
        # The scenarios, through the same expressions.
        def show(session, playing, codec, volume_zero, error=None, request=False):
            head = error if error else ("The volume is at 0%. Turn it up while audio plays."
                                        if session == "on" and playing and volume_zero else None)
            sbc = ("Using SBC audio instead of AAC." if (not request and session == "on" and playing and codec == "SBC")
                   else None)
            return (head or sbc or "by Parv", sbc if head else None)
        sbc, zero = "Using SBC audio instead of AAC.", "The volume is at 0%. Turn it up while audio plays."
        self.assertEqual(show("on", True, "AAC", False), ("by Parv", None))           # AAC: no warning
        self.assertEqual(show("on", True, "SBC", False), (sbc, None))                 # SBC: in the warning line
        self.assertEqual(show("on", True, "SBC", True), (zero, sbc))                  # both: volume first, SBC under it
        self.assertEqual(show("on", False, "SBC", False), ("by Parv", None))          # paused / stopped: gone
        self.assertEqual(show("on", True, "", False), ("by Parv", None))              # unknown codec: gone
        self.assertEqual(show("off", False, "", False), ("by Parv", None))            # after Stop: gone
        self.assertEqual(show("on", True, "SBC", False, request=True), ("by Parv", None))   # a Start/Stop in flight
        self.assertEqual(show("failed", False, "", False, error="The speaker didn't start: x")[1], None)

    def test_the_second_line_looks_like_the_first(self):
        header = read(SPEAKA_SOURCES / "MainComponents.swift").split("struct HeaderView: View {", 1)[1].split("\n}\n", 1)[0]
        first = header.split("            Text(detail)\n", 1)[1].split("            if let secondWarning {", 1)[0]
        second = header.split("            if let secondWarning {\n", 1)[1].split("\n            }\n", 1)[0]
        for style in (".font(.system(size: 14, weight: .regular))", ".lineLimit(2)", ".fixedSize(horizontal: false, vertical: true)"):
            self.assertIn(style, first); self.assertIn(style, second)
        self.assertIn(".foregroundStyle(Theme.failure)", second)                 # the screenshot's warning colour
        self.assertIn("VStack(alignment: .leading, spacing: 2)", header)         # same column, same spacing
        content = read(SPEAKA_SOURCES / "ContentView.swift")
        self.assertIn("detailIsWarning: model.headerDetailIsWarning,\n                       secondWarning: model.headerSecondWarning)", content)


class RoomCheck(unittest.TestCase):
    """v9.9.1: 9.9's dual layout (six Sources + SEID 7 AAC-LC + the SBC Sink) allocates eight endpoint slots, and the
    device registered both Sinks (10:58:30.129) - but the tweak printed expected=7 for every layout and the room step
    accepted only live_slots=7, so a correct launch showed "No room for the speaker", stored while the session was
    already on, so it stayed up all session. The tweak now reports the count its shadow returned and its layout; the
    app accepts only a readable count equal to that layout's own (shared/SessionCheck.h, run here as the app runs it)."""

    def tweak_format(self):
        hooks = read(ROOT / "tweak/Hooks.xm")
        call = hooks.split('SpeakaSetStatus("diag.v81_seven_slots",\n', 1)[1].split(");", 1)[0]
        fmt = re.search(r'"(ONE SHOT result: [^"]+)"', call).group(1)
        self.assertEqual(re.findall(r"%[a-z]+", fmt), ["%s", "%u", "%u", "%s"])
        args = [a.strip() for a in call.split(fmt + '",', 1)[1].split(",")]
        self.assertEqual(args, ['readable ? "" : "unreadable/"', "(unsigned)live_count", "expected", "layout"])
        return fmt

    def test_the_verdict_on_what_the_tweak_writes(self):
        fmt = self.tweak_format().replace("\\", "\\\\").replace('"', '\\"')
        harness = r"""
#include <assert.h>
#include <stdio.h>
#include "shared/SessionCheck.h"
static int verdict(int readable, unsigned live, unsigned expected, const char *layout) {
    char s[256];
    snprintf(s, sizeof s, "FMT", readable ? "" : "unreadable/", live, expected, layout);
    return sp_room_verdict(s);
}
int main(void) {
    assert(verdict(1, 8, 8, "dual") == SP_ROOM_DONE);       /* the dual layout, eight slots */
    assert(verdict(1, 7, 7, "single") == SP_ROOM_DONE);     /* the single fallback, seven */
    assert(verdict(1, 7, 8, "dual") == SP_ROOM_FAILED);     /* dual, but only seven allocated */
    assert(verdict(1, 8, 7, "single") == SP_ROOM_FAILED);   /* single, but eight */
    unsigned odd[] = {0, 6, 9, 63, 255};
    for (unsigned i = 0; i < sizeof odd / sizeof odd[0]; i++) {
        assert(verdict(1, odd[i], 8, "dual") == SP_ROOM_FAILED);
        assert(verdict(1, odd[i], 7, "single") == SP_ROOM_FAILED);
        assert(verdict(1, odd[i], odd[i], "dual") == SP_ROOM_FAILED);
    }
    assert(verdict(0, 8, 8, "dual") == SP_ROOM_FAILED);     /* the count couldn't be read */
    assert(verdict(0, 7, 7, "single") == SP_ROOM_FAILED);
    assert(verdict(1, 8, 7, "dual") == SP_ROOM_FAILED);     /* expected and layout disagree */
    assert(verdict(1, 7, 8, "single") == SP_ROOM_FAILED);
    assert(verdict(1, 8, 8, "triple") == SP_ROOM_FAILED);   /* a layout it doesn't know */
    /* Not a result: the step keeps waiting (and times out) - never done. */
    assert(sp_room_verdict(NULL) == SP_ROOM_NOT_REPORTED);
    assert(sp_room_verdict("") == SP_ROOM_NOT_REPORTED);
    assert(sp_room_verdict("OVERRIDE APPLIED once: config shadow=(8,3,3); verify live slots") == SP_ROOM_NOT_REPORTED);
    assert(sp_room_verdict("REFUSED: config pointer/signature/count not exact") == SP_ROOM_NOT_REPORTED);
    /* Malformed counts. */
    assert(sp_room_verdict("ONE SHOT result: live_slots=8x expected=8 layout=dual native_sources=6;") == SP_ROOM_FAILED);
    assert(sp_room_verdict("ONE SHOT result: live_slots= expected=8 layout=dual native_sources=6;") == SP_ROOM_FAILED);
    assert(sp_room_verdict("ONE SHOT result: live_slots=0008 expected=8 layout=dual native_sources=6;") == SP_ROOM_FAILED);
    assert(sp_room_verdict("ONE SHOT result: live_slots=8 layout=dual native_sources=6;") == SP_ROOM_FAILED);
    assert(sp_room_verdict("ONE SHOT result: live_slots=8 expected=8 layout=dual") == SP_ROOM_FAILED);   /* no space after */
    /* A tweak before 9.9.1 (no layout): only its single-Sink success counts; 9.9's 8/"7" is not waved through. */
    assert(sp_room_verdict("ONE SHOT result: live_slots=7 expected=7 native_sources=6; v8.2 may separately attempt Sink") == SP_ROOM_DONE);
    assert(sp_room_verdict("ONE SHOT result: live_slots=8 expected=7 native_sources=6; v8.2 may separately attempt Sink") == SP_ROOM_FAILED);
    assert(sp_room_verdict("ONE SHOT result: live_slots=unreadable/0 expected=7 native_sources=6; v8.2 may separately attempt Sink") == SP_ROOM_FAILED);
    puts("room verdict: dual 8 and single 7 pass; mismatched, odd, unreadable, malformed and unknown fail; missing waits");
    return 0;
}
""".replace("FMT", fmt)
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "room.c"; exe = pathlib.Path(td) / "room"; src.write_text(harness)
            for std in ("-std=gnu11", "-std=c99"):
                build = subprocess.run([cc, std, "-Wall", "-Wextra", "-Werror", "-Wformat=2", "-I" + str(ROOT), str(src), "-o", str(exe)],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stderr)
                ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            print(ran.stdout, end="")

    def test_the_tweak_reports_this_launchs_expectation(self):
        hooks = read(ROOT / "tweak/Hooks.xm")
        audit = hooks.split("if (g_config_shadow_returned) {\n                    uint64_t context = 0;", 1)[1].split("\n                }\n", 1)[0]
        self.assertIn("const unsigned expected = g_config_shadow[0];", audit)       # what the shadow returned
        self.assertIn('const char *layout = g_dual_sink ? "dual" : "single";', audit)   # reported separately
        self.assertIn("V81_LIVE RESULT readable=%s live_slots=%u expected=%u layout=%s sources=6", audit)
        self.assertNotIn("expected=7", hooks)                                       # no layout-blind expectation
        # The registration itself is unchanged: it still refuses unless the live count is the layout's.
        self.assertIn("(slots != 7u && slots != 8u) || avdtp_slots != slots || (slots == 8u) != (g_dual_sink != 0)", hooks)

    def test_the_app_asks_the_shared_check(self):
        steps = read(SPEAKA_SOURCES / "SessionSteps.swift")
        room = steps.split("    static let room = SessionStep(", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("switch SPTweakClient.roomVerdict(slots) {\n        case .done: return .done\n        case .failed: return .failed(slots)", room)
        self.assertNotIn('hasPrefix("ONE SHOT result: live_slots=7 ")', room)
        self.assertLess(room.index("SPTweakClient.roomVerdict(slots)"), room.index('slots.hasPrefix("REFUSED")'))
        client = read(CLIENT_M)
        self.assertIn('#include "shared/SessionCheck.h"', client)
        verdict = client.split("+ (SPRoomVerdict)roomVerdict:(NSString *)result", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("switch (sp_room_verdict(result.UTF8String)) {", verdict)
        self.assertIn("case SP_ROOM_DONE: return SPRoomDone;", verdict)
        self.assertIn("default: return SPRoomNotReported;", verdict)
        self.assertIn("typedef NS_ENUM(NSInteger, SPRoomVerdict) {", read(CLIENT_H))

    def test_an_eight_slot_launch_passes_every_layout_dependent_step(self):
        # What the 9.9 tweak writes in a dual launch, from its own formats, against the steps that read those keys.
        hooks = read(ROOT / "tweak/Hooks.xm")
        steps = read(SPEAKA_SOURCES / "SessionSteps.swift")
        table = re.search(r'"REPLACED post native Source: entries=%u, native=6, Sink SEID 7%s; original parent block intentionally retained"', hooks)
        self.assertTrue(table)
        dual_table = "REPLACED post native Source: entries=8, native=6, Sink SEID 7 (AAC-LC) + SBC; original parent block intentionally retained"
        for name in ("tableFailures", "loopHookFailures"):
            failures = re.findall(r'"([^"]+)"', steps.split(f"static let {name} = [", 1)[1].split("]", 1)[0])
            self.assertTrue(failures, name)
            self.assertFalse([f for f in failures if dual_table.startswith(f)], name)
        self.assertIn('SpeakaSetStatus("diag.v82.sep", "LOCAL register=0 SEID=%u role=SNK', hooks)
        self.assertIn('sep.hasPrefix("LOCAL register=0 SEID=7")', steps)       # SEID 7 (AAC-LC) registers first in both
        self.assertLess(hooks.index("g_sink_accepted = 1u;\n        SpeakaSetStatus(\"diag.v82.sep\""), hooks.index("    if (e8) {"))
        client = read(CLIENT_M)
        self.assertIn('[sep containsString:@"register=0"] && [sep containsString:@"SEID=7"]', client)

    def test_a_resolved_startup_failure_does_not_stay(self):
        model = read(SPEAKA_SOURCES / "SpeakaModel.swift")
        advance = model.split("    private func advance(_ pending: SPRequest) {", 1)[1].split("\n    }\n", 1)[0]
        failed = advance.split("case .failed(let reason):", 1)[1].split("case .waiting", 1)[0]
        self.assertLess(failed.index("stop(line: step.failed"), failed.index("problemStep = step"))   # after fail() resets it
        stalled = advance.split("case .waiting(let reason):", 1)[1]
        self.assertLess(stalled.index("stop(line: step.failed"), stalled.index("problemStep = step"))
        refresh = model.split("    func refresh() {", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("if let problemStep, request == nil, problem != nil, problemStep.check(self.status) == .done {", refresh)
        self.assertLess(refresh.index("self.status = status.diagnostics"), refresh.index("problemStep.check(self.status)"))
        # Every problem not set by a step's failure drops the step, so no other warning can be cleared by it.
        for m in re.finditer(r"\bproblem = (?!nil)", model):
            self.assertIn("problemStep = nil", model[m.start():m.start() + 260], model[m.start():m.start() + 80])
        for m in re.finditer(r"\n(\s*)problem = nil\n\s*problemSession = nil\n", model):
            self.assertIn("problemStep = nil", model[m.end():m.end() + 40])
        self.assertIn("var headerDetail: String { headerWarning ?? codecWarning ?? \"by Parv\" }", model)   # unchanged


class DopamineActions(unittest.TestCase):
    """v9.9.2: Settings' buttons are Dopamine's Actions buttons (Refresh Jailbreak Apps, Hide Jailbreak, ...), measured
    from an iPhone 13 screenshot at 3x: 798 x 108 px = 266 x 36 pt, 96 px (32 pt) in from each side of the 330 pt
    window, a 3 px (1 pt) border inside the edge at about 15% white over no fill (inside matched the card to 0.4 of a
    level), 10 pt circular corners (the traced corner fits a circle to 0.52 px RMS, a continuous corner to 0.83), the
    symbol 22 x 18 pt (18 pt), the title's cap height 12.0 pt (17 pt) at Regular's stem, 13 pt apart, centred to 0.2
    pt, and 24 px (8 pt) between buttons. Credits moved there; the menu's fourth row is Connect to."""

    def windows(self):
        return read(SPEAKA_SOURCES / "Windows.swift")

    def test_the_button_is_dopamines(self):
        button = block_until(self.windows(), "struct WindowActionButton: View {")
        self.assertIn("static let height: CGFloat = 36", button)
        self.assertIn("static let inset: CGFloat = 16", button)
        self.assertIn("static let spacing: CGFloat = 8", button)
        self.assertIn("HStack(spacing: 10) {", button)
        self.assertIn('Image(systemName: symbol)\n                    .font(.system(size: 18, weight: .regular))', button)
        self.assertIn("Text(title)\n                    .font(.system(size: 17, weight: .regular))", button)
        self.assertIn(".foregroundStyle(.white)", button)
        self.assertIn(".frame(maxWidth: .infinity)\n            .frame(height: Self.height)", button)
        self.assertIn(".overlay(RoundedRectangle(cornerRadius: 10, style: .circular)\n"
                      "                .strokeBorder(Color.white.opacity(0.15), lineWidth: 1))", button)   # inside the edge
        self.assertNotIn(".background(", button)                                            # no fill
        self.assertNotIn(".continuous", button)                                             # UIKit's circular corners
        self.assertIn(".buttonStyle(MenuButtonStyle())", button)                            # dims while pressed
        # 266 pt on an iPhone 13: 390 - 2 x 30 (the window) - 2 x 16 (its content padding) - 2 x 16 (the inset).
        theme = read(SPEAKA_SOURCES / "Theme.swift")
        modal = int(re.search(r"static let modalPadding: CGFloat = (\d+)", theme).group(1))
        settings = block_until(self.windows(), "struct SettingsWindow: View {")
        content = int(re.search(r"\.frame\(maxWidth: \.infinity, maxHeight: \.infinity, alignment: \.top\)\n\s*\.padding\((\d+)\)", settings).group(1))
        self.assertEqual(390 - 2 * modal - 2 * content - 2 * 16, 266)

    def test_settings_has_kill_bluetooth_and_credits(self):
        settings = block_until(self.windows(), "struct SettingsWindow: View {")
        self.assertIn("let showCredits: () -> Void", settings)
        group = settings.split("VStack(spacing: WindowActionButton.spacing) {", 1)[1].split(".padding(.horizontal, WindowActionButton.inset)", 1)[0]
        self.assertIn('WindowActionButton(title: model.killingBluetooth ? "Killing Bluetooth..." : "Kill Bluetooth",\n'
                      '                                       symbol: "arrow.triangle.2.circlepath") {\n'
                      '                        model.killBluetooth()', group)
        self.assertIn(".disabled(busy)\n                    .opacity(busy ? 0.5 : 1)", group)        # not during a Start or Stop
        self.assertIn('WindowActionButton(title: "Credits", symbol: "info.circle", action: showCredits)', group)
        self.assertLess(group.index('"Kill Bluetooth"'), group.index('title: "Credits"'))
        # The kill failure and the switch row follow, as before.
        self.assertLess(settings.index(".padding(.horizontal, WindowActionButton.inset)"), settings.index("if let failure = model.killResult {"))
        self.assertLess(settings.index("if let failure = model.killResult {"), settings.index('Text("Update notifications")'))

    def test_credits_opens_from_settings_and_goes_back(self):
        content = read(SPEAKA_SOURCES / "ContentView.swift")
        self.assertIn("case .credits:\n            CreditsWindow { self.window = .settings }", content)
        credits = block_until(self.windows(), "struct CreditsWindow: View {")
        self.assertIn("var onBack: (() -> Void)? = nil", credits)
        self.assertIn('WindowCard(title: "Credits", onBack: onBack) {', credits)
        menu = block_until(content, "    private var menuItems: [MenuItem] {", "\n    }\n")
        self.assertNotIn('title: "Credits"', menu)
        self.assertNotIn("window = .credits", menu)

    def test_connect_to_takes_the_fourth_row(self):
        content = read(SPEAKA_SOURCES / "ContentView.swift")
        menu = block_until(content, "    private var menuItems: [MenuItem] {", "\n    }\n")
        self.assertIn('MenuItem(id: "connect", title: "Connect to", symbol: "link",\n'
                      '                     chevron: true, enabled: !locked) { window = .connect },', menu)
        self.assertIn("case .connect:\n            ConnectWindow()", content)
        window = block_until(self.windows(), "struct ConnectWindow: View {")
        self.assertIn('WindowCard(title: "Connect to") {', window)

