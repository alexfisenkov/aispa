"""Warnings of a run collected in one list: state["collectedFindings"].

Owner of numbering (W001...) and status transitions. Only new runs with the
``focusedReview`` policy collect findings: a warning never stops a task; it
waits for the branch review, where it is confirmed or dismissed with a reason,
and the confirmed ones are fixed by one final repair.
"""
import copy

from .contracts import nonempty, require
from .store import timestamp

STATUSES = ("open", "confirmed", "dismissed", "fixed", "remaining")
SOURCES = ("task", "branch")
# open -> confirmed|dismissed (branch review); confirmed -> fixed|remaining (final repair).
TRANSITIONS = {"open": ("confirmed", "dismissed"), "confirmed": ("fixed", "remaining")}


def enabled(state):
    return state.get("policy", {}).get("focusedReview") is True


def _items(state):
    items = state.setdefault("collectedFindings", [])
    require(isinstance(items, list), "Повреждён список collectedFindings")
    return items


def _source(source):
    require(isinstance(source, dict) and source.get("kind") in SOURCES, "Источник находки: task или branch")
    report = source.get("report")
    require(isinstance(report, dict) and nonempty(report.get("file")) and nonempty(report.get("sha256")),
            "Источнику находки нужен отчёт {file, sha256}")
    if source["kind"] == "task":
        require(nonempty(source.get("task")) and nonempty(source.get("attempt"))
                and type(source.get("round")) is int, "Находке задачи нужны task, attempt и round")
    value = {key: copy.deepcopy(source[key]) for key in ("kind", "task", "attempt", "round", "lens") if key in source}
    value["report"] = {"file": report["file"], "sha256": report["sha256"]}
    return value


def collect(state, source, findings):
    """Append every warning of one sealed report; the same report is never collected twice."""
    if not enabled(state):
        return []
    items, origin = _items(state), _source(source)
    known = {(row["source"]["report"]["sha256"], row["criterion"], row["note"]) for row in items}
    added = []
    for finding in findings or []:
        if not isinstance(finding, dict) or finding.get("severity") != "warning":
            continue
        key = (origin["report"]["sha256"], finding["criterion"], finding["text"])
        if key in known:
            continue
        known.add(key)
        row = {"id": "W%03d" % (len(items) + 1), "source": copy.deepcopy(origin),
               "criterion": finding["criterion"], "note": finding["text"], "status": "open", "resolution": None}
        items.append(row)
        added.append(row["id"])
    return added


def collect_branch(state, source, findings, reviewer):
    """Findings of a branch reviewer: an independent reader of the whole branch confirms them by raising them.

    Warnings and blocking findings (with their class) both wait for the one final repair.
    """
    require(enabled(state), "Список находок ведётся только в прогонах с focusedReview")
    items, origin = _items(state), _source(source)
    require(origin["kind"] == "branch", "Находки ревьюера ветки требуют источник branch")
    at, added = timestamp(), []
    for finding in findings or []:
        if not isinstance(finding, dict) or finding.get("severity") not in ("warning", "blocking"):
            continue
        row = {"id": "W%03d" % (len(items) + 1), "source": copy.deepcopy(origin),
               "criterion": finding["criterion"], "note": finding["text"], "status": "confirmed",
               "resolution": {"status": "confirmed", "note": "Найдено проверкой всей ветки", "reviewer": reviewer, "at": at}}
        if finding["severity"] == "blocking":
            row.update(severity="blocking", blockingClass=finding["blockingClass"])
        items.append(row)
        added.append(row["id"])
    return added


def triage(state, decisions, reviewer):
    """Move findings along their transitions; each decision needs a reason."""
    require(enabled(state), "Список находок ведётся только в прогонах с focusedReview")
    require(nonempty(reviewer), "Нужен участник, принимающий решение по находкам")
    require(isinstance(decisions, list) and decisions, "Нужен непустой список решений по находкам")
    rows = {row["id"]: row for row in _items(state)}
    seen = set()
    for decision in decisions:
        require(isinstance(decision, dict) and decision.get("id") in rows and decision["id"] not in seen,
                "Неизвестная или повторная находка в решениях")
        seen.add(decision["id"])
        row = rows[decision["id"]]
        require(decision.get("status") in TRANSITIONS.get(row["status"], ()),
                "Находка %s: из %s можно перейти только в %s" % (
                    row["id"], row["status"], ", ".join(TRANSITIONS.get(row["status"], ())) or "никуда"))
        require(nonempty(decision.get("note")), "Решению по находке нужна причина")
    at = timestamp()
    for decision in decisions:
        row = rows[decision["id"]]
        row["status"] = decision["status"]
        row["resolution"] = {"status": decision["status"], "note": decision["note"], "reviewer": reviewer, "at": at}
    return sorted(seen)


def open_items(state):
    return [copy.deepcopy(row) for row in state.get("collectedFindings", []) if row.get("status") == "open"]


def with_status(state, status):
    return [copy.deepcopy(row) for row in state.get("collectedFindings", []) if row.get("status") == status]


def view(state):
    """Counts for the dashboard; dismissed findings are not work."""
    counts = {status: 0 for status in ("open", "confirmed", "fixed", "remaining")}
    for row in state.get("collectedFindings", []):
        if row.get("status") in counts:
            counts[row["status"]] += 1
    return counts


def listing(state, status=None):
    """Read-only `findings [--status S]`: collected findings, optionally of one status."""
    require(status is None or status in STATUSES, "Статус находки: " + ", ".join(STATUSES))
    rows = [copy.deepcopy(row) for row in state.get("collectedFindings", [])
            if status is None or row.get("status") == status]
    value = {"findings": rows, "count": len(rows), "counts": view(state)}
    if status is not None:
        value["status"] = status
    if not enabled(state):
        value["note"] = "Прогон без focusedReview: предупреждения критиков блокируют задачу и в список не копятся"
    return value
