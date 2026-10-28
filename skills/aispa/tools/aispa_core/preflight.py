"""Source-bound host observations and continuation checks, without native calls.

A hash proves binding to a host report, not that Python authenticated the host.
Checking is read-only. Only explicit adoption appends through the caller's lock.
"""
import copy
import hashlib
import json
import re
from pathlib import Path
import stat

from . import participants, scale
from .contracts import require
from .store import Invalid, confined, digest, file_hash, timestamp
from .portable import is_link


PURPOSES = ("continue", "seal", "browser", "remote", "identity")
DENIAL = re.compile(r"delegation.{0,35}(?:forbidden|denied|prohibited)|do not spawn|"
                    r"(?:запрет|запрещ).{0,45}(?:делег|субагент)|не\s+(?:запуска|использ).{0,30}субагент", re.I)


# Story 66: what the person enables when the environment forbids sub-agents, named for the host they use.
DELEGATION_DISABLED = ("Субагенты в этой среде выключены: разрешите их в настройках Codex или уберите запрет "
                       "из своих правил и начните новый чат")
DELEGATION_DISABLED_BY_RUNTIME = {
    "codex": DELEGATION_DISABLED,
    "claude-code": ("Субагенты в этой среде выключены: разрешите запуск субагентов (инструмент Agent) в разрешениях "
                    "Claude Code или уберите запрет из своих правил (CLAUDE.md) и начните новый чат"),
}
DELEGATION_DISABLED_UNKNOWN = ("Субагенты в этой среде выключены: разрешите их — в Codex в его настройках, в Claude Code "
                               "запуск субагентов (инструмент Agent) в разрешениях — или уберите запрет из своих правил "
                               "и начните новый чат")
DELEGATION_CAPABILITIES = frozenset({"delegation", "independentRoles"})
# resumeReport: a participant the new chat cannot see, accepted only without open work.
LOST = "lost"


def enabled(state):
    return state.get("policy", {}).get("managedExecution") is True


def delegation_disabled(runtime):
    """The pointer for the host the report came from; a neutral text naming both when it is unknown."""
    return DELEGATION_DISABLED_BY_RUNTIME.get(runtime if isinstance(runtime, str) else None,
                                              DELEGATION_DISABLED_UNKNOWN)


def _open_work(state, aid):
    """(task or repair, role) rows a participant still owes; finished or reported roles owe nothing."""
    rows = []
    tasks = {t.get("id"): t for t in state.get("tasks", [])}
    for tid, history in sorted(state.get("taskHistory", {}).items()):
        task = tasks.get(tid)
        attempt = history[-1] if history else {}
        if (task is None or task.get("status") in ("completed", "blocked") or attempt.get("terminal")
                or attempt.get("acceptance") or attempt.get("verifiedCompletion")):
            continue
        for role, actor in sorted((attempt.get("actors") or {}).items()):
            if actor != aid:
                continue
            reported = (role.startswith("critic") and role[-1:].isdigit()
                        and len(attempt.get("rounds") or []) >= int(role[-1]))
            if not reported:
                rows.append({"task": tid, "role": role,
                             "replacement": "assign %s --role %s с новым участником" % (tid, role)})
    for repair in state.get("finalRepairs", []) or []:
        if repair.get("status") not in ("open", "submitted"):
            continue
        worker = (repair.get("worker") or {}).get("id")
        checker = ((repair.get("rounds") or [{}])[-1].get("checker") or {}).get("id") if repair.get("rounds") else None
        if repair.get("status") == "open" and worker == aid:
            rows.append({"task": repair.get("id"), "role": "worker",
                         "replacement": "final-repair assign --role worker с новым участником"})
        if repair.get("status") == "submitted" and checker == aid:
            rows.append({"task": repair.get("id"), "role": "checker",
                         "replacement": "final-repair assign --role checker с новым участником"})
    return rows


def open_work(state, aid):
    """Public form of the open-work rule: final_repairs replaces a lost repair seat only while it is still owed."""
    return _open_work(state, aid)


def _repair_people(state):
    """Distinct seated workers and checkers of final repairs (sealed identity records)."""
    seen, people = set(), []
    for repair in state.get("finalRepairs", []) or []:
        for person in [repair.get("worker"), *[row.get("checker") for row in repair.get("rounds") or []]]:
            if isinstance(person, dict) and isinstance(person.get("id"), str) and person["id"] not in seen:
                seen.add(person["id"])
                people.append(person)
    return people


def _pin(state):
    repairs = state.get("runtimeRepairs") or []
    return repairs[-1]["runtimePath"] if repairs else "runtime"


