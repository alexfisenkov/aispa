from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import work_activity

T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc)


def iso(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


class CountedWorkTests(unittest.TestCase):
    def test_cap_is_forty_five_minutes(self):
        self.assertEqual(work_activity.IDLE_CAP_SECONDS, 2700)

    def test_short_gap_is_all_work(self):
        self.assertEqual(work_activity.counted_seconds(iso(0), iso(1200), None), 1200)

    def test_long_gap_without_file_changes_counts_at_most_the_cap(self):
        self.assertEqual(work_activity.counted_seconds(iso(0), iso(3 * 3600), None), 2700)

    def test_long_gap_with_changes_counts_up_to_the_last_change_plus_the_cap(self):
        self.assertEqual(work_activity.counted_seconds(iso(0), iso(5 * 3600), iso(3600)), 3600 + 2700)

    def test_a_change_near_the_end_never_counts_beyond_the_gap(self):
        self.assertEqual(work_activity.counted_seconds(iso(0), iso(3600), iso(3500)), 3600)

    def test_a_change_before_the_gap_is_not_evidence_of_work_inside_it(self):
        self.assertEqual(work_activity.counted_seconds(iso(1000), iso(1000 + 3 * 3600), iso(500)), 2700)


class RecordGapTests(unittest.TestCase):
    def test_long_gap_is_recorded_with_the_probe_answer(self):
        state = {}
        calls = []
        def probe(since, until):
            calls.append((since, until))
            return iso(1800)
        self.assertTrue(work_activity.record_gap(state, iso(0), iso(4 * 3600), probe))
        self.assertEqual(state["workActivity"], [{"gapFrom": iso(0), "gapTo": iso(4 * 3600), "lastChangeAt": iso(1800)}])
        self.assertEqual(calls, [(iso(0), iso(4 * 3600))])

    def test_no_changes_is_recorded_as_null(self):
        state = {}
        self.assertTrue(work_activity.record_gap(state, iso(0), iso(4 * 3600), lambda a, b: None))
        self.assertIsNone(state["workActivity"][0]["lastChangeAt"])

    def test_short_gap_is_not_recorded_and_the_probe_is_not_asked(self):
        state = {}
        def probe(since, until):
            raise AssertionError("no probe for a short gap")
        self.assertFalse(work_activity.record_gap(state, iso(0), iso(2700), probe))
        self.assertNotIn("workActivity", state)

    def test_the_same_gap_is_recorded_once(self):
        state = {}
        work_activity.record_gap(state, iso(0), iso(4 * 3600), lambda a, b: None)
        self.assertFalse(work_activity.record_gap(state, iso(0), iso(5 * 3600), lambda a, b: None))
        self.assertEqual(len(state["workActivity"]), 1)

    def test_an_unanswerable_probe_does_not_turn_the_gap_into_idle(self):
        state = {}
        def broken(since, until):
            raise OSError("no git")
        self.assertFalse(work_activity.record_gap(state, iso(0), iso(4 * 3600), broken))
        self.assertNotIn("workActivity", state)


class IdleTests(unittest.TestCase):
    def test_idle_is_the_uncounted_tail_of_each_gap(self):
        state = {"workActivity": [{"gapFrom": iso(0), "gapTo": iso(3 * 3600), "lastChangeAt": None},
                                  {"gapFrom": iso(4 * 3600), "gapTo": iso(9 * 3600), "lastChangeAt": iso(4 * 3600 + 3600)}]}
        self.assertEqual(work_activity.idle_seconds(state, iso(10 * 3600)), (3 * 3600 - 2700) + (5 * 3600 - 3600 - 2700))

    def test_segments_start_where_counting_stopped(self):
        state = {"workActivity": [{"gapFrom": iso(0), "gapTo": iso(3 * 3600), "lastChangeAt": None}]}
        start, end = work_activity.uncounted_segments(state, iso(10 * 3600))[0]
        self.assertEqual((start, end), (T0 + timedelta(seconds=2700), T0 + timedelta(seconds=3 * 3600)))

    def test_open_gap_is_counted_with_a_probe_and_not_without_one(self):
        state = {"status": "running", "updatedAt": iso(0)}
        at = iso(3 * 3600)
        self.assertEqual(work_activity.idle_seconds(state, at), 0)
        self.assertEqual(work_activity.idle_seconds(state, at, lambda a, b: None), 3 * 3600 - 2700)
        self.assertEqual(work_activity.idle_seconds(state, at, lambda a, b: iso(3600)), 3 * 3600 - 3600 - 2700)

    def test_open_gap_is_not_idle_while_the_run_is_stopped_or_waiting(self):
        for extra in ({"status": "paused"}, {"waiting": {"kind": "owner_question", "since": iso(0)}}):
            with self.subTest(extra=extra):
                state = {"status": "running", "updatedAt": iso(0), **extra}
                self.assertEqual(work_activity.idle_seconds(state, iso(3 * 3600), lambda a, b: None), 0)

    def test_malformed_activity_rows_are_ignored(self):
        state = {"workActivity": [{"gapFrom": "x"}, 5, {"gapFrom": iso(0), "gapTo": iso(-5), "lastChangeAt": None}]}
        self.assertEqual(work_activity.idle_seconds(state, iso(100)), 0)


class ThrottleTests(unittest.TestCase):
    def test_the_open_gap_is_probed_at_most_once_per_five_minutes(self):
        clock = [1000.0]
        calls = []
        def probe(since, until):
            calls.append(until)
            return None
        throttled = work_activity.ThrottledProbe(probe, clock=lambda: clock[0])
        throttled(iso(0), iso(100))
        clock[0] += 299
        throttled(iso(0), iso(200))
        self.assertEqual(len(calls), 1)
        clock[0] += 2
        throttled(iso(0), iso(300))
        self.assertEqual(len(calls), 2)


class ProjectChangeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        (self.root / "src").mkdir()
        (self.root / ".aispa").mkdir()

    def touch(self, name, seconds):
        target = self.root / name
        target.write_text("x")
        stamp = (T0 + timedelta(seconds=seconds)).timestamp()
        os.utime(target, (stamp, stamp))

    def test_newest_project_file_inside_the_window_wins_and_aispa_is_ignored(self):
        self.touch("src/a.py", 1000)
        self.touch("src/b.py", 2000)
        self.touch(".aispa/journal.txt", 5000)
        found = work_activity.latest_change(self.root, iso(0), iso(9000))
        self.assertEqual(datetime.fromisoformat(found), T0 + timedelta(seconds=2000))

    def test_changes_before_the_window_are_not_reported(self):
        self.touch("src/a.py", 100)
        self.assertIsNone(work_activity.latest_change(self.root, iso(500), iso(9000)))

    def test_ignored_files_do_not_count_as_work(self):
        (self.root / ".gitignore").write_text("build/\n")
        os.utime(self.root / ".gitignore", (T0.timestamp() - 10, T0.timestamp() - 10))
        (self.root / "build").mkdir()
        self.touch("build/out.bin", 3000)
        self.assertIsNone(work_activity.latest_change(self.root, iso(0), iso(9000)))

    def test_missing_git_raises_so_the_caller_can_skip(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(OSError):
                work_activity.latest_change(plain, iso(0), iso(9000))


if __name__ == "__main__":
    unittest.main()
