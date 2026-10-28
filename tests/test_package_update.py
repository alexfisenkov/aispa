"""Release build of AISpa 1.0: tools/package_update.py packs the 1.0 set from one Git commit."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
import package_update  # noqa: E402
import aispa_core  # noqa: E402
from aispa_core.store import Invalid  # noqa: E402

# The 1.0 set without which the old updater or the skill itself does not work (N08).
REQUIRED = (
    "SKILL.md", "LICENSE", "tools/aispa.py", "tools/aispa_core/__init__.py",
    "tools/ap.py", "phases/dashboard-template.html", "prompts/executor.md", "prompts/review.md",
    "agents/claude-code/aispa-judge.md", "agents/claude-code/aispa-mechanic-scripted.md",
    "agents/claude-code/aispa-mechanic.md", "agents/claude-code/aispa-researcher.md",
    "agents/claude-code/aispa-sensitive-worker.md", "agents/claude-code/aispa-worker.md",
    "agents/openai.yaml",
)


def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=AISpa test",
                           "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                           "-c", "core.autocrlf=false", *args],
                          check=True, capture_output=True, env=env, timeout=60).stdout.decode().strip()


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-package-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "skills/aispa", "LICENSE",
                                  "tools/install.py"], check=True, capture_output=True).stdout
        for name in filter(None, tracked.decode("utf-8").split("\0")):
            source = ROOT / name
            if source.is_file():
                (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, self.repo / name)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "package")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.outputs = 0

    def tearDown(self):
        self.temp.cleanup()

    def build(self, commit="HEAD"):
        self.outputs += 1
        return package_update.build_release(self.repo, self.root / ("out-%d" % self.outputs), commit=commit)

    def commit_change(self, message, *, remove=None, write=None):
        if remove:
            git(self.repo, "rm", "-q", "--", "skills/aispa/" + remove)
        for name, text in (write or {}).items():
            (self.repo / name).write_text(text, encoding="utf-8")
            git(self.repo, "add", "--", name)
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_release_from_a_commit_carries_the_one_point_zero_set(self):
        result = self.build()
        out = Path(result["archive"]).parent
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["aispa-update.json", "aispa.zip", "install.py"])
        manifest = json.loads((out / "aispa-update.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["version"], aispa_core.VERSION)
        self.assertEqual(manifest["commit"], self.base)
        self.assertFalse(result["published"])
        with zipfile.ZipFile(out / "aispa.zip") as archive:
            names = set(archive.namelist())
        listed = {row["path"] for row in manifest["files"]}
        for name in REQUIRED:
            with self.subTest(name=name):
                self.assertIn("aispa/" + name, names)
                self.assertIn(name, listed)

    def test_every_required_file_missing_refuses_with_its_name(self):
        for name in REQUIRED:
            if name == "LICENSE":
                continue  # the root LICENSE is packed in its place; checked below
            with self.subTest(name=name):
                commit = self.commit_change("drop " + name, remove=name)
                try:
                    with self.assertRaisesRegex(Invalid, re.escape(name)):
                        self.build(commit)
                finally:
                    git(self.repo, "reset", "-q", "--hard", self.base)

    def test_without_any_license_refuses(self):
        git(self.repo, "rm", "-q", "--", "LICENSE", "skills/aispa/LICENSE")
        git(self.repo, "commit", "-q", "-m", "no license")
        with self.assertRaisesRegex(Invalid, "LICENSE"):
            self.build()

    def test_without_ap_py_nothing_is_written(self):
        commit = self.commit_change("drop ap.py", remove="tools/ap.py")
        with self.assertRaisesRegex(Invalid, re.escape("tools/ap.py")):
            package_update.build_release(self.repo, self.root / "refused", commit=commit)
        self.assertFalse((self.root / "refused").exists())

    def test_skill_metadata_version_must_equal_package_version(self):
        skill = (self.repo / "skills/aispa/SKILL.md").read_text(encoding="utf-8")
        self.assertIn('version: "%s"' % aispa_core.VERSION, skill)
        changed = skill.replace('version: "%s"' % aispa_core.VERSION, 'version: "9.9.9"', 1)
        commit = self.commit_change("skill version drift", write={"skills/aispa/SKILL.md": changed})
        with self.assertRaisesRegex(Invalid, "SKILL.md"):
            self.build(commit)


if __name__ == "__main__":
    unittest.main()
