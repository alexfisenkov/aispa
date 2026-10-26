"""Atomic admission for this project's managed launches, not a host scheduler.

The registry has its own OS lock, independent of the journal lock. Leases survive
controllers and sessions; a lost heartbeat or an incomplete inventory never
establishes that a native resource exited. Native handles are host observations,
not authority to signal a discovered PID.
"""
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import uuid

from .portable import is_link
from .store import Invalid, atomic_json, digest, timestamp


KINDS = ("agents", "browsers", "testProcesses", "services")
DEFAULT_LIMITS = {"agents": 4, "browsers": 2, "testProcesses": 2, "services": 1}
_OCCUPIED = {"reserved", "running", "orphaned"}
_TERMINAL = {"completed", "launch_failed"}
_IDENTITY = ("root", "run", "session", "sessionKind", "owner")
_TTL = 300


class CapacityRefused(Invalid):
    """Only an occupied resource cap, distinguishable from invalid provenance."""

    code = "RESOURCE_CAPACITY"

    def __init__(self, kind, cap, occupied):
        self.kind, self.cap, self.occupied = kind, cap, occupied
        super().__init__(f"RESOURCE_CAPACITY: {kind}: {occupied}/{cap} (project)")


def enabled(state):
    return state.get("policy", {}).get("managedExecution") is True


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise Invalid("Resources: неверный " + label)
    return value


def _profile(profile):
    if (not isinstance(profile, dict) or type(profile.get("schema")) is not int
            or profile["schema"] != 1 or profile.get("scope") != "project"):
        raise Invalid("Resources: нужен профиль schema=1, scope=project")
    limits, host = profile.get("limits"), profile.get("hostLimits", {})
    if not isinstance(limits, dict) or set(limits) != set(KINDS):
        raise Invalid("Resources: нужны отдельные limits agents/browsers/testProcesses/services")
    if not isinstance(host, dict) or set(host) - set(KINDS):
        raise Invalid("Resources: неверные hostLimits")
    if any(type(value) is not int or value <= 0 for value in (*limits.values(), *host.values())):
        raise Invalid("Resources: limits должны быть конечными положительными целыми")
    if any(limits[kind] > cap for kind, cap in host.items()):
        raise Invalid("Resources: project limit превышает известный host limit")
    result = {"schema": 1, "scope": "project", "limits": dict(limits)}
    if host:
        result["hostLimits"] = dict(host)
    return result


def configure(store, state, profile):
    normalized = _profile(profile)  # No partial state assignment on rejection.
    previous = _profile(state.get("resourceProfile", _defaults()))
    ctx = {"root": str(store.root)}
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        current = registry["profileBasis"]
        requested = digest(normalized)
        if current is not None and current != digest(previous) and current != requested:
            raise Invalid("Resources: профиль изменился; перечитайте registry и явно повторите configure")
        ceilings = _ceilings(registry["knownHostLimits"], normalized.get("hostLimits", {}))
        if any(normalized["limits"][kind] > cap for kind, cap in ceilings.items()):
            raise Invalid("Resources: project limit превышает сохранённый host limit")
        registry.update(profile=normalized, profileBasis=requested, knownHostLimits=ceilings)
        atomic_json(path, registry)
    # If the caller crashes before journaling, the old context fails closed.
    # Repeating this exact requested profile is an idempotent recovery/read-back.
    state["resourceProfile"] = normalized
    return copy.deepcopy(normalized)


def _defaults():
    return {"schema": 1, "scope": "project", "limits": dict(DEFAULT_LIMITS)}


def _ceilings(previous, reported):
    result = dict(previous)
    for kind, cap in reported.items():
        result[kind] = min(result.get(kind, cap), cap)
    return result


def context(store, state, *, owner="coordinator", session=None, allow_paused=False,
            service_instance=None):
    if not enabled(state):
        return None
    owner = _text(owner, "owner")
    run = _text(state.get("run"), "run")
    profile = _profile(state.get("resourceProfile", _defaults()))
    if service_instance is not None:
        _text(service_instance, "service instance")
        if owner != "dashboard-service" or session is not None:
            raise Invalid("Resources: local-service требует владельца dashboard-service")
        session = "local-service:" + service_instance
        kind = "local-service"
    else:
        session = _text(session if session is not None else state.get("hostSession"), "native session")
        kind = "native"
        if state.get("status") != "running" and not (
                allow_paused and state.get("status") in ("paused", "blocked")):
            raise Invalid("Resources: запуск требует running либо явного recovery context")
    result = {"root": str(store.root), "run": run, "owner": owner, "session": session,
              "sessionKind": kind, "profile": profile, "profileBasis": digest(profile)}
    if service_instance is not None:
        result["serviceInstance"] = service_instance
    if "resourceProfile" not in state:
        # No journal mutation: new native/service contexts can read the existing
        # project profile. An explicit older state profile is never auto-upgraded.
        with _locked(result) as path:
            registry = _load(path, result)
            if registry["profile"] is not None:
                result.update(profile=registry["profile"], profileBasis=registry["profileBasis"])
    return _context(result)


