"""Historical execution provenance is retained separately from product carryover."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import carryover, execution_obligations as execution
from aispa_core.store import Invalid, digest
from test_adaptive_acceptance import Fixture


class ExecutionCarryoverTests(Fixture):
    def test_finish_and_projection_keep_inherited_execution_visible(self):
        from aispa_core.workflow import Workflow
        from aispa_core.display import project
        self.prepared()
        payload = self.close_capture()
        self.attach_next(payload)
        self.flow = Workflow(self.store, self.state)
        self.prepare()
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual(len(self.state["remaining"]["inheritedExecution"]), 1)
        view = project(self.store, self.state)
        self.assertEqual(view["status"], "delivered_with_remaining")
        self.assertEqual(view["remaining"]["inheritedExecution"][0]["status"], "pending")

    def prepared(self, event="host-final-handoff", deadline="2099-01-01T00:00:00+00:00", sealed=True):
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        execution.record(self.store, self.state, self.execution_input(event, deadline))
        if sealed:
            self.source_account()
            self.flow.seal()

    def close_capture(self, finish_at=None):
        if finish_at is not None:
            results = execution.evaluate(self.store, self.state, finish_at)
            receipt = {"run": self.state["run"], "event": "engine-finish", "at": finish_at, "results": results}
            self.state["executionResults"] = results
            self.state["executionResultHistory"] = [self.store.artifact("execution-results", json.dumps(receipt).encode(), ".json")]
        self.state["status"] = "cancelled" if finish_at is None else "delivered_with_remaining"
        self.store.append(self.state, "synthetic-execution-checkpoint")
        return carryover.capture(self.store, self.state)

    def attach_next(self, payload):
        self.state = self.store.create("next", "Next run")
        carryover.attach(self.store, self.state, payload)
        self.store.append(self.state, "execution-history-attached")
        return carryover.unresolved_execution(self.store, self.state)

    def test_pending_external_event_survives_three_runs_without_scope_or_pass(self):
        self.prepared()
        original = self.state["run"]
        record = copy.deepcopy(self.state["executionObligations"][0]["record"])
        payload = self.close_capture("2030-01-01T00:00:00+00:00")
        archived = copy.deepcopy(payload["executionRemainders"][0])
        sources = {s["id"]: s for s in payload["sources"]}
        self.assertEqual(sources[archived["obligationSource"]]["origin"], {"run": original, **record})
        self.assertEqual(sources[archived["source"]["source"]]["kind"], "owner_brief")
        self.assertIsNone(archived["result"]["at"])
        self.assertTrue({"execution_source_extraction", "execution_source_review"} <=
                        {source["kind"] for source in sources.values()})
        first = self.attach_next(payload)
        self.assertEqual(first[0]["originRun"], original)
        self.assertEqual(first[0]["sourceRecordSHA"], record["sha256"])
        self.assertEqual(first[0]["status"], "pending")
        self.assertTrue(first[0]["historical"])
        self.assertFalse(first[0]["currentProof"])
        self.assertEqual(self.state["requirements"], [])
        self.assertNotIn("executionObligations", self.state)
        self.assertEqual(self.state["gates"], {})
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, first[0]["id"], "resolve")
        second_payload = self.close_capture()
        self.assertEqual(second_payload["executionRemainders"], [archived])
        second = self.attach_next(second_payload)
        self.assertEqual(second, first)
        self.assertEqual(carryover.view(self.store, self.state)["summary"]["executionOutstanding"], 1)

    def test_missed_engine_deadline_stays_historical_failure(self):
        self.prepared("engine-finish", "2001-01-01T00:00:00+00:00")
        row = self.attach_next(self.close_capture("2030-01-01T00:00:00+00:00"))[0]
        self.assertEqual(row["event"], "engine-finish")
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["currentProof"])
        self.assertEqual(carryover.execution_view(self.store, self.state)["total"], 1)

    def test_passed_engine_deadline_never_transfers_as_scope_or_current_proof(self):
        self.prepared("engine-finish")
        payload = self.close_capture("2030-01-01T00:00:00+00:00")
        self.assertNotIn("executionRemainders", payload)
        self.assertEqual(self.attach_next(payload), [])
        self.assertNotIn("executionResults", self.state)
        self.assertNotIn("executionOutstanding", carryover.view(self.store, self.state)["summary"])

    def test_no_finish_even_after_expired_deadline_stays_unknown_not_fabricated_failure(self):
        self.prepared("engine-finish", "2001-01-01T00:00:00+00:00")
        payload = self.close_capture()
        row = payload["executionRemainders"][0]
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["result"])
        self.assertIsNone(row["resultSource"])
        self.assertIsNotNone(row["sealSource"])
        self.assertIn("No engine finish", self.attach_next(payload)[0]["note"])

    def test_cancelled_before_seal_keeps_declared_origin_and_unknown_status(self):
        self.prepared(sealed=False)
        payload = self.close_capture()
        self.assertIsNone(payload["executionRemainders"][0]["sealSource"])
        self.assertFalse(self.attach_next(payload)[0]["sealed"])

    def test_bounded_view_retains_full_hash_bound_archive(self):
        self.prepared()
        payload = self.close_capture()
        self.attach_next(payload)
        view = carryover.execution_view(self.store, self.state, limit=0)
        self.assertEqual(view["items"], [])
        self.assertEqual(view["omitted"], 1)
        self.assertEqual(view["total"], 1)
        archived = json.loads(self.store.text(view["archive"]))
        self.assertEqual(archived["executionRemainders"], payload["executionRemainders"])
        self.assertFalse(view["externalReceiptSupported"])
        for limit in (-1, 101, True, "1"):
            with self.subTest(limit=limit), self.assertRaises(Invalid):
                carryover.execution_view(self.store, self.state, limit)

    def test_result_requires_real_immutable_receipt_and_matching_deadline(self):
        self.prepared()
        self.state["executionResults"] = [{"id": "E01", "status": "passed"}]
        with self.assertRaises(Invalid):
            self.close_capture()
        self.state.pop("executionResults")
        payload = self.close_capture("2030-01-01T00:00:00+00:00")
        ref = self.state["executionResultHistory"][0]
        (self.store.run / ref["file"]).write_text("{}", encoding="utf-8")
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)
        self.assertEqual(payload["executionRemainders"][0]["status"], "pending")

    def test_payload_cannot_omit_or_forge_execution_even_with_recomputed_digest(self):
        self.prepared()
        payload = self.close_capture()
        self.state = self.store.create("new", "New run")
        for mutate in (lambda p: p.pop("executionRemainders"),
                       lambda p: p["executionRemainders"][0].update(status="passed"),
                       lambda p: p["executionRemainders"][0].update(currentProof=True),
                       lambda p: p["executionRemainders"][0].update(status=[]),
                       lambda p: p["executionRemainders"][0].update(source={"source": [], "quote": "x"})):
            changed = copy.deepcopy(payload)
            mutate(changed)
            changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
            with self.assertRaises(Invalid):
                carryover.attach(self.store, self.state, changed)
        self.assertNotIn("carryover", self.state)

    def test_linked_execution_source_is_not_followed(self):
        self.prepared()
        ref = self.state["executionObligations"][0]["startEvidence"]
        path = self.store.run / ref["file"]
        outside = self.root / "outside-source.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit symlink creation")
        with self.assertRaises(Invalid):
            self.close_capture()

    def test_corrupt_embedded_original_source_invalidates_historical_view(self):
        self.prepared()
        self.attach_next(self.close_capture())
        handoff = self.state["carryover"]["handoff"]
        path = self.store.run / handoff["file"]
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(Invalid):
            carryover.unresolved_execution(self.store, self.state)


class HistoricalTimingShapeTests(Fixture):
    def test_unhashable_historical_event_is_domain_error(self):
        from aispa_core import execution_history
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        execution.record(self.store, self.state, self.execution_input("host-final-handoff"))
        self.source_account(); self.flow.seal()
        self.state["status"] = "cancelled"
        self.store.append(self.state, "synthetic-history")
        payload = carryover.capture(self.store, self.state)
        for event in ([], {}):
            changed = copy.deepcopy(payload)
            row = changed["executionRemainders"][0]
            source = next(s for s in changed["sources"] if s["id"] == row["obligationSource"])
            value = json.loads(source["text"]); value["event"] = event
            source["text"] = json.dumps(value)
            import hashlib
            source["origin"]["sha256"] = hashlib.sha256(source["text"].encode()).hexdigest()
            source["id"] = digest(source["origin"])
            row["obligationSource"] = source["id"]
            row["origin"]["recordSha256"] = source["origin"]["sha256"]
            row["id"] = "CE-" + digest(row["origin"])[:24]
            with self.subTest(event=event), self.assertRaises(Invalid):
                execution_history.validate(changed["executionRemainders"], {s["id"]: s for s in changed["sources"]})
