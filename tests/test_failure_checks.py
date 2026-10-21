import copy
import unittest

import test_test_intent as fixture
from aispa_core import failure_checks, test_intent
from aispa_core.store import Invalid, file_hash


class FailureCheckTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TestIntentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.state["policy"]["errorBoundaryChecks"] = True
        self.fx.store.append(self.fx.state, "fixture-error-policy")

    def report(self):
        report = self.fx.behavior_report(self.fx.red())
        report["failureChecks"] = {
            "cases": [{"id": "E01", "boundary": "input",
                       "scenario": "An incorrect initial value must be rejected by the test",
                       "expected": "Assertion failure before changing VALUE",
                       "criterion": "VALUE equals one",
                       "test": {"path": "tests/test_app.py", "sha256": file_hash(self.fx.test_file)}}],
            "notApplicable": [{"boundaries": ["output", "storage", "protocol", "dependency"],
                               "reason": "This fixture changes only one pure value; no external boundary exists.",
                               "source": {"path": "README.md", "sha256": file_hash(self.fx.root / "README.md")}}]}
        return report

    def save(self, report):
        result = test_intent.record(self.fx.store, self.fx.state, "T01", report)
        self.fx.store.append(self.fx.state, "test-intent-recorded:T01")
        return result

    def test_missing_matrix_rejected_without_record(self):
        report = self.fx.behavior_report(self.fx.red())
        with self.assertRaises(Invalid):
            self.save(report)
        self.assertNotIn("testIntent", self.fx.state["taskHistory"]["T01"][-1])

    def test_real_red_green_and_stale_failure_source(self):
        self.save(self.report())
        self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, "T01"))
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, "T01"))
        self.assertEqual(test_intent.view(self.fx.store, self.fx.state, "T01")["failureCaseCount"], 1)
        (self.fx.root / "README.md").write_text("A new external boundary exists now.\n")
        self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, "T01"))
        self.assertTrue(test_intent.history_valid(self.fx.store, self.fx.state, "T01"))
        self.assertIn("README.md", [s["path"] for s in test_intent.source_changes(self.fx.store, self.fx.state, "T01")])

    def test_missing_overlap_unknown_and_duplicate_boundaries_rejected(self):
        valid = self.report()
        for change in (lambda x: x["notApplicable"][0]["boundaries"].remove("output"),
                       lambda x: x["notApplicable"][0]["boundaries"].append("input"),
                       lambda x: x["notApplicable"][0]["boundaries"].append("network_guess"),
                       lambda x: x["cases"].append(copy.deepcopy(x["cases"][0]))):
            report = copy.deepcopy(valid)
            change(report["failureChecks"])
            with self.assertRaises(Invalid):
                self.save(report)

    def test_case_must_link_exact_red_source_and_criterion(self):
        valid = self.report()
        for field, value in (("criterion", "Undeclared criterion"),
                             ("test", {"path": "README.md", "sha256": file_hash(self.fx.root / "README.md")}),
                             ("expected", "")):
            report = copy.deepcopy(valid)
            report["failureChecks"]["cases"][0][field] = value
            with self.assertRaises(Invalid):
                self.save(report)

    def test_arbitrary_pass_or_missing_na_reason_rejected(self):
        report = self.report()
        report["failureChecks"]["cases"][0]["passed"] = True
        with self.assertRaises(Invalid):
            self.save(report)
        del report["failureChecks"]["cases"][0]["passed"]
        report["failureChecks"]["notApplicable"][0]["reason"] = ""
        with self.assertRaises(Invalid):
            self.save(report)

    def test_secret_or_escaping_na_source_rejected(self):
        report = self.report()
        for name in ("../outside.md", ".env", "/tmp/source.md"):
            report["failureChecks"]["notApplicable"][0]["source"] = {"path": name, "sha256": "0" * 64}
            with self.assertRaises(Invalid):
                self.save(report)

    def test_template_is_not_evidence(self):
        report = self.report()
        report["failureChecks"] = failure_checks.template()["failureChecks"]
        with self.assertRaises(Invalid):
            self.save(report)


if __name__ == "__main__":
    unittest.main()
