"""One server's in-flight projection and bounded, addressed generations."""
from collections import OrderedDict
import hashlib
import threading
from time import monotonic
import uuid

from .store import Invalid, timestamp
from .verified_reads import ContextChanged, ResourceLimit


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


class SnapshotCoordinator:
    """A single in-flight job; polling an ID never starts another calculation.

    Completed generations are retained by identity, with one last good snapshot.
    No integrity data is reused when an ordinary refresh starts a new job.
    """

    def __init__(self, calculation, *, grace=.05, retained=2, retention_seconds=30):
        self.calculation = calculation
        self.grace = grace
        self.retained = retained
        self.retention_seconds = retention_seconds
        self.lock = threading.Lock()
        self.jobs = OrderedDict()
        self.pending = None
        self.last = None
        self.latest = None

    def _expired(self, job):
        return (job["id"] != self.latest
                or job.get("finishedAt") is not None
                and monotonic() - job["finishedAt"] >= self.retention_seconds)

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
                job["finishedAt"] = monotonic()
                if result is None:
                    job.update(state="source_error", error=error)
                else:
                    job.update(state="ready", result=result)
                    self.last = result
                if self.pending is job:
                    self.pending = None
                while len(self.jobs) > self.retained:
                    self.jobs.popitem(last=False)
                job["done"].set()

    def get(self, generation=None):
        launch = None
        with self.lock:
            if generation is not None:
                job = self.jobs.get(generation)
                if job is None or self._expired(job):
                    return 410, {"error": {"code": "snapshot_generation_expired"}}
            elif self.pending is not None:
                job = self.pending
            else:
                job = {"id": uuid.uuid4().hex, "state": "calculating",
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
                    job.update(state="source_error", error="snapshot_source_error", finishedAt=monotonic())
                    self.pending = None
                    job["done"].set()
        job["done"].wait(self.grace)
        with self.lock:
            calculation = {key: job[key] for key in ("id", "state", "startedAt")}
            if job["state"] == "ready":
                if self._expired(job):
                    return 410, {"error": {"code": "snapshot_generation_expired"}}
                result = job["result"]
                return 200, {**result["view"], "calculation": calculation,
                             "snapshot": {"capturedAt": result["capturedAt"], "basis": result["basis"],
                                          "freshness": "current"}}
            last = ({**self.last, "freshness": "historical"} if self.last is not None else None)
            payload = {"calculation": calculation, "lastSnapshot": last}
            if job["state"] == "source_error":
                payload["error"] = {"code": job["error"]}
                return 409, payload
            return 202, payload
