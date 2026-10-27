"""Focused review (0.5.0): a critic reads its own attempt, only the short list
blocks, warnings are collected, the engine computes the weight and the check
mode decides only how many reviews a task gets."""
import copy
import json
import unittest

import test_adaptive_review
import test_team
from aispa_core import (check_modes, contracts, design_contract, focused_review, model_routes, packets, repairs,
                        review_policy, task_reports, task_tiers, test_intent)
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, file_hash
from aispa_core.team import direct_proof_current, historical_proof_valid

POLICY = {"adaptiveCritique": True, "leanControl": True, "taskTiers": True, "focusedReview": True}


class FocusedFixture(unittest.TestCase):
    """Borrows the team helpers; only the frozen run policy differs."""
    tearDown = test_adaptive_review.AdaptiveReviewTests.tearDown
    task = test_team.TeamTests.task
    attempt = test_adaptive_review.AdaptiveReviewTests.attempt
    verify = test_adaptive_review.AdaptiveReviewTests.verify
    report = test_adaptive_review.AdaptiveReviewTests.report

    def setUp(self):
        test_team.TeamTests.setUp(self)
        for name, text in (("shared.txt", "shared"), ("c.txt", "c")):
            (self.root / name).write_text(text, encoding="utf-8", newline="\n")
        self.configure(POLICY, "balanced")

    def configure(self, policy, mode, tasks=None, runtime=None):
        self.state.update(policy=dict(policy), taskHistory={}, actors={}, checkMode=mode, verification=None)
        if runtime:
            self.state["runtime"] = runtime
        # T03 makes T02 a foundation too: both are heavy by the engine's rules.
        self.state["tasks"] = tasks or [self.plan_task("T01", "R01", ["a.txt", "shared.txt"]),
                                        self.plan_task("T02", "R02", ["b.txt", "shared.txt"], ["T01"]),
                                        self.plan_task("T03", "R02", ["c.txt"], ["T02"])]
        self.team.prepare()

    def plan_task(self, tid, rid, zones, deps=None, **extra):
        value = {"id": tid, "title": tid + " title", "requirements": [rid], "dependsOn": deps or [], "zones": zones,
                 "readZones": [], "criteria": [tid + " works"], "wave": 2 if deps else 1, "status": "pending",
                 "tier": "heavy", "tierReason": "Fixture", "changeKind": "feature"}
        value.update(extra)
        return value

    def assign(self, tid, role):
        self.serial += 1
        return self.team.assign(tid, role, "fixture", "%s-%s-%d" % (tid, role, self.serial), "session")

    def start(self, tid):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        self.assign(tid, "worker")

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8", newline="\n")

    def lean_fields(self, tid):
        return {"inheritedProofs": self.team.inherited_proofs(tid),
                "trace": [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"],
                           "result": "aligned", "note": "Compared source", "artifact": self.evidence["file"]}
                          for req in self.team.trace_requirements(tid)]}

    def submit(self, tid):
        self.verify(tid)
        self.team.submit(tid, self.report(tid))
        if "critic1" in task_tiers.roles(self.attempt(tid)):
            self.assign(tid, "critic1")

    def critic(self, tid, number=1, verdict="PASS", findings=(), **extra):
        fields = self.lean_fields(tid)
        fields.update(extra)
        return self.team.review(tid, number, self.report(tid, "critic" + str(number), verdict=verdict,
                                                         findings=list(findings), **fields))

    def complete(self, tid, changes=()):
        self.start(tid)
        for name, text in changes:
            self.write(name, text)
        self.submit(tid)
        self.assertTrue(self.critic(tid))
        self.assertEqual(self.host.task(tid)["status"], "completed")

    def warning(self, fid="W", criterion="T01 works", text="Naming could be clearer"):
        return {"id": fid, "severity": "warning", "text": text, "criterion": criterion}

    def blocking(self, fid="B1", criterion="T01 works", cls="failing_tests"):
        value = {"id": fid, "severity": "blocking", "text": "Test fails", "criterion": criterion}
        if cls is not None:
            value["blockingClass"] = cls
        return value

    def packet(self, tid, role):
        return packets.build(self.store, self.state, self.team, "task", tid, role)


