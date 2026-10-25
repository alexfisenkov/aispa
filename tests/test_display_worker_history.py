"""Real light/standard/fast receipts keep dates independently of result freshness."""
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import check_modes
from aispa_core.display import project
from aispa_core.team import direct_proof_current, task_proof_valid
import test_task_tiers as tier_fixture


TIERS = ("light", "standard", "fast")
RECORDED = {"implementation": "submitted", "verified_completion": "verifiedCompletion"}


class DisplayWorkerHistoryTests(unittest.TestCase):
    @contextmanager
    def fixture(self, tier):
        fx = tier_fixture.TaskTierTests("test_light_dependency_remains_valid_for_heavy_consumer")
        fx.setUp()
        try:
            first = fx.light_task()
            if tier in ("standard", "fast"):
                fx.state["policy"]["leanControl"] = True
                first.update(tier="standard", tierReason="Ordinary independent module", changeKind="feature", risk="normal", riskReason="")
            if tier == "fast":
                check_modes.initialize(fx.state, "fast")
            second = dict(fx.light_task("T02", "b.txt", ["T01"]), tier="heavy",
                          tierReason="Explicit descendant verification", zones=["a.txt", "b.txt"])
            fx.state.update(tasks=[first, second], taskHistory={}, actors={})
            fx.team.prepare()
            self.assertEqual(fx.attempt("T01")["tier"], tier)
            yield fx
        finally:
            fx.tearDown()

    def finish_descendant(self, fx, tier):
        if tier == "fast":
            # A real supported owner-mode transition preserves the completed
            # fast ancestor and gives the pending descendant its heavy route.
            fx.team.switch_check_mode("balanced", "Synthetic owner: use balanced checks for the pending descendant.")
        fx.team.start_guard("T02")
        fx.host.task("T02")["status"] = "running"
        roles = fx.attempt("T02")["roles"]
        for role in ("worker", "critic1", "acceptor"):
            if role in roles:
                fx.team.assign("T02", role, "fixture", "descendant-" + role, "session")
        (fx.root / "a.txt").write_text("Genuinely reattested by the heavy descendant", encoding="utf-8")
        fx.verify("T02")
        fx.team.submit("T02", fx.report("T02"))
        trace = [{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"],
                  "result": "aligned", "note": "Compared exact synthetic source", "artifact": fx.evidence["file"]}
                 for r in fx.team.trace_requirements("T02")]
        inherited = fx.team.inherited_proofs("T02")
        self.assertTrue(fx.team.review("T02", 1, fx.report("T02", "critic1", verdict="PASS", findings=[],
                                                        trace=trace, inheritedProofs=inherited)))
        if "acceptor" in roles:
            self.assertTrue(fx.team.accept("T02", fx.report(
                "T02", "acceptor", verdict="PASS", trace=trace,
                previousReport=fx.attempt("T02")["rounds"][0]["report"]["sha256"], inheritedProofs=inherited)))
        self.assertEqual(fx.host.task("T02")["status"], "completed")
        self.assertFalse(direct_proof_current(fx.store, fx.state, "T01"))
        self.assertTrue(task_proof_valid(fx.store, fx.state, "T01"))

    def assert_history(self, fx, *, current, historical):
        original = copy.deepcopy(fx.state)
        journal = (fx.store.run / "events.jsonl").read_bytes()
        task = project(fx.store, fx.state)["tasks"][0]
        self.assertEqual(task["proofCurrent"], current)
        rows = {row["id"]: row for row in task["phaseSteps"]}
        for phase, field in RECORDED.items():
            record = fx.attempt("T01")[field]
            expected_time = fx.store.receipt_time(record) if hasattr(fx.store, "receipt_time") else self.record_time(fx, record)
            self.assertIsNotNone(expected_time, "Fixture receipt must occur in the verified journal")
            self.assertEqual(rows[phase].get("basis"), record["basis"], "Worker-only phase lost its sealed receipt basis")
            self.assertEqual(rows[phase].get("recordedAt"), expected_time, "Worker-only phase lost its original journal time")
            self.assertEqual(rows[phase].get("basisCurrent"), not historical)
            self.assertEqual(rows[phase].get("historical"), historical)
            self.assertEqual(rows[phase]["status"], "done" if current else "stale")
        for phase in ("critic_1", "repair", "critic_2", "task_acceptance"):
            self.assertEqual(rows[phase]["status"], "skipped")
            self.assertFalse(any(key in rows[phase] for key in ("basis", "recordedAt", "basisCurrent", "historical")),
                             "A skipped phase invented a receipt")
        self.assertEqual(fx.state, original)
        self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), journal)

    @staticmethod
    def record_time(fx, record):
        # The original release has no receipt_time helper; still exercise its
        # actual projection and obtain expected dates from verified events.
        for event in fx.store.read_journal(include_events=True)["events"]:
            for attempt in event["state"].get("taskHistory", {}).get("T01", []):
                if record in (attempt.get("submitted"), attempt.get("verifiedCompletion")):
                    return event["at"]
        return None

    def test_direct_worker_results_keep_actual_basis_and_first_recorded_dates(self):
        for tier in TIERS:
            with self.subTest(tier=tier), self.fixture(tier) as fx:
                fx.finish_light("T01")
                self.assertTrue(direct_proof_current(fx.store, fx.state, "T01"))
                self.assert_history(fx, current=True, historical=False)

    def test_stale_worker_results_keep_historical_receipts_without_current_pass(self):
        for tier in TIERS:
            with self.subTest(tier=tier), self.fixture(tier) as fx:
                fx.finish_light("T01")
                (fx.root / "a.txt").write_text("Later unverified source", encoding="utf-8")
                self.assertFalse(task_proof_valid(fx.store, fx.state, "T01"))
                self.assert_history(fx, current=False, historical=True)

    def test_valid_heavy_descendant_reconfirms_each_worker_tier_without_redating_old_phases(self):
        for tier in TIERS:
            with self.subTest(tier=tier), self.fixture(tier) as fx:
                fx.finish_light("T01")
                old_records = {field: copy.deepcopy(fx.attempt("T01")[field]) for field in RECORDED.values()}
                self.finish_descendant(fx, tier)
                self.assert_history(fx, current=True, historical=True)
                for field, old in old_records.items():
                    self.assertEqual(fx.attempt("T01")[field], old)

    def test_unexecuted_worker_steps_and_skipped_critics_have_no_receipts(self):
        for tier in TIERS:
            with self.subTest(tier=tier), self.fixture(tier) as fx:
                fx.start_light("T01")
                rows = project(fx.store, fx.state)["tasks"][0]["phaseSteps"]
                for row in rows:
                    self.assertFalse(any(key in row for key in ("basis", "recordedAt", "basisCurrent", "historical")))


if __name__ == "__main__":
    unittest.main()
