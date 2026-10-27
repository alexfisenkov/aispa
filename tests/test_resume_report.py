"""Stories 84-86: resume in a new chat shows what changed in the code since the last stop."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.store import Invalid
import test_workflow as helpers
from test_preflight import ManagedFixture


class ResumeReportTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file
    prepare = helpers.WorkflowTests.prepare
    coverage = helpers.WorkflowTests.coverage
    header = helpers.WorkflowTests.header

    def enable(self, flag=True):
        self.state["policy"] = {"resumeReport": True} if flag else {}
        (self.root / "old.py").write_text("OLD = 1\n", encoding="utf-8")
        self.commit()

    def snapshot(self):
        ref = self.state["lastCodeSnapshot"]
        payload = self.store.json(ref)
        return ref, payload

    def edit_tree(self):
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        (self.root / "added.py").write_text("NEW = 1\n", encoding="utf-8")
        (self.root / "old.py").unlink()

    def test_close_snapshot_then_resume_reports_changed_added_removed(self):
        self.enable()
        self.flow.close("paused", "Owner asked to stop for today")
        ref, payload = self.snapshot()
        self.assertIn("records", payload)
        self.assertIn("app.py", [name for name, _ in payload["records"]])
        self.edit_tree()
        receipt = self.flow.resume()
        changes = receipt["codeChanges"]
        self.assertEqual(changes["changed"], ["app.py"])
        self.assertEqual(changes["added"], ["added.py"])
        self.assertEqual(changes["removed"], ["old.py"])
        self.assertEqual(changes["total"], 3)

    def test_wait_and_general_verify_refresh_the_snapshot(self):
        self.enable()
        self.flow.brief(self.file("Команда выводит ready.", "brief.md"))
        self.state["mode"] = "interview"
        self.flow.wait("Which output format?")
        first, _ = self.snapshot()
        self.flow.answered(self.file("Plain text.", "answer.md"))
        (self.root / "app.py").write_text("print('ready')\nprint('x')\n", encoding="utf-8")
        self.flow.verify([sys.executable, "-c", "print('ok')"], 10)
        second, payload = self.snapshot()
        self.assertNotEqual(first, second)
        self.flow.close("paused", "stop")
        self.assertEqual(self.flow.resume()["codeChanges"]["total"], 0)

    def test_task_verify_does_not_replace_the_snapshot(self):
        self.enable()
        self.prepare()
        self.flow.close("paused", "stop")
        ref, _ = self.snapshot()
        self.flow.resume()
        self.flow.start("T01", helpers.basis(self.store, self.state, "task", "T01"))
        self.flow.verify([sys.executable, "-c", "print('ok')"], 10, "T01")
        self.assertEqual(self.state["lastCodeSnapshot"], ref)

    def test_long_lists_are_capped_but_total_counts_everything(self):
        self.enable()
        self.flow.close("paused", "stop")
        for index in range(205):
            (self.root / ("generated_%03d.py" % index)).write_text("X = %d\n" % index, encoding="utf-8")
        changes = self.flow.resume()["codeChanges"]
        self.assertEqual(len(changes["added"]), 200)
        self.assertEqual(changes["total"], 205)

    def test_old_policy_keeps_the_old_resume_and_takes_no_snapshot(self):
        self.enable(flag=False)
        self.flow.close("paused", "stop")
        self.assertNotIn("lastCodeSnapshot", self.state)
        self.assertIsNone(self.flow.resume())

    def test_resume_without_snapshot_reports_no_baseline(self):
        self.enable()
        self.state["status"] = "paused"
        self.flow.save("paused-before-the-policy-snapshot")
        self.assertEqual(self.flow.resume()["codeChanges"], {"baseline": None})


class ManagedResumeReportTests(ManagedFixture):
    def test_running_continuation_in_a_new_chat_reports_changes(self):
        self.managed()
        self.state["policy"]["resumeReport"] = True
        self.flow.save("policy-fixture")
        self.api().adopt(self.flow, self.host(), expected_basis=None)
        self.flow.verify([sys.executable, "-c", "print('ok')"], 10)
        (self.root / "app.py").write_text("print('edited elsewhere')\n", encoding="utf-8")
        report = self.flow.resume(host=self.host(session="next-chat"))
        self.assertEqual(report["codeChanges"]["changed"], ["app.py"])
        self.assertEqual(self.state["continuations"][-1]["session"], "next-chat")


if __name__ == "__main__":
    unittest.main()
