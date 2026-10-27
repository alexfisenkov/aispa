import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / "tools/check.py"
sys.path.insert(0, str(REPO / "skills/aispa/tools"))
from aispa_core import calibration


def sample(**over):
    value = {"kind": "task", "class": "heavy", "seconds": 120.5, "runtime": "claude-code",
             "recordedAt": "2026-10-27T10:00:00+00:00"}
    value.update(over)
    return value


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name) / "nested" / "calibration.json"
        self.old = os.environ.get("AISPA_CALIBRATION_FILE")
        os.environ["AISPA_CALIBRATION_FILE"] = str(self.file)
        self.addCleanup(self.restore)

    def restore(self):
        if self.old is None:
            os.environ.pop("AISPA_CALIBRATION_FILE", None)
        else:
            os.environ["AISPA_CALIBRATION_FILE"] = self.old

    def test_path_prefers_the_environment_variable_else_home(self):
        self.assertEqual(calibration.path(), self.file)
        os.environ.pop("AISPA_CALIBRATION_FILE")
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                self.assertEqual(calibration.path(), Path(home) / ".aispa" / "calibration.json")
            finally:
                os.environ["HOME"] = old

    def test_record_then_load_keeps_only_the_five_allowed_fields(self):
        self.assertTrue(calibration.record(sample(extra="private/path", note="x")))
        rows = calibration.load()
        self.assertEqual(rows, [sample()])
        raw = json.loads(self.file.read_text())
        self.assertEqual(set(raw["samples"][0]), {"kind", "class", "seconds", "runtime", "recordedAt"})

    def test_invalid_samples_are_refused_and_nothing_is_written(self):
        for bad in (sample(seconds=0), sample(seconds=-1), sample(seconds=float("nan")), sample(seconds=True),
                    sample(kind="other"), sample(**{"class": ""}), sample(runtime=3), {"kind": "task"}):
            with self.subTest(bad=bad):
                self.assertFalse(calibration.record(bad))
        self.assertFalse(self.file.exists())

    def test_each_class_keeps_the_latest_two_hundred(self):
        for index in range(205):
            calibration.record(sample(seconds=index + 1))
        calibration.record(sample(**{"class": "light"}))
        rows = calibration.load()
        heavy = [row["seconds"] for row in rows if row["class"] == "heavy"]
        self.assertEqual(len(heavy), 200)
        self.assertEqual((heavy[0], heavy[-1]), (6, 205))
        self.assertEqual(len([row for row in rows if row["class"] == "light"]), 1)

    def test_broken_foreign_or_unreadable_file_is_not_used_and_never_raises(self):
        self.file.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"schema": 99, "samples": [sample()]}), json.dumps([1, 2])):
            with self.subTest(text=text):
                self.file.write_text(text)
                self.assertEqual(calibration.load(), [])
                status = calibration.inspect()
                self.assertEqual(status["samples"], [])
                self.assertEqual(len(status["notes"]), 1)
                self.assertFalse(calibration.record(sample()))
                self.assertEqual(self.file.read_text(), text)
        self.file.write_text(json.dumps({"schema": 1, "samples": [sample()]}))
        os.chmod(self.file, 0)
        try:
            if os.access(self.file, os.R_OK):
                self.skipTest("права файла не ограничивают этого пользователя")
            self.assertEqual(calibration.load(), [])
            self.assertEqual(len(calibration.inspect()["notes"]), 1)
        finally:
            os.chmod(self.file, 0o600)

    def test_missing_file_is_normal_and_has_no_warning(self):
        self.assertEqual(calibration.inspect(), {"samples": [], "notes": []})

    def test_bad_rows_inside_a_good_file_are_skipped_not_fatal(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps({"schema": 1, "samples": [sample(), {"kind": "task"}, sample(seconds=-4)]}))
        self.assertEqual(calibration.load(), [sample()])

    def test_unwritable_location_returns_false(self):
        os.environ["AISPA_CALIBRATION_FILE"] = str(Path(self.dir.name) / "blocker" / "x" / "calibration.json")
        (Path(self.dir.name) / "blocker").write_text("a file, not a directory")
        self.assertFalse(calibration.record(sample()))


