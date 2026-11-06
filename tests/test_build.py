"""The repository and the build: the layout rules, and the pinned libsbc CI fetches.

Sections, in order:
  - the repository layout
  - fetching libsbc before the build
"""
import pathlib
import re
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---- the repository layout ----
# Repository layout guards: keep the structure described in README.md ("Repository layout") true.
#
# Browser uploads sometimes drop a file's folder and put it at the root, or add a " (1)" duplicate. That is how
# 25 stale copies and leftovers had accumulated by v8.9.4. These checks turn each of those mistakes into a CI
# failure with an explanation, instead of clutter that nobody notices.

SUBPROJECTS = ("audiohelper", "settings")


FETCHED_AT_BUILD = {"audiohelper/vendor_sbc/sbc.c", "audiohelper/vendor_sbc/bits.c"}  # tools/fetch_google_sbc.sh


SKIP_DIRS = {".git", ".theos", "packages", "obj", "__pycache__"}


SOURCE_SUFFIXES = {".c", ".h", ".m", ".mm", ".xm"}


def makefile_inputs(makefile):
    text = makefile.read_text().replace("\\\n", " ")
    files = []
    for match in re.finditer(r"^\s*\w+_FILES\s*[:+]?=\s*(.+)$", text, re.M):
        files += match.group(1).split()
    return files


def repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            yield path


class RepoLayout(unittest.TestCase):
    def test_every_build_input_exists(self):
        top = (ROOT / "Makefile").read_text()
        for makefile in [ROOT / "Makefile"] + [ROOT / sub / "Makefile" for sub in SUBPROJECTS]:
            inputs = makefile_inputs(makefile)
            self.assertTrue(inputs, f"no *_FILES found in {makefile.relative_to(ROOT)}")
            for name in inputs:
                path = makefile.parent / name
                rel = path.relative_to(ROOT).as_posix()
                if rel not in FETCHED_AT_BUILD:
                    self.assertTrue(path.is_file(), f"{makefile.relative_to(ROOT)} compiles {rel}, which is missing")
        for sub in SUBPROJECTS:
            self.assertIn(f"SUBPROJECTS += {sub}", top)

    def test_shared_sources_exist_once_in_shared(self):
        # Since v9.8 (step 2) shared sources live in shared/. A subproject that compiles one holds a one-line
        # #include "../shared/<name>", so the copies can't drift apart.
        shared = {path.name for path in (ROOT / "shared").iterdir() if path.is_file()}
        for sub in SUBPROJECTS:
            for path in sorted((ROOT / sub).iterdir()):
                if not path.is_file() or path.name == "Makefile" or path.name not in shared:
                    continue
                code = [line for line in path.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("/*")]
                self.assertEqual(code, [f'#include "../shared/{path.name}"'],
                                 f"{path.name} exists both in shared/ and in {sub}/. If {sub}/'s copy is a misplaced "
                                 f"upload, delete it; otherwise make it a one-line #include \"../shared/{path.name}\".")

    def test_root_holds_only_build_inputs(self):
        # The tweak's sources are in tweak/ and shared/; the root keeps what Theos needs there.
        allowed = {"Makefile", "control", "Speaka.plist", "README.md", ".gitignore"}
        for path in ROOT.iterdir():
            if path.is_dir() or path.name in allowed:
                continue
            if path.name.startswith(("LICENSE", "CHANGELOG", ".gitattributes")):
                continue
            where = "tweak/ or shared/" if path.suffix in SOURCE_SUFFIXES else "docs/, tools/ or tests/"
            self.fail(f"{path.name} is at the repository root; it belongs in {where}")

    def test_every_tweak_and_shared_source_is_used(self):
        """A source in tweak/ or shared/ must be compiled by a Makefile, included by another file, or read by a test."""
        compiled = set(makefile_inputs(ROOT / "Makefile"))
        included, tested = set(), ""
        for path in repo_files():
            if path.suffix in SOURCE_SUFFIXES:
                included |= {pathlib.PurePosixPath(name).name for name in
                             re.findall(r'^\s*#\s*(?:include|import)\s+"([^"]+)"', path.read_text(errors="replace"), re.M)}
        for path in (ROOT / "tests").glob("*.py"):
            tested += path.read_text()
        for folder in ("tweak", "shared"):
            for path in sorted((ROOT / folder).iterdir()):
                rel = f"{folder}/{path.name}"
                if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                    self.assertTrue(rel in compiled or path.name in included or rel in tested,
                                    f"{rel} is not compiled, included or tested: remove it")

    def test_no_browser_duplicate_uploads(self):
        for path in repo_files():
            self.assertIsNone(re.search(r" \(\d+\)(\.|$)", path.name),
                              f"{path.relative_to(ROOT)} looks like a duplicate upload")

    def test_one_changelog_and_one_research_document(self):
        # Since v9.8 the release notes are one changelog and the research notes one document (the old ones are in
        # git history). The changelog must have an entry for the version being built.
        stray = [path.relative_to(ROOT).as_posix() for path in repo_files()
                 if re.match(r"(TESTING|PROVENANCE)-v.*\.md$", path.name)]
        self.assertEqual(stray, [], "release notes go in docs/CHANGELOG.md, research in docs/BLUETOOTHD.md")
        self.assertFalse((ROOT / "docs/history").exists() or (ROOT / "docs/research").exists())
        version = re.search(r"^Version:\s*(\d+\.\d+)", (ROOT / "control").read_text(), re.M).group(1)
        self.assertIn(f"\n## v{version}\n", (ROOT / "docs/CHANGELOG.md").read_text())
        readme = (ROOT / "README.md").read_text()
        for doc in ("docs/CHANGELOG.md", "docs/BLUETOOTHD.md"):
            self.assertIn(f"]({doc})", readme)

    def test_readme_links_resolve(self):
        readme = (ROOT / "README.md").read_text()
        targets = re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", readme)
        for target in targets:
            if re.match(r"[a-z]+:", target):
                continue
            self.assertTrue((ROOT / target).exists(), f"README links to {target}, which does not exist")
        # In-page links: GitHub's anchor for each heading (lower case, punctuation dropped, spaces to hyphens).
        anchors = {re.sub(r"[^a-z0-9 _-]", "", h.lower()).replace(" ", "-")
                   for h in re.findall(r"^#{1,4} (.+)$", readme, re.M)}
        for anchor in re.findall(r'(?:\]\(|href=")#([^)"]+)', readme):
            self.assertIn(anchor, anchors, f"README links to #{anchor}, which no heading has")


