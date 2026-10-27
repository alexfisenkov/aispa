"""Stories 78 and 115: a small brief change redoes only the affected tasks; an unchanged plan rebuilds nothing."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.contracts import basis
from aispa_core.store import Invalid
from aispa_core.team import task_proof_valid
import test_workflow as helpers


PLAN = [{"id": "T01", "title": "Команда", "requirements": ["R01"], "dependsOn": [],
         "zones": ["app.py"], "criteria": ["Выводит ready"]},
        {"id": "T02", "title": "Экспорт", "requirements": ["R02"], "dependsOn": [],
         "zones": ["export.py"], "criteria": ["Создаёт CSV"]},
        {"id": "T03", "title": "Отчёт", "requirements": ["R02"], "dependsOn": ["T02"],
         "zones": ["report.py"], "criteria": ["Отчёт по CSV"]}]


class TargetedRebriefTests(helpers.WorkflowTests):
    """Reuses the workflow fixture; the inherited workflow tests are not re-run here."""

    def run_scenario(self, targeted=True):
        self.state["policy"] = {"targetedRebrief": True} if targeted else {}
        self.flow.brief(self.file("Команда выводит ready. Нужен экспорт CSV.", "brief.md"))
        self.flow.requirements(self.file([
            {"id": "R01", "text": "Команда готовности", "brief": "B001", "quote": "Команда выводит ready",
             "core": True, "acceptance": ["Выводит ready"]},
            {"id": "R02", "text": "Экспорт", "brief": "B001", "quote": "Нужен экспорт CSV",
             "core": False, "acceptance": ["Создаёт CSV"]}]))
        self.flow.seal()
        self.flow.spec(self.file("Команда выводит ready. Экспорт создаёт CSV.", "spec.md"))
        self.coverage()
        self.flow.plan(self.file(PLAN))
        self.completed("T01")
        self.completed("T02")
        self.assertTrue(self.flow.wave(1, self.file(self.assessment("wave"))))
        self.completed("T03")
        # The owner adds one requirement to the export only.
        self.flow.brief(self.file("Экспорт также пишет JSON.", "more.md"))
        self.flow.requirements(self.file([
            {"id": "R03", "text": "JSON", "brief": "B002", "quote": "Экспорт также пишет JSON",
             "core": False, "acceptance": ["Создаёт JSON"]}]))
        self.flow.seal()
        self.coverage()
        changed = copy.deepcopy(PLAN)
        changed[1]["requirements"] = ["R02", "R03"]
        changed[1]["criteria"] = ["Создаёт CSV", "Создаёт JSON"]
        return changed

    def test_only_affected_tasks_and_their_descendants_are_redone(self):
        changed = self.run_scenario()
        proof = copy.deepcopy(self.flow.task("T01")["proof"])
        t01_attempt = self.attempt("T01")["id"]
        old_attempts = {tid: self.attempt(tid)["id"] for tid in ("T02", "T03")}
        # The new brief and its seal are the owner's words: plan reopens what they changed, without new-attempt.
        self.flow.plan(self.file(changed))
        self.assertEqual(self.flow.task("T01")["status"], "completed")
        self.assertEqual(self.flow.task("T01")["proof"], proof)
        self.assertEqual(self.attempt("T01")["id"], t01_attempt)
        self.assertTrue(task_proof_valid(self.store, self.state, "T01"))
        for tid in ("T02", "T03"):
            self.assertEqual(self.flow.task(tid)["status"], "pending")
            self.assertNotIn("proof", self.flow.task(tid))
            self.assertFalse(task_proof_valid(self.store, self.state, tid))
            attempt = self.attempt(tid)
            self.assertNotEqual(attempt["id"], old_attempts[tid])
            self.assertEqual((attempt["rounds"], attempt["authorization"]["brief"]), ([], "B002"))
            self.assertEqual(attempt["authorization"]["previousAttempt"], old_attempts[tid])
            self.assertEqual(attempt["contract"], self.flow.task(tid)["origin"])
        events = [row["event"] for row in self.store.read_journal(include_events=True)["events"]]
        self.assertEqual(events[-1], "plan-validated")
        self.assertNotIn("owner-task-attempt:T02", events)  # journaled together with the plan

    def test_a_plan_refused_after_reopening_leaves_no_attempt_and_no_event(self):
        changed = self.run_scenario()
        self.state["policy"]["planReview"] = True  # the plan below lacks its size review and is refused late
        before, journal = copy.deepcopy(self.state), (self.store.run / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(Invalid, "plan-check"):
            self.flow.plan(self.file(changed))
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)

    def test_preserved_task_proof_still_follows_its_zone_code(self):
        changed = self.run_scenario()
        self.flow.plan(self.file(changed))
        (self.root / "app.py").write_text("print('ready')\nprint('changed')\n", encoding="utf-8")
        self.assertFalse(task_proof_valid(self.store, self.state, "T01"))

    def test_a_reviewed_task_changed_by_the_plan_alone_is_refused_unwritten(self):
        changed = self.run_scenario()
        changed[0]["title"] = "Команда готовности"  # T01: no requirement of its chain changed
        before, journal = copy.deepcopy(self.state), (self.store.run / "events.jsonl").read_bytes()
        with self.assertRaises(Invalid) as refused:
            self.flow.plan(self.file(changed))
        self.assertIn("T01", str(refused.exception))
        self.assertIn("new-attempt", str(refused.exception))
        self.assertNotIn("T02", str(refused.exception))
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)

    def test_an_unchanged_task_with_a_failed_critique_is_not_reopened_by_the_brief(self):
        self.state["policy"] = {"targetedRebrief": True}
        self.flow.brief(self.file("Команда выводит ready. Нужен экспорт CSV.", "brief.md"))
        self.flow.requirements(self.file([
            {"id": "R01", "text": "Команда готовности", "brief": "B001", "quote": "Команда выводит ready",
             "core": True, "acceptance": ["Выводит ready"]},
            {"id": "R02", "text": "Экспорт", "brief": "B001", "quote": "Нужен экспорт CSV",
             "core": False, "acceptance": ["Создаёт CSV"]}]))
        self.flow.seal()
        self.flow.spec(self.file("Команда выводит ready. Экспорт создаёт CSV.", "spec.md"))
        self.coverage()
        self.flow.plan(self.file(PLAN))
        self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        for role in ("worker", "critic1"):
            self.flow.pipeline.assign("T01", role, "codex", "T01" + role, "synthetic-session")
        self.verify("T01")
        self.flow.pipeline.submit("T01", self.task_report("T01", "worker"))
        self.assertFalse(self.flow.pipeline.review("T01", 1, self.task_report("T01", "critic1", findings=[
            {"id": "F1", "severity": "blocking", "text": "Fix it", "criterion": "Выводит ready"}])))
        attempt, status = copy.deepcopy(self.attempt("T01")), self.flow.task("T01")["status"]
        self.flow.brief(self.file("Экспорт также пишет JSON.", "more.md"))
        self.flow.requirements(self.file([
            {"id": "R03", "text": "JSON", "brief": "B002", "quote": "Экспорт также пишет JSON",
             "core": False, "acceptance": ["Создаёт JSON"]}]))
        self.flow.seal()
        self.coverage()
        changed = copy.deepcopy(PLAN)
        changed[1]["requirements"] = ["R02", "R03"]
        self.flow.plan(self.file(changed))
        self.assertEqual(self.attempt("T01"), attempt)
        self.assertEqual(self.flow.task("T01")["status"], status)
        self.assertEqual(len(self.state["taskHistory"]["T01"]), 1)

    def test_without_the_flag_a_new_brief_still_invalidates_every_reviewed_task(self):
        changed = self.run_scenario(targeted=False)
        with self.assertRaises(Invalid):
            self.flow.plan(self.file(changed))


class UnchangedPlanTests(helpers.WorkflowTests):
    def test_same_plan_and_inputs_return_unchanged_without_events_or_artifacts(self):
        self.state["policy"] = {"focusedReview": True}
        self.prepare(secondary=True)
        plan = [{k: v for k, v in task.items() if k in ("id", "title", "requirements", "dependsOn", "zones", "criteria")}
                for task in self.state["tasks"]]
        before = copy.deepcopy(self.state)
        journal = (self.store.run / "events.jsonl").read_bytes()
        files = sorted(p for p in self.store.run.rglob("*") if p.is_file())
        receipt = self.flow.plan(self.file(plan, "same-plan.json"))
        self.assertEqual(receipt, {"unchanged": True})
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)
        self.assertEqual(sorted(p for p in self.store.run.rglob("*") if p.is_file()), files)

    def test_changed_plan_or_inputs_rebuild(self):
        self.state["policy"] = {"focusedReview": True}
        self.prepare(secondary=True)
        plan = [{k: v for k, v in task.items() if k in ("id", "title", "requirements", "dependsOn", "zones", "criteria")}
                for task in self.state["tasks"]]
        plan[1]["title"] = "Экспорт CSV"
        count = len((self.store.run / "events.jsonl").read_bytes().splitlines())
        self.assertIsNone(self.flow.plan(self.file(plan, "renamed.json")))
        self.assertEqual(self.flow.task("T02")["title"], "Экспорт CSV")
        self.assertGreater(len((self.store.run / "events.jsonl").read_bytes().splitlines()), count)
        self.assertEqual(self.flow.plan(self.file(plan, "renamed-again.json")), {"unchanged": True})

    def test_runs_without_focused_review_rebuild_as_before(self):
        self.prepare(secondary=True)
        plan = [{k: v for k, v in task.items() if k in ("id", "title", "requirements", "dependsOn", "zones", "criteria")}
                for task in self.state["tasks"]]
        count = len((self.store.run / "events.jsonl").read_bytes().splitlines())
        self.assertIsNone(self.flow.plan(self.file(plan, "same-plan.json")))
        self.assertGreater(len((self.store.run / "events.jsonl").read_bytes().splitlines()), count)


def load_tests(loader, tests, pattern):
    # Only this module's own tests: the base fixture's workflow tests run in test_workflow.
    suite = unittest.TestSuite()
    for case in (TargetedRebriefTests, UnchangedPlanTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
