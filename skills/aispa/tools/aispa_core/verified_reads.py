"""Bounded physical reads and calculation-local integrity reuse.

Only an explicitly installed read scope owns a cache. Ordinary Journal callers
keep fresh operation reads; final generation validation always bypasses caches.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from .store import Invalid, file_hash


_CURRENT = ContextVar("aispa_verified_reads", default=None)


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


def physical_read(path, budget, limit=None):
    """Keep nofollow, fstat and opened/final file identity guards together."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Invalid("AISpa read: missing or symlink input")
    before = path.stat()
    maximum = budget.max_bytes if limit is None else limit
    if limit is None:
        budget.check(before.st_size, path=str(path))
    elif before.st_size > maximum:
        raise resource_limit("RESOURCE_LIMIT chunkBytes: " + str(path)
                             + "; continuation: increase operation budget and retry complete input")
    budget.check_memory(before.st_size * 4, path=str(path))
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Invalid("AISpa read: source identity changed")
        raw = bounded_read(stream, maximum, budget=budget, path=path)
    after = path.stat()
    if path.is_symlink() or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise Invalid("AISpa read: source changed while reading")
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

    @contextmanager
    def scope(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def check(self):
        if self.failure is not None:
            raise self.failure

    def read(self, path, budget, limit=None):
        path = Path(path)
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
        raw = self.raw[path]
        budget.check(len(raw), path=str(path))
        if limit is not None and len(raw) > limit:
            raise resource_limit("RESOURCE_LIMIT chunkBytes: " + str(path))
        budget.check_memory(self.retained, path=str(path))
        return raw

    def parse(self, raw):
        if raw not in self.parsed:
            self.parsed[raw] = json.loads(raw)
        return self.parsed[raw]

    def sha256(self, path, raw):
        return self.hashes.get(Path(path)) or self.value_hash(raw)

    def value_hash(self, raw):
        if raw not in self.digests:
            self.digests[raw] = hashlib.sha256(raw).hexdigest()
        return self.digests[raw]

    def artifact(self, path, expected_sha256, *, allow_manifest=True, literal=False, maximum=None):
        from . import scale
        path = Path(path)
        old = self.expected.get(path)
        if old is not None and expected_sha256 is not None and old != expected_sha256:
            raise Invalid("AISpa read: conflicting expected artifact SHA")
        if expected_sha256 is not None:
            self.expected[path] = expected_sha256
        budget = scale.Budget()
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
            except ResourceLimit as error:
                self.failure = error
                raise
        self.check()
        budget.check(len(self.payloads[key]), path=str(path))
        budget.check_memory(self.retained, path=str(path))
        return self.payloads[key]

    def json(self, raw):
        raw.decode("utf-8")  # Never broaden a text reader to UTF-16 JSON autodetection.
        return copy.deepcopy(self.parse(raw))

    def hash_valid(self, path, expected_sha256):
        path = Path(path)
        if path not in self.hashes:
            self.hashes[path] = file_hash(path)  # binary/code remains streaming
        return self.hashes[path] == expected_sha256

    def revalidate(self):
        self.check()
        for path, sha in self.hashes.items():
            if path.is_symlink() or not path.is_file() or file_hash(path) != sha:
                raise Invalid("AISpa read: source generation changed")
