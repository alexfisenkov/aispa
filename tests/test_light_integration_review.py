"""Project-review contract tests; task-proof internals have their own suite."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import integration, participants, team
from aispa_core.store import Invalid, Journal, digest
import test_task_tiers


class LightIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-light-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "copy.txt").write_text("Before\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "-qm", "baseline")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        self.store = Journal(self.root)
        self.state = self.store.create("light-review", "Project review fixture")
        self.state["spec"] = self.store.artifact("specs", b"Correct visible copy.\n", ".md")
        self.state["tasks"] = [{"id": "T01", "title": "Copy", "requirements": [],
            "dependsOn": [], "zones": ["copy.txt"], "readZones": ["."],
            "criteria": ["Copy is correct"], "wave": 1, "status": "completed"}]
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "tier": "light",
                                              "actors": {}, "writers": []}]}
        self.state["integrationTask"] = "T01"
        integration.capture(self.store, self.state)
        (self.root / "copy.txt").write_text("After\n", encoding="utf-8")
        self.evidence = self.store.artifact("evidence", b"Reviewed current full diff.\n", ".txt")
        # These tests exercise the independent project-review layer. The task
        # completion contract is tested separately through the native pipeline.
        direct = patch.object(team, "direct_proof_current", return_value=True)
        proof = patch.object(team, "task_proof_valid", return_value=True)
        self.direct = direct.start()
        self.proof = proof.start()
        self.addCleanup(direct.stop)
        self.addCleanup(proof.stop)

    def actor(self, native="reviewer"):
        return participants.register(self.store, self.state, "integration-reviewer",
                                     "fixture", native, "session-" + native, model="host-exposed-model")

    def report(self, actor, *, failed=None):
        report = integration.project_context(self.store, self.state, actor)["template"]
        report["verdict"] = "FAIL" if failed else "PASS"
        branch = report["branchReview"]
        branch.update(note="Reviewed full code, changes and their consumers.", evidence=self.evidence["file"])
        for row in branch["axes"]:
            row.update(status="failed" if row["axis"] == failed else "passed",
                       note="Inspected " + row["axis"], evidence=self.evidence["file"])
        return report

    def test_context_is_unassessed_and_recommends_strong_without_authenticating_model(self):
        view = integration.project_context(self.store, self.state)
        self.assertIsNone(view["actor"])
        self.assertEqual(view["template"]["verdict"], "UNASSESSED")
        self.assertEqual({r["status"] for r in view["template"]["branchReview"]["axes"]}, {"unassessed"})
        self.assertEqual(view["routing"]["tier"], "strong")
        self.assertTrue(view["routing"]["advisory"])
        self.assertNotIn("model", view["routing"])
        self.assertNotIn("projectIntegrationReview", self.state)
        actor = self.actor()
        self.assertEqual(integration.project_context(self.store, self.state)["actor"], actor)
        with self.assertRaises(Invalid):
            integration.project_review(self.store, self.state, view["template"], actor)

    def test_current_task_proofs_are_required_before_context_and_recording(self):
        actor = self.actor()
        report = self.report(actor)
        for checker in (self.direct, self.proof):
            checker.return_value = False
            with self.assertRaises(Invalid):
                integration.project_context(self.store, self.state, actor)
            with self.assertRaises(Invalid):
                integration.project_review(self.store, self.state, report, actor)
            checker.return_value = True
        self.state["tasks"][0]["status"] = "running"
        with self.assertRaises(Invalid):
            integration.project_review(self.store, self.state, report, actor)

    def test_pass_is_sealed_and_required_even_for_a_completed_final_task(self):
        actor = self.actor()
        self.assertFalse(integration.proof_valid(self.store, self.state))
        report = self.report(actor)
        receipt = integration.project_review(self.store, self.state, report)
        self.assertTrue(receipt["passed"])
        self.assertTrue(team._sealed_valid(self.store, receipt))
        self.assertEqual(self.state["projectIntegrationReviewHistory"], [receipt])
        self.assertTrue(integration.proof_valid(self.store, self.state))
        self.assertNotIn("acceptance", self.state["gates"])
        self.assertEqual(self.state["taskHistory"]["T01"][-1].get("rounds", []), [])
        receipt["verdict"] = "FAIL"
        self.assertEqual(self.state["projectIntegrationReview"]["verdict"], "PASS")

    def test_fail_is_recorded_and_cannot_be_replaced_by_pass_for_same_candidate(self):
        actor = self.actor()
        receipt = integration.project_review(self.store, self.state, self.report(actor, failed="integration"))
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["failures"][0]["axis"], "integration")
        self.assertFalse(integration.proof_valid(self.store, self.state))
        new_actor = self.actor("new-reviewer")
        with self.assertRaises(Invalid):
            integration.project_review(self.store, self.state, self.report(new_actor))
        self.assertEqual(self.state["projectIntegrationReview"], receipt)

    def test_changed_candidate_requires_new_actor_and_retains_failed_history(self):
        actor = self.actor()
        failed = integration.project_review(self.store, self.state, self.report(actor, failed="behavior"))
        (self.root / "copy.txt").write_text("Repaired\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            integration.project_review(self.store, self.state, self.report(actor))
        new_actor = self.actor("new-reviewer")
        passed = integration.project_review(self.store, self.state, self.report(new_actor))
        self.assertEqual(self.state["projectIntegrationReviewHistory"], [failed, passed])
        self.assertEqual(passed["previous"], failed["record"]["sha256"])
        self.assertTrue(integration.proof_valid(self.store, self.state))
        self.state["projectIntegrationReviewHistory"].pop(0)
        self.assertFalse(integration.proof_valid(self.store, self.state))

    def test_code_and_requirement_drift_invalidate_receipt(self):
        actor = self.actor()
        integration.project_review(self.store, self.state, self.report(actor))
        original = (self.root / "copy.txt").read_bytes()
        (self.root / "untracked.txt").write_text("new consumer\n", encoding="utf-8")
        self.assertFalse(integration.proof_valid(self.store, self.state))
        (self.root / "untracked.txt").unlink()
        self.assertTrue(integration.proof_valid(self.store, self.state))
        (self.root / "copy.txt").write_text("different\n", encoding="utf-8")
        self.assertFalse(integration.proof_valid(self.store, self.state))
        (self.root / "copy.txt").write_bytes(original)
        self.state["tasks"][0]["criteria"].append("New owner requirement")
        self.assertFalse(integration.proof_valid(self.store, self.state))

    def test_missing_changed_file_stale_candidate_or_axis_mismatch_are_rejected(self):
        actor = self.actor()
        report = self.report(actor)
        for variant in ("file", "basis", "axis", "verdict", "independent", "reviewer"):
            malformed = copy.deepcopy(report)
            if variant == "file":
                malformed["branchReview"]["files"] = []
            elif variant == "basis":
                malformed["basis"] = "0" * 64
            elif variant == "axis":
                malformed["branchReview"]["axes"].pop()
            elif variant == "verdict":
                malformed["branchReview"]["axes"][0]["status"] = "failed"
            elif variant == "independent":
                malformed["independent"] = False
            else:
                malformed["reviewer"] = "invented-native-id"
            with self.subTest(variant=variant), self.assertRaises(Invalid):
                integration.project_review(self.store, self.state, malformed, actor)
        self.assertNotIn("projectIntegrationReview", self.state)

    def test_artifact_tampering_invalidates_current_receipt(self):
        actor = self.actor()
        receipt = integration.project_review(self.store, self.state, self.report(actor))
        refs = [receipt["record"], receipt["report"], receipt["artifacts"][0], receipt["participant"],
                self.state["integrationBaseline"]]
        for ref in refs:
            path = self.store.run / ref["file"]
            original = path.read_bytes()
            path.write_bytes(b"tampered\n")
            with self.subTest(artifact=ref["file"]):
                self.assertFalse(integration.proof_valid(self.store, self.state))
            path.write_bytes(original)
            self.assertTrue(integration.proof_valid(self.store, self.state))

    def test_evidence_must_exist_in_run_and_cannot_change_during_recording(self):
        actor = self.actor()
        report = self.report(actor)
        for name in ("missing.txt", "evidence/missing.txt", "evidence/../outside.txt"):
            malformed = copy.deepcopy(report)
            malformed["branchReview"]["evidence"] = name
            with self.subTest(name=name), self.assertRaises(Invalid):
                integration.project_review(self.store, self.state, malformed)

    def test_reviewer_registration_is_independent_and_requires_fresh_context(self):
        self.assertIn("integration-reviewer", participants.INDEPENDENT_ROLES)
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "integration-reviewer",
                                  "fixture", "reviewer", "session", fresh=False)
        worker = {"id": digest(["fixture", "worker", "session"]), "runtime": "fixture",
                  "nativeId": "worker", "session": "session", "role": "worker"}
        self.state.setdefault("actors", {})[worker["id"]] = team._seal(self.store, worker)
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "integration-reviewer",
                                  "fixture", "worker", "another-session")
        blind = participants.register(self.store, self.state, "blind", "fixture", "blind", "session")
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "integration-reviewer",
                                  "fixture", "blind", "another-session")
        with self.assertRaises(Invalid):
            integration.project_context(self.store, self.state, blind)

    def test_later_worker_or_final_blind_identity_reuse_invalidates_review(self):
        actor = self.actor()
        integration.project_review(self.store, self.state, self.report(actor))
        self.state["blindReview"] = {"reviewer": "reviewer"}
        self.assertFalse(integration.proof_valid(self.store, self.state))
        self.state.pop("blindReview")
        self.assertTrue(integration.proof_valid(self.store, self.state))
        aid = digest(["fixture", "reviewer", "other-session"])
        self.state.setdefault("actors", {})[aid] = {"runtime": "fixture", "nativeId": "reviewer"}
        self.assertFalse(integration.proof_valid(self.store, self.state))
        self.state["actors"].pop(aid)
        self.state["taskHistory"]["T01"][0]["writers"] = [actor]
        self.assertFalse(integration.proof_valid(self.store, self.state))

    def test_task_proof_drift_and_heavy_final_route_do_not_use_light_receipt(self):
        actor = self.actor()
        integration.project_review(self.store, self.state, self.report(actor))
        self.proof.return_value = False
        self.assertFalse(integration.proof_valid(self.store, self.state))
        self.proof.return_value = True
        self.state["taskHistory"]["T01"][-1]["tier"] = "heavy"
        with self.assertRaises(Invalid):
            integration.project_context(self.store, self.state, actor)
        self.assertFalse(integration.proof_valid(self.store, self.state))


class CompletedLightProjectReviewTests(unittest.TestCase):
    """Real task receipts and executed verification, with no proof mocks."""
    task = test_task_tiers.TaskTierTests.task
    attempt = test_task_tiers.TaskTierTests.attempt
    report = test_task_tiers.TaskTierTests.report
    verify = test_task_tiers.TaskTierTests.verify
    light_task = test_task_tiers.TaskTierTests.light_task
    start_light = test_task_tiers.TaskTierTests.start_light
    tearDown = test_task_tiers.TaskTierTests.tearDown

    def setUp(self):
        test_task_tiers.TaskTierTests.setUp(self)
        self.state["tasks"][0]["readZones"] = ["."]
        self.state["policy"]["integrationReview"] = True
        self.state["integrationTask"] = "T01"
        integration.capture(self.store, self.state)
        self.state["taskHistory"] = {}
        self.team.prepare()

    def test_executed_light_completion_requires_review_and_keeps_final_gates(self):
        from aispa_core.contracts import gate_valid
        flow = self.start_light(real=True)
        (self.root / "a.txt").write_text("Revised prose\n", encoding="utf-8")
        self.assertTrue(flow.verify([sys.executable, "-c",
            "from pathlib import Path; assert Path('a.txt').read_text()=='Revised prose\\n'; print('verified')"],
            10, "T01"))
        self.team.submit("T01", self.report())
        self.assertTrue(team.direct_proof_current(self.store, self.state, "T01"))
        self.assertFalse(integration.proof_valid(self.store, self.state))
        actor = participants.register(self.store, self.state, "integration-reviewer",
                                      "fixture", "project-reviewer", "fresh-session")
        review = LightIntegrationReviewTests.report(self, actor)
        saved = integration.project_review(self.store, self.state, review, actor)
        self.store.append(self.state, "project-integration-reviewed")
        self.assertTrue(integration.proof_valid(self.store, self.state))
        self.assertTrue(team.direct_proof_current(self.store, self.state, "T01"))
        self.assertEqual(self.attempt()["rounds"], [])
        self.assertIsNone(self.attempt()["acceptance"])
        self.assertEqual(set(self.attempt()["actors"]), {"worker"})
        self.assertEqual(saved, self.store.read_journal()["last"]["state"]["projectIntegrationReview"])
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        self.state["policy"]["cleanClone"] = True
        self.state["gates"]["acceptance"] = {"passed": True}
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        self.assertNotIn("blindReview", self.state)


if __name__ == "__main__":
    unittest.main()
