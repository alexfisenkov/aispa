"""Recorded wall-clock intervals. Explicit pauses are excluded; CPU time is not measured."""
from datetime import datetime

FIELDS = ("startedAt", "activeSince", "elapsedSeconds", "finishedAt", "attempts")


def difference(end, start):
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def elapsed(record, at):
    if not record.get("startedAt") and "elapsedSeconds" not in record:
        return None
    value = record.get("elapsedSeconds", 0.0)
    if record.get("activeSince"):
        value += difference(at, record["activeSince"])
    return round(value, 3)


def start(record, at):
    record.setdefault("startedAt", at)
    record.setdefault("elapsedSeconds", 0.0)
    if not record.get("activeSince"):
        record["activeSince"] = at
    record.pop("finishedAt", None)


def stop(record, at, completed=False):
    value = elapsed(record, at)
    if value is not None:
        record["elapsedSeconds"] = value
    record.pop("activeSince", None)
    if completed:
        record["finishedAt"] = at
