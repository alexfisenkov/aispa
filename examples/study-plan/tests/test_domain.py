"""Поведение CSV-парсера и чистого планировщика."""

import tempfile
import unittest
from pathlib import Path

from studyplan.domain import DomainError, plan_tasks, read_tasks


class ReadTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "задачи.csv"

    def write(self, content: str, *, bom: bool = False) -> None:
        self.path.write_bytes(("\ufeff" if bom else "").encode() + content.encode("utf-8"))

    def test_utf8_bom_boundaries_and_unicode(self) -> None:
        self.write("title,minutes,priority\r\nЧтение 🙂,1,3\r\nПрактика,480,1\r\n", bom=True)
        original = self.path.read_bytes()
        self.assertEqual(read_tasks(self.path), [
            {"title": "Чтение 🙂", "minutes": 1, "priority": 3},
            {"title": "Практика", "minutes": 480, "priority": 1},
        ])
        self.assertEqual(self.path.read_bytes(), original)

    def test_header_only_is_empty_list(self) -> None:
        self.write("title,minutes,priority\n")
        self.assertEqual(read_tasks(self.path), [])

    def test_exact_header_and_column_count(self) -> None:
        for content, line in [
            ("title,priority,minutes\nx,1,2\n", 1),
            ("title,minutes,priority\nx,1\n", 2),
            ("title,minutes,priority\nx,1,2,extra\n", 2),
            ("title,minutes,priority\n\n", 2),
        ]:
            with self.subTest(content=content):
                self.write(content)
                with self.assertRaisesRegex(DomainError, f"Строка {line}"):
                    read_tasks(self.path)

    def test_invalid_rows_fail_without_partial_result(self) -> None:
        for row in ["   ,1,3", "x,0,2", "x,481,2", "x,1.0,2", "x,abc,2", "x," + "9" * 5000 + ",2", "x," + "0" * 5000 + ",2", "x,1,0", "x,1,4", "x,1,3.0"]:
            with self.subTest(row=row):
                self.write(f"title,minutes,priority\nvalid,1,3\n{row}\n")
                with self.assertRaisesRegex(DomainError, "Строка 3"):
                    read_tasks(self.path)

    def test_missing_file_is_domain_error(self) -> None:
        with self.assertRaisesRegex(DomainError, "Не удалось прочитать CSV"):
            read_tasks(self.path)


class PlanTasksTests(unittest.TestCase):
    def test_priority_stability_skip_and_deferred_source_order(self) -> None:
        tasks = [
            {"title": "низкая", "minutes": 5, "priority": 1},
            {"title": "длинная", "minutes": 80, "priority": 3},
            {"title": "я", "minutes": 20, "priority": 3},
            {"title": "а", "minutes": 20, "priority": 3},
            {"title": "средняя", "minutes": 20, "priority": 2},
        ]
        snapshot = [dict(task) for task in tasks]
        first = plan_tasks(tasks, 45)
        self.assertEqual([item["title"] for item in first["planned"]], ["я", "а", "низкая"])
        self.assertEqual([item["title"] for item in first["deferred"]], ["длинная", "средняя"])
        self.assertEqual(first["total_minutes"], 45)
        self.assertEqual(first["budget"], 45)
        self.assertEqual(tasks, snapshot)
        self.assertEqual(first, plan_tasks(tasks, 45))
        self.assertIsNot(first["planned"][0], tasks[2])

    def test_zero_maximum_and_duplicate_tasks(self) -> None:
        tasks = [{"title": "same", "minutes": 480, "priority": 3}] * 4
        zero = plan_tasks(tasks, 0)
        self.assertEqual(zero, {"planned": [], "deferred": tasks, "total_minutes": 0, "budget": 0})
        full = plan_tasks(tasks, 1440)
        self.assertEqual(len(full["planned"]), 3)
        self.assertEqual(len(full["deferred"]), 1)
        self.assertEqual(full["total_minutes"], 1440)

    def test_bad_budget_and_invalid_tasks_rejected(self) -> None:
        for budget in [-1, 1441, 1.0, "30", True]:
            with self.subTest(budget=budget), self.assertRaises(DomainError):
                plan_tasks([], budget)
        for task in [
            {"title": " ", "minutes": 1, "priority": 3},
            {"title": "x", "minutes": True, "priority": 3},
            {"title": "x", "minutes": 1, "priority": 4},
        ]:
            with self.subTest(task=task), self.assertRaises(DomainError):
                plan_tasks([{"title": "valid", "minutes": 1, "priority": 3}, task], 10)