class AttemptScopeTests(FocusedFixture):
    def test_critic_reads_only_changes_of_its_attempt(self):
        self.complete("T01", [("a.txt", "a by T01"), ("shared.txt", "shared by T01")])
        self.start("T02")
        self.write("b.txt", "b by T02")
        self.submit("T02")
        packet = self.packet("T02", "critic1")
        self.assertEqual(packet["zoneChangesScope"], "attempt")
        self.assertEqual([row["path"] for row in packet["zoneChanges"]], ["b.txt"])
        self.assertEqual(packet["criteriaFilesReading"], "on-demand")
        baseline = self.attempt("T02")["lightBaseline"]
        self.assertEqual(focused_review.changes_since(self.store, baseline)[0]["path"], "b.txt")

    def test_ancestor_criteria_join_only_when_the_attempt_changed_their_files(self):
        self.complete("T01", [("a.txt", "a by T01"), ("shared.txt", "shared by T01")])
        self.start("T02")
        self.write("b.txt", "b by T02")
        self.submit("T02")
        own = {("T02", "T02 works"), ("G01", "No private data")}
        expected = self.team.expected_checks("T02", "critic1")
        self.assertNotIn(("T01", "T01 works"), expected)
        self.assertLessEqual(own, expected)
        self.assertTrue(self.critic("T02"))
        self.assertEqual(self.attempt("T02")["acceptance"]["changedAncestors"], [])
        self.assertTrue(historical_proof_valid(self.store, self.state, "T02"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))

    def test_changed_ancestor_file_adds_its_criteria_and_history_agrees(self):
        self.complete("T01", [("a.txt", "a by T01")])
        self.start("T02")
        self.write("shared.txt", "shared by T02")
        self.submit("T02")
        self.assertIn(("T01", "T01 works"), self.team.expected_checks("T02", "critic1"))
        self.assertTrue(self.critic("T02"))
        accepted = self.attempt("T02")["acceptance"]
        self.assertEqual(accepted["changedAncestors"], ["T01"])
        self.assertIn({"requirement": "T01", "criterion": "T01 works"}, accepted["coverage"])
        self.assertTrue(historical_proof_valid(self.store, self.state, "T02"))
        # A forged list of changed ancestors no longer matches the attempt's own snapshots.
        accepted["changedAncestors"] = []
        self.assertFalse(historical_proof_valid(self.store, self.state, "T02"))

    def test_criteria_files_name_interface_owner_references_read_zones_and_quoted_paths(self):
        for name in ("lib/api.py", "docs/api.md", "config/app.json", "tools/run.sh"):
            (self.root / name).parent.mkdir(exist_ok=True)
            self.write(name, "fixture")
        source = {"file": "docs/api.md", "sha256": file_hash(self.root / "docs/api.md")}
        snapshot = self.store.artifact("interface-references", (self.root / "docs/api.md").read_bytes(), ".md")
        report = {"basis": "fixture", "interfaces": [
            {"name": "api", "signature": "api()", "ownerZone": "lib", "consumers": ["b.txt"], "testSeam": "t",
             "requirements": ["R02"], "references": [source], "referenceReason": "fixture"},
            {"name": "other", "signature": "other()", "ownerZone": "c.txt", "consumers": ["a.txt"], "testSeam": "t",
             "requirements": ["R01"], "references": [], "referenceReason": "fixture"}]}
        ref = self.store.artifact("interface-contracts", json.dumps(
            {"schema": 1, "run": self.state["run"], "report": report,
             "referenceSnapshots": [dict(source, snapshot=snapshot)]}).encode(), ".json")
        self.state.update(interfaceContract=ref, interfaceContractHistory=[ref])
        task = self.host.task("T02")
        task.update(readZones=["config/app.json"], criteria=["T02 works with tools/run.sh.", "Missing ghost/file.py"])
        self.assertEqual(focused_review.criteria_files(self.store, self.state, task),
                         ["config/app.json", "docs/api.md", "lib", "tools/run.sh"])
        self.team.prepare()
        self.complete("T01", [("a.txt", "a by T01")])
        self.start("T02")
        self.submit("T02")
        self.assertEqual(self.packet("T02", "critic1")["criteriaFiles"],
                         ["config/app.json", "docs/api.md", "lib", "tools/run.sh"])


