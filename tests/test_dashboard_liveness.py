"""Dashboard must show what is really happening: whole history, current work, attempts."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import display_feed as feed
from aispa_core.display import project
from tests import test_workflow as workflow_tests


def rows(*names, start=0):
    return [{"name": n.partition(":")[0], "target": n.partition(":")[2], "at": "2026-10-22T13:%02d:00+00:00" % (start + i)}
            for i, n in enumerate(names)]


class FeedTests(unittest.TestCase):
    def test_events_never_dropped_and_unknown_gets_neutral_label(self):
        result = feed.events(rows("brief-added", "brand-new-event", "owner-task-attempt:T01"))
        self.assertEqual([r["type"] for r in result], ["brief-added", "brand-new-event", "owner-task-attempt"])
        self.assertEqual(result[1]["text"], "Событие: brand-new-event")
        self.assertIn("T01", result[2]["text"])

    def test_latest_events_survive_noise_and_repeats_collapse(self):
        names = ["brief-added"] + ["native-usage-recorded"] * 40 + ["brief-extract-recorded", "brief-review-recorded"]
        result = feed.events(rows(*names))
        self.assertEqual(result[-1]["type"], "brief-review-recorded")
        self.assertEqual(result[-2]["type"], "brief-extract-recorded")
        self.assertEqual(result[1]["count"], 40)
        self.assertTrue(result[1]["text"].endswith("×40"))

    def test_every_engine_event_name_has_a_russian_label(self):
        for name in ("check-mode-changed", "task-critic-accepted", "task-verified-completed", "task-extra-wave",
                     "brief-extract-recorded", "brief-review-recorded", "agent-decision-recorded", "requirements-added"):
            self.assertIn(name, feed.EVENT_LABELS)

    def test_brief_stage_activity_follows_last_meaningful_event(self):
        state = {"tasks": []}
        cases = {"brief-added": "Разбор нового сообщения владельца",
                 "brief-extract-recorded": "Независимая сверка брифа",
                 "brief-review-recorded": "Карта брифа и требования"}
        for event, title in cases.items():
            work = feed.current_work(state, "brief", rows(event, "native-usage-recorded"))
            self.assertEqual(work["title"], title)
            self.assertEqual(work["lastAction"], feed.EVENT_LABELS[event])
            self.assertEqual(work["lastActionAt"], "2026-10-22T13:01:00+00:00")
            self.assertIsNone(work["note"])

    def test_brief_with_saved_plan_explains_pending_tasks(self):
        state = {"tasks": [{"id": "T01", "status": "pending"}]}
        work = feed.current_work(state, "brief", rows("owner-task-attempt:T01"))
        self.assertIn("план сохранён", work["note"])

    def test_preparation_stage_says_tasks_wait(self):
        state = {"tasks": [{"id": "T01", "status": "pending"}]}
        self.assertEqual(feed.current_work(state, "coverage", rows("spec-recorded"))["note"],
                         "Задачи ждут завершения подготовки")
        self.assertIsNone(feed.current_work(state, "build", rows("plan-validated"))["title"])

    def test_attempts_summary_and_legacy(self):
        first = {"rounds": [{"passed": True}, {"passed": False}], "terminal": False}
        second = {"rounds": [], "authorization": {"brief": "b1", "quote": "q"}}
        result = feed.attempts([first, second])
        self.assertEqual(result["attemptNumber"], 2)
        self.assertEqual(result["previousAttempts"][0], {"number": 1, "criticRounds": 2, "outcome": "blocked",
                         "followedBy": "owner", "reason": "по словам владельца"})
        self.assertEqual(result["attemptNote"], "Попытка 2 · прошлая: критик ×2, блок → новая по словам владельца")
        self.assertEqual(feed.attempts(None), {"attemptNumber": 1, "previousAttempts": [], "attemptNote": None})
        promoted = feed.attempts([first, {"authorization": {"kind": "tier-escalation"}}])
        self.assertIn("повышение класса", promoted["attemptNote"])


class ProjectionTests(unittest.TestCase):
    def test_projection_exposes_new_fields(self):
        fx = workflow_tests.WorkflowTests("test_empty_run_cannot_finish")
        fx.setUp()
        try:
            fx.prepare()
            view = project(fx.store, fx.state)
            self.assertEqual(view["tasks"][0]["attemptNumber"], 1)
            self.assertEqual(view["tasks"][0]["previousAttempts"], [])
            self.assertIn("lastActionAt", view["currentWork"])
            self.assertTrue(view["events"])
            self.assertIn("team-prepared", [e["type"] for e in view["events"]])
        finally:
            fx.tearDown()


if __name__ == "__main__":
    unittest.main()
