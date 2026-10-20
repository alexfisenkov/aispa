import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import carryover, executions, project_context
from aispa_core.store import Invalid, Journal
from aispa_core.workflow import Workflow


class JournalConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-verified-consumers-")
        self.root = Path(self.temp.name).resolve()
        self.store = Journal(self.root)
        self.state = self.store.create("consumers", "Verified journal consumers")

    def tearDown(self):
        self.temp.cleanup()

    def append(self, event):
        self.state["notes"].append(event)
        self.store.append(self.state, event)

    def corrupt_first_event_preserving_mtime(self):
        path = self.store.run / "events.jsonl"
        stat = path.stat()
        lines = path.read_bytes().splitlines(keepends=True)
        first = json.loads(lines[0])
        first["event"] = "forged-init"
        lines[0] = (json.dumps(first, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        path.write_bytes(b"".join(lines))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_full_sha_and_sequence_shared_by_context_and_carryover(self):
        self.append("first")
        self.append("second")
        expected = hashlib.sha256((self.store.run / "events.jsonl").read_bytes()).hexdigest()
        context = project_context._journal(self.store, self.state)
        carry = carryover._journal(self.store, self.state)
        self.assertEqual(context["sha256"], expected)
        self.assertEqual(carry["sha256"], expected)
        self.assertEqual(context["sequence"], 3)
        self.assertEqual(carry["last"]["sequence"], 3)
        self.assertEqual(context["eventDigest"], carry["last"]["hash"])

    def test_earlier_tamper_rejected_even_when_last_event_and_mtime_unchanged(self):
        self.append("first")
        self.append("second")
        self.assertTrue(project_context._journal(self.store, self.state))
        self.assertTrue(carryover._journal(self.store, self.state))
        self.corrupt_first_event_preserving_mtime()
        with self.assertRaises(Invalid):
            project_context._journal(self.store, self.state)
        with self.assertRaises(Invalid):
            carryover._journal(self.store, self.state)

    def test_caller_state_and_returned_summary_cannot_mutate_verified_events(self):
        self.append("saved")
        baseline = self.store.read_journal(include_events=True)
        original_hash = baseline["last"]["hash"]
        baseline["last"]["state"]["notes"].append("forged-output")
        baseline["events"][0]["event"] = "forged-output"
        self.state["notes"].append("unsaved-caller-edit")
        fresh = self.store.read_journal(include_events=True)
        self.assertEqual(fresh["last"]["hash"], original_hash)
        self.assertEqual(fresh["last"]["state"]["notes"], ["saved"])
        self.assertEqual(fresh["events"][0]["event"], "init")
        with self.assertRaises(Invalid):
            carryover._journal(self.store, self.state)

    def test_execution_resolve_uses_one_verified_chain_and_rejects_early_tamper(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        flow = Workflow(self.store, self.state)
        refs = []
        for number in range(3):
            flow.verify([sys.executable, "-c", "print(" + str(number) + ")"], 10)
            refs.append(executions.record(self.store, self.state, kind="project", purpose="check"))
            self.store.append(self.state, "execution-recorded")
        with patch.object(self.store, "read_journal", wraps=self.store.read_journal) as reader:
            listed = executions.view(self.store, self.state)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(len(listed["executions"]), 3)
        self.assertEqual(executions.resolve(self.store, self.state, refs[0])["command"][-1], "print(0)")
        self.corrupt_first_event_preserving_mtime()
        with self.assertRaises(Invalid):
            executions.resolve(self.store, self.state, refs[0])

    def test_addressed_event_and_task_projection_are_detached_and_recheck_bytes(self):
        self.state["taskHistory"] = {"T01": [{"id": "A1", "testIntentHistory": [{"file": "example", "sha256": "a"}],
                                            "privateLongField": "not copied into the timeline"}]}
        self.append("task-submitted:T01")
        event, previous = self.store.event_pair(2)
        self.assertEqual(previous["event"], "init")
        event["state"]["notes"].append("forged")
        rows = self.store.task_timeline("T01")
        projected = rows[-1]["state"]["taskHistory"]["T01"][-1]
        self.assertEqual(set(projected), {"id", "testIntentHistory"})
        projected["testIntentHistory"][0]["file"] = "forged"
        self.assertEqual(self.store.event_pair(2)[0]["state"]["notes"], ["task-submitted:T01"])
        self.assertEqual(self.store.task_timeline("T01")[-1]["state"]["taskHistory"]["T01"][-1]
                         ["testIntentHistory"][0]["file"], "example")
        self.corrupt_first_event_preserving_mtime()
        with self.assertRaises(Invalid):
            self.store.event_pair(2)
        with self.assertRaises(Invalid):
            self.store.task_timeline("T01")


if __name__ == "__main__":
    unittest.main()
