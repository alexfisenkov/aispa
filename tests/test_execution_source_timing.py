"""Independent source timing, not the ledger author's self-classification."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import briefmap, execution_obligations as execution, preparation
from aispa_core.store import Invalid
from test_adaptive_acceptance import Fixture


class ExecutionSourceTimingTests(Fixture):
    def source_case(self, *, quote="Send the final reply within ten minutes.",
                    event="host-final-handoff", deadline="2000-01-01T00:10:00+00:00",
                    left=None, right=None):
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        self.flow.brief(self.file("Команда выводит ready.\n" + quote, "brief.md"))
        self.flow.requirements(self.file([{"id": "R01", "text": "Команда", "brief": "B001",
            "quote": "Команда выводит ready.", "core": True, "acceptance": ["Выводит ready"]}]))
        before = preparation.source_context(self.store, self.state)
        start = "2000-01-01T00:00:00+00:00"
        evidence = self.store.artifact("evidence", json.dumps({"event": "execution-start", "at": start,
            "note": "Synthetic observed execution start"}).encode(), ".json")
        execution.record(self.store, self.state, {"id": "E01", "kind": "deadline", "event": event,
            "brief": "B001", "quote": quote, "startedAt": start, "startEvidence": evidence, "deadline": deadline})
        # Original-only input never reveals the coordinator's classification.
        self.assertEqual(preparation.source_context(self.store, self.state), before)
        actors = self.actor("brief-analyst"), self.actor("brief-reviewer")
        for actor, annotation, command in zip(actors, (left, right), ("brief-extract", "brief-review")):
            units = [{**unit, "kind": "intent", "summary": "Independent original-source interpretation", "reason": ""}
                     for unit in before["briefs"][0]["units"]]
            if annotation is not None:
                units[1]["execution"] = annotation
            preparation.record(self.store, self.state, command, {"basis": before["basis"], "actor": actor, "units": units})
        rows = []
        for index, unit in enumerate(before["briefs"][0]["units"]):
            row = {k: unit[k] for k in ("brief", "start", "end")}
            row.update(disposition="requirement" if index == 0 else "execution",
                       requirements=["R01"] if index == 0 else [], reason="Retain original source intention")
            if index:
                row["executionObligations"] = ["E01"]
            rows.append(row)
        preparation.record(self.store, self.state, "brief-map", {
            "basis": briefmap.reconciliation_basis(self.state), "actor": actors[1], "rows": rows})

    def assert_cannot_finish(self):
        with self.assertRaises(Invalid):
            self.flow.seal()
        with self.assertRaises(Invalid):
            self.flow.finish()
        self.assertNotEqual(self.state["status"], "accepted")
        self.assertNotIn("executionResults", self.state)

    def test_wrong_2099_deadline_and_engine_substitution_fail_at_record_without_pass(self):
        self.state["policy"] = {"executionObligations": True}
        value = self.execution_input("host-final-handoff", "2026-10-21T12:00:00+00:00")
        for change in ({"deadline": "2099-01-01T00:00:00+00:00"}, {"event": "engine-finish"}):
            with self.subTest(change=change), self.assertRaises(Invalid):
                execution.record(self.store, self.state, {**value, **change})
        self.assertNotIn("executionObligations", self.state)
        with self.assertRaises(Invalid):
            self.flow.finish()
        self.assertEqual(self.state["status"], "running")

    def test_missing_both_independent_annotations_blocks_seal_and_finish(self):
        with self.assertRaises(Invalid):
            self.source_case()
        self.assert_cannot_finish()

    def test_one_sided_annotation_blocks_seal_and_finish(self):
        with self.assertRaises(Invalid):
            self.source_case(left={"event": "host-final-handoff", "budgetSeconds": 600})
        self.assert_cannot_finish()

    def test_reviewer_disagreement_blocks_seal_and_finish(self):
        with self.assertRaises(Invalid):
            self.source_case(left={"event": "host-final-handoff", "budgetSeconds": 600},
                             right={"event": "engine-finish", "budgetSeconds": 600})
        self.assert_cannot_finish()

    def test_general_language_event_cannot_be_reclassified_by_ledger_author(self):
        interpretation = {"event": "host-final-handoff", "budgetSeconds": 600}
        with self.assertRaises(Invalid):
            self.source_case(event="engine-finish", left=interpretation, right=interpretation)
        self.assert_cannot_finish()

    def test_duration_mismatch_cannot_move_original_deadline(self):
        interpretation = {"event": "host-final-handoff", "budgetSeconds": 600}
        with self.assertRaises(Invalid):
            self.source_case(deadline="2099-01-01T00:00:00+00:00", left=interpretation, right=interpretation)
        self.assert_cannot_finish()

    def test_absolute_source_interpretation_blocks_unquoted_later_ledger_deadline(self):
        interpretation = {"event": "host-final-handoff", "deadline": "2026-10-21T12:00:00+00:00"}
        with self.assertRaises(Invalid):
            self.source_case(quote="Send the final reply by noon tomorrow.",
                             deadline="2099-01-01T00:00:00+00:00", left=interpretation, right=interpretation)
        self.assert_cannot_finish()

    def test_matching_duration_keeps_host_event_pending_after_full_finish(self):
        interpretation = {"event": "host-final-handoff", "budgetSeconds": 600}
        self.source_case(left=interpretation, right=interpretation)
        self.flow.seal()
        seal = json.loads(self.store.text(self.state["executionSeal"]))
        self.assertEqual(seal["sourceReports"]["review"], self.state["briefReview"])
        self.flow.spec(self.file("Команда выводит ready.", "spec.md")); self.coverage()
        self.flow.plan(self.file([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]))
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual(self.state["remaining"]["execution"][0]["status"], "pending")
        self.assertEqual(self.state["remaining"]["execution"][0]["event"], "host-final-handoff")

    def test_explicit_iso_normalization_and_literal_guards(self):
        quote = "host-final-handoff by 2026-10-21T12:00:00Z."
        normalized = execution.annotation({"event": "host-final-handoff", "deadline": "2026-10-21T14:00:00+02:00"}, quote)
        self.assertEqual(execution.instant(normalized["deadline"]), execution.instant("2026-10-21T12:00:00+00:00"))
        for value in ({"event": "engine-finish", "deadline": "2026-10-21T12:00:00Z"},
                      {"event": "host-final-handoff", "deadline": "2099-01-01T00:00:00Z"}):
            with self.assertRaises(Invalid):
                execution.annotation(value, quote)
        execution.literal_timing("host-final-handoff by 2026-10-21T12", "host-final-handoff", "2026-10-21T12:00:00+00:00")
        with self.assertRaises(Invalid):
            execution.literal_timing("host-final-handoff by 2026-10-21T12", "host-final-handoff", "2099-10-21T12:00:00+00:00")

    def test_relative_budget_with_literal_start_seals_and_survives_history(self):
        from aispa_core import carryover
        interpretation = {"event": "host-final-handoff", "budgetSeconds": 600}
        self.source_case(quote="Starting at 2000-01-01T00:00:00Z send the final reply within ten minutes.",
                         left=interpretation, right=interpretation)
        self.flow.seal()
        self.state["status"] = "cancelled"
        self.store.append(self.state, "synthetic-relative-start-cancelled")
        payload = carryover.capture(self.store, self.state)
        next_state = self.store.create("next", "Next run")
        carryover.attach(self.store, next_state, payload)
        self.assertEqual(carryover.unresolved_execution(self.store, next_state)[0]["status"], "pending")

    def test_literal_start_does_not_bypass_absolute_annotation_or_duration(self):
        quote = "Starting at 2000-01-01T00:00:00Z send the final reply within ten minutes."
        relative = {"event": "host-final-handoff", "budgetSeconds": 600}
        with self.assertRaises(Invalid):
            self.source_case(quote=quote, deadline="2099-01-01T00:00:00Z", left=relative, right=relative)
        with self.assertRaises(Invalid):
            execution.annotation({"event": "host-final-handoff", "deadline": "2099-01-01T00:00:00Z"}, quote)

    def test_unhashable_event_shapes_raise_domain_error_before_state_change(self):
        self.state["policy"] = {"executionObligations": True}
        value = self.execution_input()
        before = copy.deepcopy(self.state)
        for event in ([], {}, None, True):
            with self.subTest(event=event), self.assertRaises(Invalid):
                execution.record(self.store, self.state, {**value, "event": event})
            with self.assertRaises(Invalid):
                execution.annotation({"event": event, "budgetSeconds": 600}, value["quote"])
        self.assertEqual(self.state, before)

    def test_source_report_receipt_mutation_prevents_existing_product_gate_from_finishing(self):
        self.prepare_execution("host-final-handoff", "2099-01-01T00:00:00+00:00")
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))
        # Even after accepted product checks the original independent source
        # interpretation remains required at finish, not the author's latest claim.
        ref = self.state["briefReview"]
        original = self.store.text(ref)
        value = json.loads(original)
        value["report"]["units"][1]["execution"]["event"] = "engine-finish"
        (self.store.run / ref["file"]).write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(Invalid):
            self.flow.finish()
        self.assertNotEqual(self.state["status"], "accepted")
        self.assertNotIn("executionResults", self.state)
