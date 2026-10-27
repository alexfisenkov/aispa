"""Composite task commands: `task-open` and `task-close` call the same methods as the
separate commands, in the same order, each step saved by its own event.

The sequence arrives with ticket 10 of the 0.5.0 build; until then the CLI
answers honestly that it is not ready.
"""
from .store import Invalid

TICKET = "10"


def _not_ready(command):
    raise Invalid("Команда %s появится в задаче %s сборки 0.5.0; в этой версии пакета её ещё нет" % (command, TICKET))


def open_task(flow, tid, role, *, runtime, native_id, session, model, reasoning, model_evidence=None):
    """context (worker basis) -> start -> assign -> role card -> role packet; one receipt."""
    _not_ready("task-open")


def close_task(flow, tid, role, report_path, *, usage_path=None):
    """submit or critic review -> documents --refresh -> usage-import (if a log is given) -> next step."""
    _not_ready("task-close")