class BlockingTests(FocusedFixture):
    def ready(self):
        self.start("T01")
        self.write("a.txt", "a by T01")
        self.submit("T01")

    def test_blocking_needs_a_class_from_the_short_list_and_nothing_is_recorded(self):
        self.ready()
        before = copy.deepcopy(self.state)
        for finding in (self.blocking(cls=None), self.blocking(cls="style")):
            with self.subTest(finding=finding), self.assertRaises(Invalid) as caught:
                self.critic("T01", verdict="BLOCK", findings=[finding])
            self.assertIn("Блокирует только список: brief_drift, broken_scenario, security, data_loss, "
                          "failing_tests; остальное отметь warning", str(caught.exception))
        self.assertEqual(self.state, before)

    def test_warnings_alone_pass_close_the_task_and_are_collected(self):
        self.ready()
        with self.assertRaises(Invalid):
            self.critic("T01", verdict="WARN", findings=[self.warning()])
        self.assertTrue(self.critic("T01", findings=[self.warning(), self.warning("N", text="Note only")]))
        task, attempt = self.host.task("T01"), self.attempt("T01")
        self.assertEqual((task["status"], task["completionKind"]), ("completed", "critic_acceptance"))
        self.assertIsNone(attempt["repair"])
        record = attempt["rounds"][0]
        self.assertEqual(review_policy.required_ids(record, review_policy.FOCUSED), set())
        self.assertEqual(review_policy.required_ids(record), {"W", "N"})
        rows = self.state["collectedFindings"]
        self.assertEqual([(row["id"], row["status"], row["note"]) for row in rows],
                         [("W001", "open", "Naming could be clearer"), ("W002", "open", "Note only")])
        self.assertEqual(rows[0]["source"], {"kind": "task", "task": "T01", "attempt": attempt["id"], "round": 1,
                                             "report": {"file": record["report"]["file"],
                                                        "sha256": record["report"]["sha256"]}})
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))

    def test_blocking_repair_is_rechecked_by_its_own_diff_and_blocking_ids(self):
        self.ready()
        findings = [self.blocking(), self.warning()]
        self.assertFalse(self.critic("T01", verdict="BLOCK", findings=findings))
        self.assertEqual(self.state["collectedFindings"][0]["note"], "Naming could be clearer")
        self.write("shared.txt", "edited before diagnosis")
        diagnosis = {"basis": repairs.basis(self.state, "T01"), "cause": "implementation_gap",
                     "diagnosis": "Test fails", "route": "same", "reason": "Local defect",
                     "changes": "Fix the branch", "addresses": ["B1"], "context": []}
        with self.assertRaises(Invalid):
            repairs.record(self.store, self.state, "T01", dict(diagnosis, addresses=["B1", "W"]))
        repairs.record(self.store, self.state, "T01", diagnosis)
        baseline = self.attempt("T01")["repairBaseline"]
        self.assertTrue(contracts.artifact_valid(self.store, baseline))
        self.write("a.txt", "repaired by T01")
        self.verify("T01")
        draft = task_reports.template(self.team, "T01", "worker")
        self.assertEqual(draft["addresses"], ["B1"])
        first = self.attempt("T01")["rounds"][0]
        self.team.repair("T01", self.report("T01", previousReport=first["report"]["sha256"], addresses=["B1"]))
        self.assign("T01", "critic2")
        packet = self.packet("T01", "critic2")
        self.assertEqual(packet["zoneChangesScope"], "repair")
        self.assertEqual([row["path"] for row in packet["zoneChanges"]], ["a.txt"])
        self.assertEqual(packet["repairFindingIds"], ["B1"])
        resolutions = [{"id": "B1", "status": "resolved", "note": "Fixed", "artifact": self.evidence["file"]}]
        self.assertTrue(self.critic("T01", 2, previousReport=first["report"]["sha256"],
                                    repairReceipt=self.attempt("T01")["repair"]["report"]["sha256"],
                                    resolutions=resolutions))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))


