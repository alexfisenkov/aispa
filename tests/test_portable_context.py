import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import project_context
from aispa_core.store import Invalid, Journal


class PortableContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-portable-context-")
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Public project guide\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs/guide.md").write_text("# Public architecture\n", encoding="utf-8")
        self.store = Journal(self.root)
        self.state = self.store.create("portable", "Portable context")

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_clone_has_public_navigation_when_private_context_is_absent(self):
        project_context.initialize(self.store, self.state, "codex")
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("README.md", agents)
        self.assertIn("docs/", agents)
        self.assertIn("Если `.aispa/context/INDEX.md` существует", agents)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "AGENTS.md", "README.md", "docs/guide.md", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "context"], check=True)
        clone = Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.root), str(clone)], check=True)
        self.assertFalse((clone / ".aispa/context/INDEX.md").exists())
        self.assertTrue((clone / "README.md").is_file())
        self.assertTrue((clone / "docs/guide.md").is_file())
        self.assertIn("README.md", (clone / "AGENTS.md").read_text(encoding="utf-8"))

    def test_alpha9_owned_block_migrates_exactly_with_backup_and_preserves_other_rules(self):
        prefix = b"Owner rules before\n\n"
        suffix = b"\n\nOther managed block: <!-- OTHER_BEGIN -->never edit<!-- OTHER_END -->\n"
        original = prefix + project_context._LEGACY_POINTER + suffix
        (self.root / "AGENTS.md").write_bytes(original)
        project_context.initialize(self.store, self.state, "codex")
        changed = (self.root / "AGENTS.md").read_bytes()
        self.assertEqual(changed, prefix + project_context.POINTER + suffix)
        backup = self.root / (".aispa/backups/AGENTS.md." + hashlib.sha256(original).hexdigest() + ".bak")
        self.assertEqual(backup.read_bytes(), original)
        project_context.initialize(self.store, self.state, "codex")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), changed)

    def test_claude_and_cursor_legacy_blocks_migrate_without_erasing_user_text(self):
        (self.root / "CLAUDE.md").write_bytes(b"Owner Claude rule\n" + project_context._LEGACY_POINTER + b"\nEnd.\n")
        project_context.initialize(self.store, self.state, "claude-code")
        result = (self.root / "CLAUDE.md").read_bytes()
        self.assertTrue(result.startswith(b"Owner Claude rule\n"))
        self.assertTrue(result.endswith(b"\nEnd.\n"))
        self.assertIn(project_context.POINTER, result)

        second = Path(self.temp.name) / "cursor-project"
        second.mkdir()
        store = Journal(second)
        state = store.create("cursor", "Cursor")
        rule = second / ".cursor/rules/aispa.mdc"
        rule.parent.mkdir(parents=True)
        rule.write_bytes(project_context._LEGACY_CURSOR_RULE)
        project_context.initialize(store, state, "cursor")
        self.assertEqual(rule.read_bytes(), project_context.CURSOR_RULE)
        old = project_context._LEGACY_CURSOR_RULE
        backup = second / (".aispa/backups/.cursor/rules/aispa.mdc." + hashlib.sha256(old).hexdigest() + ".bak")
        self.assertEqual(backup.read_bytes(), old)

    def test_unknown_or_misordered_owned_markers_are_not_rewritten(self):
        path = self.root / "AGENTS.md"
        for original in (b"user\n" + project_context.BEGIN + b"\nunknown\n" + project_context.END,
                         project_context.END + b"\n" + project_context.BEGIN):
            with self.subTest(original=original):
                path.write_bytes(original)
                with self.assertRaises(Invalid):
                    project_context.initialize(self.store, self.state, "codex")
                self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