def _package_digest(path, *, budget=None):
    require(path.is_dir() and not is_link(path), "Preflight: pinned runtime отсутствует")
    rows = {}
    budget = budget or scale.Budget()
    retained = 0
    for file in path.rglob("*"):
        budget.check(0, path=str(file))
        require(not is_link(file), "Preflight: ссылка в pinned runtime")
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            name = file.relative_to(path).as_posix()
            retained += 256 + len(name) * 4
            budget.check_memory(retained, path=str(file))
            previous_memory = budget.base_memory
            budget.base_memory += retained
            try:
                rows[name] = file_hash(file, budget=budget, nofollow=True)
            finally:
                budget.base_memory = previous_memory
    budget.check(0, path=str(path))
    result = digest(rows)
    budget.check(0, path=str(path))
    return result


def identity(store, state, *, budget=None):
    journal = store.read_journal(budget=budget)
    require(journal["last"]["state"]["run"] == state["run"] == store.run.name,
            "Preflight: active run не совпадает с журналом")
    require(digest(journal["last"]["state"]) == digest(state),
            "Preflight: состояние не сохранено или устарело относительно журнала")
    result = {"root": str(store.root), "run": state["run"], "runtimeDigest": state.get("runtimeDigest"),
            "runtimePath": _pin(state), "journalSequence": journal["last"]["sequence"],
            "journalHash": journal["last"]["hash"], "stateDigest": digest(state)}
    if budget is not None:
        budget.check(0, path="preflight identity")
    return result


def _artifact(store, ref, *, state=None, budget=None, source_metadata=None):
    require(isinstance(ref, dict) and isinstance(ref.get("file"), str)
            and isinstance(ref.get("sha256"), str), "Preflight: нужен источник с file/SHA")
    path = confined(store.run, ref["file"])
    return scale.read_file(path, allow_manifest=scale.enabled(state if state is not None else store.read_journal()["last"]["state"]),
                           expected_sha256=ref["sha256"], budget=budget,
                           source_metadata=source_metadata)


def _host(store, host, state, *, budget=None):
    require(isinstance(host, dict) and set(host) == {"source"}, "Preflight: нужен source-bound host report")
    value = json.loads(_artifact(store, host["source"], state=state, budget=budget))
    if budget is not None:
        budget.check(0, path="host report")
    require(isinstance(value, dict) and value.get("schema") == 1, "Preflight: неверный host report")
    return value


def _sources(store, state, *, budget=None):
    refs, seen = [], set()
    budget = budget or scale.Budget()

    def add(ref, kind):
        if not isinstance(ref, dict) or "file" not in ref or "sha256" not in ref:
            return
        key = (ref["file"], ref["sha256"])
        if key in seen:
            return
        physical = {}
        raw = _artifact(store, ref, state=state, budget=budget, source_metadata=physical)
        path = confined(store.run, ref["file"])
        previous_memory = budget.base_memory
        try:
            budget.check_memory(len(raw) * 4, path=str(path))
            budget.base_memory += len(raw) * 4
            current = path.stat()
            budget.check(current.st_size, path=str(path))
            generation = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            require(not is_link(path) and stat.S_ISREG(current.st_mode)
                    and generation == physical["generation"]
                    and physical["sha256"] == ref["sha256"],
                    "Preflight: source changed during reading")
            refs.append({"kind": kind, "file": ref["file"], "absolutePath": str(path),
                         "sha256": ref["sha256"], "payloadSha256": hashlib.sha256(raw).hexdigest(),
                         "representation": physical["representation"]})
        finally:
            budget.base_memory = previous_memory
        accounted = 256 + 4 * (len(kind) + len(ref["file"]) + len(ref["sha256"]))
        budget.check_memory(accounted, path=ref["file"])
        budget.base_memory += accounted
        seen.add(key)

    for ref in state.get("briefs", []):
        add(ref, "brief")
    for key in ("spec", "carryover", "handoff", "cleanrun", "verification"):
        value = state.get(key)
        if key == "spec":
            add(value, "specification")
        elif isinstance(value, dict):
            for ref in value.values():
                add(ref, key)
    for task in state.get("tasks", []):
        history = state.get("taskHistory", {}).get(task["id"], [])
        if not history:
            continue
        current = history[-1]
        for ref in current.get("roles", {}).values():
            add(ref, "role")
        for key in ("testIntent", "repairPlan", "repair"):
            value = current.get(key)
            add(value, key)
            if isinstance(value, dict):
                add(value.get("report"), key)
    budget.check(0, path="preflight sources")
    return refs


