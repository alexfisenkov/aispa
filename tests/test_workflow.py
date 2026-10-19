import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.contracts import basis, gate_valid, validate_plan
from aispa_core.display import project
from aispa_core.store import Invalid, Journal
from aispa_core.workflow import Workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-flow-")
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".aispa/\n")
        (self.root / "app.py").write_text("print('ready')\n")
        self.commit()
        self.store = Journal(self.root)
        self.state = self.store.create("example", "Пример")
        self.flow = Workflow(self.store, self.state)
        self.inbox = self.store.home / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture", "--allow-empty")

    def file(self, value, name="input.json"):
        path = self.inbox / name
        path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return path

    def prepare(self, secondary=False):
        self.flow.brief(self.file("Команда выводит ready. Нужен экспорт CSV.", "brief.md"))
        reqs = [{"id": "R01", "text": "Команда готовности", "brief": "B001",
                 "quote": "Команда выводит ready", "core": True, "acceptance": ["Выводит ready"]}]
        if secondary:
            reqs.append({"id": "R02", "text": "Экспорт", "brief": "B001", "quote": "Нужен экспорт CSV",
                         "core": False, "acceptance": ["Создаёт CSV"]})
        self.flow.requirements(self.file(reqs))
        self.flow.seal()
        self.flow.spec(self.file("Команда выводит ready. Экспорт создаёт CSV.", "spec.md"))
        self.coverage()
        tasks = [{"id": "T01", "title": "Команда", "requirements": ["R01"], "dependsOn": [],
                  "zones": ["app.py"], "criteria": ["Выводит ready"]}]
        if secondary:
            tasks.append({"id": "T02", "title": "Экспорт", "requirements": ["R02"], "dependsOn": [],
                          "zones": ["export.py"], "criteria": ["Создаёт CSV"]})
        self.flow.plan(self.file(tasks))

    def header(self, kind, tid=None):
        return {"basis": basis(self.store, self.state, kind, tid), "reviewer": "synthetic-reviewer",
                "independent": True, "unmapped": []}

    def coverage(self):
        report = self.header("coverage")
        report["checks"] = [{"requirement": r["id"], "status": "covered",
                             "specQuote": "Команда выводит ready" if r["id"] == "R01" else "Экспорт создаёт CSV"}
                            for r in self.state["requirements"] if r["scope"] == "active"]
        self.assertTrue(self.flow.coverage(self.file(report)))

    def verify(self, tid=None):
        self.assertTrue(self.flow.verify([sys.executable, "-c",
                                         "import subprocess,sys; r=subprocess.check_output([sys.executable,'app.py'],text=True); assert r.strip()=='ready'; print('runtime passed')"], 10, tid))

    def attempt(self, tid="T01"):
        return self.state["taskHistory"][tid][-1]

    def task_report(self, tid, role, **extra):
        attempt = self.attempt(tid)
        report = self.header("task", tid)
        report["reviewer"] = attempt["actors"][role]
        report["checks"] = [{"requirement": rid, "criterion": c, "status": "passed",
                             "artifact": attempt["verification"]["output"]["file"], "note": "Синтетическое наблюдение"}
                            for rid, c in sorted(self.flow.pipeline.expected_checks(tid, role))]
        report.update(extra)
        return self.file(report)

    def completed(self, tid="T01"):
        self.flow.start(tid, basis(self.store, self.state, "task", tid))
        for role in ("worker", "critic1", "critic2", "acceptor"):
            self.flow.pipeline.assign(tid, role, "codex", tid + role + self.attempt(tid)["id"], "synthetic-session")
        self.verify(tid)
        self.flow.pipeline.submit(tid, self.task_report(tid, "worker"))
        self.flow.pipeline.review(tid, 1, self.task_report(tid, "critic1", findings=[]))
        self.flow.pipeline.review(tid, 2, self.task_report(tid, "critic2", findings=[],
            previousReport=self.attempt(tid)["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[]))
        evidence = self.attempt(tid)["verification"]["output"]["file"]
        trace = [{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                  "note": "Сопоставлено с исходником", "artifact": evidence} for r in self.flow.pipeline.trace_requirements(tid)]
        self.flow.complete_task(tid, self.task_report(tid, "acceptor", trace=trace,
            inheritedProofs=self.flow.pipeline.inherited_proofs(tid), previousReport=self.attempt(tid)["rounds"][1]["report"]["sha256"]))
        self.verify()

    def assessment(self, kind="acceptance", failed=None):
        failed = failed or {}
        report = self.header(kind)
        report["checks"] = [{"requirement": r["id"], "criterion": c,
                             "status": failed.get(r["id"], "passed"),
                             "artifact": self.state["verification"]["output"]["file"],
                             "note": "Проверено локальным синтетическим сценарием"}
                            for r in self.state["requirements"] if r["scope"] == "active" for c in r["acceptance"]]
        return report

    def accepted(self):
        self.prepare(); self.completed()
        self.assertTrue(self.flow.wave(1, self.file(self.assessment("wave"))))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment())))

    def test_full_delivery_roundtrip(self):
        self.accepted(); self.flow.finish()
        self.assertEqual(self.store.load()["status"], "accepted")
        self.assertEqual(project(self.store, self.state)["status"], "accepted")

    def test_empty_run_cannot_finish(self):
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_empty_plan_is_rejected(self):
        self.prepare()
        with self.assertRaises(Invalid):
            self.flow.plan(self.file([]))

    def test_uncovered_requirement_cannot_be_lost(self):
        self.prepare(secondary=True)
        tasks = [{k: v for k, v in self.state["tasks"][0].items() if k not in ("status", "origin", "wave")}]
        with self.assertRaises(Invalid):
            self.flow.plan(self.file(tasks))

    def test_unknown_requirement_and_dependency_are_rejected(self):
        self.prepare()
        task = self.state["tasks"][0].copy()
        task["requirements"] = ["A99"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, [task])
        task["requirements"] = ["R01"]; task["dependsOn"] = ["T99"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, [task])

    def test_cyclic_dependencies_are_rejected(self):
        self.prepare(secondary=True)
        tasks = [t.copy() for t in self.state["tasks"]]
        tasks[0]["dependsOn"] = ["T02"]; tasks[1]["dependsOn"] = ["T01"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, tasks)

    def test_same_zone_requires_explicit_order(self):
        self.prepare(secondary=True)
        tasks = [t.copy() for t in self.state["tasks"]]
        tasks[0]["zones"] = ["src"]; tasks[1]["zones"] = ["src/export"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, tasks)
        tasks[1]["dependsOn"] = ["T01"]
        checked = validate_plan(self.state, tasks)
        self.assertEqual([t["wave"] for t in checked], [1, 2])

    def test_next_wave_requires_full_brief_checkpoint(self):
        self.prepare(secondary=True)
        tasks = [t.copy() for t in self.state["tasks"]]
        tasks[1]["dependsOn"] = ["T01"]
        self.flow.plan(self.file(tasks)); self.completed()
        with self.assertRaises(Invalid):
            self.flow.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertTrue(self.flow.wave(1, self.file(self.assessment("wave", {"R02": "not_tested"}))))
        self.flow.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertEqual(self.flow.task("T02")["status"], "running")

    def test_zone_aliases_cannot_bypass_conflicts_or_reserved_paths(self):
        self.prepare(secondary=True)
        tasks = [t.copy() for t in self.state["tasks"]]
        tasks[0]["zones"] = ["./src//./app.py"]
        tasks[1]["readZones"] = ["src/app.py"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, tasks)
        tasks[1]["dependsOn"] = ["T01"]
        checked = validate_plan(self.state, tasks)
        self.assertEqual(checked[0]["zones"], ["src/app.py"])
        tasks[0]["zones"] = ["./.aispa/events"]
        with self.assertRaises(Invalid):
            validate_plan(self.state, tasks)
        tasks[0]["zones"] = [".github/workflows/checks.yml"]
        self.assertEqual(validate_plan(self.state, tasks)[0]["zones"], tasks[0]["zones"])

    def test_acceptance_cannot_skip_wave_review(self):
        self.prepare(); self.completed()
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(self.assessment()))

    def test_coverage_failure_blocks_build(self):
        self.prepare()
        report = self.header("coverage")
        report["checks"] = [{"requirement": "R01", "status": "missing"}]
        self.assertFalse(self.flow.coverage(self.file(report)))
        with self.assertRaises(Invalid):
            self.flow.start("T01", basis(self.store, self.state, "task", "T01"))

    def test_quote_must_exist_in_original_brief(self):
        self.flow.brief(self.file("Нужна форма", "brief.md"))
        bad = [{"id": "R01", "text": "Платежи", "brief": "B001", "quote": "Нужны платежи", "acceptance": ["Оплата работает"]}]
        with self.assertRaises(Invalid):
            self.flow.requirements(self.file(bad))

    def test_requirement_cannot_be_replaced_by_same_id(self):
        self.prepare()
        with self.assertRaises(Invalid):
            self.flow.requirements(self.file([self.state["requirements"][0]]))

    def test_scope_change_needs_new_owner_statement(self):
        self.prepare()
        with self.assertRaises(Invalid):
            self.flow.scope("R01", "drop", "B001", "Команда выводит ready")
        self.flow.brief(self.file("Команду ready отменяю", "amendment.md"))
        self.flow.scope("R01", "drop", "B002", "Команду ready отменяю")
        self.assertEqual(self.state["requirements"][0]["scope"], "dropped")

    def test_brief_change_invalidates_gates(self):
        self.accepted()
        self.flow.brief(self.file("Добавить авторизацию", "amendment.md"))
        self.assertFalse(gate_valid(self.store, self.state, "coverage"))
        self.assertFalse(gate_valid(self.store, self.state, "plan"))
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_code_change_invalidates_acceptance(self):
        self.accepted()
        (self.root / "app.py").write_text("print('broken')\n")
        self.assertFalse(gate_valid(self.store, self.state, "acceptance"))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_finished_projection_becomes_stale_after_code_edit(self):
        self.accepted(); self.flow.finish()
        (self.root / "app.py").write_text("print('broken')\n")
        self.assertEqual(project(self.store, self.state)["status"], "stale")

    def test_old_report_cannot_be_reused(self):
        self.prepare(); self.completed()
        report = self.assessment("wave")
        self.verify()
        with self.assertRaises(Invalid):
            self.flow.wave(1, self.file(report))

    def test_done_without_tests_or_evidence_is_rejected(self):
        self.prepare()
        self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        with self.assertRaises(Invalid):
            self.flow.complete_task("T01", self.file({}))

    def test_red_verification_cannot_close_task(self):
        self.prepare()
        self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        self.assertFalse(self.flow.verify([sys.executable, "-c", "raise SystemExit(1)"], 10))
        with self.assertRaises(Invalid):
            self.flow.complete_task("T01", self.file({}))

    def test_empty_acceptance_is_rejected(self):
        self.prepare(); self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        report = self.header("acceptance"); report["checks"] = []
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(report))

    def test_missing_evidence_is_rejected(self):
        self.prepare(); self.completed()
        report = self.assessment("wave")
        report["checks"][0]["artifact"] = "evidence/absent.log"
        with self.assertRaises(Invalid):
            self.flow.wave(1, self.file(report))

    def test_changed_evidence_blocks_finish(self):
        self.accepted()
        (self.store.run / self.state["verification"]["output"]["file"]).write_text("rewritten")
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_core_not_tested_cannot_be_delivered(self):
        self.prepare(); self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        self.assertFalse(self.flow.acceptance(self.file(self.assessment(failed={"R01": "not_tested"}))))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_working_core_with_secondary_blocker_is_explicit_partial_delivery(self):
        self.prepare(secondary=True); self.completed()
        self.flow.block("T02", "Нет доступа к тестовому сервису")
        self.flow.wave(1, self.file(self.assessment("wave", {"R02": "blocked"})))
        self.assertTrue(self.flow.acceptance(self.file(self.assessment(failed={"R02": "blocked"}))))
        self.flow.finish()
        self.assertEqual(self.state["status"], "delivered_with_remaining")
        self.assertEqual(self.state["remaining"]["checks"][0]["requirement"], "R02")
        self.assertEqual(self.state["requirements"][1]["scope"], "active")

    def test_reset_cannot_discard_used_review_budget(self):
        self.accepted()
        with self.assertRaises(Invalid):
            self.flow.reset("T01")
        self.assertEqual(len(self.attempt()["rounds"]), 2)

    def test_scope_decision_cannot_replay_same_or_older_owner_source(self):
        self.prepare(secondary=True)
        self.flow.brief(self.file("Экспорт отложить", "later.md"))
        self.flow.scope("R02", "defer", "B002", "Экспорт отложить")
        with self.assertRaises(Invalid):
            self.flow.scope("R02", "restore", "B002", "Экспорт отложить")
        self.flow.brief(self.file("Вернуть экспорт", "latest.md"))
        self.flow.scope("R02", "restore", "B003", "Вернуть экспорт")
        with self.assertRaises(Invalid):
            self.flow.scope("R02", "defer", "B002", "Экспорт отложить")

    def test_changed_reviewed_task_requires_owner_attempt(self):
        self.accepted()
        task = self.state["tasks"][0].copy(); task["criteria"] = ["Новый критерий"]
        with self.assertRaises(Invalid):
            self.flow.plan(self.file([task]))

    def test_user_pause_is_respected(self):
        self.prepare(); self.flow.close("paused", "Владелец попросил остановиться")
        with self.assertRaises(Invalid):
            self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        self.flow.resume()
        self.flow.start("T01", basis(self.store, self.state, "task", "T01"))

    def test_context_conflict_after_save_does_not_invite_replaying_event(self):
        self.state["projectContext"] = {"initialized": True}
        with patch("aispa_core.workflow.project_context.refresh", side_effect=Invalid("conflicting path")):
            with self.assertRaisesRegex(Invalid, "Событие записано.*context-refresh"):
                self.flow.brief(self.file("One owner request"))
        self.assertEqual(len(self.store.load()["briefs"]), 1)

    def test_requirement_missing_from_register_blocks_acceptance(self):
        self.prepare(); self.completed()
        self.flow.wave(1, self.file(self.assessment("wave")))
        report = self.assessment()
        report["unmapped"] = ["Требование, потерянное при извлечении брифа"]
        self.assertFalse(self.flow.acceptance(self.file(report)))
        with self.assertRaises(Invalid):
            self.flow.finish()

    def test_brief_file_is_not_execution_evidence(self):
        self.prepare(); self.completed()
        report = self.assessment("wave")
        report["checks"][0]["artifact"] = self.state["briefs"][0]["file"]
        with self.assertRaises(Invalid):
            self.flow.wave(1, self.file(report))

    def test_addition_needs_original_parent(self):
        self.prepare()
        extra = [{"id": "A01", "text": "Дополнение", "kind": "addition", "parent": "R99",
                  "core": False, "acceptance": ["Работает"]}]
        with self.assertRaises(Invalid):
            self.flow.requirements(self.file(extra))

    def test_possible_secret_output_is_withheld_in_full(self):
        self.prepare()
        self.assertFalse(self.flow.verify([sys.executable, "-c", "print('gh'+'p_'+'a'*40)"], 10))
        verification = self.state["verification"]
        output = (self.store.run / verification["output"]["file"]).read_text()
        self.assertEqual(verification["exitCode"], 125)
        self.assertNotIn('a' * 40, output)
        self.assertIn("withheld", output)

    def test_workflow_task_clock_excludes_pause_and_survives_retry(self):
        self.prepare()
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T10:00:00Z"):
            self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T10:01:00Z"):
            self.flow.close("paused", "Пауза владельца")
        self.assertEqual(self.flow.task("T01")["elapsedSeconds"], 60)
        self.assertNotIn("activeSince", self.flow.task("T01"))
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T11:00:00Z"):
            self.flow.resume()
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T11:00:30Z"):
            self.flow.block("T01", "Нет доступа")
        self.assertEqual(self.flow.task("T01")["elapsedSeconds"], 90)
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T12:00:00Z"):
            self.flow.start("T01", basis(self.store, self.state, "task", "T01"))
        with patch("aispa_core.workflow.timestamp", return_value="2026-10-19T12:00:10Z"):
            self.flow.reset("T01")
        self.assertEqual(self.flow.task("T01")["elapsedSeconds"], 100)
        self.assertEqual(self.flow.task("T01")["attempts"], 2)

    def test_live_projection_includes_blockers_before_final_delivery(self):
        self.prepare(secondary=True)
        self.flow.block("T02", "Нет доступа")
        view = project(self.store, self.state)
        self.assertEqual(view["remaining"]["blockedTasks"], ["T02"])
        self.assertFalse(view["remainingAssessed"])
        self.assertEqual(view["estimate"]["reason"], "blocked")


if __name__ == "__main__":
    unittest.main()
