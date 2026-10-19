import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.store import Invalid, Journal, confined


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-test-")
        self.root = Path(self.temp.name)
        self.store = Journal(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True).stdout

    def repository(self):
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".aispa/\ncache/\n")
        (self.root / "app.py").write_text("print('example')\n")
        self.git("add", ".gitignore", "app.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")

    def test_history_roundtrip(self):
        with self.store.lock():
            state = self.store.create("example", "Пример")
            state["notes"].append("Первое решение")
            self.store.append(state, "note")
        self.assertEqual(self.store.load()["notes"], ["Первое решение"])

    def test_prior_version_can_only_be_read_for_terminal_handoff(self):
        state = self.store.create("prior", "Prior schema-one fixture")
        state["version"] = "0.1.0-alpha.3"
        self.store.append(state, "prior-version-fixture")
        with self.assertRaises(Invalid):
            self.store.load(handoff_only=True)
        state["status"] = "cancelled"
        self.store.append(state, "prior-stopped")
        with self.assertRaises(Invalid):
            self.store.load()
        self.assertEqual(self.store.load(handoff_only=True)["version"], "0.1.0-alpha.3")
        created = self.store.create("next", "New current run")
        self.assertNotEqual(created["run"], state["run"])

    def test_unknown_prior_version_is_not_silently_migrated(self):
        state = self.store.create("future", "Unsupported fixture")
        state.update(version="9.0.0", status="cancelled")
        self.store.append(state, "unsupported")
        with self.assertRaises(Invalid):
            self.store.load(handoff_only=True)

    def test_zone_aliases_hash_real_files_and_reject_reserved_paths(self):
        self.repository()
        before = self.store.code(["./app.py"])
        self.assertEqual(before, self.store.code(["app.py"]))
        (self.root / "app.py").write_text("print('changed')\n")
        self.assertNotEqual(before, self.store.code(["./app.py"]))
        for zone in ("./.aispa/context", "./.git/config", "src/../app.py"):
            with self.subTest(zone=zone), self.assertRaises(Invalid):
                self.store.code([zone])

    def test_second_writer_cannot_enter(self):
        with self.store.lock():
            with self.assertRaises(Invalid):
                with Journal(self.root).lock():
                    self.fail("entered twice")
        self.assertFalse((self.store.home / ".lock").exists())

    def test_history_corruption_is_rejected(self):
        self.store.create("example", "Пример")
        path = self.store.run / "events.jsonl"
        path.write_text(path.read_text().replace('"running"', '"accepted"'))
        with self.assertRaises(Invalid):
            self.store.load()

    def test_partial_history_is_rejected(self):
        self.store.create("example", "Пример")
        with (self.store.run / "events.jsonl").open("a") as stream:
            stream.write('{"sequence":')
        with self.assertRaises(Invalid):
            self.store.load()

    def test_source_mutation_is_rejected(self):
        state = self.store.create("example", "Пример")
        artifact = self.store.artifact("briefs", "Исходное требование".encode(), ".md")
        state["briefs"].append(dict(artifact, id="B01"))
        self.store.append(state, "brief")
        (self.store.run / artifact["file"]).write_text("Другой текст")
        with self.assertRaises(Invalid):
            self.store.load()

    def test_unfinished_run_cannot_be_overwritten(self):
        self.store.create("example", "Пример")
        with self.assertRaises(Invalid):
            self.store.create("second", "Другой")

    def test_invalid_slug_is_rejected(self):
        for slug in ("../outside", "x/y", "", "two words"):
            with self.subTest(slug=slug), self.assertRaises(Invalid):
                self.store.create(slug, "Пример")

    def test_traversal_and_symlink_escape_are_rejected(self):
        for name in ("../x", "/tmp/x", "x/../../z"):
            with self.subTest(name=name), self.assertRaises(Invalid):
                confined(self.root, name)
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(Invalid):
            confined(self.root, "link/outside")

    def test_code_hash_tracks_edits_deletes_and_untracked_files(self):
        self.repository()
        baseline = self.store.code()
        (self.root / "new file.py").write_text("# new\n")
        self.assertNotEqual(baseline, self.store.code())
        (self.root / "new file.py").unlink()
        self.assertEqual(baseline, self.store.code())
        (self.root / "app.py").unlink()
        self.assertNotEqual(baseline, self.store.code())

    def test_code_hash_ignores_run_records_and_gitignored_cache(self):
        self.repository()
        baseline = self.store.code()
        self.store.create("example", "Пример")
        (self.root / "cache").mkdir()
        (self.root / "cache/file").write_text("derived")
        self.assertEqual(baseline, self.store.code())
        self.assertTrue(self.store.clean())

    def test_clean_detects_spaces_and_renames(self):
        self.repository()
        self.assertTrue(self.store.clean())
        self.git("mv", "app.py", "renamed app.py")
        self.assertFalse(self.store.clean())

    def test_root_must_be_git_root(self):
        self.repository()
        subdir = self.root / "subdir"
        subdir.mkdir()
        with self.assertRaises(Invalid):
            Journal(subdir).code()


if __name__ == "__main__":
    unittest.main()
