"""Lean control: the passing critic closes a heavy task, standard needs no task
critic, scenarios scale with depth, and batch runs ordinary steps in one call."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_adaptive_review
import test_cli
import test_design_contract
import test_team
from aispa_core import design_contract, display, integration, packets, planning, review_policy, task_reports, task_tiers
from aispa_core.contracts import task_basis, validate_plan
from aispa_core.store import Invalid
from aispa_core.team import ACCEPTANCE_LENS, _history_valid, _seal, direct_proof_current, historical_proof_valid, task_proof_valid


Adaptive = test_adaptive_review.AdaptiveReviewTests


class LeanReviewTests(unittest.TestCase):
    """Borrow the adaptive helpers; only the frozen run policy differs."""
    tearDown = Adaptive.tearDown
    task = Adaptive.task
    attempt = Adaptive.attempt
    verify = Adaptive.verify
    report = Adaptive.report
    input = Adaptive.input
    finding = Adaptive.finding
    repair = Adaptive.repair
    second_actor = Adaptive.second_actor
    accept = Adaptive.accept


    def setUp(self):
        test_team.TeamTests.setUp(self)
        self.state.update(policy={"adaptiveCritique": True, "leanControl": True}, taskHistory={}, actors={})
        self.team.prepare()

    def start(self, tid="T01"):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        for role in ("worker", "critic1"):
            self.serial += 1
            self.team.assign(tid, role, "fixture", str(self.serial), "session")
        self.verify(tid)

    def lean_fields(self, tid="T01", drift=False):
        return {"inheritedProofs": self.team.inherited_proofs(tid),
                "trace": [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"],
                           "result": "drift" if drift and index == 0 else "aligned",
                           "note": "Compared source", "artifact": self.evidence["file"]}
                          for index, req in enumerate(self.team.trace_requirements(tid))]}

    def first(self, verdict="PASS", tid="T01", drift=False, **extra):
        self.team.submit(tid, self.report(tid))
        if verdict != "PASS":
            extra.setdefault("findings", [self.finding(criterion=tid + " works")])
        fields = self.lean_fields(tid, drift)
        fields.update(extra)
        return self.team.review(tid, 1, self.report(tid, "critic1", verdict=verdict, **fields))

    def second(self, verdict="PASS", tid="T01", **extra):
        fields = self.lean_fields(tid)
        fields.update(extra)
        return test_adaptive_review.AdaptiveReviewTests.second(self, verdict, tid, **fields)

    def test_roles_have_no_task_acceptor(self):
        self.assertEqual(review_policy.mode(self.attempt()), review_policy.LEAN)
        self.assertEqual(set(self.attempt()["roles"]), {"worker", "critic1", "critic2"})
        self.start()
        with self.assertRaises(Invalid):
            self.team.assign("T01", "acceptor", "fixture", "acceptor-1", "session")
        card = json.loads(self.store.text(self.attempt()["roles"]["critic1"]))
        self.assertIn(ACCEPTANCE_LENS, card["checks"])
        expected = self.team.expected_checks("T01", "critic1")
        self.assertIn(("G01", "No private data"), expected)

    def test_first_pass_completes_task_without_acceptor(self):
        self.start()
        self.team.submit("T01", self.report())
        draft = task_reports.template(self.team, "T01", "critic1")
        self.assertIsNone(draft["verdict"])
        self.assertEqual([row["requirement"] for row in draft["trace"]],
                         [req["id"] for req in self.team.trace_requirements("T01")])
        self.assertEqual(draft["inheritedProofs"], [])
        self.assertTrue(self.team.review("T01", 1, self.report(role="critic1", verdict="PASS", findings=[],
                                                               **self.lean_fields())))
        task, attempt = self.host.task("T01"), self.attempt()
        self.assertEqual((task["status"], task["completionKind"]), ("completed", "critic_acceptance"))
        self.assertEqual(attempt["acceptance"]["report"], attempt["rounds"][0]["report"])
        self.assertEqual(attempt["acceptance"]["round"], 1)
        self.assertNotIn("critic2", attempt["actors"])
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        for operation in (lambda: self.second_actor(), lambda: self.team.accept("T01", "missing.json"),
                          lambda: task_reports.template(self.team, "T01", "acceptor")):
            with self.assertRaises(Invalid):
                operation()
        steps = {s["id"]: s["status"] for s in display._phase_steps(self.store, self.state, task, True)}
        self.assertEqual((steps["critic_1"], steps["critic_2"], steps["task_acceptance"]), ("done", "skipped", "skipped"))

    def test_pass_requires_full_trace_and_inherited_links(self):
        self.start()
        self.team.submit("T01", self.report())
        fields = self.lean_fields()
        for broken in ({"trace": fields["trace"][1:]}, {"trace": None}, {"inheritedProofs": None},
                       {"trace": [dict(fields["trace"][0], quote="Invented quote.")] + fields["trace"][1:]}):
            report = dict(fields, **broken)
            with self.subTest(broken=broken), self.assertRaises(Invalid):
                self.team.review("T01", 1, self.report(role="critic1", verdict="PASS", findings=[], **report))
        self.assertEqual(self.attempt()["rounds"], [])

    def test_drift_cannot_pass_and_needs_acceptance_finding(self):
        self.start()
        self.team.submit("T01", self.report())
        drift = self.lean_fields(drift=True)
        with self.assertRaises(Invalid):
            self.team.review("T01", 1, self.report(role="critic1", verdict="PASS", findings=[], **drift))
        with self.assertRaises(Invalid):
            self.team.review("T01", 1, self.report(role="critic1", verdict="WARN",
                                                   findings=[self.finding()], **drift))
        self.assertFalse(self.team.review("T01", 1, self.report(role="critic1", verdict="WARN",
            findings=[self.finding(criterion=ACCEPTANCE_LENS)], **drift)))
        self.assertEqual(self.host.task("T01")["phase"], "repair")
        self.assertIsNone(self.attempt()["acceptance"])

    def test_warn_repair_then_second_pass_completes(self):
        self.start()
        self.assertFalse(self.first("WARN"))
        self.repair()
        self.assertTrue(self.second())
        attempt = self.attempt()
        self.assertEqual(attempt["acceptance"]["round"], 2)
        self.assertEqual(attempt["acceptance"]["actor"], attempt["actors"]["critic2"])
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_second_non_pass_blocks(self):
        self.start()
        self.first("FAIL")
        self.repair()
        self.assertFalse(self.second("WARN"))
        self.assertEqual(self.host.task("T01")["status"], "blocked")
        self.assertTrue(self.attempt()["terminal"])

    def test_tampered_lean_acceptance_is_not_proof(self):
        self.start()
        self.assertTrue(self.first())
        original = copy.deepcopy(self.attempt()["acceptance"])
        for change in ({"round": 2}, {"completionKind": "task_acceptance"}, {"coverage": original["coverage"][1:]}):
            value = {k: v for k, v in original.items() if k != "record"}
            value.update(change)
            self.attempt()["acceptance"] = _seal(self.store, value)
            with self.subTest(change=change):
                self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))
        self.attempt()["acceptance"] = original
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))

    def test_dependent_task_links_and_rechecks_ancestor(self):
        self.state["tasks"][1]["dependsOn"] = ["T01"]
        self.state["taskHistory"], self.state["actors"] = {}, {}
        self.team.prepare()
        self.start()
        self.assertTrue(self.first())
        self.start("T02")
        self.assertTrue(self.first(tid="T02"))
        self.assertEqual(self.attempt("T02")["acceptance"]["inheritedProofs"][0]["task"], "T01")
        self.assertTrue(historical_proof_valid(self.store, self.state, "T02"))
        coverage = {(r["requirement"], r["criterion"]) for r in self.attempt("T02")["acceptance"]["coverage"]}
        self.assertIn(("T01", "T01 works"), coverage)
        (self.root / "a.txt").write_text("changed by T02", encoding="utf-8")
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))

    def test_inherited_order_does_not_break_proof(self):
        self.state["tasks"].append(dict(self.task("T03", "R01", "c.txt", ["T01", "T02"]), wave=3))
        (self.root / "c.txt").write_text("c", encoding="utf-8")
        self.state["taskHistory"], self.state["actors"] = {}, {}
        self.team.prepare()
        for tid in ("T01", "T02"):
            self.start(tid)
            self.assertTrue(self.first(tid=tid))
        self.start("T03")
        self.team.submit("T03", self.report("T03"))
        fields = self.lean_fields("T03")
        fields["inheritedProofs"] = list(reversed(fields["inheritedProofs"]))
        self.assertTrue(self.team.review("T03", 1, self.report("T03", "critic1", verdict="PASS", findings=[], **fields)))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T03"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T03"))

    def test_routing_and_critic_packet_read_changes_first(self):
        routing = packets.routing(self.state, self.host.task("T01"), "critic1", self.attempt(), self.store)
        self.assertEqual(routing["taskRoleSlots"], {"minimum": 2, "maximum": 3})
        self.assertIn("separate task acceptor", routing["reviewRule"])
        self.assertEqual(planning._role_estimate(self.state, self.state["tasks"]), {"minimum": 4, "maximum": 6})

    def test_adaptive_runs_keep_separate_acceptor(self):
        self.state["policy"] = {"adaptiveCritique": True}
        self.state["taskHistory"], self.state["actors"] = {}, {}
        self.team.prepare()
        self.assertEqual(review_policy.mode(self.attempt()), review_policy.ADAPTIVE)
        self.assertIn("acceptor", self.attempt()["roles"])


class StandardTierTests(unittest.TestCase):
    task = test_team.TeamTests.task
    attempt = test_team.TeamTests.attempt
    report = test_team.TeamTests.report
    verify = test_team.TeamTests.verify
    tearDown = test_team.TeamTests.tearDown

    def setUp(self):
        test_team.TeamTests.setUp(self)
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.state["policy"] = {"taskTiers": True, "adaptiveCritique": True, "leanControl": True}
        self.state["tasks"] = [self.standard()]
        self.state["taskHistory"], self.state["actors"] = {}, {}
        self.team.prepare()

    def standard(self, tid="T01", zone="src", deps=None, **extra):
        return dict(self.task(tid, "R01", zone, deps), requirements=["R01", "R02", "G01"], tier="standard",
                    tierReason="Ordinary isolated module", changeKind="feature", risk="normal", riskReason="", **extra)

    def raw(self, **extra):
        value = {"id": "T01", "title": "Module", "requirements": ["R01", "R02", "G01"], "dependsOn": [],
                 "zones": ["src"], "criteria": ["Works"], "tier": "standard", "tierReason": "Isolated module",
                 "changeKind": "feature"}
        value.update(extra)
        return value

    def start(self, tid="T01"):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        self.team.assign(tid, "worker", "fixture", tid + "-writer", "session")

    def test_classification_limits(self):
        self.assertEqual(validate_plan(self.state, [self.raw()])[0]["tier"], "standard")
        for broken in ({"risk": "high", "riskReason": "money"}, {"zones": ["src/auth"]}, {"zones": ["."]},
                       {"zones": ["db/migrations"]}, {"zones": ["AGENTS.md"]}, {"zones": ["src/*.py"]}):
            with self.subTest(broken=broken), self.assertRaises(Invalid):
                validate_plan(self.state, [self.raw(**broken)])
        # Only the final whole-plan task may depend on standard work.
        middle = self.raw(id="T02", zones=["lib"], tier="heavy", tierReason="Uses module", dependsOn=["T01"])
        final = self.raw(id="T03", zones=["app"], tier="heavy", tierReason="Final", dependsOn=["T01", "T02"])
        with self.assertRaises(Invalid):
            validate_plan(self.state, [self.raw(), middle, final])
        sibling = self.raw(id="T02", zones=["lib"])
        tiers = {t["id"]: t["tier"] for t in validate_plan(self.state, [self.raw(), sibling, final])}
        self.assertEqual(tiers, {"T01": "standard", "T02": "standard", "T03": "heavy"})
        without_lean = dict(self.state, policy={"taskTiers": True, "adaptiveCritique": True})
        with self.assertRaises(Invalid):
            validate_plan(without_lean, [self.raw()])

    def test_worker_completion_inside_zone(self):
        self.assertEqual(set(self.attempt()["roles"]), {"worker"})
        self.start()
        with self.assertRaises(Invalid):
            self.team.assign("T01", "critic1", "fixture", "critic", "session")
        (self.root / "src/app.py").write_text("print('b')\n", encoding="utf-8")
        (self.root / "src/extra.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.verify()
        receipt = self.team.submit("T01", self.report())
        self.assertEqual(receipt["completionKind"], "verified_completion")
        self.assertEqual({row["path"] for row in receipt["changes"]}, {"src/app.py", "src/extra.py"})
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        routing = packets.routing(self.state, self.host.task("T01"), "worker", self.attempt(), self.store)
        self.assertEqual((routing["taskTier"], routing["perTaskReview"]), ("standard", "skipped_standard"))

    def test_changes_outside_zone_or_sensitive_require_promotion(self):
        for path in ("a.txt", "src/auth/token.py"):
            with self.subTest(path=path):
                self.tearDown(); self.setUp()
                self.start()
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed\n", encoding="utf-8")
                self.verify()
                with self.assertRaises(Invalid):
                    self.team.submit("T01", self.report())
                self.assertEqual(self.host.task("T01")["status"], "running")

    def test_restart_keeps_first_baseline(self):
        self.start()
        (self.root / "a.txt").write_text("outside\n", encoding="utf-8")
        self.verify()
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())
        self.team.start_guard("T01")
        self.verify()
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())

    def test_sensitive_names_and_deletions(self):
        for name in (".cursorrules", "src/userAuth.ts", "src/stripeCheckout.ts", "certs/server.pem", ".envrc",
                     "src/passwordReset.py", "CONVENTIONS.md", ".mcp.json", "правила/агент.md"):
            with self.subTest(name=name):
                self.assertTrue(task_tiers.sensitive_path(name))
        for name in (".gitlab-ci.yml", "deploy/Dockerfile", "src/package.json", "poetry.lock"):
            self.assertTrue(task_tiers.sensitive_path(name), name)
        for name in ("src/report/view.py", "src/author.py", "src/validationRules.ts"):
            self.assertFalse(task_tiers.sensitive_path(name), name)
        self.start()
        (self.root / "src/app.py").unlink()
        self.verify()
        receipt = self.team.submit("T01", self.report())
        self.assertEqual([row["path"] for row in receipt["changes"]], ["src/app.py"])

    def test_promote_standard_to_heavy_keeps_history(self):
        self.start()
        attempt_id = self.team.promote("T01", "Touches shared persistence")
        history = self.state["taskHistory"]["T01"]
        self.assertEqual(history[-1]["id"], attempt_id)
        self.assertEqual(history[-1]["authorization"]["previousTier"], "standard")
        self.assertEqual(history[-1]["tier"], "heavy")
        self.assertEqual(set(history[-1]["roles"]), {"worker", "critic1", "critic2"})
        self.assertTrue(_history_valid(self.store, self.state, "T01"))


class LeanScenarioTests(unittest.TestCase):
    setUp = test_design_contract.DesignContractTests.setUp
    tearDown = test_design_contract.DesignContractTests.tearDown
    feature = test_design_contract.DesignContractTests.feature
    def lean(self, depth):
        self.state["policy"] = {"leanControl": True}
        self.state["depth"] = depth
        report = self.feature(depth)
        names = design_contract.required_dimensions(self.state)
        report["features"][0]["dimensions"] = [c for c in report["features"][0]["dimensions"] if c["name"] in names]
        return report

    def test_balanced_requires_four_dimensions(self):
        report = self.lean("balanced")
        self.assertEqual({c["name"] for c in report["features"][0]["dimensions"]},
                         {"main", "empty", "error", "persistence"})
        design_contract.record_scenarios(self.store, self.state, report)
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        missing = self.lean("balanced")
        missing["features"][0]["dimensions"] = [c for c in missing["features"][0]["dimensions"] if c["name"] != "empty"]
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, missing)

    def test_strict_and_deep_bounds(self):
        self.assertEqual(design_contract.required_dimensions({"policy": {"leanControl": True}, "depth": "strict"}),
                         ("main", "error"))
        self.assertEqual(design_contract.required_dimensions({"policy": {"leanControl": True}, "depth": "deep"}),
                         design_contract.DIMENSIONS)
        self.assertEqual(design_contract.required_dimensions({"policy": {}, "depth": "strict"}),
                         design_contract.DIMENSIONS)

    def test_non_lean_still_requires_all_eight(self):
        report = self.feature()
        report["features"][0]["dimensions"] = report["features"][0]["dimensions"][:4]
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, report)


class BatchCliTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    tearDown = test_cli.CliTests.tearDown
    git = test_cli.CliTests.git
    call = test_cli.CliTests.call
    init = test_cli.CliTests.init
    input = test_cli.CliTests.input

    def batch(self, steps, expected=0):
        path = self.input(steps, "steps.json")
        r = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root), "batch", "--file", path],
                           text=True, capture_output=True, timeout=60, encoding="utf-8", env=self.command_env)
        self.assertEqual(r.returncode, expected, r.stderr + r.stdout)
        return json.loads(r.stdout) if r.stdout.strip() else r

    def test_batch_runs_steps_and_stops_at_first_failure(self):
        self.init()
        result = self.batch([["status"], ["decision", "Batch decision"], ["status"]])
        self.assertEqual((result["ok"], result["completed"], result["total"]), (True, 3, 3))
        self.assertTrue(result["steps"][1]["output"]["compact"])
        self.assertIn("Batch decision", json.dumps(self.call("status")["decisions"], ensure_ascii=False))
        failed = self.batch([["status"], ["block", "T99", "--note", "missing"], ["decision", "never"]], expected=2)
        self.assertEqual((failed["ok"], failed["completed"], failed["stoppedAt"]), (False, 1, 2))
        self.assertEqual(len(failed["steps"]), 2)
        self.assertTrue(failed["steps"][1]["error"])

    def test_batch_rejects_denied_and_malformed_steps(self):
        self.init()
        bom = self.inbox / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + json.dumps([["status"]]).encode())
        r = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root), "batch", "--file", str(bom)],
                           text=True, capture_output=True, timeout=60, encoding="utf-8", env=self.command_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        for steps in ([["init", "--slug", "x"]], [["batch", "--file", "x"]], [["update-check"]],
                      [["journal-reconcile", "--file", "x"]], [["journal-verify"]], [["status", "-h"]],
                      [["status", "--root", "/tmp"]], [], [["status"]] * 26, [[]], ["status"]):
            with self.subTest(steps=steps):
                r = self.batch(steps, expected=2)
                self.assertIn("batch", r.stderr)


del Adaptive  # keep the loader from collecting the adaptive suite a second time


if __name__ == "__main__":
    unittest.main()
