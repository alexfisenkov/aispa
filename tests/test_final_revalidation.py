"""A fresh owner retry may revalidate stale accepted ancestors in the final task."""
import hashlib
import sys
import unittest

import test_team
from aispa_core import integration
from aispa_core.contracts import basis, plan_basis, validate_plan
from aispa_core.store import Invalid
from aispa_core.team import _history_valid, direct_proof_current, historical_proof_valid, task_proof_valid
from aispa_core.workflow import Workflow


class FinalRetryStartTests(unittest.TestCase):
    def fixture(self, *, first_start=True):
        f = test_team.TeamTests(methodName="test_two_rounds_and_separate_acceptor_required")
        f.setUp()
        self.addCleanup(f.tearDown)
        integration.capture(f.store, f.state)
        f.state["policy"] = {"integrationReview": True}
        t01 = f.task("T01", "R01", "a.txt")
        t01["requirements"].append("G01")
        t05 = f.task("T05", "R02", "b.txt", ["T01"])
        t06 = f.task("T06", "R02", "final.txt", ["T05"])
        t06["readZones"] = ["."]
        f.state["tasks"] = validate_plan(f.state, [t01, t05, t06])
        f.state["integrationTask"] = "T06"
        f.state["taskHistory"] = {}
        f.state["actors"] = {}
        f.team.prepare()
        f.complete("T01")
        f.complete("T05")
        self.assertTrue(direct_proof_current(f.store, f.state, "T05"))
        flow = Workflow(f.store, f.state)
        flow.build_ready = flow.running  # Other build gates are outside this transition fixture.
        for wave in (1, 2):
            f.state.setdefault("waves", {})[str(wave)] = {
                "passed": True, "plan": plan_basis(f.state), "basis": basis(f.store, f.state, "wave"),
                "report": f.store.artifact("wave-reviews", b"fixture review\n", ".txt")}
        if first_start:
            flow.start("T06", basis(f.store, f.state, "task", "T06"))
            flow.pipeline.assign("T06", "worker", "fixture", "final-worker", "session")
        flow.block("T06", "Owner retry needed")
        (f.root / "b.txt").write_text("new predecessor test source\n", encoding="utf-8")
        self.assertTrue(historical_proof_valid(f.store, f.state, "T05"))
        self.assertFalse(direct_proof_current(f.store, f.state, "T05"))
        self.assertFalse(task_proof_valid(f.store, f.state, "T05", allowed_task_ids={"T01", "T05"}))
        self.assertTrue(direct_proof_current(f.store, f.state, "T01"))
        return f, flow

    def owner_retry(self, f, flow):
        raw = b"Question: retry T06?\nOwner: Yes, retry T06.\n"
        source = f.home / "owner-retry.txt"
        source.write_bytes(raw)
        return flow.owner_new_attempt(
            "T06", owner_source_file=source, source_sha256=hashlib.sha256(raw).hexdigest(),
            owner_decision_id="final-retry-001", quote="Yes, retry T06.")

    def branch_review(self, f):
        view = integration.context(f.store, f.state, "T06")
        evidence = f.attempt("T06")["verification"]["output"]["file"]
        return {"basis": view["basis"], "code": view["code"],
                "files": view["files"], "changes": view["changes"],
                "note": "Checked all changed fixture files and their combined behavior.",
                "evidence": evidence,
                "axes": [{"axis": axis, "status": "passed", "note": "Checked " + axis,
                          "evidence": evidence} for axis in integration.AXES]}

    def test_owner_retry_of_started_final_heavy_task_is_ready_and_starts(self):
        f, flow = self.fixture()
        old = f.state["taskHistory"]["T06"][-1]["id"]
        self.owner_retry(f, flow)
        current = f.state["taskHistory"]["T06"][-1]
        self.assertNotEqual(current["id"], old)
        card = f.store.json(current["roles"]["critic1"])
        self.assertTrue({"Вся ветка: " + axis for axis in integration.AXES} <= set(card["checks"]))
        self.assertEqual([row["id"] for row in flow.ready()["tasks"]], ["T06"])
        flow.start("T06", basis(f.store, f.state, "task", "T06"))
        self.assertEqual(flow.task("T06")["status"], "running")
        self.assertTrue(historical_proof_valid(f.store, f.state, "T05"))
        self.assertFalse(direct_proof_current(f.store, f.state, "T05"))

    def test_fresh_final_acceptance_revalidates_stale_ancestor(self):
        f, flow = self.fixture()
        self.owner_retry(f, flow)
        flow.start("T06", basis(f.store, f.state, "task", "T06"))
        for role in ("worker", "critic1", "critic2", "acceptor"):
            flow.pipeline.assign("T06", role, "fixture", "fresh-" + role, "session")
        with self.assertRaises(Invalid):
            flow.pipeline.submit("T06", f.report("T06"))
        with self.assertRaisesRegex(Invalid, "полное ревью"):
            flow.handoff_ready()
        self.assertTrue(flow.verify([sys.executable, "-B", "-c", "print('final fixture check')"], 10, "T06"))
        self.assertTrue(flow.verify([sys.executable, "-B", "-c", "print('whole fixture check')"], 10))
        with self.assertRaisesRegex(Invalid, "Приёмка одной из завершённых задач"):
            flow.wave(3, f.home / "unused-wave-review.json")
        flow.pipeline.submit("T06", f.report("T06"))
        with self.assertRaises(Invalid):
            flow.pipeline.review("T06", 1, f.report("T06", "critic1", findings=[]))
        stale = self.branch_review(f)
        stale["basis"] = "0" * 64
        with self.assertRaises(Invalid):
            flow.pipeline.review("T06", 1, f.report("T06", "critic1", findings=[], branchReview=stale))
        self.assertEqual(f.attempt("T06")["rounds"], [])
        self.assertIsNone(f.attempt("T06")["acceptance"])
        flow.pipeline.review("T06", 1, f.report("T06", "critic1", findings=[], branchReview=self.branch_review(f)))
        first = f.attempt("T06")["rounds"][0]
        flow.pipeline.review("T06", 2, f.report("T06", "critic2", findings=[],
            previousReport=first["report"]["sha256"], repairReceipt=None, resolutions=[],
            branchReview=self.branch_review(f)))
        self.assertTrue(f.accept("T06"))
        self.assertTrue(direct_proof_current(f.store, f.state, "T06"))
        self.assertFalse(direct_proof_current(f.store, f.state, "T05"))
        self.assertTrue(task_proof_valid(f.store, f.state, "T05"))
        flow.handoff_ready()

    def test_no_owner_retry_keeps_original_dependency_refusal(self):
        f, flow = self.fixture()
        flow.task("T06")["status"] = "pending"
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))

    def test_owner_retry_without_prior_start_still_refuses_stale_predecessor(self):
        f, flow = self.fixture(first_start=False)
        self.owner_retry(f, flow)
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))

    def test_light_owner_retry_cannot_use_final_heavy_exception(self):
        f, flow = self.fixture()
        self.owner_retry(f, flow)
        current = f.state["taskHistory"]["T06"][-1]
        current["tier"] = "light"
        flow.pipeline._cards("T06", current)
        self.assertTrue(_history_valid(f.store, f.state, "T06"))
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))

    def test_owner_retry_does_not_relax_other_task_or_tampered_history(self):
        f, flow = self.fixture()
        self.owner_retry(f, flow)
        f.state["integrationTask"] = "T05"
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))
        f.state["integrationTask"] = "T06"
        auth = f.state["taskHistory"]["T06"][-1]["authorization"]
        original = auth["decisionId"]
        auth["decisionId"] = "changed-owner-record"
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))
        auth["decisionId"] = original
        source = f.store.run / auth["source"]["file"]
        original_bytes = source.read_bytes()
        source.write_bytes(b"changed")
        self.assertEqual(flow.ready()["tasks"], [])
        source.write_bytes(original_bytes)
        history = f.state["taskHistory"]["T06"]
        previous = history.pop(0)
        self.assertEqual(flow.ready()["tasks"], [])
        history.insert(0, previous)
        f.state["taskHistory"]["T05"][-1]["acceptance"]["passed"] = False
        self.assertEqual(flow.ready()["tasks"], [])
        with self.assertRaisesRegex(Invalid, "Зависимости задачи"):
            flow.start("T06", basis(f.store, f.state, "task", "T06"))


if __name__ == "__main__":
    unittest.main()
