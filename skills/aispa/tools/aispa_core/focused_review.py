"""Rules of the 0.5.0 ``focusedReview`` policy that belong to no single task.

The engine, not the plan, decides how heavy a task is: a foundation (other
tasks depend on it, or a zone is on the wide sensitive list of agent rules,
prompts, CI, Docker, deploy, lock files, schemas and contracts), money, keys,
access or personal data (the narrow ``sensitive_task`` list), or a critical
user scenario (applicable ``productFlows`` of its requirements). The owner's
check mode then decides only how many reviews a task gets. A critic reads what
changed in its own attempt; accepted ancestors are re-checked only when this
attempt touched their files. Runs without the flag never call this module.
"""
import json
import re

from .contracts import active, artifact_valid, require, task_closure
from .integration import AXES, _changes, _covers
from .store import Invalid, canonical_zone, confined

BLOCKING_CLASSES = ("brief_drift", "broken_scenario", "security", "data_loss", "failing_tests")
LENSES = {"fast": [AXES],
          "balanced": [("behavior", "data_security"), ("integration", "maintainability", "test_quality")],
          "max": [("behavior",), ("integration", "maintainability"), ("data_security", "test_quality")]}
WEIGHTS = ("light", "standard", "heavy")
RANK = {name: index for index, name in enumerate(WEIGHTS)}
FULL_ROLES = ("worker", "critic1", "critic2")
WORKER_ROLES = ("worker",)
# Candidate path words inside criteria: letters, digits and path punctuation.
PATH_TOKEN = re.compile(r"[\w][\w./-]*", re.UNICODE)


def enabled(state):
    return state.get("policy", {}).get("focusedReview") is True


def _mode(state):
    from . import check_modes  # check_modes imports contracts only; resolved at call time
    return check_modes.current(state)


def flow_requirements(store, state):
    """Requirement IDs whose features declare applicable product flows (critical user scenarios)."""
    if store is None or not state.get("policy", {}).get("criticalProductFlows") or not state.get("featureScenarios"):
        return set()
    from . import design_contract
    try:
        report = design_contract._saved(store, state, "featureScenarios", "featureScenarioHistory", "feature-scenarios")
    except (Invalid, OSError, ValueError, TypeError, KeyError):
        return set()
    return {feature.get("requirement") for feature in report.get("features", [])
            if isinstance(feature, dict) and (feature.get("productFlows") or {}).get("applicability") == "applicable"}


def critical_flows(store, state, task):
    return bool(set(task.get("requirements", [])) & flow_requirements(store, state))


def heavy_reasons(state, task, store=None):
    """Why the engine treats a task as heavy; an empty list means it is not."""
    from . import task_tiers
    reasons = []
    if any(task["id"] in other.get("dependsOn", []) for other in state.get("tasks", [])):
        reasons.append("foundation: other tasks depend on it")
    wide = [zone for zone in task.get("zones", []) if not task_tiers.standard_zone(zone)]
    if wide:
        reasons.append("foundation path: " + ", ".join(sorted(wide)[:8]))
    if task_tiers.sensitive_task(state, task):
        reasons.append("money, keys, access or personal data")
    if critical_flows(store, state, task):
        reasons.append("critical user scenario (productFlows)")
    return reasons


def task_weight(state, task, store=None):
    """heavy | standard | light; light keeps the old narrow rules of task_tiers.validate."""
    if heavy_reasons(state, task, store):
        return "heavy"
    return "light" if task.get("tier") == "light" else "standard"


def critic_roles(state, task, attempt=None, store=None):
    """Roles of an attempt: the mode decides only how many reviews a task gets.

    fast — a critic only for the narrow sensitive list and critical scenarios;
    balanced — for every heavy task; max — for every task. critic2 is the
    conditional repair re-check (and the mandatory second review in max).
    """
    mode = _mode(state)
    if mode == "max":
        return FULL_ROLES
    if mode == "fast":
        from . import task_tiers
        sensitive = task_tiers.sensitive_task(state, task) or critical_flows(store, state, task)
        return FULL_ROLES if sensitive else WORKER_ROLES
    return FULL_ROLES if task_weight(state, task, store) == "heavy" else WORKER_ROLES


def second_round_required(state, task, attempt=None, store=None):
    """max mode: a heavy task gets a second fresh critic after the first round, PASS or repair."""
    if attempt is not None and "secondRound" in attempt:
        return attempt["secondRound"] is True
    return _mode(state) == "max" and task_weight(state, task, store) == "heavy"


def freeze(state, attempt, task, store=None, *, promoted=False):
    """Write the engine's weight and review roles into a new attempt; the plan keeps its own tier."""
    for key in ("plannedTier", "checkMode", "criticRoles", "secondRound", "weightReasons"):
        attempt.pop(key, None)
    planned = task.get("tier", "heavy")
    weight = "heavy" if promoted else task_weight(state, task, store)
    reasons = heavy_reasons(state, task, store)
    attempt["tier"] = weight
    if weight != planned:
        attempt["plannedTier"] = planned
    attempt["checkMode"] = _mode(state)
    attempt["criticRoles"] = list(FULL_ROLES if promoted else critic_roles(state, task, attempt, store))
    attempt["secondRound"] = bool(attempt["criticRoles"] != list(WORKER_ROLES)
                                  and attempt["checkMode"] == "max" and weight == "heavy")
    attempt["weightReasons"] = reasons or (["promoted by the worker"] if promoted else [])
    return attempt


