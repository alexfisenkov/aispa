import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import planning
from aispa_core.contracts import validate_plan
from aispa_core.store import Invalid
from tests import test_workflow as workflow_tests


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.fx = workflow_tests.WorkflowTests("test_empty_run_cannot_finish")
        self.fx.setUp(); self.fx.prepare()
        self.draft = [{"id": f"T0{i}", "title": f"CLI part {i}", "requirements": ["R01"],
            "dependsOn": [f"T0{i-1}"] if i > 1 else [], "zones": ["app.py"],
            "readZones": ["README.md"], "criteria": [f"Criterion {i}"]} for i in (1, 2, 3)]
        self.final = [dict(self.draft[0], criteria=[f"Criterion {i}" for i in (1, 2, 3)])]

    def tearDown(self):
        self.fx.tearDown()

    def report(self, merged=True):
        advice = planning.analyze(self.fx.store, self.fx.state, self.draft)
        return {"basis": advice["basis"], "draft": copy.deepcopy(self.draft),
            "groups": [{"task": "T01", "from": ["T01", "T02", "T03"], "reason": "Один CLI-сценарий и одна зона"}] if merged else
                [{"task": t["id"], "from": [t["id"]], "reason": "Отдельная проверяемая граница"} for t in self.draft],
            "separate": [] if merged else [{"suggestion": s["id"], "reason": "Необходим отдельный наблюдаемый результат"} for s in advice["suggestions"]]}

    def test_three_parts_merge_without_dropping_checks_or_critics(self):
        receipt = planning.review(self.fx.store, self.fx.state, self.final, self.report())
        self.assertEqual((receipt["taskRoleStartsBefore"], receipt["taskRoleStartsAfter"]), (12, 4))
        self.assertEqual(receipt["criticRoundsPerTask"], 2)
        self.assertIsNone(receipt["measuredTokenSavings"])
        self.fx.state["tasks"] = validate_plan(self.fx.state, self.final)
        self.fx.state["planReview"] = receipt
        self.assertTrue(planning.valid(self.fx.store, self.fx.state))
        self.fx.state["tasks"][0]["criteria"].pop()
        self.assertFalse(planning.valid(self.fx.store, self.fx.state))

    def test_boundary_can_be_kept_but_requires_each_reason(self):
        report = self.report(False)
        planning.review(self.fx.store, self.fx.state, self.draft, report)
        report["separate"].pop()
        with self.assertRaises(Invalid):
            planning.review(self.fx.store, self.fx.state, self.draft, report)

    def test_cannot_drop_scope_checks_reads_or_broaden_zone(self):
        for field, value in (("criteria", ["Criterion 1"]), ("readZones", []), ("zones", ["."])):
            with self.subTest(field=field):
                final = copy.deepcopy(self.final); final[0][field] = value
                with self.assertRaises(Invalid):
                    planning.review(self.fx.store, self.fx.state, final, self.report())

    def test_draft_and_final_dependency_must_match(self):
        draft = copy.deepcopy(self.draft)
        draft[0]["zones"] = ["one.py"]; draft[1]["zones"] = ["two.py"]; draft[2]["zones"] = ["three.py"]
        self.draft = draft
        report = self.report(False)
        final = copy.deepcopy(draft); final[2]["dependsOn"] = []
        with self.assertRaises(Invalid):
            planning.review(self.fx.store, self.fx.state, final, report)

    def test_disconnected_tasks_cannot_be_forced_into_one(self):
        self.fx.state["requirements"].append(dict(self.fx.state["requirements"][0], id="R02"))
        self.draft = [dict(self.draft[0], zones=["one.py"]),
                      dict(self.draft[1], requirements=["R02"], zones=["two.py"], dependsOn=[])]
        report = self.report(); report["groups"][0]["from"] = ["T01", "T02"]
        final = [dict(self.draft[0], requirements=["R01", "R02"], zones=["one.py", "two.py"], criteria=["Criterion 1", "Criterion 2"])]
        with self.assertRaises(Invalid):
            planning.review(self.fx.store, self.fx.state, final, report)

    def test_stale_basis_missing_mapping_and_risk_downgrade_rejected(self):
        report = self.report(); report["basis"] = "old"
        with self.assertRaises(Invalid): planning.review(self.fx.store, self.fx.state, self.final, report)
        report = self.report(); report["groups"][0]["from"].pop()
        with self.assertRaises(Invalid): planning.review(self.fx.store, self.fx.state, self.final, report)
        self.draft[1]["risk"] = "high"
        with self.assertRaises(Invalid): planning.review(self.fx.store, self.fx.state, self.final, self.report())

    def test_tampered_receipt_fails(self):
        receipt = planning.review(self.fx.store, self.fx.state, self.final, self.report())
        self.fx.state["tasks"] = validate_plan(self.fx.state, self.final); self.fx.state["planReview"] = receipt
        (self.fx.store.run / receipt["report"]["file"]).write_text("{}", encoding="utf-8")
        self.assertFalse(planning.valid(self.fx.store, self.fx.state))

    def test_duplicate_criteria_keep_each_source_identity(self):
        self.draft[1]["criteria"] = ["Criterion 1"]
        final = copy.deepcopy(self.final)
        final[0]["criteria"] = ["Criterion 1", "Criterion 3"]
        with self.assertRaises(Invalid):
            planning.review(self.fx.store, self.fx.state, final, self.report())
        final[0]["criteria"] = ["T01: Criterion 1", "T02: Criterion 1", "Criterion 3"]
        planning.review(self.fx.store, self.fx.state, final, self.report())

    def test_risk_reason_must_survive_merge(self):
        self.draft[1].update(risk="high", riskReason="Migration affects payment data")
        final = copy.deepcopy(self.final); final[0]["risk"] = "high"
        with self.assertRaises(Invalid):
            planning.review(self.fx.store, self.fx.state, final, self.report())
        final[0]["riskReason"] = "T02: Migration affects payment data; preserved shared risk"
        planning.review(self.fx.store, self.fx.state, final, self.report())


if __name__ == "__main__":
    unittest.main()