class WeightAndCoverageTests(FocusedFixture):
    def flows(self, requirement):
        report = {"features": [{"requirement": requirement, "productFlows": {
            "applicability": "applicable", "reason": "Main action", "flows": [{"id": "F"}]}}]}
        ref = self.store.artifact("feature-scenarios", json.dumps(
            {"schema": 1, "run": self.state["run"], "report": report}).encode(), ".json")
        self.state.update(featureScenarios=ref, featureScenarioHistory=[ref])
        self.state["policy"]["criticalProductFlows"] = True

    def test_engine_computes_heavy_and_refuses_a_lower_declaration(self):
        plain = self.plan_task("T07", "R01", ["c.txt"])
        ci = self.plan_task("T04", "R01", [".github/workflows/ci.yml"])
        money = self.plan_task("T05", "R01", ["billing/pay.py"])
        flow = self.plan_task("T06", "R02", ["c.txt"])
        self.flows("R02")
        self.assertEqual(focused_review.task_weight(self.state, self.host.task("T01"), self.store), "heavy")
        self.assertEqual(focused_review.task_weight(self.state, plain, self.store), "standard")
        self.assertEqual(focused_review.task_weight(self.state, ci, self.store), "heavy")
        self.assertEqual(focused_review.task_weight(self.state, money, self.store), "heavy")
        self.assertEqual(focused_review.task_weight(self.state, flow, self.store), "heavy")
        self.assertEqual(self.attempt("T03")["tier"], "standard")
        self.assertEqual(self.attempt("T03")["plannedTier"], "heavy")
        current = [self.host.task(tid) for tid in ("T01", "T02", "T03")]
        with self.assertRaises(Invalid) as caught:
            self.team.validate_replan([dict(current[0], tier="standard"), *current[1:]])
        self.assertIn("движок вычислил heavy", str(caught.exception))
        with self.assertRaises(Invalid) as caught:
            self.team.validate_replan([*current, dict(ci, tier="standard")])
        self.assertIn(".github/workflows/ci.yml", str(caught.exception))
        self.team.validate_replan([*current, dict(plain, tier="standard"), dict(ci, tier="heavy")])

    def roles(self, mode, task):
        self.configure(POLICY, mode, [task])
        return self.attempt(task["id"])["criticRoles"]

    def test_critic_coverage_follows_the_mode(self):
        ci = self.plan_task("T01", "R01", [".github/workflows/ci.yml"])
        money = self.plan_task("T01", "R01", ["billing/pay.py"])
        plain = self.plan_task("T01", "R01", ["c.txt"])
        self.assertEqual(self.roles("balanced", ci), ["worker", "critic1", "critic2"])
        self.assertEqual(self.roles("fast", ci), ["worker"])
        self.assertEqual(self.attempt()["tier"], "heavy")
        self.assertEqual(self.roles("fast", money), ["worker", "critic1", "critic2"])
        self.assertEqual(self.roles("balanced", plain), ["worker"])
        self.assertEqual(self.roles("max", plain), ["worker", "critic1", "critic2"])
        self.assertFalse(self.attempt()["secondRound"])
        self.assertEqual(self.roles("max", ci), ["worker", "critic1", "critic2"])
        self.assertTrue(self.attempt()["secondRound"])
        self.assertEqual(check_modes.effective_tier(self.state, "standard"), "standard")

    def test_max_heavy_needs_a_second_fresh_critic_after_a_first_pass(self):
        self.configure(POLICY, "max")
        self.start("T01")
        self.write("a.txt", "a by T01")
        self.submit("T01")
        self.assertTrue(self.critic("T01"))
        task = self.host.task("T01")
        self.assertEqual((task["status"], task["phase"]), ("running", "critic_2"))
        self.assign("T01", "critic2")
        packet = self.packet("T01", "critic2")
        self.assertEqual(packet["zoneChangesScope"], "attempt")
        self.assertEqual([row["path"] for row in packet["zoneChanges"]], ["a.txt"])
        first = self.attempt("T01")["rounds"][0]
        self.assertTrue(self.critic("T01", 2, previousReport=first["report"]["sha256"], repairReceipt=None,
                                    resolutions=[]))
        self.assertEqual((task["status"], self.attempt("T01")["acceptance"]["round"]), ("completed", 2))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))

    def test_fast_worker_only_heavy_task_closes_by_verified_completion(self):
        ci = self.plan_task("T01", "R01", [".github/workflows/ci.yml"])
        (self.root / ".github/workflows").mkdir(parents=True)
        self.configure(POLICY, "fast", [ci])
        self.start("T01")
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.verify("T01")
        self.team.submit("T01", self.report("T01"))
        self.assertEqual(self.host.task("T01")["completionKind"], "verified_completion")
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertEqual(check_modes.skipped_checks(self.state), ["Проверка тяжёлых задач отдельным критиком"])

    def test_promotion_survives_a_repeated_plan(self):
        self.configure(POLICY, "balanced", [self.plan_task("T01", "R01", ["c.txt"], tier="standard")])
        self.assertEqual(self.attempt()["criticRoles"], ["worker"])
        self.team.promote("T01", "Touches shared output handling")
        self.team.prepare()
        self.assertEqual((self.attempt()["tier"], self.attempt()["criticRoles"]),
                         ("heavy", ["worker", "critic1", "critic2"]))

    def test_red_follows_weight_and_scenarios_follow_depth(self):
        policy = dict(POLICY, testIntent=True)
        plain = self.plan_task("T03", "R01", ["c.txt"])
        for mode in check_modes.MODES:
            with self.subTest(mode=mode):
                self.configure(policy, mode, [self.plan_task("T01", "R01", ["a.txt"], ["T03"]), plain])
                self.assertTrue(test_intent.required(self.state, self.attempt("T03")))
                self.assertFalse(test_intent.required(self.state, self.attempt("T01")))
        dimensions = set()
        for mode in check_modes.MODES:
            self.state.update(checkMode=mode, depth="balanced")
            dimensions.add(design_contract.required_dimensions(self.state))
        self.assertEqual(dimensions, {design_contract.LEAN_REQUIRED["balanced"]})
        self.state["depth"] = "deep"
        self.assertEqual(design_contract.required_dimensions(self.state), design_contract.DIMENSIONS)