def _context(value):
    if not isinstance(value, dict):
        raise Invalid("Resources: нужен явный managed context")
    for key in _IDENTITY:
        _text(value.get(key), key)
    root = Path(value["root"])
    if not root.is_absolute() or str(root.resolve()) != value["root"] or not root.is_dir():
        raise Invalid("Resources: нужен физический абсолютный root")
    profile = _profile(value.get("profile"))
    if value.get("profileBasis") != digest(profile):
        raise Invalid("Resources: профиль context изменён")
    kind = value["sessionKind"]
    if kind == "local-service":
        instance = _text(value.get("serviceInstance"), "service instance")
        if value["owner"] != "dashboard-service" or value["session"] != "local-service:" + instance:
            raise Invalid("Resources: неверная local-service identity")
    elif kind != "native" or "serviceInstance" in value:
        raise Invalid("Resources: неверный sessionKind")
    return copy.deepcopy(value)


def _paths(ctx):
    folder = Path(ctx["root"]) / ".aispa"
    if is_link(folder):
        raise Invalid("Resources: .aispa не должна быть ссылкой")
    folder = folder / "resources"
    if is_link(folder):
        raise Invalid("Resources: каталог не должен быть ссылкой")
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / name for name in ("registry.json", ".lock")]
    if any(is_link(path) or path.exists() and not path.is_file() for path in paths):
        raise Invalid("Resources: нужны обычные файлы registry/lock")
    return paths


@contextmanager
def _locked(ctx):
    registry, lock = _paths(ctx)
    # OS-owned locks are released on process exit. Do not unlink the lock file:
    # replacing its inode would permit two independent lock holders.
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + 10
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise Invalid("Resources: registry lock занят или недоступен") from exc
                time.sleep(.01)
        yield registry
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _handle(value):
    if not isinstance(value, dict):
        raise Invalid("Resources: нужен structured native handle, не PID")
    for key in ("runtime", "nativeId", "launchId"):
        _text(value.get(key), "handle." + key)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise Invalid("Resources: неверный native handle") from exc


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (TypeError, ValueError) as exc:
        raise Invalid("Resources: неверное время lease") from exc


def _load(path, ctx):
    if not path.exists():
        return {"schema": 1, "scope": "project", "root": ctx["root"], "leases": {},
                "profile": None, "profileBasis": None, "knownHostLimits": {}}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Invalid("Resources: повреждён registry") from exc
    if (not isinstance(registry, dict) or type(registry.get("schema")) is not int
            or registry["schema"] != 1 or registry.get("scope") != "project"
            or registry.get("root") != ctx["root"] or not isinstance(registry.get("leases"), dict)):
        raise Invalid("Resources: registry не соответствует проекту")
    active_profile = registry.get("profile")
    if active_profile is None:
        if registry.get("profileBasis") is not None or registry["leases"]:
            raise Invalid("Resources: отсутствует authoritative profile")
    elif registry.get("profileBasis") != digest(_profile(active_profile)):
        raise Invalid("Resources: authoritative profile изменён")
    ceilings = registry.get("knownHostLimits")
    if (not isinstance(ceilings, dict) or set(ceilings) - set(KINDS)
            or any(type(cap) is not int or cap <= 0 for cap in ceilings.values())):
        raise Invalid("Resources: повреждены известные host limits")
    if active_profile is not None and any(active_profile["limits"][kind] > cap for kind, cap in ceilings.items()):
        raise Invalid("Resources: authoritative profile превышает host limit")
    for ident, row in registry["leases"].items():
        if not isinstance(row, dict) or row.get("id") != ident:
            raise Invalid("Resources: неверный lease")
        _context(row)
        if row["root"] != ctx["root"] or row.get("kind") not in KINDS or row.get("status") not in _OCCUPIED | _TERMINAL:
            raise Invalid("Resources: неверный lease identity/status")
        if row.get("handle") is not None:
            _handle(row["handle"])
        if row["status"] in ("running", "completed") and row.get("handle") is None:
            raise Invalid("Resources: отсутствует bound handle")
        if row["status"] == "launch_failed" and row.get("handle") is not None:
            raise Invalid("Resources: launch_failed после bind")
        if row["status"] in _TERMINAL:
            if row.get("confirmed") is not True:
                raise Invalid("Resources: terminal lease без подтверждения")
            _instant(row.get("finishedAt"))
        for key in ("createdAt", "heartbeatAt", "expiresAt"):
            _instant(row.get(key))
        if row["status"] in ("reserved", "running") and _instant(row["expiresAt"]) <= datetime.now(timezone.utc):
            row["status"] = "orphaned"  # Still occupies capacity.
    return registry


def _owned(registry, ctx, ident):
    row = registry["leases"].get(ident)
    if row is None or any(row[key] != ctx[key] for key in _IDENTITY):
        raise Invalid("Resources: lease принадлежит другому root/run/session/owner")
    return row


