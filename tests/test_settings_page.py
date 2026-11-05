"""Settings > Speaka (v9.7): Status, Debug log and one Experiments section with every switch. Start, Stop, the
session and the audio output moved to the app, which speaks the same protocol; the page's code behind them stays,
unused. Earlier: the sections carry no footer text, and the Playing on and never-play rows were removed together
with the code behind them."""
import pathlib
import plistlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOVED_ROWS = ("Playing on", "Never play to current device", "Never play to", "Clear never-play list",
                "Bluetooth speaker", "Start", "Stop and restore normal Bluetooth", "Session",
                "Audio output", "Play audio on", "Choose output device...")
REMOVED_CODE = ("playingValue:", "neverValue:", "neverList", "neverPlayCurrent:", "clearNeverPlay:",
                "currentExternalOutputs", "BTPSOutputName", "BTPSOutputsDescription", "BTPSTypeLabel", "BTPSIsBuiltIn")
SWITCHES = ["SpeakaExperiments", "SpeakaSDPProbe", "SpeakaV80ConfigLookupObserver", "SpeakaV81SevenOnce",
            "SpeakaV82DecisiveOnce"]


class SettingsPage(unittest.TestCase):
    def setUp(self):
        self.items = plistlib.loads((ROOT / "speakaprefs/Resources/Root.plist").read_bytes())["items"]
        self.controller = (ROOT / "speakaprefs/BTPSRootListController.m").read_text()

    def test_three_sections_without_footer_text(self):
        groups = [item for item in self.items if item.get("cell") == "PSGroupCell"]
        self.assertEqual([group.get("label") for group in groups], ["Status", "Debug log", "Experiments"])
        for group in groups:
            self.assertNotIn("footerText", group, group.get("label"))

    def test_status_and_debug_log_are_unchanged(self):
        labels = [item.get("label") for item in self.items]
        self.assertEqual(labels[:8], ["Status", "Loaded in daemon", "Last event", "Log channels",
                                      "Debug log", "View debug log", "Copy log to clipboard", "Clear log"])
        actions = [item.get("action") for item in self.items[5:8]]
        self.assertEqual(actions, ["showLog:", "copyLog:", "clearLog:"])

    def test_every_switch_is_under_experiments(self):
        labels = [item.get("label") for item in self.items]
        experiments = self.items[labels.index("Experiments") + 1:]
        self.assertEqual([item.get("cell") for item in experiments], ["PSSwitchCell"] * len(SWITCHES))
        self.assertEqual([item.get("key") for item in experiments], SWITCHES)
        self.assertEqual([item for item in self.items if item.get("cell") == "PSSwitchCell"], experiments)

    def test_the_apps_rows_are_gone(self):
        labels = [item.get("label") for item in self.items]
        for row in REMOVED_ROWS:
            self.assertNotIn(row, labels)

    def test_the_code_behind_the_removed_rows_is_gone(self):
        for name in REMOVED_CODE:
            self.assertNotIn(name, self.controller, name)
        # The live refresh still asks for the Session row; with the row gone, it finds nothing and does nothing.
        self.assertIn('[self reloadSpecifierID:@"session"];', self.controller)


if __name__ == "__main__":
    unittest.main()
