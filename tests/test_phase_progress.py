"""Live phase clocks and receipt-backed steps in the dashboard projection."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_host
from aispa_core.contracts import basis
from aispa_core.display import project
from aispa_core.store import Invalid
from tests import test_workflow as workflow_tests


class PhaseProgressTests(unittest.TestCase):
    def setUp(self):
        self.fx = workflow_tests.WorkflowTests("test_empty_run_cannot_finish")
        self.fx.setUp()
        self.now = "2026-10-19T10:00:00+00:00"
        self.workflow_clock = patch("aispa_core.workflow.timestamp", side_effect=lambda: self.now)
        self.team_clock = patch("aispa_core.team.timestamp", side_effect=lambda: self.now)
        self.display_clock = patch("aispa_core.display.timestamp", side_effect=lambda: self.now)
        for clock in (self.workflow_clock, self.team_clock, self.display_clock):
            clock.start()

    def tearDown(self):
        for clock in (self.display_clock, self.team_clock, self.workflow_clock):
            clock.stop()
        self.fx.tearDown()

    def at(self, hhmm):
        self.now = f"2026-10-19T{hhmm}:00+00:00"

    def task_view(self):
        return project(self.fx.store, self.fx.state)["tasks"][0]

    def steps(self):
        return {step["id"]: step["status"] for step in self.task_view()["phaseSteps"]}

    def start_with_team(self):
        fx = self.fx
        fx.flow.start("T01", basis(fx.store, fx.state, "task", "T01"))
        for role in ("worker", "critic1", "critic2", "acceptor"):
            fx.flow.pipeline.assign("T01", role, "codex", role + fx.attempt()["id"], "phase-test")
        fx.verify("T01")

    def accept(self):
        fx = self.fx
        evidence = fx.attempt()["verification"]["output"]["file"]
        trace = [{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"],
                  "result": "aligned", "note": "Checked", "artifact": evidence}
                 for r in fx.flow.pipeline.trace_requirements("T01")]
        return fx.flow.complete_task("T01", fx.task_report("T01", "acceptor", trace=trace,
            inheritedProofs=[], previousReport=fx.attempt()["rounds"][1]["report"]["sha256"]))

    def test_first_pass_skips_repair_and_stops_acceptance_clock(self):
        fx = self.fx
        fx.prepare()
        fx.flow.pipeline.assign("T01", "worker", "codex", "pending-worker", "phase-test")
        self.assertIsNone(self.task_view()["phaseTiming"])
        self.assertEqual(set(self.steps().values()), {"pending"})
        self.start_with_team()
        candidate = basis(fx.store, fx.state, "task", "T01")
        self.at("10:02")
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        self.assertEqual(basis(fx.store, fx.state, "task", "T01"), candidate)
        self.assertEqual(self.task_view()["phaseTiming"]["phase"], "critic_1")
        self.assertEqual(self.task_view()["phaseTiming"]["elapsedSeconds"], 0)
        self.assertEqual(fx.store.load()["tasks"][0]["phaseTiming"]["startedAt"], self.now)
        self.at("10:03")
        fx.flow.pipeline.review("T01", 1, fx.task_report("T01", "critic1", findings=[]))
        self.assertEqual(self.steps()["repair"], "skipped")
        self.assertEqual(self.steps()["critic_2"], "current")
        self.at("10:04")
        fx.flow.pipeline.review("T01", 2, fx.task_report("T01", "critic2", findings=[],
            previousReport=fx.attempt()["rounds"][0]["report"]["sha256"],
            repairReceipt=None, resolutions=[]))
        self.assertEqual(self.steps()["task_acceptance"], "current")
        self.at("10:05")
        self.assertTrue(self.accept())
        frozen = self.task_view()["phaseTiming"]
        self.assertEqual(frozen["elapsedSeconds"], 60)
        self.assertNotIn("activeSince", frozen)
        self.assertEqual(self.steps()["task_acceptance"], "done")
        self.at("11:00")
        self.assertEqual(self.task_view()["phaseTiming"], frozen)

    def test_first_fail_measures_repair_before_second_pass(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        self.at("10:01")
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        self.at("10:02")
        finding = {"id": "F1", "severity": "blocking", "text": "Fix output", "criterion": "Выводит ready"}
        self.assertFalse(fx.flow.pipeline.review("T01", 1,
            fx.task_report("T01", "critic1", findings=[finding])))
        self.assertEqual(self.steps()["critic_1"], "blocked")
        self.assertEqual(self.steps()["repair"], "current")
        self.at("10:05")
        (fx.root / "app.py").write_text("print('ready')\n# repaired\n", encoding="utf-8")
        fx.verify("T01")
        first = fx.attempt()["rounds"][0]
        fx.flow.pipeline.repair("T01", fx.task_report("T01", "worker",
            previousReport=first["report"]["sha256"], addresses=["F1"]))
        self.assertEqual(self.steps()["repair"], "done")
        self.assertEqual(self.steps()["critic_2"], "current")
        self.assertEqual(self.task_view()["phaseTiming"]["phase"], "critic_2")
        self.at("10:06")
        self.assertTrue(fx.flow.pipeline.review("T01", 2,
            fx.task_report("T01", "critic2", findings=[], previousReport=first["report"]["sha256"],
                repairReceipt=fx.attempt()["repair"]["report"]["sha256"], resolutions=[
                    {"id": "F1", "status": "resolved", "note": "Rechecked", "artifact": fx.attempt()["verification"]["output"]["file"]}])))
        self.assertEqual(self.steps()["task_acceptance"], "current")

    def test_first_pass_read_zone_change_stales_current_phase_until_restored(self):
        fx = self.fx
        fx.prepare()
        source = fx.root / "config.py"
        original = b"COURSE = 'alpha'\n"
        source.write_bytes(original)
        fx.flow.plan(fx.file([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "readZones": ["config.py"],
            "criteria": ["Выводит ready"]}]))
        self.start_with_team()
        self.at("10:01")
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        self.at("10:02")
        fx.flow.pipeline.review("T01", 1, fx.task_report("T01", "critic1", findings=[]))
        normal = self.task_view()
        self.assertFalse(normal["phaseStale"])
        self.assertEqual(self.steps()["critic_2"], "current")
        self.assertEqual(self.steps()["repair"], "skipped")
        source.write_bytes(b"COURSE = 'changed'\n")
        stale = self.task_view()
        self.assertTrue(stale["phaseStale"])
        self.assertIsNone(stale["phaseTiming"])
        self.assertIsNotNone(stale["elapsedSeconds"])
        self.assertEqual({step["status"] for step in stale["phaseSteps"][:4]}, {"stale"})
        with self.assertRaises(Invalid):
            fx.flow.pipeline.review("T01", 2, fx.task_report("T01", "critic2", findings=[],
                previousReport=fx.attempt()["rounds"][0]["report"]["sha256"],
                repairReceipt=None, resolutions=[]))
        source.write_bytes(original)
        restored = self.task_view()
        self.assertFalse(restored["phaseStale"])
        self.assertEqual(restored["phaseTiming"]["phase"], "critic_2")
        self.assertEqual(self.steps()["critic_2"], "current")
        self.assertEqual(self.steps()["repair"], "skipped")

    def test_corrupt_first_critic_receipt_stales_current_phase(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        fx.flow.pipeline.review("T01", 1, fx.task_report("T01", "critic1", findings=[]))
        report = fx.store.run / fx.attempt()["rounds"][0]["report"]["file"]
        original = report.read_bytes()
        report.write_bytes(b"corrupt\n")
        stale = self.task_view()
        self.assertTrue(stale["phaseStale"])
        self.assertIsNone(stale["phaseTiming"])
        self.assertEqual(self.steps()["critic_2"], "stale")
        report.write_bytes(original)
        self.assertFalse(self.task_view()["phaseStale"])

    def test_pause_resume_block_restart_reset_and_legacy_unknown(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        self.at("10:01"); fx.flow.close("paused", "Pause")
        self.at("11:00")
        self.assertEqual(self.task_view()["phaseTiming"]["elapsedSeconds"], 60)
        self.assertNotIn("activeSince", self.task_view()["phaseTiming"])
        fx.flow.resume()
        self.at("11:01"); fx.flow.block("T01", "Blocked")
        self.assertEqual(self.task_view()["phaseTiming"]["elapsedSeconds"], 120)
        self.assertEqual(self.steps()["implementation"], "blocked")
        self.at("12:00")
        fx.flow.start("T01", basis(fx.store, fx.state, "task", "T01"))
        self.at("12:01")
        self.assertEqual(self.task_view()["phaseTiming"]["elapsedSeconds"], 180)
        fx.flow.reset("T01")
        self.assertIsNone(self.task_view()["phaseTiming"])
        self.assertEqual(set(self.steps().values()), {"pending"})
        fx.flow.task("T01")["status"] = "running"  # Simulate an older journal record.
        fx.flow.decision("Old task remains without recorded phase start")
        self.assertIsNone(self.task_view()["phaseTiming"])
        self.assertNotIn("phaseTiming", fx.store.load()["tasks"][0])

    def test_stale_completed_proof_is_not_green(self):
        fx = self.fx
        fx.prepare(); fx.completed()
        self.assertEqual(self.steps()["task_acceptance"], "done")
        (fx.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        view = self.task_view()
        self.assertFalse(view["proofCurrent"])
        self.assertEqual(self.steps()["task_acceptance"], "stale")
        self.assertEqual(self.steps()["critic_2"], "stale")

    def test_owner_authorized_new_attempt_clears_previous_phase_clock(self):
        fx = self.fx
        fx.prepare(); fx.completed()
        self.assertIsNotNone(self.task_view()["phaseTiming"])
        quote = "Repeat the accepted task with a new owner instruction."
        brief = fx.flow.brief(fx.file(quote, "new-owner-brief.md"))
        fx.flow.pipeline.new_attempt("T01", brief["id"], quote)
        self.assertEqual(fx.flow.task("T01")["status"], "pending")
        self.assertIsNone(self.task_view()["phaseTiming"])
        self.assertNotIn("phaseTiming", fx.store.load()["tasks"][0])


class OpenOnceTests(unittest.TestCase):
    """init and resume start the service; one open action comes from the start answer."""

    SERVICE = {"running": True, "port": 4321, "url": "http://127.0.0.1:4321/dashboard.html", "unverified": False}

    def test_start_request_only_starts_and_points_at_the_single_open_action(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                action, = dashboard_host.start_request(managed=managed)
                self.assertEqual(action["action"], "start-dashboard")
                self.assertEqual(action["cliArguments"], ["dashboard", "start"])
                self.assertNotIn("surface", action)
                self.assertNotIn("url", action)
                self.assertIn("open-dashboard", action["instruction"])
                self.assertEqual("resourceContract" in action, managed)

    def test_exactly_one_open_action_with_the_verified_url_comes_from_the_start_answer(self):
        actions = dashboard_host.open_request(self.SERVICE)
        self.assertEqual([a["action"] for a in actions], ["open-dashboard"])
        self.assertEqual(actions[0]["url"], self.SERVICE["url"])
        self.assertEqual(actions[0]["verification"], "required")


if __name__ == "__main__":
    unittest.main()
