"""New host adapters keep rule changes and missing native evidence visible."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import VERSION, discovery, participants, telemetry, project_context
from aispa_core.contracts import validate_plan
from aispa_core.store import Invalid, Journal, common_input


class RuntimeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-host-evidence-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        self.store = Journal(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_native_rule_change_invalidates_task_fingerprint(self):
        for name in ("GEMINI.md", ".agents/rules.json", ".agents/rules/aispa.md", ".agents/rules/owner.md",
                     ".agents/rules/nested/team.md", ".cursor/rules/aispa.mdc", ".cursor/rules/owner.mdc"):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Owner instruction A\n", encoding="utf-8")
                before = self.store.code(["app.py"], include_common=True)
                path.write_text("Owner instruction B\n", encoding="utf-8")
                self.assertNotEqual(before, self.store.code(["app.py"], include_common=True))
                self.assertTrue(common_input(name))
                self.assertIsNotNone(discovery._kind(name))
        self.assertFalse(common_input("docs/GEMINI.md"))
        self.assertFalse(common_input(".gemini/settings.json"))
        self.assertIsNone(discovery._kind(".gemini/settings.json", explicit=True))
        self.assertIsNone(discovery._kind(".agents/private.md", explicit=True))
        self.assertIsNone(discovery._kind(".agents/rules/secrets.md", explicit=True))
        self.assertFalse(common_input(".agents/rules/settings.json"))

    def test_native_rule_writer_requires_dependency_with_every_reader(self):
        state = {"requirements": [{"id": "R01", "scope": "active"}]}
        for name in ("GEMINI.md", ".agents/rules.json", ".agents/rules/aispa.md", ".agents/rules/owner.md",
                     ".agents/rules/nested/team.md", ".cursor/rules/aispa.mdc", ".cursor/rules/owner.mdc"):
            with self.subTest(name=name):
                tasks = [{"id": "T01", "title": "Rules", "requirements": ["R01"],
                          "zones": [name], "criteria": ["Rules apply"], "dependsOn": []},
                         {"id": "T02", "title": "App", "requirements": ["R01"],
                          "zones": ["app.py"], "criteria": ["App works"], "dependsOn": []}]
                with self.assertRaisesRegex(Invalid, "dependsOn"):
                    validate_plan(state, tasks)
                tasks[1]["dependsOn"] = ["T01"]
                self.assertEqual(len(validate_plan(state, tasks)), 2)

    def test_new_hosts_public_rules_survive_clean_clone_without_private_memory(self):
        for runtime in ("openclaw", "gemini", "antigravity"):
            with self.subTest(runtime=runtime):
                root = self.root / runtime
                root.mkdir()
                (root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
                (root / "README.md").write_text("# Public handoff\n", encoding="utf-8")
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True)
                store = Journal(root)
                state = store.create("clone", "Public rules fixture")
                result = project_context.initialize(store, state, runtime)
                subprocess.run(["git", "-C", str(root), "add", "."], check=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                                "user.email=test@example.invalid", "commit", "-qm", "rules"], check=True)
                clone = self.root / (runtime + "-clone")
                subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(root), str(clone)], check=True)
                self.assertFalse((clone / ".aispa").exists())
                for name in result["instructionFiles"]:
                    self.assertEqual((clone / name).read_bytes(), (root / name).read_bytes())
                    self.assertIn("README.md", (clone / name).read_text(encoding="utf-8"))

    def test_new_hosts_keep_real_usage_and_reject_duplicate_or_mismatched_claims(self):
        for runtime in ("openclaw", "gemini", "antigravity"):
            with self.subTest(runtime=runtime):
                store = Journal(self.root / runtime)
                state = store.create("usage", "Synthetic host evidence")
                actor = participants.register(store, state, "coordinator", runtime,
                                              "fixture-agent", "fixture-session")
                empty = telemetry.summary(store, state)
                self.assertFalse(empty["complete"])
                row = {"actor": actor, "callId": "fixture-call", "nativeCallId": "fixture-response",
                       "runtime": runtime, "source": "synthetic adapter test",
                       "inputTokens": 40, "cachedInputTokens": 10, "outputTokens": 9}
                measured = telemetry.record(store, state, row)
                self.assertEqual(measured["inputTokens"], 40)
                self.assertEqual(measured["measuredCalls"], 1)
                self.assertIsNone(measured["reasoningOutputTokens"])
                self.assertFalse(measured["complete"])
                with self.assertRaises(Invalid):
                    telemetry.record(store, state, row)
                with self.assertRaisesRegex(Invalid, "runtime"):
                    telemetry.record(store, state, dict(row, callId="other", runtime="codex"))

    def test_020_active_run_is_not_migrated_and_terminal_handoff_is_read_only(self):
        with patch("aispa_core.store.VERSION", "0.2.0"):
            state = self.store.create("old", "Pinned old fixture")
        before = (self.store.run / "events.jsonl").read_bytes()
        for handoff in (False, True):
            with self.assertRaisesRegex(Invalid, "закреплённым runtime"):
                self.store.load(handoff_only=handoff)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), before)
        state["status"] = "cancelled"
        self.store.append(state, "synthetic-old-runtime-stop")
        terminal = (self.store.run / "events.jsonl").read_bytes()
        self.assertEqual(self.store.load(handoff_only=True)["version"], "0.2.0")
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), terminal)
        self.assertEqual(self.store.create("new", "New adapter run")["version"], VERSION)


if __name__ == "__main__":
    unittest.main()
