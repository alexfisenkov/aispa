from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import timing
from aispa_core.progress import display_stage, estimate


class TimingTests(unittest.TestCase):
    def test_wait_categories_do_not_turn_into_active_time_on_save(self):
        from aispa_core import work_progress
        begin_wait = getattr(timing, "begin_wait", None)
        end_wait = getattr(timing, "end_wait", None)
        self.assertTrue(callable(begin_wait) and callable(end_wait), "Resource/owner/blocked waiting needs explicit separate intervals")
        state = {"status": "running", "tasks": [], "policy": {"managedExecution": True}}
        timing.start(state, "2026-10-26T00:00:00+00:00")
        work_progress.begin(state, "spec", "2026-10-26T00:00:00+00:00")
        begin_wait(state, "resource_wait", "2026-10-26T00:01:00+00:00")
        work_progress.sync(state, "agent-decision-recorded", "2026-10-26T00:02:00+00:00")
        self.assertNotIn("activeSince", state["workProgress"])
        self.assertEqual(timing.elapsed(state, "2026-10-26T00:03:00+00:00"), 60)
        end_wait(state, "2026-10-26T00:04:00+00:00")
        self.assertEqual(state["waitTiming"]["resource_wait"]["elapsedSeconds"], 180)
        timing.stop(state, "2026-10-26T00:05:00+00:00")
        work_progress.sync(state, "agent-decision-recorded", "2026-10-26T00:05:00+00:00")
        self.assertEqual(state["elapsedSeconds"], 120)
        self.assertEqual(work_progress.view(state, "2026-10-26T00:05:00+00:00")["elapsedSeconds"], 120)

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

    def test_one_measured_task_provides_a_low_confidence_estimate(self):
        tasks = [{"status": "completed", "elapsedSeconds": 100}, {"status": "pending", "wave": 2}]
        result = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result["reason"], "estimate")
        self.assertEqual(result["basis"], "observed_tasks")
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["sampleCount"], 1)

    def test_initial_plan_estimate_is_an_explicit_broad_assumption(self):
        result = estimate([{"status": "pending", "wave": 1, "tier": "heavy"}], "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result["basis"], "plan_assumption")
        self.assertEqual(result["sampleCount"], 0)
        self.assertIsNone(result["medianTaskSeconds"])
        self.assertGreater(result["lowerSeconds"], 0)
        self.assertGreater(result["upperSeconds"], result["lowerSeconds"] * 2)

    def test_null_invalid_and_nonfinite_clocks_remain_unknown(self):
        for value in (None, True, "0", float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                self.assertIsNone(timing.elapsed({"elapsedSeconds": value}, "2026-10-19T10:00:00Z"))
        self.assertEqual(timing.elapsed({"elapsedSeconds": 0}, "2026-10-19T10:00:00Z"), 0)

    def test_two_samples_with_pending_null_never_crash_or_double_count_running_time(self):
        at = "2026-10-19T10:01:00Z"
        common = [{"status": "completed", "elapsedSeconds": 200},
                  {"status": "completed", "elapsedSeconds": 200},
                  {"status": "pending", "wave": 2, "elapsedSeconds": None}]
        raw = {"status": "running", "wave": 2, "elapsedSeconds": 10, "activeSince": "2026-10-19T10:00:00Z"}
        projected = {**raw, "elapsedSeconds": 70}
        self.assertEqual(estimate([*common, raw], "running", at),
                         estimate([*common, projected], "running", at, projected=True))

    def test_phase_progress_refines_the_plan_estimate_and_overrun_stays_positive(self):
        task = {"status": "running", "wave": 1, "tier": "heavy", "elapsedSeconds": 30}
        initial = estimate([task], "running", "2026-10-19T10:00:00Z")
        task.update(phase="task_acceptance", phaseTiming={"elapsedSeconds": 10})
        acceptance = estimate([task], "running", "2026-10-19T10:00:00Z")
        self.assertLess(acceptance["lowerSeconds"], initial["lowerSeconds"])
        task["phaseTiming"]["elapsedSeconds"] = 100000
        self.assertGreater(estimate([task], "running", "2026-10-19T10:00:00Z")["lowerSeconds"], 0)

    def test_same_wave_dependencies_are_not_counted_as_parallel(self):
        tasks = [{"id": "T01", "status": "pending", "wave": 1, "tier": "light", "dependsOn": []},
                 {"id": "T02", "status": "pending", "wave": 1, "tier": "light", "dependsOn": []}]
        parallel = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        tasks[1]["dependsOn"] = ["T01"]
        serial = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        self.assertGreater(serial["lowerSeconds"], parallel["lowerSeconds"])

    def test_estimate_accounts_for_parallel_and_serial_work(self):
        tasks = [{"status": "completed", "elapsedSeconds": 100}, {"status": "completed", "elapsedSeconds": 200},
                 {"status": "pending", "wave": 2}, {"status": "pending", "wave": 2}, {"status": "pending", "wave": 3}]
        result = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result["medianTaskSeconds"], 150)
        self.assertEqual(result["lowerSeconds"], 285)
        self.assertEqual(result["upperSeconds"], 870)

    def test_blocked_task_keeps_a_conditional_work_estimate_without_inventing_wait(self):
        task = {"status": "blocked", "wave": 1, "tier": "heavy"}
        result = estimate([task], "running", "2026-10-19T10:00:00Z")
        available = estimate([{**task, "status": "pending"}], "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result["reason"], "estimate")
        self.assertTrue(result["waitingForBlockers"])
        self.assertFalse(result["waitIncluded"])
        self.assertEqual(result["lowerSeconds"], available["lowerSeconds"])
        self.assertEqual(result["upperSeconds"], available["upperSeconds"])
        self.assertEqual(estimate([task], "blocked", "2026-10-19T10:00:00Z"), {"reason": "blocked"})
        self.assertEqual(estimate([task], "paused", "2026-10-19T10:00:00Z"), {"reason": "paused"})

    def test_quick_light_samples_do_not_calibrate_heavy_work(self):
        tasks = [{"status": "completed", "tier": "light", "elapsedSeconds": 1},
                 {"status": "completed", "tier": "light", "elapsedSeconds": 2},
                 {"status": "pending", "tier": "heavy", "wave": 2}]
        result = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        without_light = estimate(tasks[2:], "running", "2026-10-19T10:00:00Z")
        self.assertEqual(result, without_light)
        self.assertEqual(result["basis"], "plan_assumption")
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["sampleCount"], 0)
        tasks.append({"status": "pending", "tier": "light", "wave": 2})
        mixed = estimate(tasks, "running", "2026-10-19T10:00:00Z")
        self.assertEqual(mixed["basis"], "mixed")
        self.assertEqual(mixed["confidence"], "low")
        self.assertEqual({r["tier"]: r["sampleCount"] for r in mixed["tierEstimates"]}, {"heavy": 0, "light": 2})

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