def _relocation(store, state, host, *, budget=None):
    previous = state.get("projectRoot", str(store.root))
    if Path(previous).resolve() == store.root:
        return
    ref = host.get("relocation", {}).get("source")
    value = json.loads(_artifact(store, ref, state=state, budget=budget))
    if budget is not None:
        budget.check(0, path="relocation report")
    require(isinstance(value.get("fromRoot"), str) and isinstance(value.get("toRoot"), str)
            and Path(value["fromRoot"]).resolve() == Path(previous).resolve()
            and Path(value["toRoot"]).resolve() == store.root
            and value.get("run") == state["run"] and value.get("runtimeDigest") == state["runtimeDigest"],
            "Preflight: relocation identity не совпадает")
    brief = next((b for b in state["briefs"] if b["id"] == value.get("brief")), None)
    require(brief and isinstance(value.get("quote"), str) and value["quote"].strip()
            and value["quote"] in _artifact(store, brief, state=state, budget=budget).decode("utf-8"),
            "Preflight: relocation требует текущую цитату владельца")


def _check(store, state, envelope, purpose, *, current_basis=True):
    require(purpose in PURPOSES, "Preflight: неизвестная цель")
    budget = scale.Budget()
    expected = identity(store, state, budget=budget)
    report = {"ok": False, "basis": digest(expected), "identity": expected, "purpose": purpose,
              "blockers": [], "actions": [], "requiredSources": [],
              "evidenceLevel": "source-bound host report; not host authentication",
              "authority": {"existingPermissionsOnly": True}, "host": None}

    def block(code, detail, message=None):
        extra = {"message": message} if message else {}
        report["blockers"].append({"code": code, "detail": str(detail), **extra})
        report["actions"].append({"action": "resolve-" + code.lower(), "detail": str(detail), **extra,
                                  "requiresReply": False})

    try:
        report["requiredSources"] = _sources(store, state, budget=budget)
        require(_package_digest(confined(store.run, _pin(state)), budget=budget) == state.get("runtimeDigest"),
                "Preflight: pinned runtime digest mismatch")
        host = _host(store, envelope, state, budget=budget)
        disabled = delegation_disabled(host.get("runtime"))
        _relocation(store, state, host, budget=budget)
        observed = host.get("identity")
        require(isinstance(observed, dict), "Preflight: host identity отсутствует")
        keys = ("root", "run", "runtimeDigest", "runtimePath")
        if current_basis:
            keys += ("journalSequence", "journalHash", "stateDigest")
        for key in keys:
            actual = observed.get(key)
            if key == "root" and isinstance(actual, str):
                actual = str(Path(actual).resolve())
            require(actual == expected.get(key), "Preflight: mismatch " + key)
        require(host.get("runtime") == state.get("runtime") and isinstance(host.get("session"), str)
                and host["session"].strip(), "Preflight: runtime/current session отсутствуют или не совпадают")
        require(isinstance(host.get("observedAt"), str), "Preflight: нет времени наблюдения host")
        required = ({"browser"} if purpose == "browser" else {"remote"} if purpose == "remote"
                    else set() if purpose == "identity" else {"delegation", "independentRoles", "processes"})
        capabilities = host.get("capabilities")
        require(isinstance(capabilities, dict), "Preflight: capabilities отсутствуют")
        for name in sorted(required):
            row = capabilities.get(name, {})
            if row.get("status") != "allowed" or not row.get("source"):
                block("CAPABILITY", name + ": " + str(row.get("status", "unknown")),
                      disabled if name in DELEGATION_CAPABILITIES else None)
        tools = host.get("tools")
        require(isinstance(tools, list) and all(isinstance(t, dict) and isinstance(t.get("name"), str)
                for t in tools), "Preflight: неверный фактический список инструментов")
        available = [t["name"].casefold() for t in tools if t.get("status") == "available"]
        for name, words in (("delegation", ("spawn", "delegate", "agent")),
                            ("processes", ("exec", "shell", "process")), ("browser", ("browser", "chrome"))):
            if name in required and not any(word in tool for tool in available for word in words):
                block("TOOL", name + ": required actual tool missing",
                      disabled if name == "delegation" else None)
        minimal_identity = purpose in ("browser", "identity")
        rules = host.get("rules", [])
        require(isinstance(rules, list) and (minimal_identity or rules), "Preflight: applicable rules не прочитаны")
        if not minimal_identity:
            required_rules = set(state.get("projectContext", {}).get("instructionFiles", []))
            for name in ("AGENTS.md", "AGENTS.override.md"):
                path = store.root / name
                if path.exists() or is_link(path):
                    required_rules.add(name)
            provided_rules = {row.get("file") for row in rules if isinstance(row, dict)}
            require(required_rules <= provided_rules,
                    "Preflight: applicable rule source omitted: " + ", ".join(sorted(required_rules - provided_rules)))
        from .verified_reads import physical_read
        for rule in rules:
            path = confined(store.root, rule.get("file", ""))
            raw = physical_read(path, budget)
            require(hashlib.sha256(raw).hexdigest() == rule.get("sha256"),
                    "Preflight: rule SHA mismatch: " + str(path))
            text = raw.decode("utf-8")
            budget.check_memory(len(raw) * 4 + len(text) * 4, path=str(path))
            budget.check(0, path=str(path))
            if "delegation" in required and DENIAL.search(text):
                block("RULE", {"file": rule["file"], "sha256": rule["sha256"], "quote": DENIAL.search(text).group()},
                      disabled)
            for decision in rule.get("decisions", []):
                require(isinstance(decision.get("quote"), str) and decision["quote"].strip()
                        and decision["quote"] in text, "Preflight: rule decision quote отсутствует")
                if decision.get("action") in required | {"all"} and decision.get("decision") != "allow":
                    block("RULE", {"file": rule["file"], **decision},
                          disabled if decision.get("action") in DELEGATION_CAPABILITIES | {"all"} else None)
            budget.base_memory += len(raw) * 4 + len(text) * 4
        if purpose not in ("browser", "identity"):
            assignments = host.get("assignments")
            require(isinstance(assignments, list), "Preflight: live assignments не наблюдались")
            lost_allowed = state.get("policy", {}).get("resumeReport") is True

            def refuse_lost(aid):
                for row in _open_work(state, aid):
                    block("ASSIGNMENT", "%s: участник не виден в этой среде, но у него открытая работа — задача %s, "
                                        "роль %s; замените его штатно: %s"
                          % (aid, row["task"], row["role"], row["replacement"]))
            listed = set()
            for actor in participants.list_participants(store, state):
                listed.add(actor["id"])
                matches = [a for a in assignments if a.get("actor") == actor["id"]
                           and a.get("nativeId") == actor["nativeId"] and a.get("session") == actor["session"]]
                status = matches[0].get("status") if len(matches) == 1 else None
                if lost_allowed and status == LOST:
                    refuse_lost(actor["id"])
                    continue
                if status not in ("live", "completed"):
                    block("ASSIGNMENT", actor["id"] + ": live/completed identity unknown; preserve history and replace explicitly")
            if lost_allowed:
                # Final-repair people are not task participants: a lost one still may not leave a repair open.
                for person in _repair_people(state):
                    if person["id"] not in listed and any(
                            a.get("actor") == person["id"] and a.get("nativeId") == person.get("nativeId")
                            and a.get("session") == person.get("session") and a.get("status") == LOST
                            for a in assignments):
                        refuse_lost(person["id"])
        report["host"] = {"runtime": host["runtime"], "session": host["session"], "source": envelope["source"],
                          "observedAt": host["observedAt"]}
        budget.check(0, path="preflight")
    except Invalid as error:
        from .verified_reads import ResourceLimit
        if isinstance(error, ResourceLimit):
            raise
        block("IDENTITY_OR_SOURCE", error)
    except (OSError, ValueError, TypeError, KeyError) as error:
        block("HOST_SOURCE", error)
    report["ok"] = not report["blockers"]
    budget.check(0, path="preflight result")
    return report


