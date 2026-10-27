"""Bounded physical reads and calculation-local integrity reuse.

Only an explicitly installed read scope owns a cache. Ordinary Journal callers
keep fresh operation reads; final generation validation always bypasses caches.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import math
import os  # Existing isolated fixtures patch os.fdopen through this module.
from pathlib import Path

from .store import Invalid, confined, digest, file_hash, regular_file


_CURRENT = ContextVar("aispa_verified_reads", default=None)
_VERDICTS = ContextVar("aispa_read_verdicts", default=None)


def _json_native(value):
    """JSON normalization must not merge Python predicate-distinct values."""
    pending, active, completed = [(value, False)], set(), set()
    while pending:
        item, closing = pending.pop()
        kind = type(item)
        if kind is dict or kind is list:
            # IDs are local cycle-walk bookkeeping, never verdict cache keys or
            # a retained claim that a mutable object's content is unchanged.
            identity = id(item)
            if closing:
                active.remove(identity)
                completed.add(identity)
                continue
            if identity in active:
                return False
            if identity in completed:
                continue
            if kind is dict and any(type(key) is not str for key in item):
                return False
            active.add(identity)
            pending.append((item, True))
            pending.extend((child, False) for child in (item.values() if kind is dict else item))
        elif kind is float:
            if not math.isfinite(item):
                return False
        elif kind not in (str, int, bool, type(None)):
            return False
    return True


def current_scope():
    """Only a running projection explicitly installs a shared read scope."""
    return _CURRENT.get()


class ResourceLimit(Invalid):
    """A resource refusal remains distinguishable when a predicate catches it."""


class ContextChanged(Invalid):
    """The server no longer represents its loaded run/runtime/asset."""


def resource_limit(message):
    error = ResourceLimit(message)
    scope = _CURRENT.get()
    if scope is not None:
        scope.failure = error
    return error


def bounded_read(stream, maximum, *, budget=None, path=""):
    """Account before allocation, including one sentinel for post-stat growth."""
    allocation = maximum
    if budget is not None:
        allocation = min(maximum, max(0, (budget.memory_bytes - budget.base_memory) // 4))
    data = bytearray()
    while True:
        if budget is not None:
            budget.check(0, path=str(path), progress=len(data))
        part = stream.read(min(65536, max(1, allocation - len(data) + 1)))
        if not part:
            break
        total = len(data) + len(part)
        if budget is not None:
            budget.check_memory(total * 4, path=str(path), progress=len(data))
        if total > maximum:
            raise resource_limit("RESOURCE_LIMIT maxInputBytes: " + str(path)
                                 + "; continuation: increase operation budget and retry complete input")
        data.extend(part)
    return bytes(data)


def physical_read(path, budget, limit=None, *, metadata=None):
    """Keep nofollow, fstat and opened/final file identity guards together."""
    path = Path(path)
    try:
        with regular_file(path, nofollow=True, budget=budget) as (stream, before, opened):
            maximum = budget.max_bytes if limit is None else limit
            if limit is None:
                budget.check(before.st_size, path=str(path))
            elif before.st_size > maximum:
                raise resource_limit("RESOURCE_LIMIT chunkBytes: " + str(path)
                                     + "; continuation: increase operation budget and retry complete input")
            budget.check_memory(before.st_size * 4, path=str(path))
            raw = bounded_read(stream, maximum, budget=budget, path=path)
    except OSError as error:
        raise Invalid("AISpa read: missing or unreadable input") from error
    if metadata is not None:
        metadata.update(generation=(opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns))
    budget.check(0, path=str(path))
    return raw


class VerifiedReads:
    def __init__(self):
        self.raw = {}
        self.hashes = {}
        self.payloads = {}
        self.parsed = {}
        self.digests = {}
        self.expected = {}
        self.failure = None
        self.retained = 0
        self._confinements = {}
        self._confinement_bytes = 0
        self._hash_budget = None

    @contextmanager
    def scope(self):
        previous = _CURRENT.get()
        budget = None
        if previous is not self:
            from . import scale
            budget = scale.Budget()
        token = _CURRENT.set(self)
        verdict_token = None
        if previous is not self:
            self._hash_budget = budget
            verdict_token = _VERDICTS.set({"scope": self, "owner": None, "identity": None,
                                           "values": set(), "retained": 0})
        try:
            yield self
            self.deadline_check("read calculation exit")
        finally:
            if verdict_token is not None:
                _VERDICTS.reset(verdict_token)
            _CURRENT.reset(token)

    def _verdict_context(self, store):
        context = _VERDICTS.get()
        generation = getattr(store, "_journal_generation", None)
        if (current_scope() is not self or getattr(store, "_reads", None) is not self
                or context is None or context["scope"] is not self or generation is None):
            return None
        identity = (store.root, store.run, generation["path"], generation["sha256"])
        if context["owner"] is None:
            context.update(owner=store, identity=identity)
        if context["owner"] is not store or context["identity"] != identity:
            return None
        return context

    def _check_retained(self, extra=0):
        context = _VERDICTS.get()
        verdict_bytes = context["retained"] if context is not None and context["scope"] is self else 0
        try:
            self._stream_budget().check_memory(self.retained + self._confinement_bytes + verdict_bytes + extra,
                                               path="read validation memo")
        except ResourceLimit as error:
            self.failure = error
            raise

    def verdict(self, store, namespace, value, compute):
        """Memo only a positive immutable predicate in this exact calculation.

        Content is keyed in full, never by a mutable object identity. Unsupported
        JSON key values fall back to the original predicate and its semantics.
        """
        self.deadline_check()
        context = self._verdict_context(store)
        if context is None:
            result = compute()
            self.deadline_check()
            return result
        self.check()
        self._check_retained()
        try:
            if not _json_native(value):
                raise TypeError("Not exact JSON-native predicate content")
            key = (namespace, digest(value))
        except Invalid:
            raise
        except (TypeError, ValueError, RecursionError, OverflowError):
            result = compute()
            self.deadline_check()
            return result
        if key in context["values"]:
            self.deadline_check()
            return True
        result = compute()
        self.deadline_check()
        if result is True:
            # Conservative accounting of the small retained tuple/string/set
            # entry; no payload or caller-owned dictionary is retained here.
            accounted = 256 + len(namespace.encode("utf-8")) * 4 + len(key[1]) * 4
            self._check_retained(accounted)
            context["values"].add(key)
            context["retained"] += accounted
        self.deadline_check()
        return result

    def _remember_confinement(self, base, name, resolved):
        key = (Path(base), name)
        resolved = Path(resolved)
        previous = self._confinements.get(key)
        if previous is not None:
            if previous != resolved:
                raise Invalid("AISpa read: confinement generation changed")
            return
        accounted = 256 + 4 * (len(str(base)) + len(name) + len(str(resolved)))
        self._check_retained(accounted)
        self._confinements[key] = resolved
        self._confinement_bytes += accounted

    def remember_reference(self, store, name, resolved):
        if self._verdict_context(store) is not None:
            self._remember_confinement(store.run, name, resolved)

    def _remember_manifest(self, path):
        raw = self.raw.get(path)
        value = self.parsed.get(raw) if raw is not None else None
        if isinstance(value, dict) and value.get("aispaManifest") == 1:
            # read_file has already validated every complete child reference.
            # Keep each original logical boundary, including other directories.
            for ref in value["chunks"]:
                self._remember_confinement(path.parent, ref["file"], confined(path.parent, ref["file"]))

    def check(self):
        if self.failure is not None:
            raise self.failure

    def read(self, path, budget, limit=None):
        path = Path(path)
        self.deadline_check(str(path))
        if path not in self.raw:
            # Retained entries share the operation's memory allowance.
            previous = budget.base_memory
            budget.base_memory += self.retained
            try:
                raw = physical_read(path, budget, limit)
            finally:
                budget.base_memory = previous
            self.raw[path] = raw
            self.hashes[path] = self.value_hash(raw)
            self.retained += len(raw) * 4
            self._remember_confinement(path.parent, path.name, path)
        raw = self.raw[path]
        budget.check(len(raw), path=str(path))
        if limit is not None and len(raw) > limit:
            raise resource_limit("RESOURCE_LIMIT chunkBytes: " + str(path))
        budget.check_memory(self.retained, path=str(path))
        self.deadline_check(str(path))
        return raw

    def parse(self, raw):
        self.deadline_check("JSON parse")
        if raw not in self.parsed:
            self.parsed[raw] = json.loads(raw)
        self.deadline_check("JSON parse")
        return self.parsed[raw]

    def sha256(self, path, raw):
        self.deadline_check("SHA cache")
        value = self.hashes.get(Path(path)) or self.value_hash(raw)
        self.deadline_check("SHA cache")
        return value

    def value_hash(self, raw):
        self.deadline_check("SHA value")
        if raw not in self.digests:
            self.digests[raw] = hashlib.sha256(raw).hexdigest()
        self.deadline_check("SHA value")
        return self.digests[raw]

    def artifact(self, path, expected_sha256, *, allow_manifest=True, literal=False, maximum=None,
                 budget=None):
        self.deadline_check("artifact")
        path = Path(path)
        old = self.expected.get(path)
        if old is not None and expected_sha256 is not None and old != expected_sha256:
            raise Invalid("AISpa read: conflicting expected artifact SHA")
        if expected_sha256 is not None:
            self.expected[path] = expected_sha256
        from . import scale
        # The calculation owns the deadline; a fresh input profile still sees
        # a stricter memory/byte setting applied during this calculation.
        budget = budget or scale.Budget()
        budget.deadline = min(budget.deadline, self._stream_budget().deadline)
        maximum = budget.max_bytes if maximum is None else min(maximum, budget.max_bytes)
        key = (path, expected_sha256, bool(allow_manifest), bool(literal), maximum)
        if key not in self.payloads:
            try:
                if literal:
                    payload = self.read(path, budget, maximum)
                    if expected_sha256 is not None and self.sha256(path, payload) != expected_sha256:
                        raise Invalid("AISpa read: immutable artifact SHA mismatch")
                else:
                    payload = scale.read_file(path, expected_sha256=expected_sha256,
                                              allow_manifest=allow_manifest, budget=budget, cache=self)
                if len(payload) > maximum:
                    raise resource_limit("RESOURCE_LIMIT maxInputBytes: " + str(path))
                if payload is not self.raw.get(path):
                    budget.check_memory(self.retained + len(payload) * 4, path=str(path))
                    self.retained += len(payload) * 4
                self.payloads[key] = payload
                if not literal:
                    self._remember_manifest(path)
            except ResourceLimit as error:
                self.failure = error
                raise
        self.check()
        budget.check(len(self.payloads[key]), path=str(path))
        budget.check_memory(self.retained, path=str(path))
        self.deadline_check(str(path))
        return self.payloads[key]

    def json(self, raw):
        self.deadline_check("JSON decode")
        budget = self._stream_budget()
        budget.check_memory(self.retained + len(raw) * 4, path="JSON decode")
        raw.decode("utf-8")  # Never broaden a text reader to UTF-16 JSON autodetection.
        self.deadline_check("JSON copy")
        value = copy.deepcopy(self.parse(raw))
        self.deadline_check("JSON copy")
        return value

    def _stream_budget(self):
        if self._hash_budget is None:
            from . import scale
            self._hash_budget = scale.Budget()
        return self._hash_budget

    def deadline_check(self, path="read calculation"):
        self.check()
        self._stream_budget().check(0, path=path)

    def hash_valid(self, path, expected_sha256):
        path = Path(path)
        budget = self._stream_budget()
        budget.check(0, path=str(path))
        if path not in self.hashes:
            try:
                self.hashes[path] = file_hash(path, budget=budget, nofollow=True)  # binary/code remains streaming
            except OSError as error:
                raise Invalid("AISpa read: missing or unreadable input") from error
            self._remember_confinement(path.parent, path.name, path)
        return self.hashes[path] == expected_sha256

    def revalidate(self):
        self.check()
        budget = self._stream_budget()
        budget.check(0, path="read revalidation")
        for (base, name), expected in self._confinements.items():
            budget.check(0, path=str(base / name))
            if confined(base, name) != expected:
                raise Invalid("AISpa read: confinement generation changed")
        for path, sha in self.hashes.items():
            try:
                if path.is_symlink() or not path.is_file() or file_hash(path, budget=budget, nofollow=True) != sha:
                    raise Invalid("AISpa read: source generation changed")
            except OSError as error:
                raise Invalid("AISpa read: source generation changed") from error
        budget.check(0, path="read revalidation")
