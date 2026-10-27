"""Warnings of a run collected in one list: state["collectedFindings"].

Owner of numbering (W001...) and status transitions. Only new runs with the
``focusedReview`` policy collect findings. The logic arrives with ticket 02 of
the 0.5.0 build; until then the CLI answers honestly that it is not ready.
"""
from .store import Invalid

STATUSES = ("open", "confirmed", "dismissed", "fixed", "remaining")
TICKET = "02"


def _not_ready(command):
    raise Invalid("Команда %s появится в задаче %s сборки 0.5.0; в этой версии пакета её ещё нет" % (command, TICKET))


def listing(state, status=None):
    """Read-only `findings [--status S]`: collected findings, optionally of one status."""
    _not_ready("findings")
