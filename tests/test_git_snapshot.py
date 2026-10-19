import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import git_snapshot
from aispa_core.store import Invalid


class GitSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-eol-")
        self.root = Path(self.temp.name)
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "core.autocrlf", "false")
        (self.root / ".gitignore").write_bytes(b".aispa/\n")
        (self.root / "text.txt").write_bytes(b"first\nsecond\n")
        self.commit()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, root, *args, input_data=None):
        return subprocess.run(["git", "-C", str(root), *args], input=input_data,
                              capture_output=True, check=True)

    def commit(self):
        self.git(self.root, "add", "-A")
        self.git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def snapshot(self):
        return git_snapshot.records(self.root, self.git)

    def test_lf_and_crlf_worktrees_have_same_committed_content(self):
        before = self.snapshot()
        (self.root / "text.txt").write_bytes(b"first\r\nsecond\r\n")
        self.assertEqual(before, self.snapshot())
        git_snapshot.ensure_clean_index(self.root, self.git)
        self.git(self.root, "config", "core.autocrlf", "true")
        self.assertEqual(before, self.snapshot())

    def test_normalization_does_not_hide_semantic_edit(self):
        (self.root / "text.txt").write_bytes(b"first\r\nchanged\r\n")
        with self.assertRaises(Invalid):
            self.snapshot()

    def test_explicit_binary_attribute_is_not_normalized(self):
        (self.root / ".gitattributes").write_bytes(b"text.txt -text\n")
        self.commit()
        (self.root / "text.txt").write_bytes(b"first\r\nsecond\r\n")
        with self.assertRaises(Invalid):
            self.snapshot()

    def test_nul_binary_changes_are_rejected(self):
        (self.root / "text.txt").write_bytes(b"first\0\nsecond\n")
        self.commit()
        (self.root / "text.txt").write_bytes(b"first\0\r\nsecond\r\n")
        with self.assertRaises(Invalid):
            self.snapshot()

    def test_crlf_split_across_read_boundary(self):
        payload = b"a" * 65535 + b"\nlast\n"
        (self.root / "text.txt").write_bytes(payload)
        self.commit()
        before = self.snapshot()
        (self.root / "text.txt").write_bytes(payload.replace(b"\n", b"\r\n"))
        self.assertEqual(before, self.snapshot())

    def test_filters_rejected_without_running_them(self):
        (self.root / ".gitattributes").write_bytes(b"text.txt filter=external\n")
        self.commit()
        self.git(self.root, "config", "filter.external.clean", "a-command-that-must-never-run")
        with self.assertRaisesRegex(Invalid, "filters"):
            self.snapshot()

    def test_staged_untracked_and_hidden_tracked_edits_rejected(self):
        (self.root / "new.py").write_bytes(b"new\n")
        with self.assertRaises(Invalid):
            git_snapshot.ensure_clean_index(self.root, self.git)
        self.git(self.root, "add", "new.py")
        with self.assertRaises(Invalid):
            git_snapshot.ensure_clean_index(self.root, self.git)
        self.git(self.root, "reset", "-q", "HEAD", "--", "new.py")
        (self.root / "new.py").unlink()
        self.git(self.root, "update-index", "--assume-unchanged", "text.txt")
        (self.root / "text.txt").write_bytes(b"hidden edit\n")
        with self.assertRaises(Invalid):
            self.snapshot()

    @unittest.skipUnless(os.name == "nt", "NTFS uses Git executable mode")
    def test_windows_uses_committed_execute_mode(self):
        self.git(self.root, "update-index", "--chmod=+x", "text.txt")
        self.git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "mode")
        self.assertTrue(dict(self.snapshot())["text.txt"]["executable"])


if __name__ == "__main__":
    unittest.main()