def _touch(row):
    row["heartbeatAt"] = timestamp()
    row["expiresAt"] = (datetime.now(timezone.utc) + timedelta(seconds=_TTL)).isoformat()


def reserve(resource_context, kind):
    ctx = _context(resource_context)
    if kind not in KINDS or ctx["sessionKind"] == "local-service" and kind != "services":
        raise Invalid("Resources: неверный вид управляемого ресурса")
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        if registry["profileBasis"] is None:
            registry.update(profile=ctx["profile"], profileBasis=ctx["profileBasis"],
                            knownHostLimits=dict(ctx["profile"].get("hostLimits", {})))
        elif registry["profileBasis"] != ctx["profileBasis"]:
            raise Invalid("Resources: context использует устаревший profile basis; перечитайте профиль")
        occupied = sum(row["kind"] == kind and row["status"] in _OCCUPIED for row in registry["leases"].values())
        cap = ctx["profile"]["limits"][kind]
        cap = min(cap, registry["knownHostLimits"].get(kind, cap))
        if occupied >= cap:
            raise CapacityRefused(kind, cap, occupied)
        row = {**ctx, "id": uuid.uuid4().hex, "kind": kind, "status": "reserved",
               "createdAt": timestamp(), "handle": None}
        _touch(row)
        registry["leases"][row["id"]] = row
        atomic_json(path, registry)
        return copy.deepcopy(row)


def bind(resource_context, lease_id, handle):
    ctx, handle = _context(resource_context), _handle(handle)
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        row = _owned(registry, ctx, lease_id)
        if row["status"] in _TERMINAL or row["handle"] is not None and row["handle"] != handle:
            raise Invalid("Resources: lease уже завершён либо handle не совпадает")
        if any(other["id"] != lease_id and other.get("handle") == handle
               for other in registry["leases"].values()):
            raise Invalid("Resources: native handle уже связан с другим lease")
        row["handle"], row["status"] = handle, "running"
        _touch(row)
        atomic_json(path, registry)
        return copy.deepcopy(row)


def finish(resource_context, lease_id, *, handle=None, outcome="completed", confirmed=False):
    ctx = _context(resource_context)
    if type(confirmed) is not bool or outcome not in ("completed", "launch_failed"):
        raise Invalid("Resources: неверное подтверждение завершения")
    handle = _handle(handle) if handle is not None else None
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        row = _owned(registry, ctx, lease_id)
        if handle != row["handle"]:
            raise Invalid("Resources: завершение требует exact bound handle")
        if outcome == "launch_failed" and row["handle"] is not None:
            raise Invalid("Resources: launch_failed разрешён только до bind")
        if confirmed and outcome == "completed" and handle is None:
            raise Invalid("Resources: completed требует bound handle")
        target = outcome if confirmed else "orphaned"
        if row["status"] in _TERMINAL:
            if target != row["status"]:
                raise Invalid("Resources: противоречивый повтор завершения")
            return copy.deepcopy(row)
        row["status"] = target
        row["lastOutcome"] = outcome
        row["confirmed"] = confirmed
        if confirmed:
            row["finishedAt"] = timestamp()
        atomic_json(path, registry)
        return copy.deepcopy(row)


def heartbeat(resource_context, lease_id, *, handle=None):
    ctx = _context(resource_context)
    handle = _handle(handle) if handle is not None else None
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        row = _owned(registry, ctx, lease_id)
        if handle != row["handle"] or row["status"] in _TERMINAL:
            raise Invalid("Resources: heartbeat требует собственный действующий handle")
        _touch(row)
        atomic_json(path, registry)
        return copy.deepcopy(row)


def reconcile(resource_context, observations, *, complete=False):
    ctx = _context(resource_context)
    if not isinstance(observations, list) or type(complete) is not bool:
        raise Invalid("Resources: нужен явный inventory")
    with _locked(ctx) as path:
        registry = _load(path, ctx)
        seen = set()
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("status") not in ("running", "completed"):
                raise Invalid("Resources: неверное наблюдение native lifecycle")
            ident = _text(observation.get("leaseId"), "leaseId")
            if ident in seen:
                raise Invalid("Resources: повтор lease в inventory")
            seen.add(ident)
            row = _owned(registry, ctx, ident)
            handle = _handle(observation.get("handle"))
            if handle != row["handle"]:
                raise Invalid("Resources: inventory handle не совпадает")
            status = observation["status"]
            if row["status"] in _TERMINAL:
                if status != row["status"]:
                    raise Invalid("Resources: завершённый lease нельзя возродить")
                continue
            row["status"] = status
            _touch(row)
            if status == "completed":
                row.update(confirmed=True, lastOutcome="completed", finishedAt=timestamp())
        # Even a complete inventory's omissions are not exit observations.
        atomic_json(path, registry)
        return copy.deepcopy(registry)


def view(resource_context):
    ctx = _context(resource_context)
    with _locked(ctx) as path:
        return copy.deepcopy(_load(path, ctx))
