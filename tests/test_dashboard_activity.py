"""Read-only task assignments and matching journal action timestamps."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import display_feed as feed, participants, work_progress
from aispa_core.display import project
from tests import test_phase_progress as phase_fixture
from tests.test_dashboard_liveness import rows


class AssignmentProjectionTests(unittest.TestCase):
    setUp = phase_fixture.PhaseProgressTests.setUp
    tearDown = phase_fixture.PhaseProgressTests.tearDown
    at = phase_fixture.PhaseProgressTests.at
    task_view = phase_fixture.PhaseProgressTests.task_view
    start_with_team = phase_fixture.PhaseProgressTests.start_with_team
    accept = phase_fixture.PhaseProgressTests.accept

    def test_pending_assignment_is_not_start_and_exposes_only_safe_actor_fields(self):
        fx = self.fx
        fx.prepare()
        fx.flow.pipeline.assign("T01", "worker", "codex", "private-native-agent", "private-session", model="gpt-6.1-sol")
        before = copy.deepcopy(fx.state)
        journal = (fx.store.run / "events.jsonl").read_bytes()
        view = self.task_view()
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["assignment"], {"role": "worker", "roleLabel": "Исполнитель", "runtime": "codex",
                                           "model": "gpt-6.1-sol", "assignedAt": self.now, "status": "assigned"})
        self.assertIsNone(view["phaseTiming"])
        self.assertIsNone(view["elapsedSeconds"])
        self.assertNotIn("startedAt", view)
        self.assertNotIn("private-native-agent", json.dumps(view))
        self.assertNotIn("private-session", json.dumps(view))
        self.at("11:00")
        self.assertEqual(self.task_view()["assignment"], view["assignment"])
        self.assertEqual(fx.state, before)
        self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), journal)

    def test_assignment_follows_each_phase_and_completed_actor_is_not_active(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        self.assertEqual(self.task_view()["assignment"]["role"], "worker")
        self.assertEqual(self.task_view()["assignment"]["status"], "running")
        self.at("10:01")
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        self.assertEqual(self.task_view()["assignment"]["role"], "critic1")
        self.at("10:02")
        finding = {"id": "F1", "severity": "blocking", "text": "Fix output", "criterion": "Выводит ready"}
        fx.flow.pipeline.review("T01", 1, fx.task_report("T01", "critic1", findings=[finding]))
        self.assertEqual(self.task_view()["phase"], "repair")
        self.assertEqual(self.task_view()["assignment"]["role"], "worker")
        self.at("10:03")
        (fx.root / "app.py").write_text("print('ready')\n# repaired\n", encoding="utf-8")
        fx.verify("T01")
        first = fx.attempt()["rounds"][0]
        fx.flow.pipeline.repair("T01", fx.task_report("T01", "worker",
            previousReport=first["report"]["sha256"], addresses=["F1"]))
        self.assertEqual(self.task_view()["assignment"]["role"], "critic2")
        fx.flow.pipeline.review("T01", 2, fx.task_report("T01", "critic2", findings=[], previousReport=first["report"]["sha256"],
            repairReceipt=fx.attempt()["repair"]["report"]["sha256"], resolutions=[
                {"id": "F1", "status": "resolved", "note": "Rechecked", "artifact": fx.attempt()["verification"]["output"]["file"]}]))
        self.assertEqual(self.task_view()["assignment"]["role"], "acceptor")
        self.assertTrue(self.accept())
        self.assertIsNone(self.task_view()["assignment"])

    def test_paused_blocked_and_legacy_unknown_clock_are_not_invented(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        self.at("10:01")
        fx.flow.close("paused", "Synthetic pause")
        frozen = self.task_view()["phaseTiming"]
        self.at("11:00")
        self.assertEqual(self.task_view()["assignment"]["status"], "paused")
        self.assertEqual(self.task_view()["phaseTiming"], frozen)
        self.assertNotIn("activeSince", frozen)
        fx.flow.resume()
        fx.flow.block("T01", "Synthetic blocker")
        self.assertEqual(self.task_view()["assignment"]["status"], "blocked")
        self.assertNotIn("activeSince", self.task_view()["phaseTiming"])
        task = fx.flow.task("T01")
        task.pop("phaseTiming", None)
        task["status"] = "running"  # Old journals may lack phase clocks.
        view = self.task_view()
        self.assertIsNone(view["phaseTiming"])
        self.assertIsNotNone(view["assignment"])
        self.assertNotIn("phaseTiming", task)
        fx.state.pop("taskHistory", None)
        self.assertIsNone(self.task_view()["assignment"])
        self.assertIsNone(self.task_view()["phaseTiming"])

    def test_unsealed_actor_or_stale_role_card_never_produces_assignment(self):
        fx = self.fx
        fx.prepare()
        aid = fx.flow.pipeline.assign("T01", "worker", "codex", "native-worker", "session")
        actor = fx.state["actors"][aid]
        self.assertIsNotNone(self.task_view()["assignment"])
        record = fx.store.run / actor["record"]["file"]
        raw = record.read_bytes()
        record.write_bytes(b"corrupted")
        self.assertIsNone(self.task_view()["assignment"])
        record.write_bytes(raw)
        card = fx.store.run / fx.attempt()["roles"]["worker"]["file"]
        raw_card = card.read_bytes()
        card.write_bytes(b"corrupted")
        self.assertIsNone(self.task_view()["assignment"])
        card.write_bytes(raw_card)
        for replacement in ({}, {"runtime": "codex"}, None):
            with self.subTest(replacement=replacement):
                fx.state["actors"][aid] = replacement
                self.assertIsNone(self.task_view()["assignment"])
        fx.state["actors"][aid] = actor
        fx.attempt()["id"] += "-other"
        self.assertIsNone(self.task_view()["assignment"])

    def test_project_exposes_only_recorded_work_progress_without_mutating_it(self):
        fx = self.fx
        work_progress.begin(fx.state, "discovery", self.now)
        before = copy.deepcopy(fx.state)
        self.at("10:02")
        view = project(fx.store, fx.state)["workProgress"]
        self.assertEqual(view["phase"], "discovery")
        self.assertEqual(view["label"], "Изучение проекта")
        self.assertEqual(view["elapsedSeconds"], 120)
        self.assertEqual(fx.state, before)

    def test_changed_candidate_is_stale_assignment_and_legacy_work_progress_unknown(self):
        fx = self.fx
        fx.prepare(); self.start_with_team()
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        (fx.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        view = self.task_view()
        self.assertTrue(view["phaseStale"])
        self.assertEqual(view["assignment"]["status"], "stale")
        self.assertIsNone(view["phaseTiming"])
        self.assertIsNone(project(fx.store, fx.state)["workProgress"])


class ActivityFeedTests(unittest.TestCase):
    def test_assignments_show_current_role_and_task_not_a_generic_worker(self):
        names = ["actor-assigned:T01:" + role for role in ("worker", "critic1", "critic2", "acceptor")]
        result = feed.events(rows(*names, "participant-assigned:coverage"))
        for entry, role in zip(result, ("worker", "critic1", "critic2", "acceptor")):
            self.assertEqual(entry["text"], "Назначение: " + feed.ROLE_LABELS[role] + " · T01")
        self.assertEqual(result[-1]["text"], "Назначение: Проверяющий покрытие брифа")
        self.assertEqual(set(participants.ROLES) - set(feed.ROLE_LABELS), set())
        work = feed.current_work({"tasks": []}, "build", rows("actor-assigned:T01:critic1", "native-usage-recorded"))
        self.assertEqual(work["lastAction"], "Назначение: Критик · круг 1 · T01")
        self.assertEqual(work["lastActionAt"], "2026-10-22T13:00:00+00:00")

    def test_last_action_and_time_share_event_and_noise_cannot_refresh_activity(self):
        history = rows("brief-review-recorded", "document-metadata-refreshed", "native-usage-recorded")
        result = feed.current_work({"tasks": []}, "brief", history)
        self.assertEqual(result["lastAction"], feed.EVENT_LABELS[history[0]["name"]])
        self.assertEqual(result["lastActionAt"], history[0]["at"])
        only_noise = feed.current_work({"tasks": []}, "brief", history[1:])
        self.assertIsNone(only_noise["lastAction"])
        self.assertIsNone(only_noise["lastActionAt"])
        self.assertEqual(len(feed.events(history)), 3)

    def test_real_callers_have_labels_and_safe_compound_targets(self):
        names = ["run-blocked", "run-cancelled", "recovery-blocked", "project-document-bound:intent",
                 "work-progress-began:discovery", "work-progress-ended:discovery",
                 *["inherited-obligation-" + action for action in ("keep", "map", "resolve", "drop", "defer")],
                 "tasks-reset:T01,T02"]
        for entry in feed.events(rows(*names)):
            self.assertFalse(entry["text"].startswith("Событие:"))
        self.assertIn("Изучение проекта", feed.events(rows("work-progress-began:discovery"))[0]["text"])
        self.assertIn("Замысел проекта", feed.events(rows("project-document-bound:intent"))[0]["text"])
        self.assertIn("T01, T02", feed.events(rows("tasks-reset:T01,T02"))[0]["text"])
        for event in ("actor-assigned:T01:private-role-secret", "participant-assigned:private-role-secret"):
            self.assertNotIn("private-role-secret", feed.events(rows(event))[0]["text"])


if __name__ == "__main__":
    unittest.main()
