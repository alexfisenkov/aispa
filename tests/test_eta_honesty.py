"""The honesty check replays the estimate over old journals and counts how often the shown range held."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
sys.path.insert(0, str(ROOT / "tools"))
import eta_honesty
from aispa_core import calibration
from aispa_core.store import Journal

BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
SECRET_SLUG = "zvezda-proekt"
SECRET_TITLE = "Secret Client Title"


def stamp(seconds, base=BASE):
    return (base + timedelta(seconds=seconds)).isoformat()


def build_run(root, durations, *, base=BASE, delta_policy=False, finish=True, slug=SECRET_SLUG):
    """A finished run of sequential light tasks, one journal event per completion."""
    now = [stamp(0, base)]
    with patch("aispa_core.store.timestamp", side_effect=lambda: now[0]):
        store = Journal(root)
        policy = {"measuredTiming": True, **({"deltaJournal": True} if delta_policy else {})}
        state = store.create(slug, SECRET_TITLE, lambda s: s.update(policy=policy, runtime="claude-code"))
        state["tasks"] = [{"id": "T%d" % (i + 1), "status": "pending", "tier": "light", "wave": 1, "dependsOn": []}
                          for i in range(len(durations))]
        state["activeSince"], state["elapsedSeconds"] = stamp(0, base), 0.0
        now[0] = stamp(1, base)
        store.append(state, "plan-validated")
        clock = 0
        for index, length in enumerate(durations):
            started, clock = clock, clock + length
            state["tasks"][index].update(status="completed", startedAt=stamp(started, base), finishedAt=stamp(clock, base),
                                         elapsedSeconds=float(length))
            now[0] = stamp(clock, base)
            store.append(state, "task-verified-completed:T%d" % (index + 1))
        if finish:
            state["status"], state["finishedAt"] = "accepted", stamp(clock + 10, base)
            state["elapsedSeconds"], _ = float(clock + 10), state.pop("activeSince")
            now[0] = stamp(clock + 10, base)
            store.append(state, "delivery-recorded")
    return store.run


def snapshot(path):
    return sorted((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in Path(path).rglob("*") if p.is_file())


class HonestyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.calibration = Path(self.dir.name) / "calibration.json"
        patcher = patch.dict(os.environ, {"AISPA_CALIBRATION_FILE": str(self.calibration)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def project(self, name, durations, **kwargs):
        root = Path(self.dir.name) / name
        root.mkdir()
        build_run(root, durations, **kwargs)
        return root

    def test_every_replayed_range_that_held_is_counted_inside(self):
        root = self.project("p1", [100, 100, 100, 100])
        report = eta_honesty.analyse([root])
        self.assertEqual((report["runsUsed"], report["points"], report["ranged"], report["inside"]), (1, 4, 3, 3))
        self.assertEqual(report["share"], 1.0)
        self.assertEqual(report["misses"], [])

    def test_a_missed_range_is_listed_without_names(self):
        root = self.project("p2", [100, 100, 1000, 100])
        report = eta_honesty.analyse([root])
        self.assertEqual((report["ranged"], report["inside"]), (3, 1))
        self.assertAlmostEqual(report["share"], 1 / 3)
        miss = report["misses"][0]
        self.assertEqual((miss["run"], miss["shownLower"], miss["shownUpper"], miss["actual"]), (1, 100, 300, 1200))

    def test_earlier_runs_calibrate_later_ones(self):
        first = self.project("a", [100, 100], base=BASE)
        second = self.project("b", [100, 100], base=BASE + timedelta(days=1))
        report = eta_honesty.analyse([first, second])
        self.assertEqual(report["runsUsed"], 2)
        # run 1: init point has nothing to go on; run 2 starts calibrated from run 1.
        self.assertEqual((report["points"], report["ranged"]), (4, 3))

    def test_it_reads_the_delta_policy_runs_the_same_way(self):
        root = self.project("d", [100, 100, 100, 100], delta_policy=True)
        self.assertEqual(eta_honesty.analyse([root])["ranged"], 3)

    def test_a_runs_directory_a_run_directory_and_a_project_are_all_accepted(self):
        root = self.project("p3", [100, 100])
        run = next((root / ".aispa" / "runs").iterdir())
        for target in (root, root / ".aispa" / "runs", run):
            with self.subTest(target=target.name):
                self.assertEqual(eta_honesty.analyse([target])["runsUsed"], 1)

    def test_unfinished_and_unreadable_runs_are_skipped_and_counted(self):
        done = self.project("p4", [100, 100])
        live = self.project("p5", [100], finish=False)
        broken = self.project("p6", [100, 100])
        journal = next((broken / ".aispa" / "runs").iterdir()) / "events.jsonl"
        journal.write_bytes(journal.read_bytes()[:-30] + b"garbage\n")
        report = eta_honesty.analyse([done, live, broken])
        self.assertEqual((report["runsRead"], report["runsUsed"], report["runsSkipped"]), (3, 1, 2))

    def test_the_report_has_no_project_names_titles_or_paths(self):
        root = self.project("zvezda-folder", [100, 100, 1000, 100])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(eta_honesty.main(["--runs", str(root)]), 0)
        text = out.getvalue()
        for secret in (SECRET_SLUG, SECRET_TITLE, "zvezda-folder", self.dir.name, ".aispa", "T1"):
            self.assertNotIn(secret, text)
        self.assertIn("1", text)

    def test_reading_writes_nothing_anywhere(self):
        root = self.project("p7", [100, 100, 100])
        before = snapshot(self.dir.name)
        with redirect_stdout(io.StringIO()):
            eta_honesty.main(["--runs", str(root)])
        self.assertEqual(snapshot(self.dir.name), before)
        self.assertFalse(self.calibration.exists())

    def test_write_calibration_adds_the_finished_samples_once(self):
        root = self.project("p8", [100, 200, 300])
        for _ in range(2):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(eta_honesty.main(["--runs", str(root), "--write-calibration"]), 0)
        rows = calibration.load()
        self.assertEqual(sorted(r["seconds"] for r in rows), [100.0, 200.0, 300.0])
        self.assertEqual({(r["kind"], r["class"], r["runtime"]) for r in rows}, {("task", "light", "claude-code")})

    def test_no_directories_found_is_a_clear_error(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(eta_honesty.main(["--runs", str(Path(self.dir.name) / "nowhere")]), 2)
        self.assertIn("не найден", out.getvalue())

    def test_the_command_line_runs_as_a_script(self):
        root = self.project("p9", [100, 100, 100])
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "eta_honesty.py"), "--runs", str(root)],
                                capture_output=True, text=True, env={**os.environ})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Точек оценки", result.stdout)


if __name__ == "__main__":
    unittest.main()