def validate_plan(store, state, tasks):
    """A declared tier below the computed weight is refused with the engine's reason."""
    proposed = dict(state, tasks=tasks)
    for task in tasks:
        declared = task.get("tier", "heavy")
        computed = task_weight(proposed, task, store)
        require(RANK.get(declared, RANK["heavy"]) >= RANK[computed],
                "Задача %s объявлена %s, но движок вычислил %s (%s); тяжесть задачи определяет движок"
                % (task["id"], declared, computed, "; ".join(heavy_reasons(proposed, task, store)) or "правила light"))


def _records(store, ref, label):
    require(isinstance(ref, dict) and artifact_valid(store, ref), "Нет снимка " + label + " попытки")
    payload = json.loads(store.text(ref))
    require(isinstance(payload, dict) and isinstance(payload.get("records"), list), "Повреждён снимок " + label + " попытки")
    return payload["records"]


def changes_since(store, ref, label="начала", after=None):
    """Changed path rows since a team-records snapshot; never file bytes."""
    return _changes(_records(store, ref, label), store.code_records() if after is None else after)


def _ancestors(state, tid, attempt):
    skipped = set(attempt.get("deferredDependencies", []))
    return [t for t in task_closure(state, tid) if t["id"] != tid and t["id"] not in skipped]


def changed_ancestors(store, state, tid, attempt, after=None):
    """Accepted ancestors whose zones changed since the attempt's ``lightBaseline``."""
    paths = [row["path"] for row in changes_since(store, attempt.get("lightBaseline"), after=after)]
    return sorted(t["id"] for t in _ancestors(state, tid, attempt)
                  if any(_covers(zone, path) for zone in t["zones"] for path in paths))


def acceptance_snapshot(store, state, tid, attempt):
    """The code the accepting critic judged, so history recomputes the same changed ancestors."""
    records = store.code_records()
    data = (json.dumps({"records": records}, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return {"codeSnapshot": store.artifact("team-records", data, ".json"),
            "changedAncestors": changed_ancestors(store, state, tid, attempt, after=records)}


def sealed_ancestors(store, state, tid, attempt, accepted):
    """Changed ancestors of a sealed focused acceptance, recomputed from its own snapshots."""
    after = _records(store, accepted.get("codeSnapshot"), "приёмки")
    ancestors = changed_ancestors(store, state, tid, attempt, after=after)
    require(accepted.get("changedAncestors") == ancestors, "Сохранённые изменённые предки расходятся со снимками попытки")
    return ancestors


def attempt_changes(store, state, tid, attempt, ref, label="начала"):
    """Rows changed since a snapshot inside the task's and its ancestors' zones."""
    task = next(t for t in state["tasks"] if t["id"] == tid)
    zones = list(task["zones"]) + [z for t in _ancestors(state, tid, attempt) for z in t["zones"]]
    return [row for row in changes_since(store, ref, label) if any(_covers(zone, row["path"]) for zone in zones)]


def _existing(store, name):
    try:
        return confined(store.root, name).exists()
    except (Invalid, OSError, ValueError):
        return False


def _named_paths(store, texts):
    found = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for token in PATH_TOKEN.findall(text):
            name = token.rstrip("./-")
            if ("/" in name or "." in name) and _existing(store, name):
                found.add(canonical_zone(name))
    return found


def _overlaps(left, right):
    left, right = canonical_zone(left), canonical_zone(right)
    return _covers(left, right) or _covers(right, left)


def criteria_files(store, state, task):
    """Files the task's criteria rest on; the critic opens them on demand (story 123)."""
    paths = {canonical_zone(z) for z in task.get("readZones", [])}
    if state.get("interfaceContract"):
        from . import design_contract
        try:
            report = design_contract._saved(store, state, "interfaceContract", "interfaceContractHistory",
                                            "interface-contracts")
        except (Invalid, OSError, ValueError, TypeError, KeyError):
            report = {"interfaces": []}
        for item in report.get("interfaces", []):
            if any(_overlaps(zone, consumer) for zone in task["zones"] for consumer in item.get("consumers", [])):
                paths.add(canonical_zone(item["ownerZone"]))
                paths.update(canonical_zone(ref["file"]) for ref in item.get("references", []))
    requirements = {r["id"]: r for r in active(state)}
    texts = list(task.get("criteria", []))
    for rid in task.get("requirements", []):
        texts.extend(requirements.get(rid, {}).get("acceptance", []))
    paths |= _named_paths(store, texts)
    return sorted(name for name in paths if name not in ("", ".") and _existing(store, name))