class TempRootGuardTests(unittest.TestCase):
    """A run whose project sits in the system temp dir must not write the default ~/.aispa file."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name).resolve()
        self.home, self.temp = self.base / "home", self.base / "systemtmp"
        self.inside, self.outside = self.temp / "project", self.base / "elsewhere" / "project"
        for folder in (self.home, self.inside, self.outside):
            folder.mkdir(parents=True)
        for patch in (mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
                      mock.patch("tempfile.gettempdir", return_value=str(self.temp))):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("AISPA_CALIBRATION_FILE", None)

    def default_file(self):
        return self.home / ".aispa" / "calibration.json"

    def test_root_inside_the_temp_dir_never_writes_the_default_file(self):
        self.assertFalse(calibration.record(sample(), root=self.inside))
        self.assertFalse((self.home / ".aispa").exists())

    def test_root_outside_the_temp_dir_writes_as_before(self):
        self.assertTrue(calibration.record(sample(), root=self.outside))
        self.assertEqual(json.loads(self.default_file().read_text())["samples"], [sample()])

    def test_without_a_root_the_default_file_is_written_as_before(self):
        self.assertTrue(calibration.record(sample()))
        self.assertTrue(self.default_file().exists())

    def test_explicit_file_variable_wins_even_for_a_temp_root(self):
        chosen = self.temp / "chosen.json"
        os.environ["AISPA_CALIBRATION_FILE"] = str(chosen)
        self.assertTrue(calibration.record(sample(), root=self.inside))
        self.assertEqual(json.loads(chosen.read_text())["samples"], [sample()])
        self.assertFalse((self.home / ".aispa").exists())

    def test_symlinks_are_resolved_on_either_side(self):
        link = self.base / "tmplink"
        try:
            os.symlink(self.temp, link, target_is_directory=True)
        except OSError:
            self.skipTest("символические ссылки недоступны")
        with mock.patch("tempfile.gettempdir", return_value=str(link)):
            self.assertFalse(calibration.record(sample(), root=self.inside))
        self.assertFalse(calibration.record(sample(), root=link / "project"))
        self.assertFalse((self.home / ".aispa").exists())


PROBE = '''import os
from pathlib import Path
import tempfile
import unittest


class Probe(unittest.TestCase):
    def test_variable_points_to_a_private_temp_file(self):
        chosen = Path(os.environ["AISPA_CALIBRATION_FILE"]).resolve()
        Path(os.environ["PROBE_REPORT"]).write_text(str(chosen), encoding="utf-8")
        self.assertNotEqual(str(chosen), str(Path(os.environ["PROBE_CALLER_FILE"]).resolve()))
        self.assertTrue(chosen.parent.is_dir())
        self.assertFalse(chosen.exists())
        self.assertTrue(chosen.is_relative_to(Path(tempfile.gettempdir()).resolve()))
'''


class CheckToolCalibrationTests(unittest.TestCase):
    def test_check_gives_its_test_processes_a_private_calibration_file_and_removes_it(self):
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            (root / "tests").mkdir()
            (root / "tests/test_probe.py").write_text(PROBE, encoding="utf-8")
            caller, report = str(root / "caller" / "calibration.json"), root / "report.txt"
            result = subprocess.run(
                [sys.executable, "-B", str(CHECK), "--root", project, "--file", "tests/test_probe.py"],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
                env={**os.environ, "AISPA_CALIBRATION_FILE": caller, "PROBE_CALLER_FILE": caller,
                     "PROBE_REPORT": str(report), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(Path(report.read_text(encoding="utf-8")).parent.exists())


if __name__ == "__main__":
    unittest.main()
