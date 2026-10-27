"""One server's in-flight projection, bounded addressed generations and bounded reuse."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from time import monotonic
import uuid

from .store import Invalid, timestamp
from .verified_reads import ContextChanged, ResourceLimit

# A viewing snapshot is reused while its source files are untouched, but never longer than this.
REUSE_SECONDS = 300
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")


def calculate(root, reader_class, loaded, pinned_digest, asset_bytes, projection):
    """Retry only a concurrent journal append, always with a fresh read scope."""
    for attempt in range(3):
        reader = reader_class(root)
        try:
            state = reader.load()

            def context_current():
                if (state["run"] != loaded["run"] or state["version"] != loaded["version"]
                        or state.get("runtimeDigest") != pinned_digest
                        or hashlib.sha256(asset_bytes()).hexdigest() != loaded["assetSha256"]):
                    raise ContextChanged("Dashboard context changed")

            context_current()
            with reader._reads.scope():
                view = projection(reader, state)
                basis = reader.finish_snapshot()
                context_current()
            view.update(dashboardVersion=loaded["dashboardVersion"], viewerMode=loaded["readerMode"])
            return view, basis
        except Invalid as error:
            if str(error) == "Журнал изменился после проверки" and attempt < 2:
                continue
            raise


def _file_key(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def source_key(home):
    """(dev, ino, size, mtime_ns) of the journal, active pointer, journal cache and page, or None.

    A cheap sign that nothing was written; it is not an integrity check. None means
    "cannot tell", so the caller recalculates.
    """
    home = Path(home)
    pointer = home / "active.json"
    active = _file_key(pointer)
    if active is None:
        return None
    try:
        with open(pointer, "rb") as stream:
            run = json.loads(stream.read(65537).decode("utf-8"))["run"]
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None
    if not isinstance(run, str) or RUN_NAME.fullmatch(run) is None or ".." in run:
        return None
    directory = home / "runs" / run
    journal = _file_key(directory / "events.jsonl")
    if journal is None:
        return None
    # The served page is part of the key: a changed page must surface as a context change at once.
    return (active, journal, _file_key(directory / "journal-cache.json"), _file_key(home / "dashboard.html"))


class SnapshotCoordinator:
    """A single in-flight job; polling an ID never starts another calculation.

    Completed generations are retained by identity, with one last good snapshot.
    Viewing is not a gate: while `fingerprint()` returns the key the last ready snapshot
    was calculated under, that snapshot is served again, for at most `reuse_seconds`.
    A changed journal, pointer or cache calculates at once. Reuse reads no source
    bytes; every mutating command still verifies everything in full, and a
    same-size, same-mtime substitution is caught on the next calculation.
    Without a `fingerprint` nothing is reused.
    """

    def __init__(self, calculation, *, grace=.05, retained=2, retention_seconds=30,
                 fingerprint=None, reuse_seconds=None, clock=None):
        self.calculation = calculation
        self.grace = grace
        self.retained = retained
        self.retention_seconds = retention_seconds
        self.fingerprint = fingerprint
        self.reuse_seconds = REUSE_SECONDS if reuse_seconds is None else reuse_seconds
        self.clock = clock
        self.ready = None
        self.lock = threading.Lock()
        self.jobs = OrderedDict()
        self.pending = None
        self.last = None
        self.latest = None

    def _now(self):
        return (self.clock or monotonic)()

    def _key(self):
        if self.fingerprint is None or self.reuse_seconds <= 0:
            return None
        try:
            return self.fingerprint()
        except Exception:
            return None

    def _expired(self, job):
        return (job["id"] != self.latest
                or job.get("finishedAt") is not None
                and self._now() - job["finishedAt"] >= self.retention_seconds)

    def _run(self, job):
        error = "snapshot_source_error"
        result = None
        try:
            view, basis = self.calculation()
            result = {"view": view, "basis": basis, "capturedAt": timestamp()}
        except ContextChanged:
            error = "dashboard_context_changed"
        except ResourceLimit:
            error = "snapshot_resource_limit"
        except Exception:
            # Exceptions can contain private source text and local paths.
            pass
        finally:
            with self.lock:
                job["finishedAt"] = self._now()
                if result is None:
                    job.update(state="source_error", error=error)
                else:
                    job.update(state="ready", result=result)
                    self.last = result
                    self.ready = {"key": job.get("key"), "at": job["finishedAt"], "result": result,
                                  "calculation": {"id": job["id"], "state": "ready", "startedAt": job["startedAt"]}}
                if self.pending is job:
                    self.pending = None
                while len(self.jobs) > self.retained:
                    self.jobs.popitem(last=False)
                job["done"].set()

    def _reusable(self, key):
        """The ready snapshot, if it still describes these source files; the caller holds the lock."""
        ready = self.ready
        if (key is None or ready is None or ready["key"] != key
                or not 0 <= self._now() - ready["at"] < self.reuse_seconds):
            return None
        return ready

    def _ready_payload(self, calculation, result, finished):
        age = max(0.0, self._now() - finished)
        return 200, {**result["view"], "calculation": calculation,
                     "snapshot": {"capturedAt": result["capturedAt"], "basis": result["basis"],
                                  "freshness": "current", "ageSeconds": round(age, 3)}}

    def get(self, generation=None):
        launch = None
        key = self._key() if generation is None else None
        with self.lock:
            if generation is None and self.pending is None:
                reuse = self._reusable(key)
                if reuse is not None:
                    return self._ready_payload(reuse["calculation"], reuse["result"], reuse["at"])
            if generation is not None:
                job = self.jobs.get(generation)
                if job is None or self._expired(job):
                    return 410, {"error": {"code": "snapshot_generation_expired"}}
            elif self.pending is not None:
                job = self.pending
            else:
                job = {"id": uuid.uuid4().hex, "state": "calculating", "key": key,
                       "startedAt": timestamp(), "done": threading.Event()}
                self.jobs[job["id"]] = job
                self.latest = job["id"]
                self.pending = job
                launch = job
                while len(self.jobs) > self.retained:
                    self.jobs.popitem(last=False)
        if launch is not None:
            try:
                threading.Thread(target=self._run, args=(job,), daemon=True).start()
            except Exception:
                with self.lock:
                    job.update(state="source_error", error="snapshot_source_error", finishedAt=self._now())
                    self.pending = None
                    job["done"].set()
        job["done"].wait(self.grace)
        with self.lock:
            calculation = {key: job[key] for key in ("id", "state", "startedAt")}
            if job["state"] == "ready":
                if self._expired(job):
                    return 410, {"error": {"code": "snapshot_generation_expired"}}
                return self._ready_payload(calculation, job["result"], job["finishedAt"])
            last = ({**self.last, "freshness": "historical"} if self.last is not None else None)
            payload = {"calculation": calculation, "lastSnapshot": last}
            if job["state"] == "source_error":
                payload["error"] = {"code": job["error"]}
                return 409, payload
            return 202, payload
