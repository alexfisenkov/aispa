"""Focused finish (0.5.0) at the workflow level: no wave checks, freshness by
code, the candidate chain gates acceptance, and a failure after both blind
repair cycles is delivered with its list in every mode."""
import contextlib
import copy
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_final_repairs
import aispa
from aispa_core import check_modes, contracts, final_repairs, focused_review, integration, workflow
from aispa_core.store import Journal
from aispa_core.contracts import basis, gate_valid
from aispa_core.store import Invalid
from aispa_core.workflow import Workflow

MODES = ("fast", "balanced", "max")


class FocusedFinishTests(test_final_repairs.BranchFixture):
    def setUp(self):
        super().setUp()
        self.flow = Workflow(self.store, self.state)
        self.flow.build_ready = lambda: None
        proofs = patch.object(workflow, "task_proof_valid", return_value=True)  # task chains have their own suite
        proofs.start()
        self.addCleanup(proofs.stop)

    def commit(self):
        for args in (("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                    "commit", "-qm", "work")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_waves_are_not_checked_and_the_wave_command_explains_why(self):
        with self.assertRaisesRegex(Invalid, "волны не сверяются; бриф проверяют на входе и в слепой приёмке"):
            self.flow.wave(1, self.root / "unused.json")
        for mode in MODES:
            self.state["checkMode"] = mode
            self.assertTrue(check_modes.wave_unchecked(self.state, 1))
        self.state["tasks"].append({"id": "T02", "title": "Next", "requirements": [], "dependsOn": ["T01"],
                                    "zones": ["next.txt"], "readZones": [], "criteria": ["Next works"],
                                    "wave": 2, "status": "pending"})
        self.assertEqual([row["id"] for row in self.flow.ready()["tasks"]], ["T02"])
        old = dict(self.state, policy={"leanControl": True}, checkMode="balanced")
        self.assertFalse(check_modes.wave_unchecked(old, 1))
        legacy = Workflow(self.store, old)
        legacy.build_ready = lambda: None
        self.assertEqual(legacy.ready()["tasks"], [])

    def test_extra_general_verify_keeps_acceptance_fresh_but_an_edit_does_not(self):
        self.state["verification"] = {"code": self.store.code(), "exitCode": 0, "at": "first"}
        before = basis(self.store, self.state, "acceptance")
        self.state["verification"] = {"code": self.store.code(), "exitCode": 0, "at": "second"}
        self.assertEqual(basis(self.store, self.state, "acceptance"), before)
        (self.root / "app.txt").write_text("Edited after acceptance\n", encoding="utf-8")
        self.assertNotEqual(basis(self.store, self.state, "acceptance"), before)
        old = dict(self.state, policy={"leanControl": True})
        previous = basis(self.store, old, "acceptance")
        old["verification"] = {"code": self.store.code(), "exitCode": 0, "at": "third"}
        self.assertNotEqual(basis(self.store, old, "acceptance"), previous)

    def test_acceptance_waits_for_the_chain_to_end_at_the_current_code(self):
        self.flow.handoff_ready = lambda: None
        self.state["finalRepairs"] = [{"id": "F01", "kind": "findings", "status": "submitted", "rounds": []}]
        with self.assertRaisesRegex(Invalid, "Цепочка кандидатов"):
            self.flow.acceptance(self.root / "unused.json")

    def test_handoff_needs_every_lens_in_every_mode(self):
        for mode in MODES:
            self.state.update(checkMode=mode, projectIntegrationReviewHistory=[], projectIntegrationReview=None)
            self.mode = mode
            with self.assertRaisesRegex(Invalid, "проверка всей ветки"):
                self.flow.handoff_ready()
            self.review_all()
            self.flow.handoff_ready()

    def blind_cycles(self, count):
        self.state["finalRepairs"] = [{"id": "F%02d" % n, "kind": "blind", "status": "passed", "items": ["R01"],
                                       "rounds": []} for n in range(1, count + 1)]
        self.state["blindReview"] = {"repair": "F%02d" % count}

    def failed_gate(self):
        failures = [{"requirement": "R01", "criterion": "blind:0", "status": "failed", "note": "Export missing"}]
        self.state["gates"]["acceptance"] = {
            "passed": False, "failures": failures, "unmapped": [], "artifacts": [],
            "report": self.store.artifact("reviews", b'{"final": true}\n', ".json"),
            "basis": basis(self.store, self.state, "acceptance")}
        return failures

    def test_after_both_blind_cycles_finish_delivers_the_remainder_in_every_mode(self):
        self.commit()
        self.state["collectedFindings"] = [{"id": "W001", "source": {"kind": "task"}, "criterion": "App works",
                                            "note": "Unclear name", "status": "remaining", "resolution": None}]
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready = lambda: None
            for mode in MODES:
                with self.subTest(mode=mode):
                    self.state.update(checkMode=mode, status="running")
                    self.blind_cycles(1)
                    self.failed_gate()
                    self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
                    with self.assertRaisesRegex(Invalid, "Обязательное ядро"):
                        self.flow.finish()
                    self.blind_cycles(2)
                    failures = self.failed_gate()
                    self.flow.finish()
                    self.assertEqual(self.state["status"], "delivered_with_remaining")
                    self.assertEqual(self.state["remaining"]["checks"], failures)
                    self.assertEqual([row["id"] for row in self.state["remaining"]["findings"]], ["W001"])

    def test_a_failure_without_requirement_items_is_delivered_with_its_list_in_every_mode(self):
        """Unmapped requests and observations tied to no requirement have nothing a blind repair could fix."""
        from aispa_core import final_repairs
        self.commit()
        loose = {"requirement": None, "criterion": "blind:0", "status": "blocked",
                 "note": "Исходное наблюдение не связано с активным требованием: export"}
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready = lambda: None
            for mode in MODES:
                for failures, unmapped in (([], ["Экспорт в PDF"]), ([loose], ["Экспорт в PDF"]), ([loose], [])):
                    with self.subTest(mode=mode, failures=len(failures), unmapped=len(unmapped)):
                        self.state.update(checkMode=mode, status="running", finalRepairs=[])
                        self.state.pop("blindReview", None)
                        self.failed_gate()
                        self.state["gates"]["acceptance"].update(failures=list(failures), unmapped=list(unmapped))
                        self.flow.finish()
                        self.assertEqual(self.state["status"], "delivered_with_remaining")
                        self.assertEqual(self.state["remaining"]["checks"], failures)
                        self.assertEqual(self.state["remaining"].get("unmapped", []), unmapped)
                        self.state["status"] = "running"
                        with patch("aispa_core.contracts.gate_current", return_value=True), \
                                self.assertRaisesRegex(Invalid, "Нечего чинить.*finish"):
                            final_repairs.open(self.store, self.state, "blind", None, ["app.txt"])

    def test_a_repairable_item_beside_an_unmapped_request_still_goes_to_the_repair_first(self):
        self.commit()
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready = lambda: None
            for mode in MODES:
                with self.subTest(mode=mode):
                    self.state.update(checkMode=mode, status="running", finalRepairs=[])
                    self.failed_gate()
                    self.state["gates"]["acceptance"]["unmapped"] = ["Экспорт в PDF"]
                    self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
                    with self.assertRaisesRegex(Invalid, "Обязательное ядро"):
                        self.flow.finish()
                    self.assertEqual(self.state["status"], "running")

    def test_failed_product_flow_is_delivered_after_the_cycles_but_never_accepted(self):
        """D04: the flow veto forbids `accepted` and an unsettled finish; a settled failure goes to the remainder."""
        self.commit()
        self.state["policy"]["criticalProductFlows"] = True
        flow_failure = [{"requirement": "R01", "kind": "product-flow", "status": "failed", "note": "Flow broken"}]
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True), \
                patch("aispa_core.product_flows.saved_check", return_value=flow_failure):
            self.flow.handoff_ready = lambda: None
            for mode in MODES:
                with self.subTest(mode=mode):
                    self.state.update(checkMode=mode, status="running")
                    self.blind_cycles(1)
                    self.failed_gate()
                    with self.assertRaisesRegex(Invalid, "Обязательное ядро"):
                        self.flow.finish()
                    self.assertEqual(self.state["status"], "running")
                    self.blind_cycles(2)
                    self.failed_gate()
                    self.state["gates"]["acceptance"]["passed"] = True  # a recorded pass beside a failing flow
                    self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
                    failures = self.failed_gate() + flow_failure
                    self.state["gates"]["acceptance"]["failures"] = failures
                    self.flow.finish()
                    self.assertEqual(self.state["status"], "delivered_with_remaining")
                    self.assertEqual(self.state["remaining"]["checks"], failures)
            # Errors of the saved flow check still refuse the gate in a settled focused run.
            with patch("aispa_core.product_flows.saved_check", side_effect=Invalid("stale basis")):
                self.state["status"] = "running"
                self.assertFalse(gate_valid(self.store, self.state, "acceptance", settled=True))
        old = dict(copy.deepcopy(self.state), policy={"leanControl": True, "criticalProductFlows": True}, checkMode="fast")
        old["gates"]["acceptance"]["basis"] = basis(self.store, old, "acceptance")
        with patch("aispa_core.product_flows.saved_check", return_value=flow_failure):
            self.assertFalse(gate_valid(self.store, old, "acceptance", settled=True))  # old runs: the veto as before
        with patch("aispa_core.product_flows.saved_check", return_value=[]):
            self.assertTrue(gate_valid(self.store, old, "acceptance", settled=True))


class FinaleOutcomeTests(FocusedFinishTests):
    """Every finale ends with an honest outcome: a blocked core task, a mode switch, a missing model."""

    def blocked_core(self):
        self.state["requirements"] = [{"id": "R01", "text": "Export", "kind": "requested", "scope": "active",
                                       "core": True, "acceptance": ["Export works"]}]
        self.state["core"] = ["R01"]
        self.state["tasks"].append({"id": "T02", "title": "Export", "requirements": ["R01"], "dependsOn": [],
                                    "zones": ["export.txt"], "readZones": [], "criteria": ["Export works"],
                                    "wave": 1, "status": "blocked", "note": "Blocked by the owner's decision"})

    def test_a_blocked_core_task_without_failures_is_delivered_with_the_remainder(self):
        self.commit()
        self.blocked_core()
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready = lambda: None
            for mode in ("balanced", "fast"):
                with self.subTest(mode=mode):
                    self.state.update(checkMode=mode, status="running", finalRepairs=[])
                    self.state.pop("remaining", None)
                    self.failed_gate()
                    self.state["gates"]["acceptance"].update(failures=[], unmapped=[])
                    self.assertTrue(gate_valid(self.store, self.state, "acceptance", settled=True))
                    self.flow.finish()
                    self.assertEqual(self.state["status"], "delivered_with_remaining")
                    self.assertEqual(self.state["remaining"]["blockedTasks"], ["T02"])
            # Without a blocked core task the same empty failure is not settled: nothing explains it.
            self.state["tasks"][1]["requirements"] = []
            self.state.update(status="running")
            self.failed_gate()
            self.state["gates"]["acceptance"].update(failures=[], unmapped=[])
            with self.assertRaisesRegex(Invalid, "Обязательное ядро"):
                self.flow.finish()

    def test_check_mode_switch_after_the_final_repair_keeps_the_lenses_and_reaches_finish(self):
        self.warn()
        self.review_all()  # balanced: two lenses
        final_repairs.open(self.store, self.state, "findings", None, ["app.txt"])
        self.flow.pipeline.switch_check_mode("max", "Владелец: дальше проверяй по максимуму")
        self.assertEqual(check_modes.current(self.state), "max")
        balanced = [list(lens) for lens in focused_review.LENSES["balanced"]]
        self.assertEqual(integration.lenses(self.state, self.store), balanced)
        self.assertEqual(integration.lenses(self.state), balanced)
        self.assertTrue(integration.branch_reviewed(self.store, self.state))
        self.assertTrue(integration.lenses_complete(self.store, self.state))
        worker = final_repairs.assign(self.store, self.state, "worker", runtime="fixture", native_id="mode-worker",
                                      session="s-mode-worker")
        (self.root / "app.txt").write_text("Fixed\n", encoding="utf-8")
        final_repairs.submit(self.store, self.state, {"repair": "F01", "worker": worker["actor"], "summary": "Renamed",
                                                      "items": [{"id": "W001", "note": "Renamed the field"}]})
        checker = final_repairs.assign(self.store, self.state, "checker", runtime="fixture",
                                       native_id="mode-checker", session="s-mode-checker")
        final_repairs.check(self.store, self.state, {
            "repair": "F01", "checker": checker["actor"], "independent": True, "candidate": self.store.code(),
            "verdict": "PASS", "items": [{"id": "W001", "status": "fixed", "note": "Read the diff"}]})
        self.commit()
        with patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready()  # the gate of acceptance: lenses of the reviewed candidate, chain current
            self.state["gates"]["acceptance"] = {
                "passed": True, "failures": [], "unmapped": [], "artifacts": [],
                "report": self.store.artifact("reviews", b'{"final": "after switch"}\n', ".json"),
                "basis": basis(self.store, self.state, "acceptance")}
            self.flow.finish()
        self.assertEqual(self.state["status"], "accepted")
        self.assertEqual([row["mode"] for row in self.state["remaining"]["checkModeChanges"]], ["max"])

    def test_finish_names_every_model_blocker_in_the_remainder(self):
        self.commit()
        block = {"code": "BLOCKED_MODEL_CAPABILITY", "role": "critic1", "runtime": "codex", "routeClass": "judge",
                 "task": "T01", "model": "gpt-6.1-sol", "effort": "high", "message": "gpt-6.1-sol is not offered",
                 "options": [{"id": "enable-model", "text": "Enable it"}], "at": "2026-10-28T10:00:00+00:00"}
        self.state["modelBlocks"] = [block, dict(block, role="knowledge-writer", routeClass="research", task=None)]
        with patch("aispa_core.integration.proof_valid", return_value=True), \
                patch.object(workflow, "verified", return_value=True):
            self.flow.handoff_ready = lambda: None
            self.state["gates"]["acceptance"] = {
                "passed": True, "failures": [], "unmapped": [], "artifacts": [],
                "report": self.store.artifact("reviews", b'{"final": "blocks"}\n', ".json"),
                "basis": basis(self.store, self.state, "acceptance")}
            self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual([(row["role"], row["model"], row["task"]) for row in self.state["remaining"]["modelBlocks"]],
                         [("critic1", "gpt-6.1-sol", "T01"), ("knowledge-writer", "gpt-6.1-sol", None)])
        self.assertEqual(self.state["remaining"]["modelBlocks"][0]["options"], block["options"])


class StageRefusalTests(test_final_repairs.BranchFixture):
    """A refused step begins no measured stage: workProgress and the journal stay as they were."""

    def setUp(self):
        super().setUp()
        self.state["runtimeDigest"] = "fixture-runtime"

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(aispa, "runtime_digest", return_value="fixture-runtime"), \
                patch.object(Workflow, "build_ready", lambda flow: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = aispa.execute(aispa.parser().parse_args(["--root", str(self.root), "--full", *args]))
            except (Invalid, OSError, ValueError) as error:
                print("AISpa: " + str(error), file=sys.stderr)
                code = 2
        return code, out.getvalue(), err.getvalue()

    def saved(self):
        store = Journal(self.root)
        state = store.load()
        return store.read_journal()["count"], state.get("workProgress"), state

    def write(self, name, value):
        path = self.root / ".aispa" / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_a_repeated_lens_after_all_lenses_changes_nothing(self):
        self.review_all()
        report = self.lens_report(self.reviewer("late-reviewer"), focused_review.LENSES["balanced"][0])
        self.store.append(self.state, "fixture-late-reviewer")
        before = self.saved()[:2]
        code, _out, err = self.cli("integration-review", "--file", self.write("late-lens.json", report))
        self.assertEqual(code, 2, err)
        self.assertIn("линза этого кандидата уже записана", err)
        self.assertEqual(self.saved()[:2], before)

    def test_a_refused_blind_record_changes_nothing(self):
        self.store.append(self.state, "fixture-ready")
        before = self.saved()[:2]
        code, _out, err = self.cli("blind", "--file", self.write("blind.json", {
            "basis": "x", "reviewer": "fresh-acceptor", "independent": True, "observations": []}))
        self.assertEqual(code, 2, err)
        self.assertEqual(self.saved()[:2], before)

    def test_a_failed_clean_prepare_exits_2_keeps_its_blocker_and_begins_no_blind_stage(self):
        self.state["policy"]["cleanClone"] = True
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True, capture_output=True)
        token = "ghp_" + "A" * 36  # synthetic: matches the secret pattern, belongs to nobody
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "work " + token], check=True, capture_output=True)
        self.store.append(self.state, "fixture-ready")
        count, progress, _state = self.saved()
        with patch.object(Workflow, "handoff_ready", lambda flow: None):
            code, out, err = self.cli("clean-prepare")
        self.assertEqual(code, 2, err)
        answer = json.loads(out)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["receipt"]["blocker"]["code"], "CLEAN_CLONE_FAILED")
        after, progress_after, state = self.saved()
        self.assertEqual((after, progress_after), (count + 1, progress))
        self.assertEqual(state["cleanroom"]["blocker"]["code"], "CLEAN_CLONE_FAILED")
        self.assertIn("секрет", state["cleanroom"]["reason"])
        journal = Journal(self.root)
        journal.load()
        last = journal.read_journal(include_events=True)["events"][-1]
        self.assertEqual(last["event"], "clean-clone-prepared")
        self.assertEqual(last["state"]["cleanroom"]["blocker"]["code"], "CLEAN_CLONE_FAILED")