# ---- fetching libsbc before the build ----
# Regression for v7.6 CI: dependency fetch must precede Theos build.
#
# The workflow at .github/workflows/build.yml is authoritative. A similarly
# named file in the repository root does NOT run under GitHub Actions.

WORKFLOW = ROOT / '.github' / 'workflows' / 'build.yml'


class LibsbcBootstrap(unittest.TestCase):
    def test_correct_workflow_exists(self):
        self.assertTrue(WORKFLOW.is_file())

    def test_decoder_fetch_before_tests_before_make(self):
        text = WORKFLOW.read_text()
        fetch = text.index('bash tools/fetch_google_sbc.sh')
        tests = text.index('python3 -m unittest discover -s tests -v')
        build = text.index('make clean package FINALPACKAGE=1')
        self.assertLess(fetch, tests)
        self.assertLess(tests, build)

    def test_required_decoder_sources_checked_before_build(self):
        text = WORKFLOW.read_text()
        for name in ('sbc.c', 'bits.c', 'sbc.h', 'bits.h', 'LICENSE', 'UPSTREAM_COMMIT'):
            self.assertIn(name, text)
        self.assertIn('BTPSAudioHelper missing from the .deb', text)

    def test_upstream_commit_pinned(self):
        fetch = (ROOT / 'tools' / 'fetch_google_sbc.sh').read_text()
        self.assertIn('6e505650145c9973d08a0bdd5e5f5e1914305e40', fetch)
        self.assertIn('git -C "$work/src" fetch', fetch)


if __name__ == "__main__":
    unittest.main()
