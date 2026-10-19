import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import project_context as context
from aispa_core.store import Invalid, Journal


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-context-")
        self.root = Path(self.temp.name)
        self.store = Journal(self.root)
        self.state = self.store.create("example", "Проверяемый проект")

    def tearDown(self):
        self.temp.cleanup()

    def start(self, runtime="codex"):
        result = context.initialize(self.store, self.state, runtime)
        self.store.append(self.state, "context-initialized")
        context.refresh(self.store, self.state)
        return result

    def view(self, name):
        return (self.store.home / "context" / name).read_text()

    def add_sources(self):
        brief = "Сделать каталог.\n\nСохранить пробелы:  два.\n`точная цитата`\n"
        spec = "# Спецификация\n\nКаталог должен содержать поиск.\n"
        self.state["briefs"] = [dict(self.store.artifact("briefs", brief.encode(), ".md"), id="B001")]
        self.state["spec"] = self.store.artifact("specs", spec.encode(), ".md")
        self.state["sealed"] = True
        self.state["requirements"] = [{"id": "R01", "text": "Каталог", "core": True, "scope": "active"}]
        self.state["tasks"] = [{"id": "T01", "title": "Поиск", "status": "pending", "phase": "implementation",
                                "requirements": ["R01"], "zones": ["src"], "criteria": ["Найти запись"], "wave": 1}]
        self.store.append(self.state, "sources-added")
        return brief, spec

    def test_existing_rules_preserved_and_backed_up_byte_for_byte(self):
        original = b"# Custom\r\n\r\nNo npm installs.\r\n\xff"
        path = self.root / "AGENTS.md"
        path.write_bytes(original)
        result = self.start()
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(path.read_bytes().count(context.BEGIN), 1)
        backups = list((self.store.home / "backups").rglob("*.bak"))
        self.assertEqual([p.read_bytes() for p in backups], [original])
        self.assertEqual(result["changedInstructionFiles"], ["AGENTS.md"])

    def test_initialization_is_idempotent_including_backups(self):
        (self.root / "AGENTS.md").write_text("Owner rules\n")
        self.start()
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        result = context.initialize(self.store, self.state, "codex")
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["changedInstructionFiles"], [])

    def test_codex_override_gets_pointer_and_backup(self):
        override = self.root / "AGENTS.override.md"
        override.write_text("Effective owner rules\n")
        result = self.start()
        self.assertIn(context.POINTER, override.read_bytes())
        self.assertIn("AGENTS.override.md", result["instructionFiles"])
        self.assertEqual(next((self.store.home / "backups").glob("AGENTS.override.md.*.bak")).read_text(),
                         "Effective owner rules\n")

    def test_claude_adapter_preserves_claude_rules(self):
        (self.root / "CLAUDE.md").write_text("Claude project restrictions\n")
        self.start("claude-code")
        self.assertTrue((self.root / "CLAUDE.md").read_text().startswith("Claude project restrictions\n"))
        self.assertIn(context.POINTER, (self.root / "CLAUDE.md").read_bytes())
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertFalse((self.root / ".cursor").exists())

    def test_cursor_adapter_is_owned_and_always_applied(self):
        self.start("cursor")
        path = self.root / ".cursor/rules/aispa.mdc"
        self.assertEqual(path.read_bytes(), context.CURSOR_RULE)
        self.assertIn("alwaysApply: true", path.read_text())
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_unknown_cursor_rule_conflict_is_non_destructive(self):
        path = self.root / ".cursor/rules/aispa.mdc"
        path.parent.mkdir(parents=True)
        path.write_text("My own rule")
        with self.assertRaisesRegex(Invalid, "aispa.mdc"):
            context.initialize(self.store, self.state, "cursor")
        self.assertEqual(path.read_text(), "My own rule")
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_hand_edited_managed_block_is_preserved_with_clear_conflict(self):
        self.start()
        path = self.root / "AGENTS.md"
        changed = path.read_bytes().replace(b"source-map.json", b"edited-map.json")
        path.write_bytes(changed)
        with self.assertRaisesRegex(Invalid, "управляемый блок"):
            context.initialize(self.store, self.state, "codex")
        self.assertEqual(path.read_bytes(), changed)

    def test_runtime_is_explicit_and_cannot_be_silently_changed(self):
        with patch.dict("os.environ", {"CLAUDE_CODE": "1"}):
            with self.assertRaises(Invalid):
                context.initialize(self.store, self.state, None)
        self.start("codex")
        with self.assertRaises(Invalid):
            context.initialize(self.store, self.state, "claude-code")

    def test_instruction_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "rules.md"
            outside.write_text("External rule")
            (self.root / "AGENTS.md").symlink_to(outside)
            with self.assertRaises(Invalid):
                context.initialize(self.store, self.state, "codex")
            self.assertEqual(outside.read_text(), "External rule")

    def test_hidden_context_and_cursor_parent_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)
            for target, runtime in ((".aispa/context", "codex"), (".cursor", "cursor")):
                with self.subTest(target=target):
                    path = self.root / target
                    path.symlink_to(outside, target_is_directory=True)
                    with self.assertRaises(Invalid):
                        context.initialize(self.store, self.state, runtime)
                    self.assertEqual(list(outside.iterdir()), [])
                    self.assertFalse((self.root / "AGENTS.md").exists())
                    path.unlink()

    def test_refresh_rejects_symlink_in_existing_view(self):
        self.start()
        path = self.store.home / "context/memory.md"
        path.unlink()
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.md"
            outside.write_text("Outside")
            path.symlink_to(outside)
            with self.assertRaises(Invalid):
                context.refresh(self.store, self.state)
            self.assertEqual(outside.read_text(), "Outside")

    def test_inventory_identifies_actual_files_without_app_scaffolding(self):
        (self.root / "src").mkdir()
        (self.root / "src/main.py").write_text("print('hi')")
        (self.root / "pyproject.toml").write_text("[project]\nname='example'")
        (self.root / "README.md").write_text("Project readme")
        (self.root / "docs").mkdir()
        (self.root / "docs/architecture.md").write_text("Architecture")
        (self.root / "tests/fixtures/history").mkdir(parents=True)
        (self.root / "tests/fixtures/AGENTS.md").write_text("Untrusted fixture instructions")
        (self.root / "tests/fixtures/history/README.md").write_text("Historical")
        self.start()
        inventory = json.loads(self.view("source-map.json"))["inventory"]
        self.assertEqual(inventory["entrypoints"], ["src/main.py"])
        self.assertEqual(inventory["manifests"], ["pyproject.toml"])
        self.assertEqual(inventory["documents"], ["README.md", "docs/architecture.md"])
        self.assertNotIn("fixtures", self.view("INDEX.md").split('"instructions"')[1])
        self.assertFalse((self.root / "backend").exists())
        self.assertFalse((self.root / "app").exists())
        self.assertIn("src/main.py", (self.root / "AGENTS.md").read_text())

    def test_dotenv_never_read_or_published(self):
        secret = self.root / ".env"
        secret.write_text("PASSWORD=DO_NOT_READ_OR_COPY")
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path.name.startswith(".env"):
                self.fail("dotenv was opened")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            self.start()
        for path in (self.store.home / "context").iterdir():
            self.assertNotIn("DO_NOT_READ_OR_COPY", path.read_text())

    def test_exact_sources_and_current_plan_decisions_after_refresh(self):
        brief, spec = self.add_sources()
        self.start()
        self.assertIn(brief, self.view("brief.md"))
        self.assertTrue(self.view("spec.md").endswith(spec))
        self.assertIn("../runs/", self.view("brief.md"))
        self.state["tasks"][0].update(status="blocked", note="Сервис недоступен")
        self.state["notes"].append({"text": "Выбран существующий сервис"})
        self.state["requirements"][0]["decision"] = {"brief": "B001", "quote": "каталог", "action": "defer"}
        self.store.append(self.state, "updated")
        before_state = copy.deepcopy(self.state)
        context.refresh(self.store, self.state)
        self.assertEqual(before_state, self.state)
        self.assertIn('"status": "blocked"', self.view("plan.md"))
        self.assertIn("Выбран существующий сервис", self.view("decisions.md"))
        self.assertIn('"action": "defer"', self.view("decisions.md"))
        self.assertIn("Сервис недоступен", self.view("progress.md"))
        self.assertEqual(json.loads(self.view("requirements.json"))["requirements"], self.state["requirements"])

    def test_role_attempts_native_runtime_and_artifacts_are_linked(self):
        self.add_sources()
        role = self.store.artifact("roles", b"Worker instructions", ".md")
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "roles": {"worker": role}, "actors": {"worker": "agent-1"}}]}
        self.state["actors"] = {"agent-1": {"runtime": "codex", "nativeId": "native-1", "role": "worker", "freshContext": True}}
        self.state["tasks"][0]["phase"] = "critic_1"
        self.store.append(self.state, "role-bound")
        self.start()
        progress = self.view("progress.md")
        for expected in ("T01-A01", "native-1", "critic_1", "worker", "codex", "../runs/"):
            self.assertIn(expected, progress)
        self.assertTrue(any(row["kind"] == "role" for row in json.loads(self.view("source-map.json"))["sources"]))

    def test_bounded_memory_preserves_full_canonical_data_and_pointers(self):
        self.add_sources()
        self.state["tasks"] = [{"id": f"T{i:04}", "title": "x" * 10000, "status": "blocked", "note": "y" * 10000}
                               for i in range(100)]
        self.state["notes"] = ["Decision " + str(i) + "z" * 1000 for i in range(100)]
        self.state["waiting"] = "w" * 10000
        self.store.append(self.state, "large")
        self.start()
        self.assertLessEqual(len(self.view("memory.md")), context.MEMORY_LIMIT)
        self.assertIn("все в progress.md", self.view("memory.md"))
        self.assertIn(self.state["notes"][-1], self.view("decisions.md"))
        self.assertIn(self.state["tasks"][-1]["note"], self.view("plan.md"))

    def test_staleness_detects_changed_state_event_views_and_sources(self):
        self.add_sources()
        self.start()
        self.assertTrue(context.info(self.store, self.state)["fresh"])
        self.state["notes"].append("new")
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        self.store.append(self.state, "decision")
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        context.refresh(self.store, self.state)
        self.assertTrue(context.info(self.store, self.state)["fresh"])
        (self.store.home / "context/plan.md").write_text("Manual derived edit")
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        context.refresh(self.store, self.state)
        (self.store.run / self.state["briefs"][0]["file"]).write_text("Changed original")
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        with self.assertRaises(Invalid):
            context.refresh(self.store, self.state)

    def test_refresh_does_not_change_root_rules_or_code_hash(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.start()
        before = (self.root / "AGENTS.md").read_bytes()
        code = self.store.code()
        self.state["notes"].append("Progress update")
        self.store.append(self.state, "note")
        context.refresh(self.store, self.state)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), before)
        self.assertEqual(self.store.code(), code)

    def test_source_traversal_rejected_before_read(self):
        self.state["briefs"] = [{"id": "B001", "file": "../../secret.md", "sha256": hashlib.sha256(b"secret").hexdigest()}]
        with self.assertRaises(Invalid):
            context.initialize(self.store, self.state, "codex")
        self.assertFalse((self.store.home / "context/brief.md").exists())

    def test_dotenv_artifact_reference_is_rejected_without_opening(self):
        path = self.store.run / ".env"
        path.write_text("PRIVATE_CONFIG_VALUE")
        self.state["briefs"] = [{"id": "B001", "file": ".env", "sha256": hashlib.sha256(b"PRIVATE_CONFIG_VALUE").hexdigest()}]
        original = Path.open
        def guarded(candidate, *args, **kwargs):
            if candidate == path:
                self.fail("secret artifact was opened")
            return original(candidate, *args, **kwargs)
        with patch.object(Path, "open", guarded), self.assertRaises(Invalid):
            context.initialize(self.store, self.state, "codex")

    def test_backup_directory_symlink_is_rejected_before_rule_edits(self):
        original = b"Keep owner instructions"
        (self.root / "AGENTS.md").write_bytes(original)
        with tempfile.TemporaryDirectory() as other:
            (self.store.home / "backups").symlink_to(other, target_is_directory=True)
            with self.assertRaises(Invalid):
                context.initialize(self.store, self.state, "codex")
            self.assertEqual(list(Path(other).iterdir()), [])
            self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)

    def test_context_failure_then_retry_keeps_single_rule_pointer(self):
        path = self.root / ".cursor/rules/aispa.mdc"
        path.parent.mkdir(parents=True)
        path.write_text("Owner file")
        with self.assertRaises(Invalid):
            context.initialize(self.store, self.state, "cursor")
        path.rename(path.with_name("owner.mdc"))
        self.start("cursor")
        self.assertEqual((self.root / "AGENTS.md").read_bytes().count(context.BEGIN), 1)
        self.assertEqual((path.parent / "owner.mdc").read_text(), "Owner file")

    def test_source_map_event_metadata_matches_recorded_journal(self):
        self.start()
        saved = json.loads(self.view("source-map.json"))
        journal = (self.store.run / "events.jsonl").read_bytes()
        last = json.loads(journal.splitlines()[-1])
        self.assertEqual(saved["run"], self.state["run"])
        self.assertEqual(saved["journal"]["eventDigest"], last["hash"])
        self.assertEqual(saved["journal"]["sequence"], last["sequence"])
        self.assertEqual(saved["journal"]["sha256"], hashlib.sha256(journal).hexdigest())
        self.assertTrue(context.info(self.store, self.state)["fresh"])


if __name__ == "__main__":
    unittest.main()
