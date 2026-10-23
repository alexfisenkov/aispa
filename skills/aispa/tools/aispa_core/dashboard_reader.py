"""Explicit compatible dashboard reader. It never upgrades a run or writes proofs."""
from . import VERSION
from .store import Invalid, Journal, canonical_zone


SUPPORTED_VERSIONS = frozenset({"0.1.0", "0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.3.4", VERSION})


class SnapshotJournal(Journal):
    """Strict reader with code digests reused only within one HTTP request.

    Each status GET constructs a new instance. Journal and evidence reads retain
    their original byte checks; no result is shared between HTTP requests.
    """

    def __init__(self, root):
        super().__init__(root)
        self._code_snapshots = {}

    def load(self, *, handoff_only=False):
        self._code_snapshots.clear()
        return super().load(handoff_only=handoff_only)

    def code(self, zones=None, include_common=False):
        selected = None if zones is None else tuple(sorted({canonical_zone(zone) for zone in zones}))
        key = (selected, bool(include_common))
        if key not in self._code_snapshots:
            self._code_snapshots[key] = super().code(selected, include_common=bool(include_common))
        return self._code_snapshots[key]


class DisplayJournal(SnapshotJournal):
    """Reuse journal/schema/hash/source checks; relax only the stable version list."""

    def _require_version(self, state, *, handoff_only=False):
        if state.get("version") not in SUPPORTED_VERSIONS:
            raise Invalid("Версия журнала не поддерживается совместимым просмотром dashboard")

    def _read_only(self, *args, **kwargs):
        raise Invalid("Совместимый просмотр dashboard не изменяет журнал или runtime")

    lock = _read_only
    preflight_create = _read_only
    create = _read_only
    append = _read_only
    artifact = _read_only
    apply_recovery = _read_only

    def git(self, *args):
        if not args or args[0] not in ("rev-parse", "ls-files", "status", "check-ignore"):
            raise Invalid("Совместимый просмотр dashboard допускает только чтение Git")
        return super().git(*args)
