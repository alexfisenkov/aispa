"""Explicit compatible dashboard reader. It never upgrades a run or writes proofs."""
from . import VERSION

from .store import Invalid, Journal, canonical_zone, confined, digest
from .verified_reads import ContextChanged, VerifiedReads, current_scope


SUPPORTED_VERSIONS = frozenset({"0.1.0", "0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.3.4", "0.3.5", "0.3.6", "0.4.0", VERSION})


class SnapshotJournal(Journal):
    """Strict calculation-scoped reader; a new load discards all cached bytes."""

    def __init__(self, root):
        super().__init__(root)
        self._code_snapshots = {}
        self._reads = VerifiedReads()
        self._journal_generation = None

    def load(self, *, handoff_only=False):
        self._code_snapshots.clear()
        self._reads = VerifiedReads()
        self._journal_generation = None
        state = super().load(handoff_only=handoff_only)
        self._journal_generation = {key: self._verified[key] for key in ("path", "sha256", "count")}
        return state

    def _journal_cursor(self, *, include_events=False, budget=None):
        if current_scope() is not self._reads or self._journal_generation is None:
            return super()._journal_cursor(include_events=include_events, budget=budget)
        self._reads.deadline_check("journal cache")
        frozen, cached = self._journal_generation, self._verified
        if cached is None or cached["path"] != frozen["path"] or cached["sha256"] != frozen["sha256"]:
            raise Invalid("Журнал изменился после проверки")
        if include_events and "events" not in cached:
            # The full index is lazy, but cannot adopt a newer valid append.
            cached = self._scan_journal(include_events=True, budget=self._reads._stream_budget())
            if cached["sha256"] != frozen["sha256"]:
                raise Invalid("Журнал изменился после проверки")
            self._verified = cached
        self._reads.deadline_check("journal cache")
        return cached

    def _verify_sources(self, state, *, budget=None):
        for ref in state["briefs"] + ([state["spec"]] if state["spec"] else []):
            self._reads.artifact(confined(self.run, ref["file"]), ref["sha256"], budget=budget)
        if budget is not None:
            budget.check(0, path="saved sources")

    def finish_snapshot(self):
        """Recheck the defined read generation; no stat/TTL integrity shortcuts."""
        self._reads.deadline_check("snapshot")
        pointer = self._active_pointer(self._reads._stream_budget())
        if pointer.get("run") != self.run.name:
            raise ContextChanged("Dashboard active run changed")
        self._reads.revalidate()
        for (zones, common), expected in self._code_snapshots.items():
            self._reads.deadline_check("code revalidation")
            if Journal.code(self, zones, include_common=common,
                            budget=self._reads._stream_budget()) != expected:
                raise Invalid("Dashboard code generation changed")
        frozen = self._journal_generation
        if frozen is None:
            raise Invalid("Dashboard snapshot was not loaded")
        # Calling read_journal (even through Journal) dispatches the virtual
        # cursor. This final full-byte hash must be independent of that cache.
        journal_sha = Journal._hash_file(confined(self.run, "events.jsonl"),
                                        budget=self._reads._stream_budget())
        if journal_sha != frozen["sha256"]:
            raise Invalid("Журнал изменился после проверки")
        pointer = self._active_pointer(self._reads._stream_budget())
        if pointer.get("run") != self.run.name:
            raise ContextChanged("Dashboard active run changed")
        self._reads.deadline_check("snapshot")
        candidate_sha = digest([(zones, common, sha)
                                for (zones, common), sha in self._code_snapshots.items()])
        self._reads.deadline_check("snapshot digest")
        return {"journalSha256": journal_sha, "journalEvents": frozen["count"],
                "candidateSha256": candidate_sha}

    def code(self, zones=None, include_common=False):
        if current_scope() is self._reads:
            self._reads.deadline_check("code cache")
        selected = None if zones is None else tuple(sorted({canonical_zone(zone) for zone in zones}))
        key = (selected, bool(include_common))
        if key not in self._code_snapshots:
            self._code_snapshots[key] = super().code(selected, include_common=bool(include_common),
                                                     budget=self._reads._stream_budget()
                                                     if current_scope() is self._reads else None)
        if current_scope() is self._reads:
            self._reads.deadline_check("code cache")
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
