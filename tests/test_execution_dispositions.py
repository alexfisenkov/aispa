"""Owner waivers close historical execution debt without inventing event PASS."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import carryover, execution_obligations as execution
from aispa_core.store import Invalid, digest, timestamp
from aispa_core.workflow import Workflow
from test_adaptive_acceptance import Fixture


class ExecutionDispositionTests(Fixture):
    def inherited(self, *, actual_finish=False, event="host-final-handoff", deadline="2099-01-01T00:00:00+00:00"):
        if actual_finish:
            self.prepare_execution(event, deadline)
            self.completed()
            self.flow.wave(1, self.file(self.assessment("wave")))
            self.flow.acceptance(self.file(self.assessment()))
            self.flow.finish()
        else:
            self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
            execution.record(self.store, self.state, self.execution_input(event))
            self.source_account(); self.flow.seal()
            self.state["status"] = "cancelled"
            self.store.append(self.state, "cancelled-before-finish")
        payload = carryover.capture(self.store, self.state)
        self.original = copy.deepcopy(payload["executionRemainders"][0])
        self.original_brief = self.store.text(self.state["briefs"][0])
        self.state = self.store.create("new-owner", "New owner decision")
        carryover.attach(self.store, self.state, payload)
        self.store.append(self.state, "handoff-attached")
        self.flow = Workflow(self.store, self.state)
        return self.original["id"]

    def owner(self, text):
        ref = self.flow.brief(self.file(text, "owner.md"))
        return ref["id"]

    def test_drop_requires_new_owner_source_and_never_changes_pending_outcome(self):
        item = self.inherited()
        text = "Owner: waive the inherited host-final-handoff deadline for " + item + "."
        brief = self.owner(text)
        result = carryover.decide(self.store, self.state, item, "drop", brief=brief, quote=text)
        self.store.append(self.state, "owner-execution-waiver")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["disposition"], "dropped")
        self.assertTrue(result["timingOutcomeUnchanged"])
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])
        view = carryover.execution_view(self.store, self.state)
        self.assertEqual(view["ownerDropped"], 1)
        self.assertEqual(view["items"][0]["status"], "pending")
        self.assertFalse(view["items"][0]["currentProof"])
        self.assertEqual(view["items"][0]["ownerDecision"]["binding"], self.original["origin"])
        self.assertEqual(self.state["requirements"], [])
        for action in ("keep", "map", "resolve"):
            with self.assertRaises(Invalid):
                carryover.decide(self.store, self.state, item, action)

    def test_owner_waiver_preserves_failed_engine_timing_as_failure(self):
        item = self.inherited(actual_finish=True, event="engine-finish", deadline="2001-01-01T00:00:00+00:00")
        quote = "Owner waives the missed historical engine deadline for " + item + "."
        bid = self.owner(quote)
        result = carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])
        row = carryover.execution_view(self.store, self.state)["items"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["deadline"], "2001-01-01T00:00:00+00:00")
        receipt = json.loads(self.store.text(result["receipt"]))
        self.assertEqual(receipt["after"]["result"], self.original["result"])

    def test_keep_and_defer_stay_visible_until_separately_authorized_drop(self):
        item = self.inherited()
        carryover.decide(self.store, self.state, item, "keep")
        self.assertEqual(carryover.unresolved_execution(self.store, self.state)[0]["disposition"], "outstanding")
        quote = "Owner: defer the inherited deadline decision for " + item + "."
        bid = self.owner(quote)
        carryover.decide(self.store, self.state, item, "defer", brief=bid, quote=quote)
        carryover.decide(self.store, self.state, item, "keep")
        self.assertEqual(carryover.unresolved_execution(self.store, self.state)[0]["disposition"], "deferred")
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        quote = "Owner now waives the old deadline for " + item + "."
        bid = self.owner(quote)
        carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])

    def test_one_new_owner_brief_can_waive_two_distinct_execution_items(self):
        first_item = self.inherited()
        self.state["policy"] = {"executionObligations": True, "sourceAccounting": True}
        execution.record(self.store, self.state, self.execution_input("host-final-handoff"))
        self.source_account(); self.flow.seal()
        self.state["status"] = "cancelled"
        self.store.append(self.state, "second-execution-origin")
        payload = carryover.capture(self.store, self.state)
        self.assertEqual(len(payload["executionRemainders"]), 2)
        self.state = self.store.create("two-executions", "One owner decision for two debts")
        carryover.attach(self.store, self.state, payload)
        self.store.append(self.state, "two-historical-executions")
        self.flow = Workflow(self.store, self.state)
        items = [row["id"] for row in carryover.unresolved_execution(self.store, self.state)]
        self.assertIn(first_item, items)
        quote = "Owner waives both historical execution debts: " + " and ".join(items) + "."
        brief = self.owner(quote)
        self.assertEqual(brief, "B001")
        for item in items:
            result = carryover.decide(self.store, self.state, item, "drop", brief=brief, quote=quote)
            self.assertEqual(result["disposition"], "dropped")
            self.assertEqual(result["status"], "pending")
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])
        self.assertEqual(carryover.execution_view(self.store, self.state)["ownerDropped"], 2)

    def test_higher_brief_id_with_identical_bytes_cannot_change_defer_to_drop(self):
        item = self.inherited()
        quote = "Owner: defer the inherited deadline for " + item + "."
        first = self.owner(quote)
        carryover.decide(self.store, self.state, item, "defer", brief=first, quote=quote)
        second = self.owner(quote)
        self.assertNotEqual(first, second)
        before = copy.deepcopy(self.state["carryover"])
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item, "drop", brief=second, quote=quote)
        self.assertEqual(self.state["carryover"], before)
        self.assertEqual(carryover.unresolved_execution(self.store, self.state)[0]["disposition"], "deferred")
        genuine = "Owner now explicitly waives the historical deadline for " + item + "."
        third = self.owner(genuine)
        carryover.decide(self.store, self.state, item, "drop", brief=third, quote=genuine)
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])

    def test_recomputed_receipt_and_bound_source_cannot_replay_identical_owner_words(self):
        from aispa_core import execution_history
        item = self.inherited()
        quote = "Owner: defer the inherited deadline for " + item + "."
        first = self.owner(quote)
        carryover.decide(self.store, self.state, item, "defer", brief=first, quote=quote)
        second = self.owner(quote)
        ledger = carryover._ledger(self.store, self.state)
        before = next(row for row in ledger["executionRemainders"] if row["id"] == item)
        ref = next(row for row in self.state["briefs"] if row["id"] == second)
        source = carryover._source(self.store, self.state, ref, "owner_brief")
        after = copy.deepcopy(before)
        after["dispositions"].append({"run": self.state["run"], "action": "drop", "at": timestamp(),
            "binding": copy.deepcopy(before["origin"]), "brief": second, "quote": quote, "source": source["id"]})
        receipt = {"schema": 1, "run": self.state["run"], "item": item, "action": "drop",
                   "before": digest(before), "after": after, "sources": [source]}
        ref = self.store.artifact("carryover-decisions", json.dumps(receipt).encode(), ".json")
        self.state["carryover"]["decisions"].append(ref)
        with self.assertRaises(Invalid):
            carryover.unresolved_execution(self.store, self.state)
        sources = {row["id"]: row for row in ledger["sources"]}
        sources[source["id"]] = source
        with self.assertRaises(Invalid):
            execution_history.validate([after], sources)
        self.state["carryover"]["decisions"].pop()
        self.assertEqual(carryover.unresolved_execution(self.store, self.state)[0]["disposition"], "deferred")

    def test_no_quote_wrong_quote_wrong_brief_and_copied_old_source_are_denied(self):
        item = self.inherited()
        original = copy.deepcopy(self.state)
        for action in ("drop", "defer", "map", "resolve"):
            with self.subTest(action=action), self.assertRaises(Invalid):
                carryover.decide(self.store, self.state, item, action)
        self.assertEqual(self.state, original)
        old = self.owner(self.original_brief)
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item, "drop", brief=old, quote=self.original_brief)
        text = "Owner waives only the inherited deadline " + item + "."
        new = self.owner(text)
        for bid, quote in (("B999", text), (new, "Changed quote"), (new, ""), (new, None)):
            with self.subTest(bid=bid, quote=quote), self.assertRaises(Invalid):
                carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        self.assertEqual(len(carryover.unresolved_execution(self.store, self.state)), 1)

    def test_owner_disposition_survives_later_runs_with_original_record_and_result(self):
        item = self.inherited()
        quote = "Owner removes the old execution deadline debt for " + item + "."
        bid = self.owner(quote)
        carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        self.state["status"] = "cancelled"; self.store.append(self.state, "finished-owner-decision")
        payload = carryover.capture(self.store, self.state)
        saved = payload["executionRemainders"][0]
        self.assertEqual({k: v for k, v in saved.items() if k != "dispositions"}, self.original)
        self.assertEqual(saved["dispositions"][0]["quote"], quote)
        self.state = self.store.create("later", "Later unrelated run")
        carryover.attach(self.store, self.state, payload)
        self.store.append(self.state, "inherited-owner-disposition")
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])
        self.assertEqual(carryover.execution_view(self.store, self.state)["items"][0]["status"], "pending")
        self.assertEqual(carryover.view(self.store, self.state)["summary"]["executionOutstanding"], 0)

    def test_forged_receipt_cannot_change_binding_status_quote_or_history(self):
        item = self.inherited()
        quote = "Owner waives the inherited deadline for " + item + "."
        bid = self.owner(quote)
        result = carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        original_ref = result["receipt"]
        saved = json.loads(self.store.text(original_ref))
        changes = (
            lambda r: r["after"].update(status="passed"),
            lambda r: r["after"]["dispositions"][-1]["binding"].update(recordSha256="0" * 64),
            lambda r: r["after"]["dispositions"][-1].update(quote="Forged owner wording"),
            lambda r: r["after"]["dispositions"][-1].update(action="resolve"),
            lambda r: r["after"].update(dispositions=[]),
        )
        for change in changes:
            altered = copy.deepcopy(saved); change(altered)
            ref = self.store.artifact("carryover-decisions", json.dumps(altered).encode(), ".json")
            self.state["carryover"]["decisions"][-1] = ref
            with self.assertRaises(Invalid):
                carryover.unresolved_execution(self.store, self.state)
        self.state["carryover"]["decisions"][-1] = original_ref
        self.assertEqual(carryover.unresolved_execution(self.store, self.state), [])

    def test_owner_waiver_allows_later_delivery_without_perpetual_remaining(self):
        item = self.inherited(actual_finish=True)
        quote = "Owner waives the inherited host deadline for " + item + "."
        bid = self.owner("Команда выводит ready. " + quote)
        carryover.decide(self.store, self.state, item, "drop", brief=bid, quote=quote)
        self.store.append(self.state, "owner-waiver-recorded")
        self.flow.requirements(self.file([{"id": "R01", "text": "Команда готовности", "brief": bid,
            "quote": "Команда выводит ready", "core": True, "acceptance": ["Выводит ready"]}]))
        self.flow.seal(); self.flow.spec(self.file("Команда выводит ready.", "spec.md")); self.coverage()
        self.flow.plan(self.file([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]))
        self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.flow.acceptance(self.file(self.assessment()))
        self.flow.finish()
        self.assertEqual(self.state["status"], "accepted")
        self.assertEqual(self.state["remaining"].get("inheritedExecution", []), [])
        self.assertEqual(carryover.execution_view(self.store, self.state)["items"][0]["status"], "pending")
        self.assertEqual(carryover.execution_view(self.store, self.state)["items"][0]["disposition"], "dropped")
