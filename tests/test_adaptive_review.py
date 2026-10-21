import copy
import json
import unittest

import test_team as helpers
from aispa_core import display, integration, packets, planning, repairs, task_reports, test_intent
from aispa_core.store import Invalid, file_hash
from aispa_core.team import _artifact_json, _seal, direct_proof_current, historical_proof_valid, task_proof_valid


class AdaptiveReviewTests(unittest.TestCase):
    tearDown = helpers.TeamTests.tearDown
    task = helpers.TeamTests.task
    attempt = helpers.TeamTests.attempt
    verify = helpers.TeamTests.verify

    def setUp(self):
        helpers.TeamTests.setUp(self)
        # This fixture selects policy before any actor or submitted work exists.
        self.state.update(policy={"adaptiveCritique": True}, taskHistory={}, actors={})
        self.team.prepare()

    def restart(self):
        self.tearDown()
        self.setUp()

    def start(self, tid="T01"):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        for role in ("worker", "critic1", "acceptor"):
            self.serial += 1
            self.team.assign(tid, role, "fixture", str(self.serial), "session")
        self.verify(tid)

    def second_actor(self, tid="T01"):
        self.serial += 1
        return self.team.assign(tid, "critic2", "fixture", str(self.serial), "session")

    def reject_second_assignment(self):
        before = copy.deepcopy(self.state)
        records = {p.name for p in (self.store.run / "team-records").iterdir()}
        with self.assertRaises(Invalid):
            self.second_actor()
        self.assertEqual(self.state, before)
        self.assertEqual(records, {p.name for p in (self.store.run / "team-records").iterdir()})
        self.assertNotIn("critic2", self.attempt()["actors"])

    def report(self, tid="T01", role="worker", **extra):
        if role != "worker":
            extra.setdefault("verdict", "PASS")
        if role.startswith("critic"):
            extra.setdefault("findings", [])
        return helpers.TeamTests.report(self, tid, role, **extra)

    def input(self, value):
        self.serial += 1
        path = self.home / ("input-" + str(self.serial) + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def finding(self, severity="warning", fid="F1", criterion="T01 works"):
        return {"id": fid, "severity": severity, "text": "Required correction", "criterion": criterion}

    def first(self, verdict="PASS", **extra):
        self.team.submit("T01", self.report())
        if verdict != "PASS":
            extra.setdefault("findings", [self.finding()])
        return self.team.review("T01", 1, self.report(role="critic1", verdict=verdict, **extra))

    def repair(self, tid="T01"):
        first = self.attempt(tid)["rounds"][0]
        (self.root / self.host.task(tid)["zones"][0]).write_text("repaired candidate", encoding="utf-8")
        self.verify(tid)
        return self.team.repair(tid, self.report(tid, previousReport=first["report"]["sha256"], addresses=["F1"]))

    def second(self, verdict="PASS", tid="T01", **extra):
        attempt = self.attempt(tid)
        self.second_actor(tid)
        extra.setdefault("resolutions", [{"id": "F1", "status": "resolved", "note": "Checked repair", "artifact": self.evidence["file"]}])
        if verdict != "PASS":
            extra.setdefault("findings", [self.finding(fid="F2", criterion=tid + " works")])
        return self.team.review(tid, 2, self.report(tid, "critic2", verdict=verdict,
            previousReport=attempt["rounds"][0]["report"]["sha256"],
            repairReceipt=attempt["repair"]["report"]["sha256"], **extra))

    def accept(self, tid="T01", **extra):
        value = {"previousReport": self.attempt(tid)["rounds"][-1]["report"]["sha256"],
                 "inheritedProofs": self.team.inherited_proofs(tid),
                 "trace": [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"],
                            "result": "aligned", "note": "Compared source", "artifact": self.evidence["file"]}
                           for req in self.team.trace_requirements(tid)]}
        value.update(extra)
        return self.team.accept(tid, self.report(tid, "acceptor", **value))

    def branch(self, tid, failed=None):
        context = integration.context(self.store, self.state, tid)
        return {**{key: context[key] for key in ("basis", "code", "files", "changes")},
                "note": "Reviewed full candidate", "evidence": self.evidence["file"],
                "axes": [{"axis": axis, "status": "failed" if failed == axis else "passed",
                          "note": "Observed axis", "evidence": self.evidence["file"]}
                         for axis in integration.AXES]}

    def test_first_pass_goes_to_separate_acceptor_without_second_actor(self):
        self.start()
        self.assertTrue(self.first(findings=[self.finding(severity="note")]))
        self.assertEqual(self.host.task("T01")["phase"], "task_acceptance")
        self.assertNotIn("critic2", self.attempt()["actors"])
        draft = task_reports.template(self.team, "T01", "acceptor")
        self.assertIsNone(draft["verdict"])
        self.assertEqual(draft["previousReport"], self.attempt()["rounds"][0]["report"]["sha256"])
        for operation in (lambda: self.second_actor(), lambda: self.team.review("T01", 2, self.report(role="critic1")),
                          lambda: self.team.repair("T01", self.report()),
                          lambda: task_reports.template(self.team, "T01", "critic2")):
            with self.assertRaises(Invalid):
                operation()
        self.assertTrue(self.accept())
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        self.assertEqual(len(self.attempt()["rounds"]), 1)
        self.assertEqual(len(self.attempt()["actors"]), 3)

    def test_unknown_missing_and_contradictory_pass_do_not_consume_round(self):
        self.start()
        self.team.submit("T01", self.report())
        base = json.loads(self.report(role="critic1").read_text())
        invalid = []
        for value in (None, "pass", "UNKNOWN", True):
            invalid.append(dict(base, verdict=value))
        missing = dict(base)
        missing.pop("verdict")
        invalid.append(missing)
        invalid.append(dict(base, findings=[self.finding()]))
        invalid.append(dict(base, unmapped=["missing requirement"]))
        failed = copy.deepcopy(base)
        failed["checks"][0]["status"] = "failed"
        failed["findings"] = [self.finding(criterion=failed["checks"][0]["criterion"])]
        invalid.append(failed)
        for report in invalid:
            with self.subTest(report=report), self.assertRaises(Invalid):
                self.team.review("T01", 1, self.input(report))
            self.assertEqual(self.attempt()["rounds"], [])

    def ignored_intent_source(self):
        self.state["policy"].update(testIntent=True, errorBoundaryChecks=True)
        (self.root / ".gitignore").write_text(".aispa/\nchecks/\n", encoding="utf-8")
        (self.root / "checks").mkdir()
        source = self.root / "checks/existing.txt"
        source.write_text("Observed unchanged behavior and absent external boundaries.\n", encoding="utf-8")
        self.start()
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.store.append(self.state, "fixture-test-baseline:T01")
        ref = {"path": "checks/existing.txt", "sha256": file_hash(source)}
        intent = {"mode": "not_applicable", "attempt": self.attempt()["id"],
                  "basis": helpers.task_basis(self.store, self.state, "T01"), "criteria": ["T01 works"],
                  "notApplicable": [{"criterion": "T01 works", "purpose": "existing_behavior",
                                     "source": ref, "reason": "Existing text behavior remains unchanged."}],
                  "failureChecks": {"cases": [], "notApplicable": [{
                      "boundaries": ["input", "output", "storage", "protocol", "dependency"],
                      "reason": "Fixture contains static text only; no execution boundary is changed.", "source": ref}]}}
        test_intent.record(self.store, self.state, "T01", intent)
        self.store.append(self.state, "test-intent-recorded:T01")
        return source

    def test_current_proof_rechecks_ignored_intent_source_after_acceptance(self):
        source = self.ignored_intent_source()
        self.assertTrue(self.first())
        self.assertTrue(self.accept())
        self.host.task("T01")["status"] = "completed"
        original_code = self.store.code()
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        for deleted in (False, True):
            if deleted:
                source.unlink()
            else:
                source.write_text("Evidence was replaced.\n", encoding="utf-8")
            with self.subTest(deleted=deleted):
                self.assertEqual(self.store.code(), original_code)
                self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
                self.assertFalse(direct_proof_current(self.store, self.state, "T01"))
                self.assertFalse(task_proof_valid(self.store, self.state, "T01"))
                self.assertFalse(display.project(self.store, self.state)["tasks"][0]["proofCurrent"])

    def test_repaired_source_is_current_only_while_equal_to_acceptor_snapshot(self):
        source = self.ignored_intent_source()
        self.assertFalse(self.first("WARN"))
        source.write_text("Rechecked source during repair.\n", encoding="utf-8")
        self.repair()
        changes = test_intent.source_changes(self.store, self.state, "T01")
        self.assertTrue(self.second(testSourceChanges=changes))
        self.assertTrue(self.accept(testSourceChanges=changes))
        self.host.task("T01")["status"] = "completed"
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        original_code = self.store.code()
        source.write_text("Later unreviewed change.\n", encoding="utf-8")
        self.assertEqual(self.store.code(), original_code)
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))
        self.assertFalse(task_proof_valid(self.store, self.state, "T01"))

    def test_nonpass_and_failed_checks_require_actionable_ids(self):
        self.start()
        self.team.submit("T01", self.report())
        base = json.loads(self.report(role="critic1", verdict="WARN").read_text())
        failed = copy.deepcopy(base)
        failed["checks"][0]["status"] = "blocked"
        for findings in ([], [self.finding(severity="note")]):
            for value in (base, failed):
                with self.subTest(findings=findings), self.assertRaises(Invalid):
                    self.team.review("T01", 1, self.input(dict(value, findings=findings)))
        hidden = copy.deepcopy(failed)
        hidden["findings"] = [self.finding(criterion="different criterion")]
        with self.assertRaises(Invalid):
            self.team.review("T01", 1, self.input(hidden))
        self.assertEqual(self.attempt()["rounds"], [])

    def test_all_first_nonpass_verdicts_require_repair_then_fresh_second(self):
        for index, verdict in enumerate(("WARN", "BLOCK", "FAIL")):
            if index:
                self.restart()
            with self.subTest(verdict=verdict):
                self.start()
                self.assertFalse(self.first(verdict))
                self.assertEqual(self.attempt()["rounds"][0]["verdict"], verdict)
                self.assertEqual(self.host.task("T01")["phase"], "repair")
                self.reject_second_assignment()
                with self.assertRaises(Invalid):
                    self.accept()
                first = self.attempt()["rounds"][0]
                with self.assertRaises(Invalid):
                    self.team.repair("T01", self.report(previousReport=first["report"]["sha256"], addresses=[]))
                self.repair()
                self.assertTrue(self.second())
                self.assertTrue(self.accept())
                self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
                self.assertEqual(len(self.attempt()["actors"]), 4)

    def test_second_assignment_requires_current_repair_candidate(self):
        self.start()
        self.first("WARN")
        self.repair()
        path = self.root / "a.txt"
        repaired = path.read_bytes()
        path.write_text("candidate changed after repair", encoding="utf-8")
        self.verify()
        self.reject_second_assignment()
        path.write_bytes(repaired)
        self.verify()
        aid = self.second_actor()
        self.assertEqual(self.attempt()["actors"]["critic2"], aid)
        self.assertTrue(self.state["actors"][aid]["fresh"])

    def test_second_assignment_rejects_wrong_or_forged_repair(self):
        self.start()
        self.first("WARN")
        self.repair()
        attempt = self.attempt()
        actual = copy.deepcopy(attempt["repair"])
        plain = {key: value for key, value in actual.items() if key != "record"}
        bad_actor = attempt["actors"]["critic1"]
        bad_report = json.loads(self.store.text(actual["report"]))
        bad_report["reviewer"] = bad_actor
        raw_bad_actor = dict(plain, actor=bad_actor, report=_artifact_json(self.store, "reviews", bad_report))
        invalid = [
            {"passed": True, "basis": actual["basis"]},
            attempt["submitted"],
            attempt["rounds"][0],
            _seal(self.store, dict(plain, previousReport="wrong-first-report")),
            _seal(self.store, dict(plain, addresses=[])),
            _seal(self.store, dict(plain, passed=False)),
            _seal(self.store, raw_bad_actor),
        ]
        for repair in invalid:
            with self.subTest(repair=repair):
                attempt["repair"] = repair
                self.reject_second_assignment()
        attempt["repair"] = actual
        self.assertTrue(self.second_actor())

    def test_second_assignment_rejects_tampered_repair_report(self):
        self.start()
        self.first("BLOCK")
        self.repair()
        path = self.store.run / self.attempt()["repair"]["report"]["file"]
        original = path.read_bytes()
        path.write_text("forged receipt", encoding="utf-8")
        self.reject_second_assignment()
        path.write_bytes(original)
        self.assertTrue(self.second_actor())

    def test_legacy_second_actor_can_still_be_assigned_before_first_review(self):
        self.state.update(policy={}, taskHistory={}, actors={})
        self.team.prepare()
        helpers.TeamTests.start(self)
        self.assertEqual(self.attempt()["rounds"], [])
        self.assertIsNone(self.attempt()["repair"])
        self.assertIn("critic2", self.attempt()["actors"])

    def test_warning_ids_flow_through_diagnosis_template_repair_and_resolutions(self):
        self.state["policy"]["repairDiagnosis"] = True
        self.start()
        self.first("WARN")
        plan = {"basis": repairs.basis(self.state, "T01"), "cause": "implementation_gap", "route": "same",
                "diagnosis": "Concrete missing check", "reason": "Local correction", "changes": "Add missing behavior",
                "addresses": [], "context": []}
        with self.assertRaises(Invalid):
            repairs.record(self.store, self.state, "T01", plan)
        plan["addresses"] = ["F1"]
        repairs.record(self.store, self.state, "T01", plan)
        self.assertEqual(repairs.view(self.store, self.state, "T01")["requiredFindingIds"], ["F1"])
        self.assertEqual(task_reports.template(self.team, "T01", "worker")["addresses"], ["F1"])
        self.repair()
        self.second_actor()
        draft = task_reports.template(self.team, "T01", "critic2")
        self.assertEqual([r["id"] for r in draft["resolutions"]], ["F1"])
        attempt = self.attempt()
        args = {"previousReport": attempt["rounds"][0]["report"]["sha256"],
                "repairReceipt": attempt["repair"]["report"]["sha256"]}
        for resolutions in ([], [{"id": "F1", "status": "unresolved", "note": "Still unresolved"}]):
            with self.assertRaises(Invalid):
                self.team.review("T01", 2, self.report(role="critic2", resolutions=resolutions, **args))
            self.assertEqual(len(self.attempt()["rounds"]), 1)

    def test_second_nonpass_is_terminal_for_all_statuses_and_no_reset(self):
        for index, verdict in enumerate(("WARN", "BLOCK", "FAIL")):
            if index:
                self.restart()
            with self.subTest(verdict=verdict):
                self.start()
                self.first("WARN")
                self.repair()
                self.assertFalse(self.second(verdict))
                self.assertTrue(self.attempt()["terminal"])
                self.assertEqual(self.host.task("T01")["status"], "blocked")
                for operation in (lambda: self.team.reset_guard("T01"), lambda: self.team.start_guard("T01"),
                                  lambda: self.team.review("T01", 3, self.report(role="critic2")),
                                  lambda: self.team.new_attempt("T01", "B001", "Build A.")):
                    with self.assertRaises(Invalid):
                        operation()

    def test_second_unresolved_original_id_blocks_without_duplicate_finding(self):
        self.start()
        self.first("WARN")
        self.repair()
        self.assertFalse(self.second("WARN", findings=[], resolutions=[
            {"id": "F1", "status": "unresolved", "note": "Original warning remains"}]))
        self.assertTrue(self.attempt()["terminal"])
        self.assertEqual(self.attempt()["rounds"][1]["verdict"], "WARN")

    def test_acceptor_nonpass_blocks_without_extra_critic(self):
        for index, verdict in enumerate(("WARN", "BLOCK", "FAIL")):
            if index:
                self.restart()
            with self.subTest(verdict=verdict):
                self.start()
                self.first()
                self.assertFalse(self.accept(verdict=verdict))
                self.assertTrue(self.attempt()["terminal"])
                self.assertFalse(task_proof_valid(self.store, self.state, "T01"))
                with self.assertRaises(Invalid):
                    self.team.reset_guard("T01")
                with self.assertRaises(Invalid):
                    self.second_actor()

    def test_acceptor_unknown_or_contradictory_pass_is_not_saved(self):
        self.start()
        self.first()
        draft = task_reports.template(self.team, "T01", "acceptor")
        draft.update(independent=True, unmapped=[], verdict="PASS")
        for row in draft["checks"]:
            row.update(status="passed", note="Verified", artifact=self.evidence["file"])
        for row in draft["trace"]:
            row.update(result="aligned", note="Compared", artifact=self.evidence["file"])
        invalid = [dict(draft, verdict=None), dict(draft, verdict="UNKNOWN"),
                   dict(draft, findings=[self.finding()])]
        failed = copy.deepcopy(draft)
        failed["checks"][0]["status"] = "blocked"
        invalid.append(failed)
        drift = copy.deepcopy(draft)
        drift["trace"][0]["result"] = "drift"
        invalid.append(drift)
        for value in invalid:
            with self.assertRaises(Invalid):
                self.team.accept("T01", self.input(value))
            self.assertIsNone(self.attempt()["acceptance"])
        self.assertTrue(self.accept())

    def test_actor_independence_wrong_signature_and_tampered_binding(self):
        self.start()
        worker = self.state["actors"][self.attempt()["actors"]["worker"]]
        with self.assertRaises(Invalid):
            self.team.assign("T01", "acceptor", worker["runtime"], worker["nativeId"], worker["session"])
        self.team.submit("T01", self.report())
        with self.assertRaises(Invalid):
            self.team.review("T01", 1, self.report(role="critic1", reviewer=worker["id"]))
        self.team.review("T01", 1, self.report(role="critic1"))
        self.accept()
        self.attempt()["actors"]["acceptor"] = self.attempt()["actors"]["critic1"]
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))

    def test_candidate_change_marks_skipped_round_stale_and_denies_reset(self):
        self.start()
        self.first()
        steps = {r["id"]: r["status"] for r in display._phase_steps(self.store, self.state, self.host.task("T01"), False)}
        self.assertEqual(steps["critic_2"], "skipped")
        (self.root / "a.txt").write_text("changed after PASS", encoding="utf-8")
        self.verify()
        for operation in (lambda: self.accept(), lambda: task_reports.template(self.team, "T01", "acceptor"),
                          lambda: self.team.reset_guard("T01")):
            with self.assertRaises(Invalid):
                operation()
        steps = {r["id"]: r["status"] for r in display._phase_steps(self.store, self.state, self.host.task("T01"), False)}
        self.assertEqual(steps["critic_2"], "stale")

    def test_frozen_policy_ignores_run_toggle_and_cannot_be_rewritten(self):
        self.start()
        self.state["policy"]["adaptiveCritique"] = False
        self.first()
        self.assertTrue(self.accept())
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.attempt()["reviewPolicy"] = "two_rounds"
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))

    def test_dependency_and_integration_acceptance_use_one_real_round_each(self):
        self.state["policy"]["integrationReview"] = True
        integration.capture(self.store, self.state)
        self.state["integrationTask"] = "T02"
        self.host.task("T02").update(dependsOn=["T01"], readZones=["."], wave=2)
        self.team.prepare()
        self.start()
        self.first()
        self.accept()
        self.start("T02")
        (self.root / "b.txt").write_text("integrated", encoding="utf-8")
        self.verify("T02")
        self.team.submit("T02", self.report("T02"))
        draft = task_reports.template(self.team, "T02", "critic1")
        self.assertEqual(len(draft["branchReview"]["axes"]), 5)
        with self.assertRaises(Invalid):
            self.team.review("T02", 1, self.report("T02", "critic1"))
        self.team.review("T02", 1, self.report("T02", "critic1", branchReview=self.branch("T02")))
        self.assertTrue(self.accept("T02"))
        self.assertTrue(integration.proof_valid(self.store, self.state))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T02"))
        self.assertEqual(len(self.attempt("T02")["rounds"]), 1)
        (self.root / "a.txt").write_text("changed dependency", encoding="utf-8")
        self.assertFalse(integration.proof_valid(self.store, self.state))

    def test_integration_failed_axis_requires_repair_and_full_second_review(self):
        self.state["tasks"] = [self.host.task("T01")]
        self.host.task("T01")["readZones"] = ["."]
        self.state["policy"]["integrationReview"] = True
        integration.capture(self.store, self.state)
        self.state["integrationTask"] = "T01"
        self.team.prepare()
        self.start()
        axis = integration.AXES[0]
        self.first("FAIL", findings=[self.finding("blocking", criterion="Вся ветка: " + axis)],
                   branchReview=self.branch("T01", failed=axis))
        self.repair()
        self.assertTrue(self.second(branchReview=self.branch("T01")))
        self.assertTrue(self.accept())
        self.assertTrue(integration.proof_valid(self.store, self.state))

    def test_estimates_and_routing_distinguish_limit_from_actual(self):
        tasks = copy.deepcopy(self.state["tasks"])
        tasks[0]["requirements"].append("G01")
        result = planning.analyze(self.store, self.state, tasks)
        self.assertEqual(result["taskRoleSlots"], {"minimum": 6, "maximum": 8})
        self.assertIsNone(result["taskRoleStarts"])
        self.assertEqual(result["maximumCriticRoundsPerTask"], 2)
        self.assertIsNone(result["actualCriticRounds"])
        self.start()
        self.first()
        route = packets.routing(self.state, self.host.task("T01"), "acceptor", self.attempt(), self.store)
        self.assertEqual(route["actualCriticRounds"], 1)
        self.assertEqual(route["maximumCriticRounds"], 2)
        self.assertEqual(route["taskRoleSlots"], {"minimum": 3, "maximum": 4})

    def test_existing_legacy_attempt_cannot_upgrade_via_policy_toggle(self):
        self.restart()
        self.state.update(policy={}, taskHistory={}, actors={})
        self.team.prepare()
        self.assertEqual(self.attempt()["reviewPolicy"], "two_rounds")
        self.state["policy"]["adaptiveCritique"] = True
        helpers.TeamTests.start(self)
        helpers.TeamTests.reviews(self)
        self.assertTrue(helpers.TeamTests.accept(self))
        self.assertEqual(len(self.attempt()["rounds"]), 2)
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_historical_attempt_without_policy_field_remains_legacy(self):
        self.state.update(policy={}, taskHistory={}, actors={})
        self.team.prepare()
        attempt = self.attempt()
        attempt.pop("reviewPolicy")
        for role, ref in list(attempt["roles"].items()):
            card = json.loads(self.store.text(ref))
            card.pop("reviewPolicy")
            attempt["roles"][role] = {**_artifact_json(self.store, "roles", card),
                                      "checks": card["checks"], "specialization": card["specialization"]}
        self.state["policy"]["adaptiveCritique"] = True
        helpers.TeamTests.start(self)
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1"))
        with self.assertRaises(Invalid):
            self.accept()
        self.team.review("T01", 2, self.report(role="critic2",
            previousReport=attempt["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[]))
        self.assertTrue(self.accept())
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))


if __name__ == "__main__":
    unittest.main()
