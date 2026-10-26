"""Source-bound host observations and continuation checks, without native calls.

A hash proves binding to a host report, not that Python authenticated the host.
Checking is read-only. Only explicit adoption appends through the caller's lock.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

from . import participants, scale
from .contracts import require
from .store import Invalid, confined, digest, file_hash, timestamp
from .portable import is_link


PURPOSES = ("continue", "seal", "browser", "remote", "identity")
DENIAL = re.compile(r"delegation.{0,35}(?:forbidden|denied|prohibited)|do not spawn|"
                    r"(?:запрет|запрещ).{0,45}(?:делег|субагент)|не\s+(?:запуска|использ).{0,30}субагент", re.I)


def enabled(state):
    return state.get("policy", {}).get("managedExecution") is True


def _pin(state):
    repairs = state.get("runtimeRepairs") or []
    return repairs[-1]["runtimePath"] if repairs else "runtime"


def _package_digest(path):
    require(path.is_dir() and not is_link(path), "Preflight: pinned runtime отсутствует")
    rows = {}
    for file in sorted(path.rglob("*")):
        require(not is_link(file), "Preflight: ссылка в pinned runtime")
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            rows[file.relative_to(path).as_posix()] = file_hash(file)
    return digest(rows)


def identity(store, state):
    journal = store.read_journal()
    require(journal["last"]["state"]["run"] == state["run"] == store.run.name,
            "Preflight: active run не совпадает с журналом")
    require(digest(journal["last"]["state"]) == digest(state),
            "Preflight: состояние не сохранено или устарело относительно журнала")
    return {"root": str(store.root), "run": state["run"], "runtimeDigest": state.get("runtimeDigest"),
            "runtimePath": _pin(state), "journalSequence": journal["last"]["sequence"],
            "journalHash": journal["last"]["hash"], "stateDigest": digest(state)}


def _artifact(store, ref, *, state=None):
    require(isinstance(ref, dict) and isinstance(ref.get("file"), str)
            and isinstance(ref.get("sha256"), str), "Preflight: нужен источник с file/SHA")
    path = confined(store.run, ref["file"])
    return scale.read_file(path, allow_manifest=scale.enabled(state if state is not None else store.read_journal()["last"]["state"]),
                           expected_sha256=ref["sha256"])


def _host(store, host, state):
    require(isinstance(host, dict) and set(host) == {"source"}, "Preflight: нужен source-bound host report")
    value = json.loads(_artifact(store, host["source"], state=state))
    require(isinstance(value, dict) and value.get("schema") == 1, "Preflight: неверный host report")
    return value


def _sources(store, state):
    refs, seen = [], set()

    def add(ref, kind):
        if not isinstance(ref, dict) or "file" not in ref or "sha256" not in ref:
            return
        key = (ref["file"], ref["sha256"])
        if key in seen:
            return
        raw = _artifact(store, ref, state=state)
        path = confined(store.run, ref["file"])
        physical = path.read_bytes()
        require(hashlib.sha256(physical).hexdigest() == ref["sha256"], "Preflight: source changed during reading")
        refs.append({"kind": kind, "file": ref["file"], "absolutePath": str(path),
                     "sha256": ref["sha256"], "payloadSha256": hashlib.sha256(raw).hexdigest(),
                     "representation": "manifest" if scale.is_manifest(physical) else "plain"})
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
    return refs


def _relocation(store, state, host):
    previous = state.get("projectRoot", str(store.root))
    if Path(previous).resolve() == store.root:
        return
    ref = host.get("relocation", {}).get("source")
    value = json.loads(_artifact(store, ref, state=state))
    require(isinstance(value.get("fromRoot"), str) and isinstance(value.get("toRoot"), str)
            and Path(value["fromRoot"]).resolve() == Path(previous).resolve()
            and Path(value["toRoot"]).resolve() == store.root
            and value.get("run") == state["run"] and value.get("runtimeDigest") == state["runtimeDigest"],
            "Preflight: relocation identity не совпадает")
    brief = next((b for b in state["briefs"] if b["id"] == value.get("brief")), None)
    require(brief and isinstance(value.get("quote"), str) and value["quote"].strip()
            and value["quote"] in _artifact(store, brief, state=state).decode("utf-8"),
            "Preflight: relocation требует текущую цитату владельца")


def _check(store, state, envelope, purpose, *, current_basis=True):
    require(purpose in PURPOSES, "Preflight: неизвестная цель")
    expected = identity(store, state)
    report = {"ok": False, "basis": digest(expected), "identity": expected, "purpose": purpose,
              "blockers": [], "actions": [], "requiredSources": [],
              "evidenceLevel": "source-bound host report; not host authentication",
              "authority": {"existingPermissionsOnly": True}, "host": None}

    def block(code, detail):
        report["blockers"].append({"code": code, "detail": str(detail)})
        report["actions"].append({"action": "resolve-" + code.lower(), "detail": str(detail), "requiresReply": False})

    try:
        report["requiredSources"] = _sources(store, state)
        require(_package_digest(confined(store.run, _pin(state))) == state.get("runtimeDigest"),
                "Preflight: pinned runtime digest mismatch")
        host = _host(store, envelope, state)
        _relocation(store, state, host)
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
                block("CAPABILITY", name + ": " + str(row.get("status", "unknown")))
        tools = host.get("tools")
        require(isinstance(tools, list) and all(isinstance(t, dict) and isinstance(t.get("name"), str)
                for t in tools), "Preflight: неверный фактический список инструментов")
        available = [t["name"].casefold() for t in tools if t.get("status") == "available"]
        for name, words in (("delegation", ("spawn", "delegate", "agent")),
                            ("processes", ("exec", "shell", "process")), ("browser", ("browser", "chrome"))):
            if name in required and not any(word in tool for tool in available for word in words):
                block("TOOL", name + ": required actual tool missing")
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
        for rule in rules:
            path = confined(store.root, rule.get("file", ""))
            require(path.is_file() and file_hash(path) == rule.get("sha256"), "Preflight: rule SHA mismatch: " + str(path))
            text = path.read_text(encoding="utf-8")
            if "delegation" in required and DENIAL.search(text):
                block("RULE", {"file": rule["file"], "sha256": rule["sha256"], "quote": DENIAL.search(text).group()})
            for decision in rule.get("decisions", []):
                require(isinstance(decision.get("quote"), str) and decision["quote"].strip()
                        and decision["quote"] in text, "Preflight: rule decision quote отсутствует")
                if decision.get("action") in required | {"all"} and decision.get("decision") != "allow":
                    block("RULE", {"file": rule["file"], **decision})
        if purpose not in ("browser", "identity"):
            assignments = host.get("assignments")
            require(isinstance(assignments, list), "Preflight: live assignments не наблюдались")
            for actor in participants.list_participants(store, state):
                matches = [a for a in assignments if a.get("actor") == actor["id"]
                           and a.get("nativeId") == actor["nativeId"] and a.get("session") == actor["session"]]
                if len(matches) != 1 or matches[0].get("status") not in ("live", "completed"):
                    block("ASSIGNMENT", actor["id"] + ": live/completed identity unknown; preserve history and replace explicitly")
        report["host"] = {"runtime": host["runtime"], "session": host["session"], "source": envelope["source"],
                          "observedAt": host["observedAt"]}
    except Invalid as error:
        from .verified_reads import ResourceLimit
        if isinstance(error, ResourceLimit):
            raise
        block("IDENTITY_OR_SOURCE", error)
    except (OSError, ValueError, TypeError, KeyError) as error:
        block("HOST_SOURCE", error)
    report["ok"] = not report["blockers"]
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
