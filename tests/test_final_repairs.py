"""Focused finish (0.5.0): the branch is reviewed once by 1-3 lens reviewers,
confirmed findings are fixed by one final repair checked by its own diff, and
every new candidate joins the chain of checked candidates."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import final_repairs, findings, focused_review, integration, participants, team
from aispa_core.store import Invalid, Journal

POLICY = {"focusedReview": True, "leanControl": True, "integrationReview": True}


class BranchFixture(unittest.TestCase):
    mode = "balanced"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-final-repairs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.txt").write_text("Before\n", encoding="utf-8")
        (self.root / "other.txt").write_text("Other\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "-qm", "baseline")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        self.store = Journal(self.root)
        self.state = self.store.create("focused-finish", "Focused finish fixture")
        self.state.update(policy=dict(POLICY), checkMode=self.mode, status="running")
        proof = {"report": self.store.artifact("reviews", b"{}\n", ".json"), "artifacts": []}
        self.state["tasks"] = [{"id": "T01", "title": "App", "requirements": [], "dependsOn": [],
                                "zones": ["app.txt", "other.txt"], "readZones": ["."], "criteria": ["App works"],
                                "wave": 1, "status": "completed", "proof": proof}]
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "tier": "heavy", "actors": {"worker": "task-worker"},
                                              "writers": []}]}
        self.state["integrationTask"] = "T01"
        integration.capture(self.store, self.state)
        (self.root / "app.txt").write_text("After\n", encoding="utf-8")
        self.evidence = self.store.artifact("evidence", b"Reviewed the branch diff.\n", ".txt")
        for name in ("direct_proof_current", "task_proof_valid"):
            patcher = patch.object(team, name, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.serial = 0

    def reviewer(self, native=None):
        self.serial += 1
        native = native or "lens-reviewer-%d" % self.serial
        return participants.register(self.store, self.state, "integration-reviewer", "fixture", native,
                                     "session-" + native, model="host-exposed-model")

    def lens_report(self, actor, lens, *, failed=None, triage=None, found=()):
        report = integration.project_context(self.store, self.state, actor)["template"]
        report["lens"] = list(lens)
        branch = report["branchReview"]
        branch.update(note="Read the whole branch diff.", evidence=self.evidence["file"],
                      axes=[{"axis": axis, "status": "failed" if axis == failed else "passed",
                             "note": "Inspected " + axis, "evidence": self.evidence["file"]} for axis in lens])
        report["findings"] = list(found)
        if failed:
            report["findings"].append({"id": "B1", "severity": "blocking", "blockingClass": "broken_scenario",
                                       "criterion": "Вся ветка: " + failed, "text": "Scenario breaks"})
        report["verdict"] = "FAIL" if failed else "PASS"
        if "behavior" in lens:
            report["triage"] = triage if triage is not None else [
                {"id": row["id"], "status": "confirmed", "note": "Real issue"} for row in findings.open_items(self.state)]
        else:
            report.pop("triage", None)
        return report

    def review_all(self, **first):
        lenses = focused_review.LENSES[self.mode]
        for index, lens in enumerate(lenses):
            extra = first if index == 0 else {}
            integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), lens, **extra))

    def warn(self, text="Name is unclear"):
        return findings.collect(self.state, {"kind": "task", "task": "T01", "attempt": "T01-A01", "round": 1,
                                             "report": {"file": "reviews/x.json", "sha256": "0" * 64}},
                                [{"id": "W", "severity": "warning", "criterion": "App works", "text": text}])


class LensTests(BranchFixture):
    def test_each_mode_needs_its_number_of_distinct_lenses(self):
        for mode, count in (("fast", 1), ("balanced", 2), ("max", 3)):
            with self.subTest(mode=mode):
                self.state.update(checkMode=mode, projectIntegrationReviewHistory=[], projectIntegrationReview=None)
                self.mode = mode
                lenses = focused_review.LENSES[mode]
                self.assertEqual(len(lenses), count)
                for lens in lenses:
                    self.assertFalse(integration.branch_reviewed(self.store, self.state))
                    integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), lens))
                self.assertTrue(integration.branch_reviewed(self.store, self.state))
                self.assertEqual(sorted(a for lens in lenses for a in lens), sorted(integration.AXES))

    def test_repeated_lens_task_participant_and_foreign_lens_are_refused(self):
        lens = focused_review.LENSES["balanced"][0]
        integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), lens))
        with self.assertRaisesRegex(Invalid, "линза этого кандидата уже записана"):
            integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), lens))
        with self.assertRaisesRegex(Invalid, "линза не из режима"):
            integration.project_review(self.store, self.state,
                                       self.lens_report(self.reviewer(), ["behavior"]))
        self.state["actors"] = {"task-worker": {"id": "task-worker", "nativeId": "native-task-worker"}}
        self.state["taskHistory"]["T01"][0]["actors"]["critic1"] = "native-critic"
        critic = self.reviewer("native-critic")
        with self.assertRaisesRegex(Invalid, "отличаться от исполнителей"):
            integration.project_review(self.store, self.state,
                                       self.lens_report(critic, focused_review.LENSES["balanced"][1]))

    def test_behavior_lens_decides_every_open_warning(self):
        self.warn("First")
        self.warn("Second")
        behavior, other = focused_review.LENSES["balanced"]
        partial = [{"id": "W001", "status": "confirmed", "note": "Real"}]
        with self.assertRaisesRegex(Invalid, "каждое открытое предупреждение"):
            integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), behavior, triage=partial))
        missing_reason = [{"id": "W001", "status": "confirmed", "note": "Real"}, {"id": "W002", "status": "dismissed", "note": ""}]
        with self.assertRaises(Invalid):
            integration.project_review(self.store, self.state,
                                       self.lens_report(self.reviewer(), behavior, triage=missing_reason))
        integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), other))
        self.assertFalse(integration.branch_reviewed(self.store, self.state))  # warnings still open
        decided = [{"id": "W001", "status": "confirmed", "note": "Real"},
                   {"id": "W002", "status": "dismissed", "note": "Intended wording"}]
        integration.project_review(self.store, self.state, self.lens_report(self.reviewer(), behavior, triage=decided))
        self.assertEqual([r["status"] for r in self.state["collectedFindings"]], ["confirmed", "dismissed"])
        self.assertEqual(self.state["collectedFindings"][1]["resolution"]["note"], "Intended wording")
        self.assertTrue(integration.branch_reviewed(self.store, self.state))
        self.assertFalse(integration.proof_valid(self.store, self.state))  # the confirmed one waits for the repair

    def test_failed_axis_needs_a_blocking_branch_finding_that_goes_to_the_repair(self):
        behavior = focused_review.LENSES["balanced"][0]
        report = self.lens_report(self.reviewer(), behavior, failed="behavior")
        report["findings"] = []
        with self.assertRaisesRegex(Invalid, "блокирующую находку"):
            integration.project_review(self.store, self.state, report)
        self.review_all(failed="behavior")
        row = self.state["collectedFindings"][-1]
        self.assertEqual((row["status"], row["severity"], row["source"]["kind"]), ("confirmed", "blocking", "branch"))

    def test_final_critic_reads_its_attempt_and_old_runs_keep_one_full_review(self):
        self.assertEqual(integration.check(self.store, self.state, "T01", "critic1", {}), ([], []))
        self.state["policy"] = {"integrationReview": True}
        with self.assertRaises(Invalid):
            integration.check(self.store, self.state, "T01", "critic1", {})


class FindingsRepairTests(BranchFixture):
    def opened(self):
        self.warn()
        self.review_all()
        return final_repairs.open(self.store, self.state, "findings", None, ["app.txt"])

    def assign(self, role):
        self.serial += 1
        native = "repair-%s-%d" % (role, self.serial)
        return final_repairs.assign(self.store, self.state, role, runtime="fixture", native_id=native,
                                    session="s-" + native)

    def submit(self, receipt):
        return final_repairs.submit(self.store, self.state, {
            "repair": "F01", "worker": receipt["actor"], "summary": "Renamed",
            "items": [{"id": "W001", "note": "Renamed the field"}]})

    def check(self, receipt, status="fixed"):
        return final_repairs.check(self.store, self.state, {
            "repair": "F01", "checker": receipt["actor"], "independent": True, "candidate": self.store.code(),
            "verdict": "PASS" if status == "fixed" else "FAIL",
            "items": [{"id": "W001", "status": status, "note": "Read the diff"}]})

    def test_open_requires_reviewed_branch_and_takes_every_confirmed_finding(self):
        self.warn()
        with self.assertRaisesRegex(Invalid, "проверки всей ветки"):
            final_repairs.open(self.store, self.state, "findings", None, ["app.txt"])
        self.review_all()
        with self.assertRaisesRegex(Invalid, "все пункты сразу"):
            final_repairs.open(self.store, self.state, "findings", [], ["app.txt"])
        receipt = final_repairs.open(self.store, self.state, "findings", ["W001"], ["app.txt"])
        self.assertEqual(receipt["repair"]["items"], ["W001"])
        self.assertEqual(self.state["candidateChain"][0]["source"], "branch")
        self.assertNotIn("F01", [t["id"] for t in self.state["tasks"]])
        with self.assertRaisesRegex(Invalid, "открытый ремонт"):
            final_repairs.open(self.store, self.state, "findings", None, ["app.txt"])

    def test_checker_is_fresh_reads_the_diff_and_the_fix_closes_the_chain(self):
        self.opened()
        worker = self.assign("worker")
        with self.assertRaisesRegex(Invalid, "после сдачи"):
            self.assign("checker")
        (self.root / "app.txt").write_text("Fixed\n", encoding="utf-8")
        self.submit(worker)
        with self.assertRaisesRegex(Invalid, "свежий"):
            final_repairs.assign(self.store, self.state, "checker", runtime="fixture",
                                 native_id=self.state["finalRepairs"][0]["worker"]["nativeId"], session="other")
        checker = self.assign("checker")
        self.assertEqual([row["path"] for row in checker["packet"]["changes"]], ["app.txt"])
        self.assertEqual([row["id"] for row in checker["packet"]["items"]], ["W001"])
        with self.assertRaisesRegex(Invalid, "не назначенным"):
            final_repairs.check(self.store, self.state, {"repair": "F01", "checker": worker["actor"]})
        self.assertFalse(final_repairs.chain_current(self.store, self.state))
        self.check(checker)
        self.assertEqual(self.state["finalRepairs"][0]["status"], "passed")
        self.assertEqual(self.state["collectedFindings"][0]["status"], "fixed")
        self.assertTrue(final_repairs.chain_current(self.store, self.state))
        self.assertTrue(integration.proof_valid(self.store, self.state))
        (self.root / "app.txt").write_text("Edited after the check\n", encoding="utf-8")
        self.assertFalse(final_repairs.chain_current(self.store, self.state))
        self.assertFalse(integration.proof_valid(self.store, self.state))

    def test_repair_outside_its_zones_or_without_changes_is_refused(self):
        self.opened()
        worker = self.assign("worker")
        with self.assertRaisesRegex(Invalid, "не изменил"):
            self.submit(worker)
        (self.root / "other.txt").write_text("Outside\n", encoding="utf-8")
        with self.assertRaisesRegex(Invalid, "вне своих зон"):
            self.submit(worker)

    def test_two_rounds_then_the_unfixed_finding_remains(self):
        self.opened()
        worker = self.assign("worker")
        for number, text in ((1, "Try 1\n"), (2, "Try 2\n")):
            (self.root / "app.txt").write_text(text, encoding="utf-8")
            self.submit(worker)
            receipt = self.check(self.assign("checker"), status="not_fixed")
            self.assertEqual(receipt["round"], number)
        repair = self.state["finalRepairs"][0]
        self.assertEqual(repair["status"], "exhausted")
        self.assertEqual(self.state["collectedFindings"][0]["status"], "remaining")
        self.assertEqual([row["id"] for row in final_repairs.remaining_findings(self.state)], ["W001"])
        self.assertEqual(len(self.state["candidateChain"]), 3)
        self.assertTrue(final_repairs.chain_current(self.store, self.state))
        with self.assertRaises(Invalid):
            self.submit(worker)

    def test_repair_participants_cannot_review_the_branch_later(self):
        self.opened()
        worker = self.assign("worker")
        native = self.state["finalRepairs"][0]["worker"]["nativeId"]
        self.assertIn(native, final_repairs.used_identities(self.state))
        self.assertTrue(worker["actor"])
        with self.assertRaisesRegex(Invalid, "закрыта"):
            integration.project_review(self.store, self.state,
                                       self.lens_report(self.reviewer(), focused_review.LENSES["balanced"][0]))


class BlindRepairTests(BranchFixture):
    def setUp(self):
        super().setUp()
        brief = self.store.artifact("briefs", b"Build export.\n", ".md")
        self.state["briefs"] = [dict(brief, id="B001")]
        self.state["requirements"] = [{"id": "R01", "text": "Export", "kind": "requested", "scope": "active",
                                       "core": True, "brief": "B001", "quote": "Build export.",
                                       "acceptance": ["Export works"]}]

    def failed_acceptance(self, tag):
        report = self.store.artifact("reviews", ('{"tag": "%s"}\n' % tag).encode(), ".json")
        self.state.setdefault("gates", {})["acceptance"] = {
            "passed": False, "report": report, "artifacts": [], "unmapped": [],
            "failures": [{"requirement": "R01", "criterion": "blind:0", "status": "failed", "note": "Export missing"},
                         {"requirement": None, "criterion": "product-flow-review", "status": "blocked", "note": "x"}]}
        self.state["blindReview"] = {"report": self.store.artifact("blind-reviews", ('{"b": "%s"}\n' % tag).encode(), ".json")}

    def test_divergence_opens_a_blind_repair_and_a_third_cycle_is_refused(self):
        self.review_all()
        with patch("aispa_core.contracts.gate_current", return_value=True):
            for cycle in (1, 2):
                self.failed_acceptance(str(cycle))
                receipt = final_repairs.open(self.store, self.state, "blind", None, ["app.txt"])
                repair = receipt["repair"]
                self.assertEqual((repair["kind"], repair["items"]), ("blind", ["R01"]))
                self.assertEqual(repair["recheckOf"], self.state["blindReview"]["report"])
                self.state["finalRepairs"][-1]["status"] = "passed"
                if cycle == 1:
                    with self.assertRaisesRegex(Invalid, "уже открывался"):
                        final_repairs.open(self.store, self.state, "blind", None, ["app.txt"])
            self.failed_acceptance("3")
            with self.assertRaisesRegex(Invalid, "третий не открывается"):
                final_repairs.open(self.store, self.state, "blind", None, ["app.txt"])
        self.assertEqual([r["id"] for r in self.state["finalRepairs"]], ["F01", "F02"])
        self.assertIsNotNone(final_repairs.pending_recheck(self.state))
        self.assertFalse(final_repairs.blind_exhausted(self.state))
        self.state["blindReview"] = {"repair": "F02"}
        self.assertTrue(final_repairs.blind_exhausted(self.state))

    def test_passed_acceptance_opens_nothing(self):
        self.review_all()
        self.failed_acceptance("ok")
        self.state["gates"]["acceptance"]["passed"] = True
        with patch("aispa_core.contracts.gate_current", return_value=True), \
                self.assertRaisesRegex(Invalid, "непройденной"):
            final_repairs.open(self.store, self.state, "blind", None, ["app.txt"])


class StatusContractTests(unittest.TestCase):
    def test_finished_repair_statuses_are_the_ones_the_estimate_counts_as_closed(self):
        from aispa_core import measured_estimate
        self.assertLessEqual(set(final_repairs.FINISHED), measured_estimate.REPAIR_FINISHED)
        self.assertFalse({"open", "submitted"} & measured_estimate.REPAIR_FINISHED)


class ZoneChangesTests(BranchFixture):
    def test_baseline_argument_diffs_from_a_snapshot(self):
        self.assertEqual([r["path"] for r in integration.zone_changes(self.store, self.state, ["app.txt"])], ["app.txt"])
        snapshot = {"records": self.store.code_records()}
        ref = self.store.artifact("team-records", (json.dumps(snapshot) + "\n").encode(), ".json")
        self.assertEqual(integration.zone_changes(self.store, self.state, ["app.txt"], baseline=ref), [])
        (self.root / "app.txt").write_text("Again\n", encoding="utf-8")
        self.assertEqual([r["path"] for r in integration.zone_changes(self.store, self.state, ["app.txt"], baseline=ref)],
                         ["app.txt"])


if __name__ == "__main__":
    unittest.main()
