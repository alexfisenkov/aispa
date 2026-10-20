"""Actual executor receipts, bounded history and non-current command evidence."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import cleanroom, executions, knowledge
from aispa_core.store import Invalid, Journal, digest
from aispa_core.workflow import Workflow


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa executions space-🙂-")
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('one')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.store = Journal(self.root)
        self.state = self.store.create("executions", "Execution history")
        self.flow = Workflow(self.store, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, command, purpose="check"):
        self.flow.verify(command, 10)
        ref = executions.record(self.store, self.state, kind="project", purpose=purpose)
        self.store.append(self.state, "execution-recorded")
        return ref

    def test_multiple_actual_commands_and_failed_command_cannot_be_curated(self):
        first = self.execute([sys.executable, "app.py"], "run")
        failed = self.execute([sys.executable, "-c", "raise SystemExit(3)"], "test")
        third = self.execute([sys.executable, "-c", "print('three')"], "check")
        view = executions.view(self.store, self.state)
        self.assertEqual([row["purpose"] for row in view["executions"]], ["check", "test", "run"])
        self.assertEqual([row["successful"] for row in view["executions"]], [True, False, True])
        self.assertNotIn("command", view["executions"][0])
        self.assertNotIn("file", view["executions"][0])
        self.assertEqual(executions.resolve(self.store, self.state, first)["command"], [sys.executable, "app.py"])
        self.assertEqual(executions.resolve(self.store, self.state, third)["exitCode"], 0)
        self.assertFalse(executions.resolve(self.store, self.state, failed)["successful"])

    def test_unsaved_forged_receipt_and_wrong_event_rejected(self):
        self.flow.verify([sys.executable, "app.py"], 10)
        self.state["verification"]["command"] = [sys.executable, "not-run.py"]
        with self.assertRaises(Invalid):
            executions.record(self.store, self.state, kind="project")
        self.state = self.store.load()
        self.flow = Workflow(self.store, self.state)
        self.store.append(self.state, "unrelated-event")
        with self.assertRaises(Invalid):
            executions.record(self.store, self.state, kind="project")

    def test_bad_hash_and_tampered_output_rejected(self):
        ref = self.execute([sys.executable, "app.py"])
        with self.assertRaises(Invalid):
            executions.resolve(self.store, self.state, {"sha256": "0" * 64})
        output = self.state["verification"]["output"]["file"]
        (self.store.run / output).write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            executions.resolve(self.store, self.state, ref)

    def test_rehashed_forgery_cannot_turn_failed_receipt_into_success(self):
        original = self.execute([sys.executable, "-c", "raise SystemExit(3)"])
        body = copy.deepcopy(executions.resolve(self.store, self.state, original))
        body["successful"] = True
        body["id"] = digest({key: value for key, value in body.items() if key != "id"})
        forged = {**executions._write(self.store, "record", body), "id": body["id"]}
        self.state["executions"]["recent"][-1] = forged
        self.store.append(self.state, "test-forged-index")
        with self.assertRaises(Invalid):
            executions.resolve(self.store, self.state, forged)

    def test_rollover_preserves_old_refs_without_global_limit(self):
        with patch.object(executions, "HOT_LIMIT", 2):
            refs = [self.execute([sys.executable, "-c", f"print({i})"]) for i in range(6)]
            self.assertEqual(self.state["executions"]["count"], 6)
            self.assertLessEqual(len(self.state["executions"]["recent"]), 2)
            self.assertEqual(executions.resolve(self.store, self.state, refs[0])["command"][-1], "print(0)")
            self.assertEqual(len(executions.view(self.store, self.state, limit=6)["executions"]), 6)

    def test_task_receipt_scope_and_attempt_are_preserved(self):
        self.execute([sys.executable, "-c", "print('older project check')"])
        self.state["tasks"] = [{"id": "T01", "title": "Execute", "requirements": [],
                                "dependsOn": [], "zones": ["app.py"], "criteria": [], "wave": 1,
                                "status": "running"}]
        self.state["taskHistory"] = {"T01": [{"id": "attempt-1", "roles": {}}]}
        self.store.append(self.state, "test-task-fixture")
        self.assertTrue(self.flow.verify([sys.executable, "app.py"], 10, "T01"))
        with self.assertRaises(Invalid):
            executions.record(self.store, self.state, kind="project", purpose="run")
        ref = executions.record(self.store, self.state, kind="task", task_id="T01", purpose="test")
        self.store.append(self.state, "execution-recorded")
        body = executions.resolve(self.store, self.state, ref)
        self.assertEqual((body["task"], body["attempt"], body["candidate"]),
                         ("T01", "attempt-1", self.state["taskHistory"]["T01"][-1]["verification"]["candidate"]))
        with self.assertRaises(Invalid):
            executions.resolve(self.store, self.state, {"id": "0" * 64, "sha256": ref["sha256"]})

    def test_cleanrun_record_uses_saved_clone_receipt(self):
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["activity"] = {"kind": "clean-verification"}
        self.store.append(self.state, "clean-verification-started")
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 10)
        self.state.pop("activity")
        self.store.append(self.state, "clean-verification-recorded")
        ref = executions.record(self.store, self.state, kind="cleanrun", purpose="install")
        self.store.append(self.state, "execution-recorded")
        body = executions.resolve(self.store, self.state, ref)
        self.assertEqual(body["kind"], "cleanrun")
        self.assertTrue(body["successful"])
        self.assertEqual(body["output"], self.state["cleanrun"]["output"])
        self.assertNotIn("path", executions.view(self.store, self.state)["executions"][0])

    def test_historical_success_does_not_prove_current_pass(self):
        old = self.execute([sys.executable, "app.py"])
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertNotEqual(executions.resolve(self.store, self.state, old)["code"], self.store.code())
        self.assertFalse(knowledge.verified(self.store, self.state))
        with self.assertRaises(Invalid):
            executions.record(self.store, self.state, kind="project")

    def test_credential_assignment_is_rejected_before_execute_or_journal(self):
        before = (self.store.run / "events.jsonl").read_bytes()
        with patch("aispa_core.process_runner.run") as runner:
            with self.assertRaises(Invalid):
                self.flow.verify([sys.executable, "app.py", "--password=abcdefgh"], 10)
            runner.assert_not_called()
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
