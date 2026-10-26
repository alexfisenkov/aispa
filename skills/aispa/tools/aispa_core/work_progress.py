"""Host-reported non-task work intervals, never CPU usage or acceptance proof.

Only explicit begin records a start. Reports may end an existing interval;
reads, participant registration and old journals never manufacture one.
The host owns the journal lock and persists changes through Workflow.save.
"""
import math
from datetime import datetime

from . import timing
from .contracts import require


LABELS = {
    "idea": "Разбор идеи", "brief": "Подготовка брифа",
    "discovery": "Изучение проекта", "spec": "Подготовка спецификации",
    "coverage": "Сверка покрытия требований", "plan": "Подготовка плана",
    "team": "Подготовка команды", "wave": "Сверка волны с брифом",
    "integration": "Общий обзор версии", "handoff": "Подготовка передачи проекта",
    "cleanroom": "Проверка чистой копии", "blind": "Независимая слепая проверка",
    "acceptance": "Итоговая приёмка", "knowledge": "Сохранение знаний проекта",
}
PHASES = tuple(LABELS)
COMPLETION_EVENTS = {
    "idea-reviewed": "idea", "brief-sealed": "brief",
    "project-discovery-recorded": "discovery", "spec-recorded": "spec",
    "coverage-reviewed": "coverage", "plan-validated": "plan",
    "team-prepared": "team", "team-specialized": "team",
    "wave-reviewed": "wave", "project-integration-reviewed": "integration",
    "project-handoff-recorded": "handoff", "clean-verification-recorded": "cleanroom",
    "blind-observations-recorded": "blind", "acceptance-reviewed": "acceptance",
    "project-knowledge-recorded": "knowledge",
}
TERMINAL = frozenset({"accepted", "delivered_with_remaining", "cancelled"})


def _record(state):
    record = state.get("workProgress")
    if record is None:
        return None
    require(isinstance(record, dict) and record.get("phase") in PHASES
            and isinstance(record.get("startedAt"), str), "Повреждён интервал progress")
    value = record.get("elapsedSeconds")
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            "Повреждено время progress")
    for key in ("startedAt", "activeSince", "finishedAt"):
        if key in record:
            try:
                parsed = datetime.fromisoformat(record[key])
            except (TypeError, ValueError) as error:
                raise ValueError("Повреждена дата progress") from error
            require(parsed.tzinfo is not None, "Дата progress требует часовой пояс")
    require(not (record.get("activeSince") and record.get("finishedAt")),
            "Завершённый progress не может иметь активный таймер")
    return record


def _waiting(state):
    return timing.waiting(state)


def begin(state, phase, at):
    """Return whether an explicit host declaration changed the current interval."""
    require(phase in PHASES, "Неизвестная фаза progress")
    require(state.get("status") == "running" and not _waiting(state),
            "Начать progress можно только в работающем прогоне без ожидания владельца")
    previous = _record(state)
    if previous and previous["phase"] == phase and not previous.get("finishedAt"):
        return False
    if previous and not previous.get("finishedAt"):
        timing.stop(previous, at, completed=True)
    state["workProgress"] = {"phase": phase}
    timing.start(state["workProgress"], at)
    return True


def end(state, at):
    """Close the reported interval without asserting success or a passed gate."""
    record = _record(state)
    if record is None or record.get("finishedAt"):
        return False
    timing.stop(record, at, completed=True)
    return True


def sync(state, event, at):
    """Synchronize an existing interval at a real journal transition only."""
    record = _record(state)
    if record is None or record.get("finishedAt"):
        return
    event_name = event.partition(":")[0]
    if (state.get("status") in TERMINAL or event_name == "task-started"
            or COMPLETION_EVENTS.get(event_name) == record["phase"]):
        end(state, at)
    elif state.get("status") == "running" and not _waiting(state):
        timing.start(record, at)
    else:
        timing.stop(record, at)


def view(state, observed):
    """Project a recorded wall-clock interval without mutating the source state."""
    record = _record(state)
    if record is None:
        return None
    return {**record, "label": LABELS[record["phase"]], "elapsedSeconds": timing.elapsed(record, observed)}