def check(store, state, host=None, *, purpose="continue"):
    return _check(store, state, host, purpose)


def adopt(flow, host, *, expected_basis=None, purpose="continue", save=True):
    report = check(flow.store, flow.s, host, purpose=purpose)
    require(report["ok"], "Preflight blocked: " + json.dumps(report["blockers"], ensure_ascii=False))
    require(expected_basis is None or expected_basis == report["basis"], "Preflight: stale adoption basis")
    require(identity(flow.store, flow.s) == report["identity"], "Preflight: basis changed before adoption")
    observation = {"purpose": purpose, **report["host"], "basis": report["basis"], "at": timestamp()}
    observation["record"] = flow.store.artifact("host-preflight", (json.dumps(observation, sort_keys=True) + "\n").encode(), ".json")
    flow.s["hostPreflight"] = observation
    flow.s["hostSession"] = observation["session"]
    flow.s.setdefault("hostSessionHistory", []).append(copy.deepcopy(observation))
    if flow.s.get("projectRoot", str(flow.store.root)) != str(flow.store.root):
        flow.s.setdefault("rootBindings", []).append({"previous": flow.s["projectRoot"], "current": str(flow.store.root), "source": host["source"]})
    flow.s["projectRoot"] = str(flow.store.root)
    if save:
        flow.save("host-context-adopted:" + purpose)
    return report


def require_adopted(store, state, *, purpose="seal"):
    if not enabled(state):
        return
    saved = state.get("hostPreflight")
    require(isinstance(saved, dict) and state.get("hostSession") == saved.get("session"),
            "Preflight: сначала host-context с реальными возможностями среды")
    value = json.loads(_artifact(store, saved.get("record"), state=state))
    require(value == {k: v for k, v in saved.items() if k != "record"}, "Preflight: adoption record изменён")
    report = _check(store, state, {"source": saved["source"]}, purpose, current_basis=False)
    require(report["ok"], "Preflight blocked: " + json.dumps(report["blockers"], ensure_ascii=False))