class FreshnessTests(FocusedFixture):
    def test_resigned_dependency_proof_keeps_report_fresh_but_reopen_and_edits_do_not(self):
        self.complete("T01", [("a.txt", "a by T01")])
        self.start("T02")
        self.write("b.txt", "b by T02")
        self.verify("T02")
        before = task_basis(self.store, self.state, "T02")
        dependency = self.host.task("T01")
        dependency["proof"] = dict(dependency["proof"], report="resigned-" + "0" * 55)
        self.assertEqual(task_basis(self.store, self.state, "T02"), before)
        dependency["status"] = "pending"
        self.assertNotEqual(task_basis(self.store, self.state, "T02"), before)
        dependency["status"] = "completed"
        self.assertEqual(task_basis(self.store, self.state, "T02"), before)
        self.assign("T02", "critic1") if "critic1" in task_tiers.roles(self.attempt("T02")) else None
        self.assertEqual(task_basis(self.store, self.state, "T02"), before)
        self.write("b.txt", "edited again")
        self.assertNotEqual(task_basis(self.store, self.state, "T02"), before)


class RoutesAndCardsTests(FocusedFixture):
    def test_route_table_advice_in_card_and_packet(self):
        policy = dict(POLICY, modelRoutes=True)
        money = self.plan_task("T02", "R02", ["billing/pay.py"], ["T01"])
        for runtime, judge in (("claude-code", "claude-opus-5-5"), ("codex", "gpt-6.1-sol")):
            with self.subTest(runtime=runtime):
                self.configure(policy, "balanced", [self.plan_task("T01", "R01", ["a.txt"]), money], runtime)
                self.start("T01")
                self.write("a.txt", "a by T01")
                self.submit("T01")
                card = json.loads(self.store.text(self.attempt("T01")["roles"]["critic1"]))
                packet = self.packet("T01", "critic1")
                for routing in (card["routing"], packet["routing"]):
                    self.assertEqual((routing["routeClass"], routing["model"], routing["effort"]), ("judge", judge, "high"))
                self.assertNotIn(packets.CODEX_MODEL_RULE, json.dumps(packet, ensure_ascii=False))
                worker = json.loads(self.store.text(self.attempt("T01")["roles"]["worker"]))["routing"]
                self.assertEqual(worker["routeClass"], "worker")
                self.assertEqual(worker["model"], model_routes.ROUTES["worker"][runtime]["model"])
                sensitive = packets.routing(self.state, money, "worker", self.attempt("T02"), self.store)
                self.assertEqual(sensitive["routeClass"], "sensitive_worker")

    def test_cards_are_created_on_assignment_only(self):
        tasks = [self.plan_task("T0%d" % n, "R01", ["f%d.txt" % n]) for n in range(1, 7)]
        self.configure(POLICY, "max", tasks)
        roles = self.store.run / "roles"
        initial = len(list(roles.iterdir()))  # cards of the legacy fixture before the policy was chosen

        def count():
            return len(list(roles.iterdir())) - initial
        self.assertEqual(count(), 0)
        for task in tasks:
            self.assign(task["id"], "worker")
        for task in tasks[:3]:
            self.assign(task["id"], "critic1")
        for _ in range(3):
            self.team.prepare()
        self.assertEqual(count(), 9)
        self.assertEqual(set(self.attempt("T01")["roles"]), {"worker", "critic1"})
        self.assertEqual(set(self.attempt("T06")["roles"]), {"worker"})
        self.state["spec"] = self.store.artifact("specs", b"Changed spec\n", ".md")
        self.team.prepare()
        self.assertEqual((count(), self.attempt("T01")["roles"]), (9, {}))
        self.assign("T01", "worker")
        self.assertEqual(count(), 10)


if __name__ == "__main__":
    unittest.main()