class BriefChecksTests(test_final_repairs.BranchFixture):
    def test_seal_needs_the_brief_map_and_coverage_ignores_the_mode(self):
        brief = self.store.artifact("briefs", b"Build export.\n", ".md")
        self.state.update(briefs=[dict(brief, id="B001")], spec=None, sealed=False)
        self.state["policy"]["sourceAccounting"] = True
        self.state["requirements"] = [{"id": "R01", "text": "Export", "kind": "requested", "scope": "active",
                                       "core": True, "brief": "B001", "quote": "Build export.",
                                       "acceptance": ["Export works"]}]
        coverage = set()
        with patch.object(workflow.preflight, "require_adopted"), \
                patch.object(workflow.project_documents, "require_fresh_intent"), \
                patch.object(workflow.autonomy, "close_briefing", return_value=False):
            for mode in MODES:
                with self.subTest(mode=mode):
                    self.state.update(checkMode=mode, sealed=False)
                    flow = Workflow(self.store, self.state)
                    with patch.object(workflow.briefmap, "valid", return_value=False), \
                            self.assertRaisesRegex(Invalid, "brief-map"):
                        flow.seal()
                    with patch.object(workflow.briefmap, "valid", return_value=True):
                        flow.seal()
                    self.assertTrue(self.state["sealed"])
                    coverage.add(basis(self.store, self.state, "coverage"))
        self.assertEqual(len(coverage), 1)
        missing = copy.deepcopy(self.state)
        missing["requirements"].append(dict(missing["requirements"][0], id="R02", quote="Build export."))
        task = {"id": "T01", "title": "Export", "requirements": ["R01"], "dependsOn": [], "zones": ["app.txt"],
                "criteria": ["Export works"]}
        for mode in MODES:
            missing["checkMode"] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(Invalid, "потеряны требования: R02"):
                contracts.validate_plan(missing, [dict(task)])


if __name__ == "__main__":
    unittest.main()
