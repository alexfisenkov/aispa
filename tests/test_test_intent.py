import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import executions, test_intent
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, Journal, file_hash
from aispa_core.workflow import Workflow


class TestIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-test-intent-🙂-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
        (self.root / "tests").mkdir()
        self.test_file = self.root / "tests/test_app.py"
        self.test_file.write_text("assert VALUE == 1\n", encoding="utf-8")
        (self.root / "README.md").write_text("Integration notes\n", encoding="utf-8")
        self.store = Journal(self.root)
        self.state = self.store.create("intent", "Test intent fixture")
        self.state["policy"] = {"testIntent": True}
        self.state["spec"] = self.store.artifact("specs", b"Set VALUE to one", ".md")
        self.state["tasks"] = [{"id": "T01", "title": "Value", "requirements": [],
                                "dependsOn": [], "zones": ["app.py", "tests"],
                                "readZones": [], "criteria": ["VALUE equals one"],
                                "wave": 1, "status": "running"}]
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "roles": {}, "actors": {},
                                               "rounds": [], "submitted": None, "acceptance": None,
                                               "terminal": False}]}
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.store.append(self.state, "fixture-task-started")
        self.flow = Workflow(self.store, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def red(self, failure="tests/test_app.py: expected VALUE equals one"):
        command = [sys.executable, "-c", "import runpy; " + f"print({failure!r}); " +
                   "scope=runpy.run_path('app.py'); exec(compile(open('tests/test_app.py').read(),'tests/test_app.py','exec'),scope)"]
        self.assertFalse(self.flow.verify(command, 10, "T01"))
        ref = executions.record(self.store, self.state, kind="task", purpose="check", task_id="T01")
        self.store.append(self.state, "execution-recorded")
        return ref

    def green(self):
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertTrue(self.flow.verify([sys.executable, "-c",
            "import runpy; scope=runpy.run_path('app.py'); exec(compile(open('tests/test_app.py').read(),'tests/test_app.py','exec'),scope)"], 10, "T01"))

    def behavior_report(self, red_ref, failure="tests/test_app.py: expected VALUE equals one"):
        return {"mode": "behavior_change", "attempt": "T01-A01",
                "basis": task_basis(self.store, self.state, "T01"),
                "criteria": ["VALUE equals one"], "redExecution": red_ref,
                "newTestFiles": [{"path": "tests/test_app.py", "sha256": file_hash(self.test_file),
                                  "expectedFailure": failure, "criteria": ["VALUE equals one"]}]}

    def record_behavior(self, red_ref):
        ref = test_intent.record(self.store, self.state, "T01", self.behavior_report(red_ref))
        self.store.append(self.state, "test-intent-recorded:T01")
        return ref

    def test_behavior_red_then_distinct_fresh_green(self):
        red_ref = self.red()
        contract = self.record_behavior(red_ref)
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))
        self.green()
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        status = test_intent.view(self.store, self.state, "T01")
        self.assertEqual((status["mode"], status["sourceCount"], status["revision"]),
                         ("behavior_change", 1, 1))
        self.assertEqual(self.state["taskHistory"]["T01"][-1]["testIntentHistory"], [contract])
        self.test_file.write_text("assert VALUE == 2\n", encoding="utf-8")
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))

    def test_wrong_red_and_old_red_after_test_change_rejected(self):
        red_ref = self.red()
        wrong = self.behavior_report(red_ref, failure="tests/test_app.py: something else")
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", wrong)
        self.record_behavior(red_ref)
        self.green()
        self.test_file.write_text("assert VALUE == 2\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", self.behavior_report(red_ref))

    def test_changed_test_requires_new_red_before_next_implementation(self):
        first_red = self.red()
        first = self.record_behavior(first_red)
        self.green()
        self.state["taskHistory"]["T01"][-1]["submitted"] = {"fixture": True}
        self.store.append(self.state, "task-submitted:T01")
        self.test_file.write_text("assert VALUE == 2\n", encoding="utf-8")
        second_red = self.red("tests/test_app.py: expected VALUE equals two")
        report = self.behavior_report(second_red, "tests/test_app.py: expected VALUE equals two")
        second = test_intent.record(self.store, self.state, "T01", report)
        self.store.append(self.state, "test-intent-revised:T01")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.state["taskHistory"]["T01"][-1]["testIntentHistory"]), 2)
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertTrue(self.flow.verify([sys.executable, "-c", "print('passed')"], 10, "T01"))
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        self.state["taskHistory"]["T01"][-1]["rounds"].append({"number": 1})
        self.store.append(self.state, "critic-started:T01")
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", report)

    def test_not_applicable_is_criterion_bound_and_allows_existing_behavior(self):
        source = {"path": "README.md", "sha256": file_hash(self.root / "README.md")}
        report = {"mode": "not_applicable", "attempt": "T01-A01",
                  "basis": task_basis(self.store, self.state, "T01"),
                  "criteria": ["VALUE equals one"],
                  "notApplicable": [{"criterion": "VALUE equals one", "purpose": "existing_behavior",
                                     "source": source, "reason": "Existing integration already checks this value."}]}
        test_intent.record(self.store, self.state, "T01", report)
        self.store.append(self.state, "test-intent-recorded:T01")
        self.assertTrue(self.flow.verify([sys.executable, "-c", "import runpy; assert runpy.run_path('app.py')['VALUE']==0"], 10, "T01"))
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        (self.root / "README.md").write_text("Changed notes\n", encoding="utf-8")
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))

    def test_launch_failure_cannot_be_recorded_as_red(self):
        self.assertFalse(self.flow.verify(["aispa-command-that-does-not-exist", "tests/test_app.py"], 10, "T01"))
        ref = executions.record(self.store, self.state, kind="task", purpose="check", task_id="T01")
        self.store.append(self.state, "execution-recorded")
        self.assertTrue(executions.resolve(self.store, self.state, ref)["launchError"])
        report = self.behavior_report(ref, "AISpa: command could not start.")
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", report)

    def test_docs_exception_cannot_hide_implementation_change(self):
        report = {"mode": "not_applicable", "attempt": "T01-A01",
                  "basis": task_basis(self.store, self.state, "T01"), "criteria": ["VALUE equals one"],
                  "notApplicable": [{"criterion": "VALUE equals one", "purpose": "docs",
                                    "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")},
                                    "reason": "Documentation-only clarification."}]}
        test_intent.record(self.store, self.state, "T01", report)
        self.store.append(self.state, "test-intent-recorded:T01")
        self.green()
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))

    def test_secret_test_source_is_rejected_before_hashing(self):
        path = self.root / ".env"
        path.write_text("FAKE_TEST_ONLY", encoding="utf-8")
        with self.assertRaises(Invalid):
            test_intent._file(self.store, {"path": ".env", "sha256": file_hash(path)})

    def test_empty_reason_and_first_record_after_submit_rejected(self):
        source = {"path": "README.md", "sha256": file_hash(self.root / "README.md")}
        report = {"mode": "not_applicable", "attempt": "T01-A01",
                  "basis": task_basis(self.store, self.state, "T01"),
                  "criteria": ["VALUE equals one"],
                  "notApplicable": [{"criterion": "VALUE equals one", "purpose": "docs",
                                     "source": source, "reason": ""}]}
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", report)
        report["notApplicable"][0]["reason"] = "Documentation-only clarification."
        self.store.append(self.state, "task-submitted:T01")
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", report)

    def test_policy_off_leaves_old_run_unaffected(self):
        self.state["policy"]["testIntent"] = False
        self.store.append(self.state, "legacy-policy")
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        self.assertFalse(test_intent.view(self.store, self.state, "T01")["present"])


if __name__ == "__main__":
    unittest.main()
