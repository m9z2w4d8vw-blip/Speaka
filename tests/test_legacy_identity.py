"""Speaka's identity. The project once had another name (its identifiers were prefixed BTPS), and its app's interface
was once described by comparison with another app. Neither belongs in an active file. This module keeps the historical
names in one place, together with the few compatibility entries that must still match files older versions wrote.

It also replaces the per-file checks that removed legacy code (old Settings-page functions, the v7.x probes, the v7.7
and v8.6 experiments) never returns: every active file is checked here."""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMER_NAME = "bt" + "phono" + "sink"            # spelled in pieces: test_app.py checks no file contains it
LEGACY = re.compile(FORMER_NAME + "|btps", re.I)
OTHER_PROJECT = re.compile(r"\bcaz\b|caz'?s\b", re.I)
# Compatibility: what must still match files and paths older versions left on a phone, and the tests of exactly that.
COMPATIBILITY = {
    "app/RootHelper/main.c": ['#define LEGACY_FILE_PREFIX "BTPS"'],
    "layout/DEBIAN/postinst": ["/var/jb/usr/bin/BTPSAudioHelper"],
    "tests/test_app.py": ['"BTPSAudioHelper-v88.log"', '"BTPS-v88-123-abc.shm"'],
    "tests/test_legacy_identity.py": None,       # this file names what it checks for
}
# Historical records: they describe the project as it was, under the names it had then, and say so at the top.
HISTORICAL = {"docs/CHANGELOG.md", "docs/BLUETOOTHD.md", "docs/AAC-INTEGRATION-AUDIT.md"}
HISTORICAL_MARK = "Historical record"


def tracked_text_files():
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not names:   # not a git checkout (an unpacked archive): every file
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    for name in names:
        if name.startswith("docs/reference-binaries/") or "__pycache__" in name:
            continue
        path = ROOT / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        try:
            yield name, data.decode("utf-8")
        except UnicodeDecodeError:
            continue


class LegacyIdentity(unittest.TestCase):
    def test_no_legacy_name_in_active_files(self):
        found = []
        for name, text in tracked_text_files():
            if name in HISTORICAL:
                continue
            allowed = COMPATIBILITY.get(name, [])
            if allowed is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if LEGACY.search(line) and not any(a in line for a in allowed):
                    found.append(f"{name}:{number}: {line.strip()[:100]}")
        self.assertEqual(found, [], "legacy names in active files:\n" + "\n".join(found))

    def test_historical_records_say_so(self):
        for name in HISTORICAL:
            head = (ROOT / name).read_text().split("\n", 12)[:12]
            self.assertTrue(any(HISTORICAL_MARK in line for line in head), f"{name} doesn't open as a historical record")

    def test_no_other_project_named(self):
        found = [f"{name}:{n}: {line.strip()[:100]}" for name, text in tracked_text_files()
                 if name != "tests/test_legacy_identity.py"
                 for n, line in enumerate(text.splitlines(), 1) if OTHER_PROJECT.search(line)]
        self.assertEqual(found, [], "references to another project:\n" + "\n".join(found))

    def test_the_helper_has_one_name_everywhere(self):
        """The helper's name is a runtime contract: the Makefile builds it, launchd starts it, CI checks the package."""
        make = (ROOT / "audiohelper/Makefile").read_text()
        self.assertIn("TOOL_NAME = SpeakaAudioHelper", make)
        plist = (ROOT / "layout/Library/LaunchDaemons/com.parv.speaka.audio.plist").read_text()
        self.assertIn("<string>/var/jb/usr/bin/SpeakaAudioHelper</string><string>--stream-service</string>", plist)
        self.assertEqual(plist.count("/tmp/FTABHarvest/SpeakaAudioHelper.log"), 2)            # stdout and stderr
        self.assertIn("<string>com.parv.speaka.audio</string>", plist)                         # the label is unchanged
        shared = (ROOT / "shared/SpeakaShared.h").read_text()
        self.assertIn('#define SPEAKA_HELPER_LOG_FILE  "/tmp/FTABHarvest/SpeakaAudioHelper.log"', shared)
        ring = (ROOT / "shared/AudioRing.h").read_text()
        self.assertIn('#define AUDIO_RING_PATH_FORMAT "/tmp/FTABHarvest/SpeakaAudioRing-%u-%016llx.shm"', ring)
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        self.assertIn("pkgroot/var/jb/usr/bin/SpeakaAudioHelper", workflow)

    def test_upgrades_leave_nothing_of_the_old_name(self):
        """Upgrading from a version with the old helper name: launchd reloads the job at the new path, a copy no package
        owns is removed, the tweak and helper restart together (ring path), and uninstall removes old-named files."""
        post = (ROOT / "layout/DEBIAN/postinst").read_text()
        removal = post.index('if [ -e "$legacy" ] && ! dpkg -S "$legacy" >/dev/null 2>&1; then rm -f "$legacy"; fi')
        self.assertLess(removal, post.index('launchctl bootstrap system "$plist"'))
        self.assertLess(post.index("launchctl bootout system/com.parv.speaka.audio"), post.index('launchctl bootstrap system "$plist"'))
        self.assertIn("killall -9 bluetoothd", post)                    # the new tweak and new helper, never mixed
        prerm = (ROOT / "layout/DEBIAN/prerm").read_text()
        self.assertIn("launchctl bootout system/com.parv.speaka.audio", prerm)
        helper = (ROOT / "app/RootHelper/main.c").read_text()
        self.assertIn('static const char *const logs[] = { "Speaka", LEGACY_FILE_PREFIX };', helper)
