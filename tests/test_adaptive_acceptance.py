"""Deterministic lifecycle guards; fixture reviewers are synthetic, not model runs."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import blind, briefmap, cleanroom, execution_obligations as obligations, participants, phase_receipts, preparation, telemetry
from aispa_core.contracts import basis, gate_valid
from aispa_core.store import Invalid
import test_workflow as legacy


class Fixture(unittest.TestCase):
    # Reuse setup/helpers only: inherited test cases would repeat the legacy suite.
    setUp = legacy.WorkflowTests.setUp
    tearDown = legacy.WorkflowTests.tearDown
    git = legacy.WorkflowTests.git
    commit = legacy.WorkflowTests.commit
    file = legacy.WorkflowTests.file
    prepare = legacy.WorkflowTests.prepare
    header = legacy.WorkflowTests.header
    coverage = legacy.WorkflowTests.coverage
    verify = legacy.WorkflowTests.verify
    attempt = legacy.WorkflowTests.attempt
    task_report = legacy.WorkflowTests.task_report
    completed = legacy.WorkflowTests.completed
    assessment = legacy.WorkflowTests.assessment

    def actor(self, role, number=None):
        native = role + (number or "")
        return participants.register(self.store, self.state, role, "codex", native, native + "-session")

    def source_account(self, execution=True):
        analyst, reviewer = self.actor("brief-analyst"), self.actor("brief-reviewer")
        ctx = preparation.source_context(self.store, self.state)
        def source(actor):
            units = []
            for brief in ctx["briefs"]:
                for unit in brief["units"]:
                    row = {**unit, "kind": "intent", "summary": "Original source obligation", "reason": ""}
                    if execution and unit["text"].startswith("Finish "):
                        # Synthetic reader interprets only its original source text.
                        event, deadline = unit["text"][len("Finish "):].rstrip(".").split(" by ")
                        row["execution"] = {"event": event, "deadline": deadline}
                    units.append(row)
            return {"basis": ctx["basis"], "actor": actor, "units": units}
        preparation.record(self.store, self.state, "brief-extract", source(analyst))
        preparation.record(self.store, self.state, "brief-review", source(reviewer))
        rows = []
        for index, unit in enumerate(ctx["briefs"][0]["units"]):
            row = {k: unit[k] for k in ("brief", "start", "end")}
            row.update(disposition="requirement" if index == 0 or not execution else "execution",
                       requirements=["R01"] if index == 0 or not execution else [],
                       reason="Preserve the original product and separate delivery deadline.")
            if index and execution:
                row["executionObligations"] = ["E01"]
            rows.append(row)
        report = {"basis": briefmap.reconciliation_basis(self.state), "actor": reviewer, "rows": rows}
        preparation.record(self.store, self.state, "brief-map", report)
        return report

    def execution_input(self, event="engine-finish", deadline="2099-01-01T00:00:00+00:00"):
        quote = f"Finish {event} by {deadline}."
        self.flow.brief(self.file("Команда выводит ready.\n" + quote, "brief.md"))
        self.flow.requirements(self.file([{"id": "R01", "text": "Команда", "brief": "B001",
            "quote": "Команда выводит ready.", "core": True, "acceptance": ["Выводит ready"]}]))
        start = "2000-01-01T00:00:00+00:00"
        evidence = self.store.artifact("evidence", json.dumps({"event": "execution-start", "at": start,
            "note": "Synthetic start event for deterministic boundary test"}).encode(), ".json")
        return {"id": "E01", "kind": "deadline", "event": event, "brief": "B001", "quote": quote,
                "startedAt": start, "startEvidence": evidence, "deadline": deadline}

    def prepare_execution(self, event="engine-finish", deadline="2099-01-01T00:00:00+00:00"):
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        obligations.record(self.store, self.state, self.execution_input(event, deadline))
        self.source_account()
        self.flow.seal()
        self.flow.spec(self.file("Команда выводит ready.", "spec.md"))
        self.coverage()
        self.flow.plan(self.file([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]))

    def final_blind(self, execution=False):
        self.state.setdefault("policy", {})["cleanClone"] = True
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        observation = {"brief": "B001", "quote": "Команда выводит ready", "status": "passed",
                       "note": "Observed source-first fixture", "artifact": self.state["cleanrun"]["output"]["file"]}
        report = {"basis": blind.basis(self.store, self.state), "reviewer": "fresh-final-blind",
                  "independent": True, "observations": [observation]}
        if execution:
            report["observations"].append({"brief": "B001", "quote": self.state["executionObligations"][0]["quote"],
                "status": "not_tested", "note": "The final event has not happened yet"})
        blind.record(self.store, self.state, report)
        final = self.assessment()
        final.update(reviewer=report["reviewer"], blindReport=self.state["blindReview"]["report"]["sha256"],
                     cleanrun=self.state["cleanrun"]["artifact"]["sha256"],
                     blindMapping=[{"observation": 0, "requirement": "R01"}])
        if execution:
            final["blindMapping"].append({"observation": 1, "requirement": None, "executionObligation": "E01",
                "executionRecord": self.state["executionObligations"][0]["record"]["sha256"]})
        return final


class ExecutionTests(Fixture):
    def test_requires_origin_and_does_not_accept_late_or_product_waiver(self):
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        value = self.execution_input()
        for changed in ({"event": "product-release"}, {"kind": "waiver"},
                        {"quote": "Команда выводит ready."}, {"quote": "Invented deadline"},
                        {"startEvidence": {"file": "evidence/missing", "sha256": "0" * 64}}):
            with self.subTest(changed=changed), self.assertRaises(Invalid):
                obligations.record(self.store, self.state, {**value, **changed})
        obligations.record(self.store, self.state, value)
        with self.assertRaises(Invalid):
            self.flow.seal()
        self.source_account()
        self.flow.seal()
        self.state["sealed"] = False
        with self.assertRaises(Invalid):
            obligations.record(self.store, self.state, {**value, "id": "E02"})
        self.assertEqual(self.state["requirements"][0]["core"], True)
        self.assertEqual(self.state["requirements"][0]["scope"], "active")

    def test_no_late_creation_even_if_first_seal_had_no_execution(self):
        self.state["policy"] = {"executionObligations": True}
        self.prepare()
        value = {"id": "E01"}
        self.state["sealed"] = False
        self.state["spec"] = None
        self.state["tasks"] = []
        with self.assertRaises(Invalid):
            obligations.record(self.store, self.state, value)

    def test_source_reconciliation_cannot_silently_omit_deadline(self):
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        obligations.record(self.store, self.state, self.execution_input())
        report = self.source_account()
        report["rows"][1]["executionObligations"] = []
        with self.assertRaises(Invalid):
            preparation.record(self.store, self.state, "brief-map", report)
        row = self.state["executionObligations"][0]
        row["deadline"] = "2100-01-01T00:00:00+00:00"
        self.assertFalse(briefmap.valid(self.store, self.state, preparation.actor_check(self.store)))

    def test_engine_finish_pass_is_actual_event_not_blind_prediction(self):
        self.prepare_execution()
        self.completed()
        final = self.final_blind(execution=True)
        frozen = copy.deepcopy(self.state["blindReview"])
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(final)))
        self.assertEqual(self.state["gates"]["acceptance"]["executionFindings"][0]["status"], "not_tested")
        with patch("aispa_core.workflow.timestamp", return_value="2030-01-01T00:00:00+00:00"):
            self.flow.finish()
        self.assertEqual(self.state["status"], "accepted")
        self.assertEqual(self.state["remaining"]["execution"], [])
        self.assertEqual(self.state["executionResults"][0]["at"], self.state["finishedAt"])
        self.assertEqual(self.state["blindReview"], frozen)
        self.assertEqual(json.loads(self.store.text(frozen["report"]))["observations"][1]["status"], "not_tested")

    def test_deadline_fail_preserves_product_acceptance_with_remaining(self):
        self.prepare_execution(deadline="2001-01-01T00:00:00+00:00")
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual(self.state["remaining"]["execution"][0]["status"], "failed")
        self.assertTrue(self.state["gates"]["acceptance"]["passed"])

    def test_future_host_final_handoff_is_pending_never_engine_finish(self):
        self.prepare_execution(event="host-final-handoff")
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        pending = self.state["remaining"]["execution"][0]
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["at"])
        self.assertFalse(pending["deadlineElapsed"])
        self.assertFalse(obligations.view(self.store, self.state)["externalReceiptSupported"])

    def test_blind_mapping_requires_exact_presealed_execution_origin(self):
        self.prepare_execution(); self.completed()
        final = self.final_blind(execution=True)
        for key, value in (("executionRecord", "0" * 64), ("executionObligation", "E99"), ("requirement", "R01")):
            changed = copy.deepcopy(final)
            changed["blindMapping"][1][key] = value
            with self.subTest(key=key), self.assertRaises(Invalid):
                blind.mapping(self.store, self.state, changed)
        changed = copy.deepcopy(final)
        changed["blindMapping"][0] = dict(final["blindMapping"][1], observation=0)
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, changed)


class FinalWaveTests(Fixture):
    def test_single_task_final_blind_derives_sealed_checkpoint_and_source_changes_stale_it(self):
        self.prepare(); self.completed()
        self.state["policy"] = {"finalWaveReuse": True}
        final = self.final_blind()
        self.assertTrue(self.flow.acceptance(self.file(final)))
        wave = self.state["waves"]["1"]
        gate = self.state["gates"]["acceptance"]
        self.assertEqual(wave["derivedFrom"], gate["report"]["sha256"])
        self.assertNotEqual(wave["report"]["sha256"], gate["report"]["sha256"])
        self.assertTrue(blind.derived_waves_valid(self.store, self.state))
        self.assertTrue(gate_valid(self.store, self.state, "acceptance"))
        self.flow.finish()
        self.assertEqual(self.state["status"], "accepted")
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        self.assertFalse(blind.derived_waves_valid(self.store, self.state))

    def test_max_mode_does_not_reuse_the_final_blind_review(self):
        from aispa_core import check_modes
        self.prepare(); self.completed()
        self.state["policy"] = {"finalWaveReuse": True, "leanControl": True}
        check_modes.initialize(self.state, "max")
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(self.final_blind()))
        self.assertEqual(self.state.get("waves", {}), {})

    def test_exact_full_scope_required_and_task_acceptor_cannot_substitute(self):
        self.prepare(); self.completed()
        self.state["policy"] = {"finalWaveReuse": True}
        final = self.final_blind()
        missing = copy.deepcopy(final); missing["checks"] = []
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(missing))
        final["reviewer"] = self.attempt()["actors"]["acceptor"]
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(final))
        self.assertEqual(self.state.get("waves", {}), {})

    def test_multiple_tasks_and_intermediate_waves_cannot_reuse(self):
        self.prepare(secondary=True); self.completed("T01")
        self.flow.block("T02", "Explicit fixture missing optional task")
        self.state["policy"] = {"finalWaveReuse": True}
        final = self.final_blind()
        # No missing wave may be synthesized for any multi-task plan.
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(final))
        self.assertEqual(self.state.get("waves", {}), {})
        self.state["tasks"][1]["wave"] = 2
        self.assertFalse(blind.derived_waves_valid(self.store, {**self.state, "waves": {
            "1": {"derivedFrom": "made-up"}}}))

    def test_legacy_absent_policy_still_requires_wave(self):
        self.prepare(); self.completed()
        final = self.final_blind()
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(final))


class PhaseReceiptTests(Fixture):
    def test_one_actual_coordinator_authors_multiple_phases_usage_once(self):
        self.state["policy"] = {"phaseReceipts": True}
        actor = self.actor("coordinator")
        # These fixture reports isolate immutable authorship from semantic report validation.
        for phase, key in (("idea", "ideaReview"), ("brief-analyst", "briefExtraction"), ("discovery", "discovery")):
            ref = self.store.artifact("fixture-reports", json.dumps({"phase": phase}).encode(), ".json")
            if phase == "discovery":
                ref["basis"] = "source-bound-discovery-fixture"
            self.state[key] = ref
            self.assertEqual(phase_receipts.authorize(self.store, self.state, phase), actor)
            phase_receipts.record(self.store, self.state, phase, ref, actor=actor)
            self.assertTrue(phase_receipts.valid(self.store, self.state, phase))
        telemetry.record(self.store, self.state, {"actor": actor, "callId": "one", "source": "native fixture",
            "inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 3})
        result = telemetry.summary(self.store, self.state)
        self.assertEqual(result["inputTokens"], 10)
        self.assertEqual(result["measuredCalls"], 1)
        self.assertEqual(set(result["byRole"]), {"coordinator"})
        self.assertEqual(set(result["coverage"]["phaseAuthors"].values()), {actor})
        self.assertNotIn("idea", result["coverage"]["missingRoles"])
        self.assertNotIn("brief-analyst", result["coverage"]["missingRoles"])
        self.assertEqual(len(participants.list_participants(self.store, self.state)), 1)

    def test_independent_roles_cannot_be_coordinator_aliases_or_duplicate_identity(self):
        self.state["policy"] = {"phaseReceipts": True}
        actor = self.actor("coordinator")
        for phase in ("coverage", "brief-reviewer", "brief-map", "blind", "acceptance"):
            with self.subTest(phase=phase), self.assertRaises(Invalid):
                phase_receipts.authorize(self.store, self.state, phase, actor)
        for role in participants.INDEPENDENT_ROLES:
            self.assertFalse(participants.actor_check(self.store, self.state, actor, role))
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "coverage", "codex", "coordinator", "new-session")

    def test_ambiguous_default_missing_and_stale_receipts_are_explicit(self):
        self.state["policy"] = {"phaseReceipts": True}
        coordinator = self.actor("coordinator")
        self.actor("idea")
        with self.assertRaises(Invalid):
            phase_receipts.authorize(self.store, self.state, "idea")
        ref = self.store.artifact("fixture-reports", b"{}", ".json")
        self.state["ideaReview"] = ref
        self.assertIn("idea", telemetry.summary(self.store, self.state)["coverage"]["missingPhases"])
        phase_receipts.record(self.store, self.state, "idea", ref, actor=coordinator)
        self.assertIn("idea", telemetry.summary(self.store, self.state)["coverage"]["unattributedPhases"])
        self.state["requirements"].append({"id": "R99"})
        self.assertFalse(phase_receipts.valid(self.store, self.state, "idea"))
        self.assertIn("idea", telemetry.summary(self.store, self.state)["coverage"]["missingPhases"])

    def test_current_coverage_uses_gates_coverage_and_validates_actor_before_mutation(self):
        self.prepare()
        self.state["policy"] = {"phaseReceipts": True}
        coordinator = self.actor("coordinator")
        coverage = self.actor("coverage")
        report = self.header("coverage")
        report["checks"] = [{"requirement": "R01", "status": "covered", "specQuote": "Команда выводит ready"}]
        before = copy.deepcopy(self.state)
        report["reviewer"] = coordinator
        with self.assertRaises(Invalid):
            self.flow.coverage(self.file(report))
        self.assertEqual(self.state, before)
        result = telemetry.summary(self.store, self.state)
        self.assertIn("coverage", result["coverage"]["missingPhases"])
        report["reviewer"] = coverage
        self.flow.coverage(self.file(report))
        self.assertTrue(phase_receipts.valid(self.store, self.state, "coverage"))
        self.assertEqual(telemetry.summary(self.store, self.state)["coverage"]["phaseAuthors"]["coverage"], coverage)
        self.state["phaseReceipts"][-1]["actor"] = coordinator
        self.assertFalse(phase_receipts.valid(self.store, self.state, "coverage"))

    def test_combined_real_actor_receipts_finish_without_duplicate_final_wave(self):
        self.state["policy"] = {"phaseReceipts": True, "executionObligations": True,
                                "sourceAccounting": True, "finalWaveReuse": True}
        coordinator, reviewer = self.actor("coordinator"), self.actor("brief-reviewer")
        coverage, final_actor = self.actor("coverage"), self.actor("blind")
        self.flow.brief(self.file("Команда выводит ready.", "brief.md"))
        self.flow.requirements(self.file([{"id": "R01", "text": "Команда", "brief": "B001",
            "quote": "Команда выводит ready.", "core": True, "acceptance": ["Выводит ready"]}]))
        context = preparation.source_context(self.store, self.state)
        unit = context["briefs"][0]["units"][0]
        report = {"basis": context["basis"], "actor": coordinator, "units": [
            {**unit, "kind": "intent", "summary": "Original output request", "reason": ""}]}
        preparation.record(self.store, self.state, "brief-extract", report)
        preparation.record(self.store, self.state, "brief-review", {**report, "actor": reviewer})
        preparation.record(self.store, self.state, "brief-map", {
            "basis": briefmap.reconciliation_basis(self.state), "actor": reviewer,
            "rows": [{k: unit[k] for k in ("brief", "start", "end")} | {
                "disposition": "requirement", "requirements": ["R01"], "reason": "Whole source preserved"}]})
        self.flow.seal()
        self.flow.spec(self.file("Команда выводит ready.", "spec.md"))
        report = self.header("coverage")
        report.update(reviewer=coverage, checks=[{"requirement": "R01", "status": "covered",
                                                 "specQuote": "Команда выводит ready"}])
        self.flow.coverage(self.file(report))
        self.flow.plan(self.file([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]), actor=coordinator)
        self.completed()
        self.state["policy"]["cleanClone"] = True
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        blind.record(self.store, self.state, {"basis": blind.basis(self.store, self.state),
            "reviewer": final_actor, "independent": True, "observations": [
                {"brief": "B001", "quote": "Команда выводит ready.", "status": "passed",
                 "note": "Read source and observed stdout", "artifact": self.state["cleanrun"]["output"]["file"]}]})
        final = self.assessment()
        final.update(reviewer=final_actor, cleanrun=self.state["cleanrun"]["artifact"]["sha256"],
                     blindReport=self.state["blindReview"]["report"]["sha256"],
                     blindMapping=[{"observation": 0, "requirement": "R01"}])
        self.assertTrue(self.flow.acceptance(self.file(final)))
        self.assertTrue(gate_valid(self.store, self.state, "acceptance"))
        self.assertEqual(phase_receipts.summary(self.store, self.state)["missingPhases"], [])
        # Re-recording an actual final review updates its own derived provenance.
        final["checks"][0]["note"] = "Same observed output, clarified final report"
        self.assertTrue(self.flow.acceptance(self.file(final)))
        self.assertTrue(gate_valid(self.store, self.state, "acceptance"))
        self.flow.finish()
        self.assertEqual(self.state["status"], "accepted")

    def test_source_reports_support_coordinator_without_fabricated_analyst(self):
        self.state["policy"] = {"phaseReceipts": True, "sourceAccounting": True}
        coordinator, reviewer = self.actor("coordinator"), self.actor("brief-reviewer")
        self.flow.brief(self.file("Команда выводит ready.", "brief.md"))
        self.flow.requirements(self.file([{"id": "R01", "text": "Команда", "brief": "B001",
            "quote": "Команда выводит ready.", "core": True, "acceptance": ["Выводит ready"]}]))
        context = preparation.source_context(self.store, self.state)
        report = {"basis": context["basis"], "actor": coordinator, "units": [
            {**unit, "kind": "intent", "summary": "Read original output request", "reason": ""}
            for unit in context["briefs"][0]["units"]]}
        preparation.record(self.store, self.state, "brief-extract", report)
        report["actor"] = reviewer
        preparation.record(self.store, self.state, "brief-review", report)
        unit = context["briefs"][0]["units"][0]
        mapping = {"basis": briefmap.reconciliation_basis(self.state), "actor": reviewer,
                   "rows": [{k: unit[k] for k in ("brief", "start", "end")} | {
                       "disposition": "requirement", "requirements": ["R01"], "reason": "Full original quote preserved"}]}
        preparation.record(self.store, self.state, "brief-map", mapping)
        self.flow.seal()
        self.assertEqual(len(participants.list_participants(self.store, self.state)), 2)
        self.assertTrue(briefmap.valid(self.store, self.state, preparation.actor_check(self.store)))
        self.state["phaseReceipts"].pop()
        self.assertFalse(briefmap.valid(self.store, self.state, preparation.actor_check(self.store)))
