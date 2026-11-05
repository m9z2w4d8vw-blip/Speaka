"""Settings > Speaka: the status rows, the debug log screen and its export.

Sections, in order:
  - the page
  - saving the log
"""
import pathlib
import plistlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---- the page ----
# Settings > Speaka: Status and Debug log, read-only. Start, Stop, the session and the audio output moved to the app
# in v9.7, and the code behind them went in v9.8, with the Experiments switches (three did nothing useful; Start sets
# the other two itself) and the log screen's v7 test buttons. Earlier: the sections carry no footer text, and the
# Playing on and never-play rows were removed together with the code behind them.

REMOVED_ROWS = ("Playing on", "Never play to current device", "Never play to", "Clear never-play list",
                "Bluetooth speaker", "Start", "Stop and restore normal Bluetooth", "Session",
                "Audio output", "Play audio on", "Choose output device...", "Experiments")


REMOVED_CODE = ("playingValue:", "neverValue:", "neverList", "neverPlayCurrent:", "clearNeverPlay:",
                "currentExternalOutputs", "BTPSOutputName", "BTPSOutputsDescription", "BTPSTypeLabel", "BTPSIsBuiltIn",
                "startSink:", "stopSink:", "sendSession", "pollSession", "confirmSession", "sessionValue:",
                "BTPSSessionSummary", "chooseOutput:", "AVRoutePickerView", "reloadSpecifierID", "refreshTimer",
                "BTPSWritePref", "notify_post")


REMOVED_TESTS = ("Verify Test Checklist", "Test iOS Audio Output", "Send PCM", "Send SBC", "BTPSAudioProbe",
                 "BTPSIPCSender", "showChecklist", "promptIPCTest", "promptDaemonIPCTest", "promptSBCIPCTest")


class SettingsPage(unittest.TestCase):
    def setUp(self):
        self.items = plistlib.loads((ROOT / "settings/Resources/Root.plist").read_bytes())["items"]
        self.controller = (ROOT / "settings/SettingsPage.m").read_text()

    def test_three_sections_without_footer_text(self):
        groups = [item for item in self.items if item.get("cell") == "PSGroupCell"]
        self.assertEqual([group.get("label") for group in groups], ["Status", "Debug log"])
        for group in groups:
            self.assertNotIn("footerText", group, group.get("label"))

    def test_status_and_debug_log_are_unchanged(self):
        labels = [item.get("label") for item in self.items]
        self.assertEqual(labels[:8], ["Status", "Loaded in daemon", "Last event", "Log channels",
                                      "Debug log", "View debug log", "Copy log to clipboard", "Clear log"])
        actions = [item.get("action") for item in self.items[5:8]]
        self.assertEqual(actions, ["showLog:", "copyLog:", "clearLog:"])

    def test_no_switches(self):
        # The page only reads: nothing on it can arm the speaker, or block Start (SpeakaExperiments did).
        self.assertEqual(len(self.items), 8)
        self.assertEqual([item for item in self.items if item.get("cell") == "PSSwitchCell"], [])
        self.assertEqual([item for item in self.items if "key" in item], [])

    def test_the_apps_rows_are_gone(self):
        labels = [item.get("label") for item in self.items]
        for row in REMOVED_ROWS:
            self.assertNotIn(row, labels)

    def test_the_code_behind_the_removed_rows_is_gone(self):
        for name in REMOVED_CODE:
            self.assertNotIn(name, self.controller, name)
        self.assertNotIn("reloadSpecifierID", (ROOT / "settings/SettingsPage.m").read_text())

    def test_the_log_screen_only_shows_and_saves_the_log(self):
        viewer = (ROOT / "settings/DebugLog.m").read_text()
        reader = (ROOT / "settings/DebugLog.m").read_text()
        for name in REMOVED_TESTS:
            self.assertNotIn(name, viewer, name)
        for name in ("BTPSAudioTestLastResult", "BTPSIPCPlaybackLastResult", "BTPSV75SBCRequestLastResult",
                     "BTPSDaemonIPCRequestLastResult", "diag.v74_ipc", "diag.v75_sbc"):
            self.assertNotIn(name, reader, name)
        self.assertIn("Save Log to iCloud Drive", viewer)
        make = (ROOT / "settings/Makefile").read_text()
        self.assertIn("SpeakaPrefs_FILES      = SettingsPage.m DebugLog.m\n", make)
        self.assertIn("SpeakaPrefs_FRAMEWORKS = UIKit Foundation CoreFoundation\n", make)


# ---- saving the log ----
# Static regression checks for the Settings-only iCloud export action.

UI = (ROOT / 'settings' / 'DebugLog.m').read_text()


class LogExport(unittest.TestCase):
    def test_export_is_in_settings_not_daemon(self):
        self.assertIn('UIDocumentPickerViewController', UI)
        self.assertIn('initForExportingURLs:@[fileURL] asCopy:YES', UI)
        self.assertIn('self.navigationItem.rightBarButtonItems', UI)

    def test_button_is_visible_and_interactive(self):
        self.assertIn('Save Log to iCloud Drive', UI)
        self.assertIn('action:@selector(exportToFiles)', UI)
        self.assertIn('[self.view addSubview:exportButton]', UI)
        self.assertIn('[_textView.bottomAnchor constraintEqualToAnchor:exportButton.topAnchor constant:-6.0]', UI)

    def test_full_report_is_used(self):
        self.assertIn('[[SpeakaLogReader shared] fullText]', UI)
        self.assertIn('log writeToURL:fileURL atomically:YES encoding:NSUTF8StringEncoding', UI)
        self.assertIn('@"Speaka-%@-%@.txt"', UI)

    def test_user_selects_location(self):
        self.assertIn('picker.delegate = self;', UI)
        self.assertIn('didPickDocumentsAtURLs:', UI)
        self.assertIn('documentPickerWasCancelled:', UI)
        self.assertNotIn('URLForUbiquityContainerIdentifier', UI)

    def test_cancel_and_completion_clean_temporary_file(self):
        self.assertEqual(UI.count('[self cleanupPendingExport];'), 3)
        self.assertIn('removeItemAtURL:_pendingExportURL', UI)


if __name__ == "__main__":
    unittest.main()
