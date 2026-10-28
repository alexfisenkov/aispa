"""Remaining time from measurements only: run samples, then calibration, then "not known yet"."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import calibration, measured_estimate, progress, work_activity
from aispa_core.store import Journal
from aispa_core.workflow import Workflow

T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc)
KINDS = ["tasks", "branchReview", "finalRepair", "blind"]


def iso(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def task(tid, status="pending", tier="light", elapsed=None, wave=1, deps=(), **extra):
    row = {"id": tid, "status": status, "tier": tier, "wave": wave, "dependsOn": list(deps)}
    if elapsed is not None:
        row["elapsedSeconds"] = elapsed
        row["startedAt"] = iso(0)
        if status == "completed":
            row["finishedAt"] = iso(elapsed)
    row.update(extra)
    return row


def run(tasks, **extra):
    state = {"status": "running", "policy": {"measuredTiming": True}, "runtime": "claude-code",
             "startedAt": iso(0), "elapsedSeconds": 0.0, "tasks": tasks, "updatedAt": iso(0)}
    state.update(extra)
    return state


def cal(klass, seconds, kind="task", runtime="claude-code"):
    return {"kind": kind, "class": klass, "seconds": seconds, "runtime": runtime, "recordedAt": iso(0)}


def rows(result):
    return {row["kind"]: row for row in result["rows"]}


def stages_done():
    return {"stageSamples": [
        {"key": "a", "stage": "branchReview", "seconds": 60.0, "finishedAt": iso(1)},
        {"key": "b", "stage": "blind", "seconds": 60.0, "finishedAt": iso(2)}]}


class IntervalRuleTests(unittest.TestCase):
    def test_one_sample_is_a_single_value(self):
        self.assertEqual(measured_estimate.interval([90]), (90, 90))

    def test_two_to_four_samples_use_min_and_max(self):
        self.assertEqual(measured_estimate.interval([30, 10, 20]), (10, 30))
        self.assertEqual(measured_estimate.interval([5, 100, 7, 9]), (5, 100))

    def test_five_and_more_use_the_25th_to_80th_percentile_by_linear_interpolation(self):
        self.assertEqual(measured_estimate.interval([10, 20, 30, 40, 50]), (20, 42))
        self.assertEqual(measured_estimate.interval([60, 10, 20, 30, 40, 50]), (22.5, 50))


class EstimateShapeTests(unittest.TestCase):
    def test_without_any_measurement_it_says_not_known_and_every_row_has_no_numbers(self):
        result = measured_estimate.estimate(run([task("T1")]), iso(10), calibration=[])
        self.assertEqual(result["reason"], "insufficient_data")
        self.assertIsNone(result["lowerSeconds"])
        self.assertIsNone(result["upperSeconds"])
        self.assertEqual([row["kind"] for row in result["rows"]], KINDS)
        for row in result["rows"]:
            self.assertIsNone(row["lowerSeconds"], row)
            self.assertIsNone(row["upperSeconds"], row)
            self.assertEqual(row["sampleCount"], 0)

    def test_duration_constants_are_never_read(self):
        state = run([task("T1", "completed", "heavy", 600), task("T2", "pending", "heavy"), task("T3", "pending", "light")])
        before = measured_estimate.estimate(copy.deepcopy(state), iso(10), calibration=[cal("light", 100)])
        crazy_plan = {tier: 10 ** 7 for tier in progress.PLAN_SECONDS}
        crazy_phase = {phase: 99.0 for phase in progress.PHASE_REMAINING}
        with patch.dict(progress.PLAN_SECONDS, crazy_plan), patch.dict(progress.PHASE_REMAINING, crazy_phase):
            after = measured_estimate.estimate(copy.deepcopy(state), iso(10), calibration=[cal("light", 100)])
        self.assertEqual(before, after)
        self.assertNotIn(str(10 ** 7), json.dumps(after))

    def test_status_reasons_stay_and_no_plan_is_reported(self):
        for status in ("paused", "blocked", "accepted", "delivered_with_remaining", "cancelled"):
            with self.subTest(status=status):
                reason = measured_estimate.estimate(run([task("T1")], status=status), iso(1), calibration=[])["reason"]
                self.assertEqual(reason, "finished" if status.startswith(("accepted", "delivered")) else status)
        self.assertEqual(measured_estimate.estimate(run([]), iso(1), calibration=[])["reason"], "no_plan")

    def test_finished_tasks_and_stages_leave_nothing_to_wait_for(self):
        state = run([task("T1", "completed", "light", 100)], **stages_done())
        self.assertEqual(measured_estimate.estimate(state, iso(500), calibration=[])["reason"], "final_review")


class TaskRangeTests(unittest.TestCase):
    def pending_light(self, samples, extra=()):
        tasks = [task("S%d" % i, "completed", "light", value) for i, value in enumerate(samples)]
        return run([*tasks, task("N1"), *extra], **stages_done())

    def test_run_samples_give_a_range_and_a_sample_count(self):
        result = measured_estimate.estimate(self.pending_light([100, 200]), iso(5000), calibration=[])
        row = rows(result)["tasks"]
        self.assertEqual((row["lowerSeconds"], row["upperSeconds"], row["sampleCount"], row["basis"]), (100, 200, 2, "run"))
        self.assertEqual((result["reason"], result["basis"]), ("estimate", "run"))

    def test_single_sample_keeps_its_count_of_one(self):
        row = rows(measured_estimate.estimate(self.pending_light([240]), iso(5000), calibration=[]))["tasks"]
        self.assertEqual((row["lowerSeconds"], row["upperSeconds"], row["sampleCount"]), (240, 240, 1))

    def test_lower_is_the_critical_path_of_lows_and_upper_is_the_sum_of_highs(self):
        extra = [task("N2"), task("N3", deps=["N1"])]
        state = self.pending_light([100, 200], extra)
        row = rows(measured_estimate.estimate(state, iso(5000), calibration=[]))["tasks"]
        self.assertEqual(row["lowerSeconds"], 200)   # N1 -> N3 on lows; N2 runs beside
        self.assertEqual(row["upperSeconds"], 600)   # three outstanding tasks, 200 each

    def test_waves_are_sequential(self):
        state = self.pending_light([100, 200], [task("N2", wave=2)])
        row = rows(measured_estimate.estimate(state, iso(5000), calibration=[]))["tasks"]
        self.assertEqual(row["lowerSeconds"], 200)
        state["tasks"][-1]["wave"] = 1
        row = rows(measured_estimate.estimate(state, iso(5000), calibration=[]))["tasks"]
        self.assertEqual(row["lowerSeconds"], 100)

    def test_a_cycle_is_an_invalid_plan(self):
        state = self.pending_light([100], [task("N2", deps=["N3"]), task("N3", deps=["N2"])])
        self.assertEqual(measured_estimate.estimate(state, iso(5000), calibration=[])["reason"], "invalid_plan")

    def test_a_running_task_only_owes_what_is_left_of_its_range(self):
        state = self.pending_light([100, 300])
        state["tasks"][-1].update(status="running", elapsedSeconds=150, startedAt=iso(0))
        row = rows(measured_estimate.estimate(state, iso(5000), calibration=[]))["tasks"]
        self.assertEqual((row["lowerSeconds"], row["upperSeconds"]), (0, 150))

    def test_class_follows_tier_and_critic(self):
        samples = [task("A", "completed", "heavy", 1000, completionKind="critic_acceptance"),
                   task("B", "completed", "heavy", 400, completionKind="verified_completion"),
                   task("C", "completed", "standard", 200), task("D", "completed", "fast", 50)]
        state = run(samples, **stages_done())
        classes = {tid: measured_estimate.task_class(state, t) for tid, t in zip("ABCD", samples)}
        self.assertEqual(classes, {"A": "heavy+critic", "B": "heavy", "C": "standard", "D": "light"})

    def test_other_classes_do_not_calibrate_a_class(self):
        state = run([task("A", "completed", "light", 5), task("H", "pending", "heavy")], **stages_done())
        result = measured_estimate.estimate(state, iso(100), calibration=[])
        self.assertEqual(result["reason"], "insufficient_data")
        self.assertEqual(rows(result)["tasks"]["sampleCount"], 0)

    def test_class_is_the_engine_weight_of_the_attempt_the_panel_shows(self):
        # Blind acceptance: the plan said heavy, the engine weighed the attempt standard
        # (balanced, no critic) and the panel shows standard — the estimate must agree.
        attempt = {"tier": "standard", "plannedTier": "heavy", "criticRoles": ["worker"], "rounds": []}
        state = run([task("T02", "running", "heavy", 3)], taskHistory={"T02": [attempt]})
        self.assertEqual(measured_estimate.task_class(state, state["tasks"][0]), "standard")
        promoted = {"tier": "heavy", "criticRoles": ["worker", "critic1", "critic2"], "rounds": []}
        state = run([task("T03", "pending", "standard")], taskHistory={"T03": [promoted]})
        self.assertEqual(measured_estimate.task_class(state, state["tasks"][0]), "heavy+critic")

    def test_finished_tasks_of_another_class_are_counted_so_the_panel_does_not_wait_for_a_first_one(self):
        state = run([task("T01", "completed", "heavy", 9, completionKind="critic_acceptance"),
                     task("T02", "running", "heavy", 3)],
                    taskHistory={"T01": [{"tier": "heavy", "criticRoles": ["worker", "critic1", "critic2"]}],
                                 "T02": [{"tier": "standard", "plannedTier": "heavy", "criticRoles": ["worker"]}]})
        result = measured_estimate.estimate(state, iso(10), calibration=[])
        self.assertEqual(result["reason"], "insufficient_data")
        self.assertEqual(rows(result)["tasks"]["sampleCount"], 0)
        self.assertEqual(result["completedTasks"], 1)
        self.assertEqual(result["unmeasuredTasks"], ["T02"])
        empty = measured_estimate.estimate(run([task("T1")]), iso(10), calibration=[])
        self.assertEqual(empty["completedTasks"], 0)

    def test_a_class_without_data_leaves_the_upper_bound_open_and_is_listed(self):
        state = run([task("A", "completed", "light", 100), task("N", "pending", "light"), task("H", "pending", "heavy")],
                    **stages_done())
        result = measured_estimate.estimate(state, iso(100), calibration=[])
        self.assertEqual(result["reason"], "estimate")
        self.assertEqual(result["lowerSeconds"], 100)
        self.assertIsNone(result["upperSeconds"])
        self.assertEqual(result["unmeasuredTasks"], ["H"])
        self.assertTrue(result["notes"])


class OverrunTests(unittest.TestCase):
    def test_a_task_beyond_the_top_of_its_range_is_flagged_and_adds_nothing(self):
        done = [task("A", "completed", "light", 100), task("B", "completed", "light", 200)]
        slow = task("SLOW", "running", "light", 5000)
        state = run([*done, slow, task("N")], **stages_done())
        result = measured_estimate.estimate(state, iso(9000), calibration=[])
        self.assertEqual(result["overrunning"], ["SLOW"])
        row = rows(result)["tasks"]
        self.assertEqual((row["lowerSeconds"], row["upperSeconds"]), (100, 200))
        self.assertTrue(result["afterOverrun"])

    def test_no_overrun_inside_the_range(self):
        done = [task("A", "completed", "light", 100), task("B", "completed", "light", 200)]
        state = run([*done, task("R", "running", "light", 150)], **stages_done())
        self.assertEqual(measured_estimate.estimate(state, iso(9000), calibration=[])["overrunning"], [])


class CalibrationBasisTests(unittest.TestCase):
    def test_calibration_serves_until_the_run_has_its_own_samples(self):
        state = run([task("N")], **stages_done())
        result = measured_estimate.estimate(state, iso(5), calibration=[cal("light", 300), cal("light", 500)])
        row = rows(result)["tasks"]
        self.assertEqual((row["basis"], row["lowerSeconds"], row["upperSeconds"], row["sampleCount"]), ("calibration", 300, 500, 2))
        self.assertEqual(result["basis"], "calibration")

    def test_run_samples_replace_calibration_for_their_class(self):
        state = run([task("A", "completed", "light", 100), task("N")], **stages_done())
        row = rows(measured_estimate.estimate(state, iso(5), calibration=[cal("light", 9000)]))["tasks"]
        self.assertEqual((row["basis"], row["upperSeconds"]), ("run", 100))

    def test_mixed_when_classes_rest_on_different_sources(self):
        state = run([task("A", "completed", "light", 100), task("N"), task("H", tier="heavy")], **stages_done())
        result = measured_estimate.estimate(state, iso(5), calibration=[cal("heavy+critic", 4000), cal("heavy", 3000)])
        self.assertEqual(rows(result)["tasks"]["basis"], "mixed")
        self.assertEqual(result["basis"], "mixed")

    def test_the_same_runtime_is_preferred(self):
        state = run([task("N")], **stages_done())
        result = measured_estimate.estimate(state, iso(5), calibration=[cal("light", 100, runtime="codex"),
                                                                       cal("light", 700, runtime="claude-code")])
        self.assertEqual(rows(result)["tasks"]["upperSeconds"], 700)
        state["runtime"] = "gemini"
        self.assertEqual(rows(measured_estimate.estimate(state, iso(5), calibration=[
            cal("light", 100, runtime="codex")]))["tasks"]["upperSeconds"], 100)

    def test_calibration_warnings_reach_the_notes(self):
        state = run([task("N")])
        result = measured_estimate.estimate(state, iso(5), calibration={"samples": [], "notes": ["файл повреждён"]})
        self.assertIn("файл повреждён", result["notes"])
        self.assertEqual(result["reason"], "insufficient_data")


class StageRowTests(unittest.TestCase):
    def test_stages_without_samples_have_numberless_rows_and_the_total_has_no_upper_bound(self):
        state = run([task("A", "completed", "light", 100), task("N")])
        result = measured_estimate.estimate(state, iso(5), calibration=[])
        by_kind = rows(result)
        self.assertIsNone(by_kind["branchReview"]["lowerSeconds"])
        self.assertIsNone(by_kind["blind"]["upperSeconds"])
        self.assertEqual(result["reason"], "estimate")
        self.assertIsNone(result["upperSeconds"])

    def test_stage_samples_from_the_run_or_calibration_are_added_to_the_total(self):
        state = run([task("A", "completed", "light", 100), task("N")],
                    stageSamples=[{"key": "x", "stage": "branchReview", "seconds": 600.0, "finishedAt": iso(1)}])
        result = measured_estimate.estimate(state, iso(5), calibration=[cal("blind", 900, kind="stage")])
        by_kind = rows(result)
        self.assertEqual((by_kind["branchReview"]["status"], by_kind["branchReview"]["basis"]), ("done", "run"))
        self.assertEqual((by_kind["blind"]["status"], by_kind["blind"]["basis"], by_kind["blind"]["upperSeconds"]),
                         ("pending", "calibration", 900))
        self.assertEqual((result["lowerSeconds"], result["upperSeconds"]), (100 + 900, 100 + 900))

    def test_a_running_stage_owes_only_what_is_left(self):
        progress_row = {"phase": "blind", "startedAt": iso(0), "activeSince": iso(0), "elapsedSeconds": 0.0}
        state = run([task("A", "completed", "light", 100)], workProgress=progress_row,
                    stageSamples=[stages_done()["stageSamples"][0]])
        result = measured_estimate.estimate(state, iso(400), calibration=[cal("blind", 1000, kind="stage")])
        blind = rows(result)["blind"]
        self.assertEqual((blind["status"], blind["lowerSeconds"], blind["upperSeconds"]), ("running", 600, 600))

    def test_a_final_repair_only_counts_while_one_is_open(self):
        state = run([task("A", "completed", "light", 100)], **stages_done())
        samples = [cal("finalRepair", 500, kind="stage")]
        quiet = measured_estimate.estimate(state, iso(5), calibration=samples)
        self.assertEqual(rows(quiet)["finalRepair"]["status"], "conditional")
        self.assertEqual(quiet["reason"], "final_review")
        state["finalRepairs"] = [{"id": "F01", "status": "assigned"}]
        state["stageClocks"] = {"finalRepair:F01": {"startedAt": iso(0)}}
        busy = measured_estimate.estimate(state, iso(200), calibration=samples)
        self.assertEqual((rows(busy)["finalRepair"]["status"], busy["lowerSeconds"]), ("running", 300))


class WorkSplitTests(unittest.TestCase):
    def test_work_idle_and_owner_wait_are_separate_numbers(self):
        state = run([task("N")], elapsedSeconds=0.0, activeSince=iso(0), startedAt=iso(0),
                    waitTiming={"owner_question": {"startedAt": iso(0), "elapsedSeconds": 1200.0}},
                    workActivity=[{"gapFrom": iso(0), "gapTo": iso(3 * 3600), "lastChangeAt": None}])
        result = measured_estimate.estimate(state, iso(4 * 3600), calibration=[])
        work = result["work"]
        self.assertEqual(work["ownerWaitSeconds"], 1200)
        self.assertEqual(work["idleSeconds"], 3 * 3600 - 2700)
        self.assertEqual(work["workSeconds"], 4 * 3600 - (3 * 3600 - 2700))
        self.assertEqual(measured_estimate.work_seconds(state, iso(4 * 3600)), work["workSeconds"])

    def test_idle_inside_a_task_is_taken_out_of_its_sample(self):
        long_task = task("A", "completed", "light", 4 * 3600)
        quick = task("B", "completed", "light", 600)
        state = run([long_task, quick, task("N")], **stages_done(),
                    workActivity=[{"gapFrom": iso(0), "gapTo": iso(3 * 3600), "lastChangeAt": None}])
        row = rows(measured_estimate.estimate(state, iso(5 * 3600), calibration=[]))["tasks"]
        self.assertEqual((row["lowerSeconds"], row["upperSeconds"]), (600, 4 * 3600 - (3 * 3600 - 2700)))

    def test_an_open_gap_uses_the_probe_only_when_given(self):
        state = run([task("N")], activeSince=iso(0))
        seen = []
        def probe(since, until):
            seen.append((since, until))
            return None
        quiet = measured_estimate.estimate(state, iso(3 * 3600), calibration=[])
        self.assertEqual(quiet["work"]["idleSeconds"], 0)
        probed = measured_estimate.estimate(state, iso(3 * 3600), calibration=[], idle_probe=probe)
        self.assertEqual(probed["work"]["idleSeconds"], 3 * 3600 - 2700)
        self.assertEqual(seen, [(iso(0), iso(3 * 3600))])


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.calibration = Path(self.dir.name) / "calibration.json"
        patcher = patch.dict(os.environ, {"AISPA_CALIBRATION_FILE": str(self.calibration)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.probe_answer = None
        self.store = type("S", (), {"root": Path(self.dir.name)})()

    def tracker(self, state):
        return measured_estimate.Tracker(state, probe=lambda since, until: self.probe_answer)

    def test_a_long_silence_since_the_last_event_is_marked_once(self):
        state = run([task("N")])
        tracker = self.tracker(state)
        self.probe_answer = iso(1000)
        tracker.prepare(self.store, state, iso(3 * 3600))
        tracker.prepare(self.store, state, iso(3 * 3600))
        self.assertEqual(state["workActivity"], [{"gapFrom": iso(0), "gapTo": iso(3 * 3600), "lastChangeAt": iso(1000)}])

    def test_a_silence_while_the_run_was_waiting_is_not_idle_work(self):
        state = run([task("N")], waiting={"kind": "owner_question", "since": iso(0)})
        tracker = self.tracker(state)
        tracker.prepare(self.store, state, iso(3 * 3600))
        self.assertNotIn("workActivity", state)

    def test_a_finished_task_is_recorded_for_calibration_exactly_once(self):
        state = run([task("A", "running", "heavy", startedAt=iso(0))])
        tracker = self.tracker(state)
        self.assertEqual(tracker.prepare(self.store, state, iso(10)), [])
        state["tasks"][0].update(status="completed", elapsedSeconds=900.0, finishedAt=iso(900),
                                 completionKind="critic_acceptance")
        pending = tracker.prepare(self.store, state, iso(901))
        self.assertEqual([(s["kind"], s["class"], s["seconds"], s["runtime"]) for s in pending],
                         [("task", "heavy+critic", 900.0, "claude-code")])
        tracker.commit(pending)
        self.assertEqual(tracker.prepare(self.store, state, iso(902)), [])
        self.assertEqual(len(calibration.load()), 1)

    def test_fixture_sized_durations_never_reach_the_calibration_file(self):
        state = run([task("A", "running", "light", startedAt=iso(0))])
        tracker = self.tracker(state)
        state["tasks"][0].update(status="completed", elapsedSeconds=0.2, finishedAt=iso(1))
        self.assertEqual(tracker.prepare(self.store, state, iso(1)), [])

    def test_tasks_finished_before_the_tracker_started_are_not_recorded_again(self):
        state = run([task("A", "completed", "light", 100)])
        self.assertEqual(self.tracker(state).prepare(self.store, state, iso(200)), [])

    def test_a_finished_progress_interval_becomes_one_stage_sample(self):
        state = run([task("A", "completed", "light", 100)])
        tracker = self.tracker(state)
        state["workProgress"] = {"phase": "integration", "startedAt": iso(1000), "elapsedSeconds": 420.0,
                                 "finishedAt": iso(1420)}
        pending = tracker.prepare(self.store, state, iso(1500))
        self.assertEqual([(s["kind"], s["class"], s["seconds"]) for s in pending], [("stage", "branchReview", 420.0)])
        self.assertEqual([(s["stage"], s["seconds"]) for s in state["stageSamples"]], [("branchReview", 420.0)])
        self.assertEqual(tracker.prepare(self.store, state, iso(1600)), [])
        self.assertEqual(len(state["stageSamples"]), 1)

    def test_non_stage_phases_are_not_samples(self):
        state = run([])
        tracker = self.tracker(state)
        state["workProgress"] = {"phase": "spec", "startedAt": iso(0), "elapsedSeconds": 60.0, "finishedAt": iso(60)}
        self.assertEqual(tracker.prepare(self.store, state, iso(100)), [])
        self.assertNotIn("stageSamples", state)

    def test_a_final_repair_is_timed_from_first_sight_to_its_end(self):
        state = run([task("A", "completed", "light", 100)])
        tracker = self.tracker(state)
        state["finalRepairs"] = [{"id": "F01", "status": "assigned"}]
        self.assertEqual(tracker.prepare(self.store, state, iso(100)), [])
        state["finalRepairs"][0]["status"] = "passed"
        pending = tracker.prepare(self.store, state, iso(700))
        self.assertEqual([(s["class"], s["seconds"]) for s in pending], [("finalRepair", 600.0)])
        self.assertEqual(tracker.prepare(self.store, state, iso(800)), [])

    def test_probe_failure_never_raises(self):
        state = run([task("N")])
        def broken(since, until):
            raise OSError("no git")
        measured_estimate.Tracker(state, probe=broken).prepare(self.store, state, iso(3 * 3600))
        self.assertNotIn("workActivity", state)


class ForStateTests(unittest.TestCase):
    def test_it_reads_this_computers_calibration_and_reports_a_broken_file_as_a_note(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "calibration.json"
            store = type("S", (), {"root": Path(folder)})()
            state = run([task("N")], **stages_done())
            with patch.dict(os.environ, {"AISPA_CALIBRATION_FILE": str(target)}):
                self.assertTrue(calibration.record(cal("light", 300)))
                good = measured_estimate.for_state(store, state, iso(5))
                self.assertEqual((good["reason"], good["basis"]), ("estimate", "calibration"))
                target.write_text("{broken")
                bad = measured_estimate.for_state(store, state, iso(5))
                self.assertEqual(bad["reason"], "insufficient_data")
                self.assertTrue(bad["notes"])


class WorkflowSaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {"AISPA_CALIBRATION_FILE": str(Path(self.dir.name) / "cal" / "calibration.json")})
        env.start()
        self.addCleanup(env.stop)
        self.now = [iso(0)]
        for target in ("aispa_core.workflow.timestamp", "aispa_core.store.timestamp"):
            clock = patch(target, side_effect=lambda: self.now[0])
            clock.start()
            self.addCleanup(clock.stop)
        self.store = Journal(self.dir.name)
        self.state = self.store.create("honest", "Honest time", lambda s: s.update(policy={"measuredTiming": True}, runtime="codex"))
        self.flow = Workflow(self.store, self.state)

    def finish_task(self, at, elapsed):
        self.state["tasks"].append(task("A", "completed", "light", elapsed))
        self.now[0] = at
        self.flow.save("task-verified-completed:A", at)

    def test_completion_writes_one_calibration_sample_even_if_saved_again(self):
        self.finish_task(iso(500), 480.0)
        self.now[0] = iso(600)
        self.flow.save("agent-decision-recorded", iso(600))
        rows_ = calibration.load()
        self.assertEqual([(r["kind"], r["class"], r["seconds"], r["runtime"]) for r in rows_],
                         [("task", "light", 480.0, "codex")])

    def test_runs_without_the_flag_write_nothing(self):
        plain = Journal(tempfile.mkdtemp(dir=self.dir.name))
        state = plain.create("plain", "Plain", lambda s: s.update(policy={}))
        flow = Workflow(plain, state)
        state["tasks"].append(task("A", "completed", "light", 100))
        flow.save("task-verified-completed:A", iso(100))
        self.assertEqual(calibration.load(), [])
        self.assertNotIn("workActivity", state)

    def test_a_long_silence_between_saves_is_marked_in_the_state(self):
        with patch.object(work_activity, "latest_change", return_value=None):
            self.now[0] = iso(4 * 3600)
            self.flow.save("agent-decision-recorded", iso(4 * 3600))
        self.assertEqual(self.store.load()["workActivity"][0]["gapFrom"], iso(0))

    def test_a_broken_calibration_file_does_not_break_saving(self):
        target = Path(os.environ["AISPA_CALIBRATION_FILE"])
        target.parent.mkdir(parents=True)
        target.write_text("{broken")
        self.finish_task(iso(500), 480.0)
        self.assertEqual(self.store.load()["tasks"][0]["id"], "A")
        self.assertEqual(target.read_text(), "{broken")


class WorkflowTempRootTests(unittest.TestCase):
    """Without AISPA_CALIBRATION_FILE, a run living in the system temp dir must not touch ~/.aispa."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.home, self.project, self.elsewhere = base / "home", base / "project", base / "othertmp"
        for folder in (self.home, self.project, self.elsewhere):
            folder.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AISPA_CALIBRATION_FILE", None)
        for target in ("aispa_core.workflow.timestamp", "aispa_core.store.timestamp"):
            clock = patch(target, side_effect=lambda: iso(500))
            clock.start()
            self.addCleanup(clock.stop)
        self.store = Journal(self.project)
        self.state = self.store.create("honest", "Honest time", lambda s: s.update(policy={"measuredTiming": True}, runtime="codex"))
        self.flow = Workflow(self.store, self.state)

    def finish_task(self):
        self.state["tasks"].append(task("A", "completed", "light", 480.0))
        self.flow.save("task-verified-completed:A", iso(500))

    def test_a_run_in_the_temp_dir_writes_nothing_to_the_default_file(self):
        self.finish_task()
        self.assertFalse((self.home / ".aispa").exists())
        self.assertEqual(self.store.load()["tasks"][0]["id"], "A")

    def test_a_run_outside_the_temp_dir_still_writes_the_default_file(self):
        with patch("tempfile.gettempdir", return_value=str(self.elsewhere)):
            self.finish_task()
        rows_ = json.loads((self.home / ".aispa" / "calibration.json").read_text())["samples"]
        self.assertEqual([(r["kind"], r["class"], r["seconds"]) for r in rows_], [("task", "light", 480.0)])


if __name__ == "__main__":
    unittest.main()
