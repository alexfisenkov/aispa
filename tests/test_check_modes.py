"""Owner-chosen check modes in the engine: fast, balanced and max."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_cli
import test_lean_control
import test_team
import test_workflow
from aispa_core import check_modes, task_tiers
from aispa_core.contracts import basis, gate_valid, plan_basis, scope_basis, task_basis, validate_plan
from aispa_core.store import Invalid, Journal
from aispa_core.team import _history_valid, _seal, direct_proof_current, historical_proof_valid, task_proof_valid
from aispa_core.display import project
from aispa_core.workflow import Workflow


Lean = test_lean_control.LeanReviewTests
FAST_WORDS, MAX_WORDS = "Switch to the fast mode now.", "Switch to the max mode now."


class ModeCase(unittest.TestCase):
    """Real Workflow over the team fixture; repository gates are out of scope here."""
    tearDown = Lean.tearDown
    task = Lean.task
    attempt = Lean.attempt
    verify = Lean.verify
    report = Lean.report
    finding = Lean.finding
    repair = Lean.repair
    second_actor = Lean.second_actor
    lean_fields = Lean.lean_fields
    first = Lean.first
    second = Lean.second

    def setUp(self):
        test_team.TeamTests.setUp(self)

    def run_as(self, mode, tasks=None, **policy):
        self.state.update(policy={"adaptiveCritique": True, "leanControl": True, "taskTiers": True, **policy},
                          taskHistory={}, actors={})
        check_modes.initialize(self.state, mode)
        if tasks is not None:
            self.state["tasks"] = tasks
        self.host = Workflow(self.store, self.state)
        self.host.build_ready = self.host.running
        self.team = self.host.pipeline
        self.team.prepare()

    def begin(self, tid, *roles):
        """Start through the real Workflow, then seat the roles the class needs."""
        self.host.start(tid, basis(self.store, self.state, "task", tid))
        for role in roles or [r for r in ("worker", "critic1") if r in self.attempt(tid)["roles"]]:
            self.serial += 1
            self.team.assign(tid, role, "fixture", str(self.serial), "session")
        self.verify(tid)

    def standard(self, tid, zone, deps=None):
        return dict(self.task(tid, "R01", zone, deps), tier="standard", tierReason="Ordinary module",
                    changeKind="feature", risk="normal", riskReason="")

    def fail_twice(self, tid="T01"):
        self.begin(tid)
        self.assertFalse(self.first("WARN", tid=tid))
        self.repair(tid)
        self.assertFalse(self.second("WARN", tid=tid))

    def defer(self, tid="T01"):
        """Both circles and the one extra wave fail: the task is deferred."""
        self.fail_twice(tid)
        self.team.extra_wave(tid)
        self.begin(tid)
        (self.root / self.host.task(tid)["zones"][0]).write_text("second wave candidate", encoding="utf-8")
        self.verify(tid)
        self.assertFalse(self.first("FAIL", tid=tid))
        self.repair(tid)
        self.assertFalse(self.second("WARN", tid=tid))


class FastModeTests(ModeCase):
    def setUp(self):
        super().setUp()
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("fast", [self.standard("T01", "src")], testIntent=True)

    def test_attempt_is_fast_and_keeps_planned_class(self):
        attempt = self.attempt()
        self.assertEqual((attempt["tier"], attempt["plannedTier"], attempt["checkMode"]), ("fast", "standard", "fast"))
        self.assertEqual(self.host.task("T01")["tier"], "standard")
        self.assertEqual(set(attempt["roles"]), {"worker"})
        self.assertFalse(task_tiers.is_standard(attempt))

    def test_worker_completes_without_test_intent(self):
        self.host.start("T01", basis(self.store, self.state, "task", "T01"))
        self.team.assign("T01", "worker", "fixture", "w1", "session")
        (self.root / "src/app.py").write_text("print('b')\n", encoding="utf-8")
        self.verify()
        receipt = self.team.submit("T01", self.report())
        self.assertEqual(receipt["completionKind"], "verified_completion")
        self.assertNotIn("testIntent", receipt)
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_write_outside_zones_is_rejected_sensitive_path_inside_is_not(self):
        self.begin("T01", "worker")
        (self.root / "a.txt").write_text("outside\n", encoding="utf-8")
        self.verify()
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "src/auth").mkdir()
        (self.root / "src/auth/token.py").write_text("TOKEN = None\n", encoding="utf-8")
        self.verify()
        self.assertEqual(self.team.submit("T01", self.report())["completionKind"], "verified_completion")

    def test_replan_of_unchanged_plan_passes(self):
        self.begin("T01", "worker")
        self.team.validate_replan(copy.deepcopy(self.state["tasks"]))
        changed = copy.deepcopy(self.state["tasks"])
        changed[0]["tier"] = "light"
        with self.assertRaises(Invalid):
            self.team.validate_replan(changed)

    def test_next_wave_starts_without_brief_check(self):
        self.run_as("fast", [self.standard("T01", "src"), self.standard("T02", "lib")])
        (self.root / "lib").mkdir()
        (self.root / "lib/m.py").write_text("x = 1\n", encoding="utf-8")
        self.host.task("T02")["wave"] = 2
        self.begin("T01", "worker")
        (self.root / "src/app.py").write_text("print('c')\n", encoding="utf-8")
        self.verify()
        self.team.submit("T01", self.report())
        self.assertEqual(self.host.s["waveEntries"]["1"]["checkMode"], "fast")
        self.assertEqual([t["id"] for t in self.host.ready()["tasks"]], ["T02"])
        self.host.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertEqual(self.host.task("T02")["status"], "running")

    def test_balanced_control_still_requires_the_check(self):
        self.run_as("balanced", [self.standard("T01", "src"), self.standard("T02", "lib")])
        (self.root / "lib").mkdir()
        (self.root / "lib/m.py").write_text("x = 1\n", encoding="utf-8")
        self.host.task("T02")["wave"] = 2
        self.begin("T01", "worker")
        (self.root / "src/app.py").write_text("print('c')\n", encoding="utf-8")
        self.verify()
        self.team.submit("T01", self.report())
        self.assertEqual(self.host.ready()["tasks"], [])
        with self.assertRaises(Invalid):
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))


    def test_acceptance_gate_does_not_need_integration_review_in_fast(self):
        self.run_as("fast", [self.standard("T01", "src")], integrationReview=True)
        self.state["integrationTask"] = "T01"
        report = self.store.artifact("reviews", b"{}\n", ".json")
        self.state["gates"]["acceptance"] = {"passed": True, "basis": basis(self.store, self.state, "acceptance"),
                                             "report": report}
        self.assertTrue(gate_valid(self.store, self.state, "acceptance"))
        self.state["checkMode"] = "balanced"  # the same record is refused once the review is required again
        self.state["gates"]["acceptance"]["basis"] = basis(self.store, self.state, "acceptance")
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))

    def test_fast_wave_mark_survives_reset_but_not_a_new_plan(self):
        self.run_as("fast", [self.standard("T01", "src"), self.standard("T02", "lib"),
                             self.standard("T03", "lib2")])
        for name in ("lib", "lib2"):
            (self.root / name).mkdir()
            (self.root / name / "m.py").write_text("x = 1\n", encoding="utf-8")
        self.host.task("T03")["wave"] = 2
        self.begin("T01", "worker")
        (self.root / "src/app.py").write_text("print('d')\n", encoding="utf-8")
        self.verify()
        self.team.submit("T01", self.report())
        self.begin("T02", "worker")
        self.host.reset("T02")
        self.assertEqual(self.state["waveEntries"], {})
        self.state["checkMode"] = "balanced"
        self.assertTrue(self.host._wave_unchecked(1))
        self.state["tasks"][0]["title"] = "Replanned"
        self.assertFalse(self.host._wave_unchecked(1))


class FastFinishTests(unittest.TestCase):
    """Fast mode reports the problems at the end instead of repairing them first."""
    setUp = test_workflow.WorkflowTests.setUp
    tearDown = test_workflow.WorkflowTests.tearDown
    git = test_workflow.WorkflowTests.git
    commit = test_workflow.WorkflowTests.commit
    file = test_workflow.WorkflowTests.file
    prepare = test_workflow.WorkflowTests.prepare
    header = test_workflow.WorkflowTests.header
    coverage = test_workflow.WorkflowTests.coverage
    verify = test_workflow.WorkflowTests.verify
    attempt = test_workflow.WorkflowTests.attempt
    task_report = test_workflow.WorkflowTests.task_report
    completed = test_workflow.WorkflowTests.completed
    assessment = test_workflow.WorkflowTests.assessment

    def refused(self, mode):
        self.prepare()
        self.state.setdefault("policy", {})["leanControl"] = True
        check_modes.initialize(self.state, mode)
        self.completed()
        if mode != "fast":
            self.flow.wave(1, self.file(self.assessment("wave")))
        return self.flow.acceptance(self.file(self.assessment(failed={"R01": "not_tested"})))

    def test_fast_names_the_reduced_scenarios_as_skipped(self):
        self.prepare()
        self.state.setdefault("policy", {}).update(leanControl=True, designContract=True)
        check_modes.initialize(self.state, "fast")
        self.assertIn("Подробные сценарии (только main и error)", check_modes.skipped_checks(self.state))
        check_modes.initialize(self.state, "balanced")
        self.assertNotIn("Подробные сценарии (только main и error)", check_modes.skipped_checks(self.state))

    def test_fast_delivers_with_the_failures_and_the_skipped_checks(self):
        self.assertFalse(self.refused("fast"))
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        self.assertTrue(gate_valid(self.store, self.state, "acceptance", settled=True))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        remaining = self.state["remaining"]
        self.assertEqual([row["requirement"] for row in remaining["checks"]], ["R01"])
        self.assertIn("Сверка волн с полным брифом", remaining["skippedChecks"])
        self.assertEqual(remaining["checkModeChanges"], [])
        view = project(self.store, self.state)
        self.assertEqual(view["status"], "delivered_with_remaining")
        self.assertEqual(view["remaining"]["skippedChecks"], remaining["skippedChecks"])
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")  # a stale acceptance is still stale
        self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
        self.assertEqual(project(self.store, self.state)["status"], "stale")

    def test_balanced_and_max_still_refuse_a_failed_not_deferred_core(self):
        for mode in ("balanced", "max"):
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()
                self.assertFalse(self.refused(mode))
                self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
                with self.assertRaises(Invalid):
                    self.flow.finish()

    def deferred_core(self, failed, deferred=("T01",)):
        """Max: the core task T01 is blocked (deferred when named), T02 is done; R01 is reported as failed or not."""
        self.prepare(secondary=True)
        self.state.setdefault("policy", {})["leanControl"] = True
        check_modes.initialize(self.state, "max")
        self.completed("T02")
        self.flow.task("T01")["status"] = "blocked"
        patch = mock.patch.object(check_modes, "deferred_ids", return_value=set(deferred))
        patch.start()
        self.addCleanup(patch.stop)
        self.verify()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertFalse(self.flow.acceptance(self.file(self.assessment(failed=failed))))

    def test_max_delivers_with_a_deferred_core_task_as_remainder(self):
        self.deferred_core({"R01": "not_tested"})
        self.assertTrue(gate_valid(self.store, self.state, "acceptance", settled=True))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual(self.state["remaining"]["deferredTasks"], ["T01"])
        self.assertEqual(self.state["remaining"]["blockedTasks"], ["T01"])
        self.assertEqual(project(self.store, self.state)["remaining"]["deferredTasks"], ["T01"])

    def test_max_refuses_when_a_failure_is_not_of_a_deferred_task(self):
        self.deferred_core({"R01": "not_tested", "R02": "not_tested"})
        self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_max_refuses_a_blocked_core_task_that_is_not_deferred(self):
        self.deferred_core({"R01": "not_tested"}, deferred=())
        self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_changes_made_by_the_owner_are_in_the_final_state(self):
        self.prepare()
        self.state.setdefault("policy", {})["leanControl"] = True
        check_modes.initialize(self.state, "balanced")
        check_modes.record_switch(self.store, self.state, "fast", "Давай быстро, потом скажешь проблемы")
        self.completed()
        self.assertFalse(self.flow.acceptance(self.file(self.assessment(failed={"R01": "not_tested"}))))
        self.flow.finish()
        changes = self.state["remaining"]["checkModeChanges"]
        self.assertEqual([(c["previous"], c["mode"], c["text"]) for c in changes],
                         [("balanced", "fast", "Давай быстро, потом скажешь проблемы")])


class MaxModeTests(ModeCase):
    def setUp(self):
        super().setUp()
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("max", [self.standard("T01", "src/app.py")])

    def test_planned_standard_becomes_heavy(self):
        attempt = self.attempt()
        self.assertEqual((attempt["tier"], attempt["plannedTier"], attempt["checkMode"]), ("heavy", "standard", "max"))
        self.assertEqual(set(attempt["roles"]), {"worker", "critic1", "critic2"})

    def test_replan_of_unchanged_plan_passes(self):
        self.begin("T01")
        self.assertTrue(self.first())
        self.team.validate_replan(copy.deepcopy(self.state["tasks"]))

    def test_extra_wave_only_after_exhausted_circles_and_only_once(self):
        with self.assertRaises(Invalid):  # nothing consumed yet
            self.team.extra_wave("T01")
        self.fail_twice()
        self.assertTrue(self.attempt()["terminal"])
        self.state["checkMode"] = "balanced"
        with self.assertRaises(Invalid):
            self.team.extra_wave("T01")
        self.state["checkMode"] = "max"
        new_id = self.team.extra_wave("T01")
        history = self.state["taskHistory"]["T01"]
        self.assertEqual((history[-1]["id"], history[-1]["authorization"]["kind"]), (new_id, "extra-wave"))
        self.assertEqual((self.host.task("T01")["status"], history[-1]["tier"]), ("pending", "heavy"))
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        with self.assertRaises(Invalid):  # the new attempt is not exhausted
            self.team.extra_wave("T01")

    def test_second_failure_defers_task(self):
        self.defer()
        task = self.host.task("T01")
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(task["deferred"])
        self.assertIn("Отложено в остатки следующего прогона после дополнительной волны", task["note"])
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        with self.assertRaises(Invalid):
            self.team.extra_wave("T01")

    def test_extra_wave_history_is_checked(self):
        self.fail_twice()
        self.team.extra_wave("T01")
        history = self.state["taskHistory"]["T01"]
        history[0]["terminal"] = False
        self.assertFalse(_history_valid(self.store, self.state, "T01"))
        history[0]["terminal"] = True
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        history[-1]["terminal"] = True
        history.append(self.team._new("T01", _seal(self.store, {"kind": "extra-wave", "previousAttempt": history[-1]["id"],
                                                                   "at": "2026-10-22T00:00:00Z"})))
        history[-1]["tier"] = "heavy"
        self.assertFalse(_history_valid(self.store, self.state, "T01"))


class DeferredDependencyTests(ModeCase):
    def setUp(self):
        super().setUp()
        tasks = [self.task("T01", "R01", "a.txt"), dict(self.task("T02", "R02", "b.txt", ["T01"]), wave=1)]
        self.run_as("max", tasks)

    def test_descendant_is_built_without_deferred_criteria(self):
        self.defer("T01")
        attempt = self.attempt("T02")
        self.assertEqual(attempt["deferredDependencies"], ["T01"])
        card = test_team.json.loads(self.store.text(attempt["roles"]["critic1"]))
        self.assertEqual([t["id"] for t in card["closure"]], ["T02"])
        self.assertEqual(card["deferredDependencies"], ["T01"])
        self.assertEqual([r["id"] for r in card["sources"]], ["G01", "R02"])
        self.assertEqual(self.team.inherited_proofs("T02"), [])
        self.assertNotIn(("T01", "T01 works"), self.team.expected_checks("T02", "critic1"))
        self.assertEqual([t["id"] for t in self.host.ready()["tasks"]], ["T02"])
        self.begin("T02")
        self.assertTrue(self.first(tid="T02"))
        accepted = self.attempt("T02")["acceptance"]
        self.assertEqual(accepted["inheritedProofs"], [])
        self.assertNotIn("T01", {row["requirement"] for row in accepted["coverage"]})
        self.assertTrue(historical_proof_valid(self.store, self.state, "T02"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))

    def defer_without_recard(self):
        """Deferral through the real route, but dependants keep the cards they had."""
        self.team._recard_dependents = lambda tid: None
        self.defer("T01")

    def test_stale_idle_cards_are_rebuilt_before_start(self):
        self.defer_without_recard()
        with self.assertRaises(Invalid):  # refreshes the cards and asks for a new packet
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertEqual(self.attempt("T02")["deferredDependencies"], ["T01"])
        self.host.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertEqual(self.host.task("T02")["status"], "running")

    def test_seated_worker_blocks_stale_cards(self):
        self.team.assign("T02", "worker", "fixture", "early", "session")
        self.defer_without_recard()
        with self.assertRaises(Invalid):
            self.team.start_guard("T02")

    def test_plain_blocked_dependency_still_blocks_start(self):
        self.host.task("T01").update(status="blocked")
        with self.assertRaises(Invalid):
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))

    def test_a_flag_alone_does_not_defer(self):
        self.host.task("T01").update(status="blocked", deferred=True)
        self.assertEqual(self.team._deferred_ids(), set())
        self.assertEqual(self.host.ready()["tasks"], [])
        with self.assertRaises(Invalid):
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))

    def test_deferral_is_read_from_history(self):
        self.defer("T01")
        self.assertEqual(self.team._deferred_ids(), {"T01"})
        self.host.task("T01").pop("deferred")  # the panel flag is not the source
        self.assertEqual(self.team._deferred_ids(), {"T01"})

    def test_retry_of_the_deferred_task_keeps_dependant_proofs(self):
        self.defer("T01")
        self.begin("T02")
        self.assertTrue(self.first(tid="T02"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))
        # What new-attempt does to the deferred task: pending again, no note.
        self.host.task("T01").update(status="pending")
        self.host.task("T01").pop("note")
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))
        self.assertTrue(task_basis(self.store, self.state, "T02"))

    def test_deferred_task_writing_its_zone_keeps_dependant_proofs(self):
        self.defer("T01")
        self.begin("T02")
        self.assertTrue(self.first(tid="T02"))
        self.host.task("T01").update(status="pending")
        self.host.task("T01").pop("note", None)
        (self.root / "a.txt").write_text("T01 retry writes its own zone\n", encoding="utf-8")
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))
        self.assertTrue(task_proof_valid(self.store, self.state, "T02"))
        (self.root / "b.txt").write_text("the dependant's own zone still counts\n", encoding="utf-8")
        self.assertFalse(direct_proof_current(self.store, self.state, "T02"))

    def test_zone_of_a_deferred_task_stays_when_another_task_owns_it(self):
        t2 = dict(self.task("T02", "R02", "b.txt", ["T01"]), wave=1)
        t2["zones"] = ["b.txt", "a.txt"]  # the dependant owns the deferred task's zone as well
        self.run_as("max", [self.task("T01", "R01", "a.txt"), t2])
        self.defer("T01")
        self.begin("T02")
        self.assertTrue(self.first(tid="T02"))
        (self.root / "a.txt").write_text("changed\n", encoding="utf-8")
        self.assertFalse(direct_proof_current(self.store, self.state, "T02"))

    def test_forged_extra_wave_entry_does_not_defer(self):
        hist = self.state["taskHistory"]["T01"]
        hist.append(dict(self.team._new("T01", {"kind": "extra-wave"}), terminal=True))
        self.host.task("T01")["status"] = "blocked"
        self.assertFalse(_history_valid(self.store, self.state, "T01"))
        self.assertEqual(self.team._deferred_ids(), set())
        with self.assertRaises(Invalid):
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))

    def test_sealed_extra_wave_without_a_failure_does_not_defer(self):
        self.fail_twice()
        self.team.extra_wave("T01")
        self.host.task("T01")["status"] = "blocked"
        self.attempt("T01")["terminal"] = True  # the extra wave was opened but nothing failed in it
        self.assertEqual(self.team._deferred_ids(), set())

    def test_replan_and_reset_keep_deferral(self):
        raw = [{"id": "T01", "title": "T01 title", "requirements": ["R01", "G01"], "dependsOn": [], "zones": ["a.txt"],
                "criteria": ["T01 works"]},
               {"id": "T02", "title": "T02 title", "requirements": ["R02"], "dependsOn": ["T01"], "zones": ["b.txt"],
                "criteria": ["T02 works"]}]
        self.run_as("max", [])
        self.state["tasks"] = validate_plan(self.state, raw)
        self.team.prepare()
        self.state["gates"]["coverage"] = {"passed": True, "basis": basis(self.store, self.state, "coverage"),
                                           "report": self.store.artifact("reviews", b"{}", ".json")}
        path = Path(tempfile.mkdtemp()) / "plan.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.host.plan(str(path))
        self.defer("T01")
        self.assertEqual(self.team._deferred_ids(), {"T01"})
        self.host.plan(str(path))  # identical plan: statuses are kept, the old flag is not
        self.assertEqual((self.host.task("T01")["status"], self.team._deferred_ids()), ("blocked", {"T01"}))
        with self.assertRaises(Invalid):
            self.team.extra_wave("T01")

    def test_failure_after_extra_wave_and_mode_changes_is_still_deferred(self):
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("max", [self.standard("T01", "src/app.py")])
        self.fail_twice()
        self.team.extra_wave("T01")
        for mode in ("fast", "max"):
            self.team.switch_check_mode(mode, "Owner words to switch to " + mode)
        self.begin("T01")
        (self.root / "src/app.py").write_text("third\n", encoding="utf-8")
        self.verify()
        self.assertFalse(self.first("FAIL"))
        self.repair()
        self.assertFalse(self.second("WARN"))
        self.assertEqual((self.host.task("T01")["status"], self.team._deferred_ids()), ("blocked", {"T01"}))
        self.assertIn("Отложено в остатки", self.host.task("T01")["note"])


class SwitchTests(ModeCase):
    def setUp(self):
        super().setUp()
        tasks = [self.task(f"T0{n}", "R01", f"f{n}.txt") for n in range(1, 5)]
        for n in range(1, 5):
            (self.root / f"f{n}.txt").write_text("x", encoding="utf-8")
        self.run_as("balanced", tasks)
        self.begin("T01")
        self.assertFalse(self.first("WARN", tid="T01"))      # criticism has begun
        self.begin("T03")
        self.assertTrue(self.first(tid="T03"))               # finished
        self.begin("T04")                                    # running, nothing submitted

    def test_only_idle_tasks_get_a_new_attempt(self):
        before = {tid: self.attempt(tid)["id"] for tid in ("T01", "T03")}
        affected = self.team.switch_check_mode("fast", FAST_WORDS)
        self.assertEqual(affected, ["T02", "T04"])
        self.assertEqual(self.state["checkModeHistory"][-1]["previous"], "balanced")
        for tid in ("T01", "T03"):
            self.assertEqual(self.attempt(tid)["id"], before[tid])
        for tid in affected:
            attempt = self.attempt(tid)
            self.assertEqual((attempt["tier"], attempt["plannedTier"]), ("fast", "heavy"))
            self.assertEqual(attempt["authorization"]["kind"], "check-mode")
            self.assertEqual(attempt["authorization"]["request"], self.state["checkModeHistory"][-1]["request"])
            self.assertEqual(self.store.text(attempt["authorization"]["request"]), FAST_WORDS)
            self.assertEqual(set(attempt["roles"]), {"worker"})
            self.assertTrue(_history_valid(self.store, self.state, tid))
        self.assertEqual((self.host.task("T04")["status"], self.host.task("T03")["status"]), ("pending", "completed"))
        self.assertEqual(self.attempt("T04")["actors"], {})
        self.assertEqual([row["mode"] for row in self.state["checkModeHistory"]], ["balanced", "fast"])
        self.assertTrue(historical_proof_valid(self.store, self.state, "T03"))

    def test_switching_back_restores_the_planned_class(self):
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.assertEqual(self.team.switch_check_mode("balanced", "Back to the careful mode."), ["T02", "T04"])
        attempt = self.attempt("T02")
        self.assertNotIn("plannedTier", attempt)
        self.assertEqual(attempt.get("tier", "heavy"), "heavy")
        self.assertTrue(_history_valid(self.store, self.state, "T02"))

    def test_switch_is_a_separate_record_not_a_brief(self):
        self.state["sealed"] = True
        briefs, scope, plan = copy.deepcopy(self.state["briefs"]), scope_basis(self.state), plan_basis(self.state)
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.assertEqual((self.state["briefs"], scope_basis(self.state), plan_basis(self.state)), (briefs, scope, plan))
        self.assertIs(self.state["sealed"], True)
        self.assertEqual(self.state["checkModeHistory"][-1]["mode"], "fast")

    def test_words_must_be_real_and_new(self):
        for mode, words in (("fast", ""), ("fast", "  \n "), ("balanced", FAST_WORDS)):
            with self.subTest(mode=mode, words=words), self.assertRaises(Invalid):
                self.team.switch_check_mode(mode, words)
        self.assertEqual(self.state["checkMode"], "balanced")

    def test_short_and_repeated_words_are_accepted(self):
        self.team.switch_check_mode("max", "Тщательно")  # a short wish is still the owner's word
        self.assertEqual(self.state["checkMode"], "max")
        self.team.switch_check_mode("fast", MAX_WORDS)
        self.team.switch_check_mode("max", MAX_WORDS)  # the same words at another moment are lawful
        self.assertEqual([row["mode"] for row in self.state["checkModeHistory"]], ["balanced", "max", "fast", "max"])
        history = self.state["checkModeHistory"]
        self.assertEqual(history[-1]["request"]["sha256"], history[-2]["request"]["sha256"])
        self.assertNotEqual(history[-1]["request"]["file"], history[-2]["request"]["file"])  # each moment keeps its own record

    def test_workflow_command_reads_a_file_and_refuses_secrets(self):
        words = self.root / "words.txt"
        words.write_text("Switch now, key ghp_" + "a" * 24 + " please", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.host.check_mode("fast", str(words))
        self.assertEqual(self.state["checkMode"], "balanced")
        words.write_text(FAST_WORDS + "\n", encoding="utf-8")
        self.assertEqual(self.host.check_mode("fast", str(words)), ["T02", "T04"])
        self.assertEqual(self.state["checkMode"], "fast")

    def test_forged_request_is_rejected(self):
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.attempt("T02")["authorization"]["request"] = self.store.artifact("mode-requests", b"Other words.\n", ".md")
        self.assertFalse(_history_valid(self.store, self.state, "T02"))

    def test_history_rejects_forged_switch(self):
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.state["checkModeHistory"].pop()
        self.assertFalse(_history_valid(self.store, self.state, "T02"))

    def test_wave_opened_before_the_switch_is_skipped_while_fast(self):
        self.assertEqual(self.state["waveEntries"]["1"]["checkMode"], "balanced")
        self.assertFalse(self.host._wave_unchecked(1))
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.assertTrue(self.host._wave_unchecked(1))


class BlockedAndBaselineTests(ModeCase):
    def test_ordinary_blocked_task_takes_the_new_class_and_stays_blocked(self):
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("balanced", [self.standard("T01", "src")])
        self.host.block("T01", "No access to the service")
        self.assertEqual(self.team.switch_check_mode("max", MAX_WORDS), ["T01"])
        attempt = self.attempt()
        self.assertEqual((attempt["tier"], attempt["plannedTier"], attempt["authorization"]["kind"]),
                         ("heavy", "standard", "check-mode"))
        self.assertEqual(self.host.task("T01")["status"], "blocked")
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        self.host.start("T01", basis(self.store, self.state, "task", "T01"))
        self.assertEqual(self.host.task("T01")["status"], "running")

    def test_exhausted_task_is_not_given_a_new_attempt(self):
        self.run_as("max", [self.task("T01", "R01", "a.txt")])
        self.fail_twice()
        before = self.attempt()["id"]
        self.assertEqual(self.team.switch_check_mode("fast", FAST_WORDS), [])
        self.assertEqual(self.attempt()["id"], before)

    def test_heavy_to_fast_keeps_writes_made_outside_the_zones(self):
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("max", [self.standard("T01", "src")])
        self.begin("T01")
        self.assertIn("lightBaseline", self.attempt())
        (self.root / "a.txt").write_text("written by the heavy worker outside zones\n", encoding="utf-8")
        self.team.switch_check_mode("fast", FAST_WORDS)
        self.host.start("T01", basis(self.store, self.state, "task", "T01"))
        self.team.assign("T01", "worker", "fixture", "w-new", "session")
        (self.root / "src/app.py").write_text("print('b')\n", encoding="utf-8")
        self.verify()
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())

    def test_heavy_route_does_not_check_zones(self):
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        self.run_as("max", [self.standard("T01", "src")])
        self.begin("T01")
        (self.root / "a.txt").write_text("outside the zones\n", encoding="utf-8")
        self.verify()
        self.assertTrue(self.first())


class OldRunTests(ModeCase):
    def test_runs_without_lean_control_ignore_check_mode(self):
        self.state.update(policy={"adaptiveCritique": True, "taskTiers": True}, taskHistory={}, actors={},
                          checkMode="fast")
        self.state["tasks"] = [self.standard("T01", "a.txt")]
        self.host = Workflow(self.store, self.state)
        self.team = self.host.pipeline
        self.team.prepare()
        attempt = self.attempt()
        self.assertEqual(attempt["tier"], "standard")
        self.assertNotIn("plannedTier", attempt)
        self.assertEqual(check_modes.current(self.state), "balanced")
        with self.assertRaises(Invalid):
            self.team.switch_check_mode("max", MAX_WORDS)

    def test_balanced_keeps_the_declared_class(self):
        self.run_as("balanced", [self.standard("T01", "a.txt"), self.task("T02", "R02", "b.txt")])
        self.assertEqual(self.attempt()["tier"], "standard")
        self.assertNotIn("plannedTier", self.attempt())
        self.assertNotIn("tier", self.attempt("T02"))
        self.assertNotIn("deferredDependencies", self.attempt())


class CheckFlagTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    tearDown = test_cli.CliTests.tearDown
    git = test_cli.CliTests.git
    call = test_cli.CliTests.call

    def init(self, *extra, expected=0):
        extra = extra if "--check" in extra else (*extra, "--check", "balanced")
        return self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto",
                         "--runtime", "codex", *extra, expected=expected)

    def test_init_records_each_mode(self):
        for flag, mode in ((("--check", "fast"), "fast"),
                           (("--check", "balanced"), "balanced"), (("--check", "max"), "max")):
            with self.subTest(flag=flag):
                self.tearDown(); self.setUp()
                self.init(*flag)
                state = Journal(self.root).load()
                self.assertEqual((state["checkMode"], state["checkModeHistory"][0]["mode"]), (mode, mode))

    def test_check_mode_defaults_to_balanced(self):
        self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto", "--runtime", "codex")
        state = Journal(self.root).load()
        self.assertEqual(state["checkMode"], "balanced")
        self.assertEqual(state["checkModeHistory"][0]["source"], "default")

    def test_unknown_mode_is_rejected(self):
        self.init("--check", "careless", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_new_commands_are_wired(self):
        self.init("--check", "max")
        self.call("check-mode", "fast", "--brief", "B001", "--quote", "none", expected=2)  # the old form is gone
        self.call("extra-wave", "T01", expected=2)
        self.assertEqual(Journal(self.root).load()["checkMode"], "max")
        words = self.root / "words.txt"
        words.write_text(FAST_WORDS + "\n", encoding="utf-8")
        self.assertEqual(self.call("check-mode", "fast", "--file", str(words))["receipt"]["mode"], "fast")
        state = Journal(self.root).load()
        self.assertEqual((state["checkMode"], len(state["briefs"])), ("fast", 0))
        self.assertEqual(self.call("check-mode", "balanced", "--file", str(words))["receipt"]["mode"], "balanced")  # same words, another moment


del Lean  # keep the loader from collecting the lean suite a second time



class FocusedSkippedChecksTests(unittest.TestCase):
    """Focused (0.5.0): only what the mode really left out is named; waves are never checked."""

    def state(self, mode, attempts):
        from aispa_core import check_modes
        tasks = [{"id": "T%02d" % n, "wave": 1} for n in range(1, len(attempts) + 1)]
        history = {t["id"]: [dict(a, id=t["id"] + "-A01")] for t, a in zip(tasks, attempts)}
        return {"policy": {"leanControl": True, "focusedReview": True}, "checkMode": mode, "tasks": tasks,
                "taskHistory": history}

    def test_rows_follow_the_frozen_review_roles(self):
        from aispa_core import check_modes
        full = ["worker", "critic1", "critic2"]
        heavy_alone = {"tier": "heavy", "criticRoles": ["worker"]}
        standard = {"tier": "standard", "criticRoles": ["worker"]}
        heavy_once = {"tier": "heavy", "criticRoles": full, "secondRound": False}
        heavy_twice = {"tier": "heavy", "criticRoles": full, "secondRound": True}
        self.assertEqual(check_modes.skipped_checks(self.state("fast", [heavy_alone, standard])),
                         ["Проверка тяжёлых задач отдельным критиком", "Проверка задач standard и light отдельным критиком"])
        self.assertEqual(check_modes.skipped_checks(self.state("balanced", [heavy_once, standard])),
                         ["Проверка задач standard и light отдельным критиком", "Второе независимое ревью тяжёлых задач"])
        self.assertEqual(check_modes.skipped_checks(self.state("max", [heavy_twice, dict(standard, criticRoles=full)])), [])
        state = self.state("fast", [heavy_twice])
        self.assertTrue(all(check_modes.wave_unchecked(dict(state, checkMode=m), 1) for m in check_modes.MODES))
        self.assertNotIn("Сверка волн с полным брифом", check_modes.skipped_checks(state))

if __name__ == "__main__":
    unittest.main()
