import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import integration, team
from aispa_core.store import Invalid, Journal, digest


class IntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-integration-")
        self.root = Path(self.temp.name).resolve()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        (self.root / "b.txt").write_text("b\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "-qm", "baseline")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        self.store = Journal(self.root)
        self.state = self.store.create("integration", "Integrated review")
        integration.capture(self.store, self.state)
        self.state["tasks"] = [
            self.task("T01", "a.txt"),
            self.task("T02", "b.txt", ["T01"]),
            self.task("T03", "review.txt", ["T02"], reads=["."]),
        ]
        self.state["integrationTask"] = "T03"
        self.evidence = self.store.artifact("evidence", b"Examined complete diff and test output.\n", ".txt")

    def tearDown(self):
        self.temp.cleanup()

    def task(self, tid, zone, deps=None, reads=None):
        return {"id": tid, "title": tid, "requirements": ["R01"],
                "dependsOn": deps or [], "zones": [zone], "readZones": reads or [],
                "criteria": [tid + " works"], "wave": 1, "status": "pending"}

    def report(self, role="critic1", failed=None):
        view = integration.context(self.store, self.state, "T03")
        branch = {"basis": view["basis"], "code": view["code"],
                  "files": view["files"], "changes": view["changes"],
                  "note": "Compared all changed paths, module consumers and security behavior.",
                  "evidence": self.evidence["file"],
                  "axes": [{"axis": axis, "status": "failed" if axis == failed else "passed",
                            "note": "Examined " + axis + " across integrated code.",
                            "evidence": self.evidence["file"]} for axis in integration.AXES]}
        return {"basis": view["candidateBasis"], "reviewer": role, "branchReview": branch}

    def saved_record(self, report):
        ref = self.store.artifact("reviews", json.dumps(report, ensure_ascii=False).encode(), ".json")
        return {"report": ref, "basis": report["basis"]}

    def test_code_records_preserve_existing_fingerprint_and_capture_exact_baseline(self):
        records = self.store.code_records()
        self.assertEqual(self.store.code(), digest(records))
        baseline = json.loads(self.store.text(self.state["integrationBaseline"]))
        self.assertEqual(baseline["records"], records)
        self.assertEqual(baseline["code"], self.store.code())
        self.assertEqual(len(baseline["head"]), 40)
        self.assertEqual(integration.capture(self.store, self.state), self.state["integrationBaseline"])

    def test_unique_final_task_must_cover_all_dependencies_and_actual_tree(self):
        self.assertEqual(integration.choose_task(self.store, self.state, self.state["tasks"]), "T03")
        weak = copy.deepcopy(self.state["tasks"])
        weak[-1]["readZones"] = ["a.txt", "b.txt"]
        with self.assertRaises(Invalid):
            integration.choose_task(self.store, self.state, weak)
        weak = copy.deepcopy(self.state["tasks"])
        weak[-1]["dependsOn"] = ["T01"]
        with self.assertRaises(Invalid):
            integration.choose_task(self.store, self.state, weak)
        one = [self.task("T01", "a.txt", reads=["."])]
        self.assertEqual(integration.choose_task(self.store, self.state, one), "T01")

    def test_context_includes_unstaged_staged_untracked_deleted_and_mode_changes(self):
        (self.root / "a.txt").write_text("edited\n", encoding="utf-8")
        if os.name != "nt":
            (self.root / "a.txt").chmod(0o755)
        (self.root / "b.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "b.txt"], check=True)
        (self.root / "новый файл.txt").write_text("untracked\n", encoding="utf-8")
        view = integration.context(self.store, self.state)
        self.assertEqual(view["files"], [row["path"] for row in view["changes"]])
        self.assertEqual(set(view["files"]), {"a.txt", "b.txt", "новый файл.txt"})
        self.assertEqual(view["code"], self.store.code())
        self.assertEqual(view["changes"][-1]["before"], None)
        if os.name != "nt":
            changed = next(row for row in view["changes"] if row["path"] == "a.txt")
            self.assertEqual((changed["beforeMode"], changed["afterMode"]), ("100644", "100755"))
        report = self.report()
        artifacts, failures = integration.check(self.store, self.state, "T03", "critic1", report)
        self.assertEqual(failures, [])
        self.assertEqual(artifacts, [self.evidence])
        missing = copy.deepcopy(report)
        missing["branchReview"]["files"].pop()
        with self.assertRaises(Invalid):
            integration.check(self.store, self.state, "T03", "critic1", missing)
        (self.root / "a.txt").unlink()
        self.assertIn("a.txt", integration.context(self.store, self.state)["files"])

    def test_failed_axis_uses_existing_critic_failure_and_historical_report_survives_repair(self):
        (self.root / "a.txt").write_text("candidate one\n", encoding="utf-8")
        first = self.report(failed="data_security")
        refs, failures = integration.check(self.store, self.state, "T03", "critic1", first)
        self.assertEqual(refs, [self.evidence])
        self.assertEqual([row["axis"] for row in failures], ["data_security"])
        sealed = self.saved_record(first)
        self.assertTrue(integration.record_valid(self.store, self.state, "T03", "critic1", sealed))
        (self.root / "a.txt").write_text("candidate repaired\n", encoding="utf-8")
        self.assertTrue(integration.record_valid(self.store, self.state, "T03", "critic1", sealed))
        with self.assertRaises(Invalid):
            integration.check(self.store, self.state, "T03", "critic1", first)
        second = self.report(role="critic2")
        self.assertNotEqual(first["branchReview"]["basis"], second["branchReview"]["basis"])
        self.assertEqual(integration.check(self.store, self.state, "T03", "critic2", second)[1], [])

    def test_historical_report_requires_its_sealed_candidate_and_current_final_code(self):
        (self.root / "a.txt").write_text("final candidate\n", encoding="utf-8")
        first, second = self.report(), self.report(role="critic2")
        first_record, second_record = self.saved_record(first), self.saved_record(second)
        self.state["taskHistory"] = {"T03": [{"id": "new-attempt", "rounds": [first_record, second_record]}]}
        self.assertTrue(integration.record_valid(self.store, self.state, "T03", "critic2", second_record))
        with patch.object(team, "direct_proof_current", return_value=True):
            # Adding taskHistory changes the candidate basis, so fresh reports
            # must be generated after role/attempt creation in a real run.
            self.assertFalse(integration.proof_valid(self.store, self.state))
        changed = copy.deepcopy(second)
        changed["branchReview"]["changes"][0]["after"]["sha256"] = "0" * 64
        self.assertFalse(integration.record_valid(self.store, self.state, "T03", "critic2", self.saved_record(changed)))


class FocusedLensFallbackTests(unittest.TestCase):
    """F6: a failure to find the candidate never lends it the lens set of the previous candidate."""

    def test_a_context_failure_gives_the_lenses_of_the_current_mode_not_of_the_previous_candidate(self):
        from aispa_core import focused_review
        with tempfile.TemporaryDirectory(prefix="aispa-lens-fallback-") as temp:
            store = Journal(Path(temp).resolve())
            state = store.create("lens-fallback", "Lens fallback")
            state.update(policy={"focusedReview": True, "leanControl": True, "integrationReview": True},
                         checkMode="max")
            # The previous candidate was reviewed in balanced mode; the owner has since switched to max.
            state["projectIntegrationReviewHistory"] = [
                {"integrationBasis": "previous-candidate", "lens": ["behavior", "data_security"],
                 "checkMode": "balanced"}]
            with patch.object(integration, "context", side_effect=Invalid("кандидат не вычислен")):
                got = integration.lenses(state, store)
            self.assertEqual(got, [list(lens) for lens in focused_review.LENSES["max"]])


if __name__ == "__main__":
    unittest.main()
