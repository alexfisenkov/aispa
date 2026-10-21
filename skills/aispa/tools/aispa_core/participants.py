"""Native participant identities across AISpa phases.

Task actors and the knowledge writer retain their original authoritative stores.
This module resolves them together with general phase participants; it never
claims that a declared native identity was authenticated by the host.
"""
import json

from .contracts import artifact_valid, require
from .store import digest, timestamp


ROLES = frozenset({
    "coordinator", "idea", "source-reader", "brief-analyst", "brief-reviewer",
    "branch-reviewer", "integration-reviewer", "recovery-reviewer", "discovery", "coverage", "planner",
    "worker", "critic1", "critic2", "acceptor", "blind", "knowledge-writer",
})
INDEPENDENT_ROLES = frozenset({
    "brief-reviewer", "branch-reviewer", "integration-reviewer", "recovery-reviewer", "coverage",
    "critic1", "critic2", "acceptor", "blind", "knowledge-writer",
})
TASK_ROLES = frozenset({"worker", "critic1", "critic2", "acceptor"})


def _sealed(store, actor, field):
    ref = actor.get(field)
    if not isinstance(ref, dict) or not artifact_valid(store, ref):
        return False
    try:
        return json.loads(store.text(ref)) == {k: v for k, v in actor.items() if k != field}
    except (OSError, ValueError, TypeError):
        return False


def _identity(actor, aid):
    return (isinstance(actor, dict) and actor.get("id") == aid
            and aid == digest([actor.get("runtime"), actor.get("nativeId"), actor.get("session")]))


def _knowledge(state):
    memory = state.get("knowledge") or {}
    if not isinstance(memory, dict):
        return []
    history = memory.get("writerHistory", [])
    if not isinstance(history, list):
        history = []
    return [row for row in [memory.get("writer"), *history]
            if isinstance(row, dict)]


def identity_conflicts(state, runtime, native_id, *, exclude_actor=None):
    """Return IDs reusing one native identity within the same runtime.

    This checks all current-run registries without trusting a changed session
    to make a reviewer independent. It never equates IDs from different hosts.
    """
    require(isinstance(runtime, str) and runtime.strip()
            and isinstance(native_id, str) and native_id.strip(),
            "Нужны runtime и native ID для проверки повторного участника")
    runtime, native_id = runtime.strip().casefold(), native_id.strip()
    rows = [*state.get("participants", {}).items(), *state.get("actors", {}).items()]
    rows.extend((row.get("id"), row) for row in _knowledge(state))
    return sorted({aid for aid, row in rows if isinstance(aid, str) and aid != exclude_actor
                   and isinstance(row, dict) and isinstance(row.get("runtime"), str)
                   and row["runtime"].casefold() == runtime and row.get("nativeId") == native_id})


def require_unused_identity(state, runtime, native_id, *, exclude_actor=None):
    """Guard for team and knowledge assignment before creating a new role."""
    require(not identity_conflicts(state, runtime, native_id, exclude_actor=exclude_actor),
            "Native identity уже участвовала в этом прогоне")


def resolve(store, state, actor):
    """Return a validated identity, or None. No synthetic role is inferred."""
    if not isinstance(actor, str) or not actor:
        return None
    general = state.get("participants", {}).get(actor)
    if general is not None:
        if (_identity(general, actor) and general.get("run") == state.get("run")
                and _sealed(store, general, "record")):
            return dict(general)
        return None
    task = state.get("actors", {}).get(actor)
    if task is not None:
        if _identity(task, actor) and _sealed(store, task, "record"):
            return dict(task)
        return None
    for author in _knowledge(state):
        if author.get("id") == actor:
            if (_identity(author, actor) and author.get("run") == state.get("run")
                    and author.get("role") == "knowledge-writer" and _sealed(store, author, "artifact")):
                return dict(author)
            return None
    return None


def actor_check(store, state, actor, role):
    resolved = resolve(store, state, actor)
    return bool(resolved and (resolved.get("role") == role or
                state.get("policy", {}).get("phaseReceipts") and resolved.get("role") == "coordinator"
                and role in {"idea", "brief-analyst", "discovery", "planner"}))


def list_participants(store, state):
    """Validated view; malformed legacy records are omitted, never repaired."""
    ids = set(state.get("participants", {})) | set(state.get("actors", {}))
    ids.update(row["id"] for row in _knowledge(state) if isinstance(row.get("id"), str))
    return [row for aid in sorted(aid for aid in ids if isinstance(aid, str))
            if (row := resolve(store, state, aid)) is not None]


def register(store, state, role, runtime, native_id, session, *, model=None,
             external=False, fresh=True):
    """Register one host-reported identity for a non-task phase role.

    ``fresh`` is a declaration, not proof of an independent model context.
    Unknown named roles require the explicit external flag and cannot gain the
    independent-review semantics of built-in roles.
    """
    recovery = (role == "recovery-reviewer" and state.get("status") == "paused"
                and isinstance(state.get("journalRecovery"), dict)
                and state["journalRecovery"].get("pending") is True)
    require(state.get("status") == "running" or recovery,
            "Участников назначают только в активном прогоне или при восстановлении журнала")
    require(isinstance(role, str) and bool(role.strip()), "Нужна роль участника")
    role = role.strip()
    require(role not in TASK_ROLES and role != "knowledge-writer",
            "Роли задач и автора знаний назначаются своими командами")
    require(type(external) is bool and type(fresh) is bool, "Неверные флаги участника")
    require((role in ROLES and not external) or (role not in ROLES and external),
            "Внешняя роль требует отдельного имени и явного external=true")
    require(role not in INDEPENDENT_ROLES or fresh, "Проверяющему нужен свежий контекст")
    require(all(isinstance(v, str) and v.strip() for v in (runtime, native_id, session)),
            "Нужны реальные runtime, native ID и session")
    require(model is None or isinstance(model, str) and model.strip(), "Неверный ID модели")
    runtime, native_id, session = runtime.strip().casefold(), native_id.strip(), session.strip()
    aid = digest([runtime, native_id, session])
    require(resolve(store, state, aid) is None and aid not in state.get("actors", {})
            and aid not in state.get("participants", {})
            and all(row.get("id") != aid for row in _knowledge(state)),
            "Native identity уже зарегистрирована")
    # The same native ID in a different session still names the same host actor;
    # do not let that make a reviewer appear independent.
    require_unused_identity(state, runtime, native_id)
    record = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session,
              "role": role, "external": external, "fresh": fresh, "model": model,
              "run": state["run"], "at": timestamp(), "identityEvidence": "host-declared"}
    record["record"] = store.artifact("participants", (json.dumps(record, ensure_ascii=False,
        sort_keys=True, indent=2) + "\n").encode(), ".json")
    state.setdefault("participants", {})[aid] = record
    return aid
