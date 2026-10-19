"""Сквозные проверки консольного интерфейса и сохранности файлов."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studyplan import __main__ as cli


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "задачи с пробелом.csv"
        self.source.write_text(
            "title,minutes,priority\r\nЧтение 🙂,25,2\r\nПрактика,45,3\r\n"
            "Разбор,20,2\r\nПовторение,10,1\r\n", encoding="utf-8", newline="",
        )

    def run_cli(self, *args: str | Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "studyplan", *map(str, args)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_stdout_unicode_json_and_source_unchanged(self) -> None:
        before = self.source.read_bytes()
        result = self.run_cli(self.source, "--budget", "90")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(result.stderr, b"")
        self.assertIn("Чтение 🙂".encode("utf-8"), result.stdout)
        plan = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual([task["title"] for task in plan["planned"]], ["Практика", "Чтение 🙂", "Разбор"])
        self.assertEqual([task["title"] for task in plan["deferred"]], ["Повторение"])
        self.assertEqual((plan["total_minutes"], plan["budget"]), (90, 90))
        self.assertEqual(self.source.read_bytes(), before)

    def test_output_file_matches_stdout_and_replaces_old_result(self) -> None:
        stdout = self.run_cli(self.source, "--budget", "90")
        output = self.directory / "результат с пробелом.json"
        output.write_bytes(b"old result")
        before = self.source.read_bytes()
        saved = self.run_cli(self.source, "--budget", "90", "--output", output)
        self.assertEqual(saved.returncode, 0, saved.stderr.decode("utf-8"))
        self.assertEqual(saved.stdout, b"")
        self.assertEqual(output.read_bytes(), stdout.stdout)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_simulated_windows_stdout_matches_lf_file_bytes(self) -> None:
        def invoke(*args: str | Path) -> tuple[int, bytes]:
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n", write_through=True)
            with patch.object(sys, "stdout", stream):
                code = cli.main(list(map(str, args)))
            stream.flush()
            return code, raw.getvalue()

        before = self.source.read_bytes()
        code, stdout = invoke(self.source, "--budget", "90")
        self.assertEqual(code, 0)
        self.assertIn("Чтение 🙂".encode("utf-8"), stdout)
        self.assertIn(b"\n", stdout)
        self.assertNotIn(b"\r\n", stdout)
        output = self.directory / "windows-output.json"
        output.write_bytes(b"old result")
        saved_code, saved_stdout = invoke(self.source, "--budget", "90", "--output", output)
        self.assertEqual(saved_code, 0)
        self.assertEqual(saved_stdout, b"")
        self.assertEqual(output.read_bytes(), stdout)
        self.assertEqual(self.source.read_bytes(), before)

    def test_invalid_late_row_preserves_existing_and_new_output(self) -> None:
        self.source.write_text("title,minutes,priority\nВерно,5,2\nОшибка,abc,3\n", encoding="utf-8")
        before = self.source.read_bytes()
        existing = self.directory / "existing.json"
        existing.write_bytes(b"existing content")
        new = self.directory / "new.json"
        for output in (existing, new):
            with self.subTest(output=output):
                result = self.run_cli(self.source, "--budget", "90", "--output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn("Строка 3".encode("utf-8"), result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(existing.read_bytes(), b"existing content")
        self.assertFalse(new.exists())

    def test_source_as_output_is_rejected_for_same_path_and_hard_link(self) -> None:
        before = self.source.read_bytes()
        alias = self.directory / "тот же файл.csv"
        alias.hardlink_to(self.source)
        for output in (self.source, alias):
            with self.subTest(output=output):
                result = self.run_cli(self.source, "--budget", "90", "--output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn("исходным CSV".encode("utf-8"), result.stderr)
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(alias.read_bytes(), before)

    def test_missing_source_bad_budget_and_bad_destination(self) -> None:
        output = self.directory / "existing.json"
        output.write_bytes(b"keep")
        cases = (
            (self.directory / "missing.csv", "90", output),
            (self.source, "-1", output),
            (self.source, "1441", output),
            (self.source, "1.5", output),
            (self.source, "1_0", output),
            (self.source, "90", self.directory / "missing parent" / "new.json"),
        )
        source_before = self.source.read_bytes()
        for source, budget, destination in cases:
            with self.subTest(source=source, budget=budget, destination=destination):
                result = self.run_cli(source, "--budget", budget, "--output", destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual(output.read_bytes(), b"keep")
                self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse((self.directory / "missing parent" / "new.json").exists())

    def test_committed_example_and_help(self) -> None:
        result = self.run_cli("examples/tasks.csv", "--budget", "90")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout)["total_minutes"], 90)
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn(b"--output", help_result.stdout)
        self.assertIn(b"--budget", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
