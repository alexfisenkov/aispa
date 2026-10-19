"""Чтение задач и детерминированный расчёт учебного плана."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Mapping, Sequence


HEADER = ["title", "minutes", "priority"]
DECIMAL = re.compile(r"[0-9]+\Z")


class DomainError(ValueError):
    """Некорректный CSV, задача или бюджет."""


def _csv_integer(value: str, field: str, row_number: int, lower: int, upper: int) -> int:
    if not DECIMAL.fullmatch(value):
        raise DomainError(f"Строка {row_number}: {field} должно быть целым числом от {lower} до {upper}")
    significant = value.lstrip("0") or "0"
    if len(significant) > len(str(upper)):
        raise DomainError(f"Строка {row_number}: {field} должно быть от {lower} до {upper}")
    number = int(significant)
    if not lower <= number <= upper:
        raise DomainError(f"Строка {row_number}: {field} должно быть от {lower} до {upper}")
    return number


def read_tasks(path: str | Path) -> list[dict[str, str | int]]:
    """Прочитать и целиком проверить CSV с точным заголовком.

    Возвращает задачи лишь после успешной проверки всех строк. Файл не меняется.
    """
    tasks: list[dict[str, str | int]] = []
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise DomainError("CSV пуст: нужен заголовок title,minutes,priority") from None
            if header != HEADER:
                raise DomainError("Строка 1: нужен точный заголовок title,minutes,priority")
            while True:
                row_number = reader.line_num + 1
                try:
                    row = next(reader)
                except StopIteration:
                    break
                if len(row) != 3:
                    raise DomainError(f"Строка {row_number}: ожидаются ровно 3 столбца title,minutes,priority")
                title, minutes_text, priority_text = row
                if not title.strip():
                    raise DomainError(f"Строка {row_number}: title не может быть пустым")
                tasks.append({
                    "title": title,
                    "minutes": _csv_integer(minutes_text, "minutes", row_number, 1, 480),
                    "priority": _csv_integer(priority_text, "priority", row_number, 1, 3),
                })
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DomainError(f"Не удалось прочитать CSV '{path}': {exc}") from exc
    return tasks


def _checked_task(task: Mapping[str, object], position: int) -> dict[str, str | int]:
    if not isinstance(task, Mapping):
        raise DomainError(f"Задача {position}: ожидается объект title,minutes,priority")
    title = task.get("title")
    minutes = task.get("minutes")
    priority = task.get("priority")
    if not isinstance(title, str) or not title.strip():
        raise DomainError(f"Задача {position}: title не может быть пустым")
    if type(minutes) is not int or not 1 <= minutes <= 480:
        raise DomainError(f"Задача {position}: minutes должно быть целым числом от 1 до 480")
    if type(priority) is not int or not 1 <= priority <= 3:
        raise DomainError(f"Задача {position}: priority должно быть целым числом от 1 до 3")
    return {"title": title, "minutes": minutes, "priority": priority}


def plan_tasks(tasks: Sequence[Mapping[str, object]], budget: int) -> dict[str, object]:
    """Выбрать задачи по приоритету с сохранением порядка равных приоритетов.

    Не меняет входные объекты. Непоместившаяся задача не прерывает отбор.
    """
    if type(budget) is not int or not 0 <= budget <= 1440:
        raise DomainError("Бюджет должен быть целым числом от 0 до 1440 минут")
    checked = [_checked_task(task, position) for position, task in enumerate(tasks, start=1)]
    selected = [False] * len(checked)
    planned: list[dict[str, str | int]] = []
    total = 0
    for index in sorted(range(len(checked)), key=lambda item: -int(checked[item]["priority"])):
        task = checked[index]
        minutes = int(task["minutes"])
        if total + minutes <= budget:
            planned.append(task)
            selected[index] = True
            total += minutes
    deferred = [task for index, task in enumerate(checked) if not selected[index]]
    return {"planned": planned, "deferred": deferred, "total_minutes": total, "budget": budget}
