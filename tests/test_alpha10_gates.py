import json
import unittest

import test_workflow as helpers
from aispa_core import integration
from aispa_core.contracts import basis
from aispa_core.store import Invalid


class Alpha10GateTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file
    prepare = helpers.WorkflowTests.prepare
    coverage = helpers.WorkflowTests.coverage
    header = helpers.WorkflowTests.header
    verify = helpers.WorkflowTests.verify
    attempt = helpers.WorkflowTests.attempt
    task_report = helpers.WorkflowTests.task_report

    def configure(self):
        self.prepare()
        self.state["policy"] = {"integrationReview": True}
        integration.capture(self.store, self.state)
        task = dict(self.state["tasks"][0], readZones=["."])
        self.flow.plan(self.file([task]))
        self.assertEqual(self.state["integrationTask"], "T01")
        self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        for role in ("worker", "critic1", "critic2", "acceptor"):
            self.flow.pipeline.assign("T01", role, "codex", role + "-fixture", "fixture")
        self.verify("T01")
        self.flow.pipeline.submit("T01", self.task_report("T01", "worker"))

    def branch(self):
        context = integration.context(self.store, self.state)
        evidence = self.attempt()["verification"]["output"]["file"]
        return {**{k: context[k] for k in ("basis", "code", "files", "changes")},
                "axes": [{"axis": axis, "status": "passed", "note": "Synthetic reviewed axis", "evidence": evidence}
                         for axis in integration.AXES],
                "note": "Synthetic whole candidate review", "evidence": evidence}

    def test_first_critic_requires_complete_branch_review(self):
        self.configure()
        with self.assertRaises(Invalid):
            self.flow.pipeline.review("T01", 1, self.task_report("T01", "critic1", findings=[]))
        self.assertEqual(self.attempt()["rounds"], [])
        self.assertTrue(self.flow.pipeline.review("T01", 1, self.task_report(
            "T01", "critic1", findings=[], branchReview=self.branch())))
        with self.assertRaises(Invalid):
            self.flow.pipeline.review("T01", 2, self.task_report(
                "T01", "critic2", findings=[], previousReport=self.attempt()["rounds"][0]["report"]["sha256"],
                repairReceipt=None, resolutions=[]))
        self.assertEqual(len(self.attempt()["rounds"]), 1)

    def test_failed_axis_consumes_first_round_and_requires_a_blocking_finding(self):
        self.configure()
        branch = self.branch()
        branch["axes"][0]["status"] = "failed"
        with self.assertRaises(Invalid):
            self.flow.pipeline.review("T01", 1, self.task_report("T01", "critic1", findings=[], branchReview=branch))
        report = self.task_report("T01", "critic1", branchReview=branch, findings=[
            {"id": "F1", "severity": "blocking", "criterion": "Вся ветка: behavior",
             "text": "Synthetic behavior failure"}])
        self.assertFalse(self.flow.pipeline.review("T01", 1, report))
        self.assertEqual(len(self.attempt()["rounds"]), 1)
        self.assertEqual(self.flow.task("T01")["phase"], "repair")
        with self.assertRaises(Invalid):
            self.flow.pipeline.review("T01", 1, report)

    def test_task_acceptance_alone_cannot_skip_integrated_review_gate(self):
        self.configure()
        with self.assertRaises(Invalid):
            self.flow.handoff_ready()


if __name__ == "__main__":
    unittest.main()
