from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import timing
from aispa_core.progress import display_stage, estimate


class TimingTests(unittest.TestCase):
    def test_running_interval_is_measured_from_recorded_start(self):
        task = {}
        timing.start(task, "2026-10-19T10:00:00+00:00")
        self.assertEqual(timing.elapsed(task, "2026-10-19T10:01:30+00:00"), 90)

    def test_pause_and_resume_exclude_the_pause(self):
        task = {}
        timing.start(task, "2026-10-19T10:00:00+00:00")
        timing.stop(task, "2026-10-19T10:01:00+00:00")
        self.assertEqual(timing.elapsed(task, "2026-10-19T11:00:00+00:00"), 60)
        timing.start(task, "2026-10-19T11:00:00+00:00")
        timing.stop(task, "2026-10-19T11:02:00+00:00", completed=True)
        self.assertEqual(task["elapsedSeconds"], 180)
        self.assertEqual(task["startedAt"], "2026-10-19T10:00:00+00:00")
        self.assertEqual(task["finishedAt"], "2026-10-19T11:02:00+00:00")

    def test_unknown_times_are_not_zero_duration(self):
        self.assertIsNone(timing.elapsed({}, "2026-10-19T10:00:00+00:00"))

    def test_clock_rollback_never_creates_negative_time(self):
        task = {}
        timing.start(task, "2026-10-19T10:01:00+00:00")
        timing.stop(task, "2026-10-19T10:00:00+00:00")
        self.assertEqual(task["elapsedSeconds"], 0)

    def test_estimate_needs_two_measured_completed_tasks(self):
        tasks = [{"status": "completed", "elapsedSeconds": 100}, {"status": "pending", "wave": 2}]
        self.assertEqual(estimate(tasks, "running", "2026-10-19T10:00:00Z")["reason"], "insufficient_data")

    def test_estimate_accounts_for_parallel_and_serial_work(self):
        tasks = [{"status": "completed", "elapsedSeconds": 100}, {"status": "completed", "elapsedSeconds": 200},
                 {"status": "pending", "wave": 2}, {"status": "pending", "wave": 2}, {"status": "pending", "wave": 3}]
        result = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result["medianTaskSeconds"], 150)
        self.assertEqual(result["lowerSeconds"], 285)
        self.assertEqual(result["upperSeconds"], 870)

    def test_blocker_has_no_finite_eta(self):
        self.assertEqual(estimate([{"status": "blocked"}], "running", "2026-10-19T10:00:00Z"), {"reason": "blocked"})

    def test_final_review_has_no_invented_remaining_time(self):
        result = estimate([{"status": "completed", "elapsedSeconds": 100}], "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result, {"reason": "final_review"})

    def test_overrun_is_not_zero_remaining(self):
        tasks = [{"status": "completed", "elapsedSeconds": 100}, {"status": "completed", "elapsedSeconds": 100},
                 {"status": "running", "wave": 2, "elapsedSeconds": 300}]
        self.assertGreater(estimate(tasks, "running", "2026-10-19T10:00:00Z")["lowerSeconds"], 60)

    def test_seven_display_steps_are_grounded_in_actual_state(self):
        state = {"requirements": [], "sealed": False, "spec": None, "stage": "brief"}
        gates = {"coverage": False, "plan": False}
        self.assertEqual(display_stage(state, gates), "brief")
        state["requirements"] = [{"id": "R01"}]
        self.assertEqual(display_stage(state, gates), "requirements")
        state["sealed"] = True; state["stage"] = "spec"
        self.assertEqual(display_stage(state, gates), "spec")
        state["spec"] = {"file": "spec.md"}
        self.assertEqual(display_stage(state, gates), "coverage")
        gates["coverage"] = True
        self.assertEqual(display_stage(state, gates), "plan")
        gates["plan"] = True; state["stage"] = "build"
        self.assertEqual(display_stage(state, gates), "build")
        state["stage"] = "acceptance"
        self.assertEqual(display_stage(state, gates), "acceptance")
