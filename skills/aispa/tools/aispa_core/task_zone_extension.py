"""Append exact files to a failed task's existing scope, never its review budget.

Epoch zero retains the submitted work and critic1. A planner-authored, journal-
bound extension opens a new contract/card/test-intent epoch for the repair.
No owner words, task requirements, native identity, attempt or round is invented.
"""
import copy
import json
import stat

from . import design_contract, integration, participants, phase_receipts, planning, review_policy, scale, task_tiers
from .contracts import (artifact_valid, basis, definition, nonempty, require,
                        task_closure, task_contract, validate_plan)
from .portable import relative_name
from .store import Invalid, canonical_zone, confined, digest

EVENT = "task-zone-extended:"
MAX_EXTENSIONS = 8
MAX_FILES = 32


def _json(store, ref):
    require(isinstance(ref, dict) and artifact_valid(store, ref), "Zone extension: изменён материал")
    return store.json(ref)


def _unconsumed(task, attempt):
    return (task.get("status") == "pending" and not task.get("startedAt")
            and not any(attempt.get(k) for k in ("actors", "writers", "actorHistory", "submitted", "rounds",
                "repair", "acceptance", "verifiedCompletion", "terminal", "testIntent", "testBaseline", "lightBaseline"))
            and not any(r.get("actors") or r.get("writers") for r in attempt.get("cardHistory", [])))


def _stage(task, attempt):
    rounds = attempt.get("rounds", [])
    require(task.get("status") == "running" and task.get("phase") == "repair"
            and not task_tiers.worker_only(attempt) and not attempt.get("terminal")
            and not attempt.get("acceptance") and not attempt.get("verifiedCompletion")
            and len(rounds) == 1 and rounds[0].get("number") == 1 and rounds[0].get("passed") is False
            and attempt.get("submitted") and not attempt.get("repair") and not attempt.get("repairHistory")
            and not attempt.get("actors", {}).get("critic2"),
            "Zone extension: только running/repair после первого non-PASS до сдачи ремонта и назначения critic2")


def _review_anchor(store, tid, attempt, events=None):
    """Match epoch zero to real submission/critic events, not a later seal.

    The shared resolver only reads event order and exact records; it does not
    select an incremental policy or reinterpret a legacy intent schema.
    """
    rounds = attempt.get("rounds")
    if (not isinstance(attempt.get("submitted"), dict) or not isinstance(rounds, list)
            or not rounds or not isinstance(rounds[0], dict)):
        return False
    from .incremental_test_intent import _review_anchor as decision_anchor
    events = store.task_timeline(tid) if events is None else events
    return decision_anchor(store, {"id": tid}, attempt, events)


def _exact_file(store, value):
    require(isinstance(value, str) and value == relative_name(value) and value != "."
            and not any(c in value for c in "*?"),
            "Zone extension: нужны точные файлы, не каталоги или шаблоны")
    name = canonical_zone(value)
    design_contract.safe_reference_name(value)
    path = confined(store.root, name)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass  # A future exact file is legal; its later type stays constrained.
    else:
        require(stat.S_ISREG(mode), "Zone extension: нужен обычный файл, не каталог, ссылка или специальный файл")
    return name


def require_exact_files(store, attempt):
    """Current filesystem boundary only; never reinterpret historical evidence."""
    for row in attempt.get("zoneExtensions", []):
        for name in row["files"]:
            _exact_file(store, name)


def epochs(attempt):
    rows = attempt.get("zoneExtensions", [])
    if not rows:
        return [{"contract": attempt.get("contract"), "roles": attempt.get("roles", {}), "afterSequence": 0}]
    return [{**rows[0]["before"], "afterSequence": 0},
            *[{**r["after"], "afterSequence": r["beforeSequence"] + 1} for r in rows]]


def contract_at(attempt, sequence):
    return next(e["contract"] for e in reversed(epochs(attempt)) if sequence > e["afterSequence"])


def card_contract(attempt, role, ref):
    """A critic1 card remains in epoch zero; other cards advance monotonically."""
    for epoch in reversed(epochs(attempt)):
        if epoch["roles"].get(role) == ref:
            # critic1 is intentionally retained, so find its original contract.
            if role == "critic1":
                return epochs(attempt)[0]["contract"]
            return epoch["contract"]
    return None


def actor_card_valid(attempt, role, aid, card):
    target = attempt["roles"][role]["sha256"]
    for row in attempt.get("zoneExtensions", []):
        if (aid in row.get("continuedActors", [])
                and card == row["before"]["roles"][role]["sha256"]):
            card = row["after"]["roles"][role]["sha256"]
    return card == target


def _add_baseline(store, tid, attempt, files):
    source = attempt.get("lightBaseline")
    old = _json(store, source)
    require(isinstance(old, dict) and isinstance(old.get("records"), list),
            "Zone extension: нужен полный исходный снимок старта, не текущего кода")
    starts = [e for e in store.task_timeline(tid) if e.get("event") == "task-started:" + tid
              and (e["state"]["taskHistory"][tid] or [{}])[-1].get("id") == attempt["id"]]
    first = store.event_pair(starts[0]["sequence"])[0] if starts else None
    require(first and first["state"]["taskHistory"][tid][-1].get("lightBaseline") == source,
            "Zone extension: исходный снимок не привязан к первому старту попытки")
    records = integration._records_map(old["records"])
    return {"source": copy.deepcopy(source), "records": [[p, records.get(p)] for p in files]}


def baseline_records(store, attempt):
    records = {}
    for row in attempt.get("zoneExtensions", []):
        baseline = row["addedBaseline"]
        source = _json(store, baseline["source"])
        old = dict(source["records"])
        require(baseline["records"] == [[p, old.get(p)] for p in row["files"]],
                "Zone extension: изменён исходный снимок добавленных файлов")
        records.update((p, value) for p, value in baseline["records"] if value is not None)
    return records


def _missing_roles(attempt):
    return [role for role in task_tiers.roles(attempt) if role not in attempt.get("roles", {})]


def _added_cards_valid(store, old, new):
    """Only a focused epoch may add cards: the attempt's missing roles, on the new contract."""
    added = set(new["roles"]) - set(old["roles"])
    if not set(old["roles"]) <= set(new["roles"]):
        return False
    if not added:
        return True
    if not (review_policy.focused(new) and added == set(_missing_roles(old)) and "critic1" not in added):
        return False
    for role in added:
        card = store.json(new["roles"][role])
        if not (isinstance(card, dict) and card.get("role") == role and card.get("attempt") == new["id"]
                and card.get("contract") == new["contract"]):
            return False
    return True


def history_valid(store, state, tid, attempt):
    """Bind every epoch to its exact append, retaining all earlier state bytes."""
    from .team import _sealed_valid
    rows = attempt.get("zoneExtensions", [])
    require(isinstance(rows, list) and (scale.enabled(state) or len(rows) <= MAX_EXTENSIONS),
            "Zone extension: повреждена история")
    budget = scale.check_value(rows, state)
    events = store.task_timeline(tid)
    require(not rows or _review_anchor(store, tid, attempt, events),
            "Zone extension: исходная сдача и первый критик не связаны с фактическими событиями")
    matched = [e for e in events if e.get("event") == EVENT + tid
                and (e["state"].get("taskHistory", {}).get(tid) or [{}])[-1].get("id") == attempt["id"]]
    require(len(matched) == len(rows), "Zone extension: потеряна или добавлена эпоха")
    pairs = [store.event_pair(e["sequence"]) for e in matched]
    relevant = [e for e, _ in pairs]
    for index, (row, event) in enumerate(zip(rows, relevant)):
        if budget is not None:
            budget.check(path="task-zone history", progress=index, progress_unit="items")
        require(_sealed_valid(store, row) and type(row.get("schema")) is int
                and row["schema"] in (1, 2) and row.get("run") == state["run"]
                and row.get("task") == tid and row.get("attempt") == attempt["id"]
                and row.get("epoch") == index + 1 and row.get("beforeSequence") == event["sequence"] - 1
                and row.get("previous") == (rows[index - 1]["record"]["sha256"] if index else None),
                "Zone extension: повреждена квитанция эпохи")
        require(isinstance(row.get("files"), list) and row["files"]
                and (row["schema"] == 2 and scale.enabled(state)
                     or row["schema"] == 1 and index < MAX_EXTENSIONS and len(row["files"]) <= MAX_FILES)
                and (not index or rows[index - 1]["schema"] <= row["schema"]),
                "Zone extension: версия квитанции изменила прежний предел или policy")
        old_event = pairs[index][1]
        before, after = old_event["state"], event["state"]
        old = before["taskHistory"][tid][-1]
        new = after["taskHistory"][tid][-1]
        require(row["beforeState"] == digest(before) and row["afterState"] == digest({
                    **after, "updatedAt": before.get("updatedAt"),
                    "taskHistory": {**after["taskHistory"], tid: [*after["taskHistory"][tid][:-1],
                        {**new, "zoneExtensions": new["zoneExtensions"][:-1]}]}})
                and new["zoneExtensions"] == rows[:index + 1]
                and row["before"] == {"contract": old["contract"], "roles": old["roles"]}
                and row["after"] == {"contract": new["contract"], "roles": new["roles"]},
                "Zone extension: квитанция расходится с переходом журнала")
        require(before["actors"] == after["actors"] and row["continuedActors"] == sorted(set(old["actors"].values()) | set(old["writers"]))
                and all(before.get(k) == after.get(k) for k in ("policy", "requirements", "briefs", "spec", "core", "sealed", "checkMode", "checkModeHistory")),
                "Zone extension: изменены полномочия или прежние actors")
        require(all(old.get(k) == new.get(k) for k in set(old) | set(new)
                    if k not in {"contract", "roles", "verification", "zoneExtensions"}),
                "Zone extension: попытка потеряла историю или бюджет")
        require(phase_receipts.suitable(participants.resolve(store, before, row["actor"]), "planner")
                and after.get("phaseReceipts", [])[:-1] == before.get("phaseReceipts", [])
                and after["phaseReceipts"][-1]["actor"] == row["actor"],
                "Zone extension: отсутствует авторство реального planner/coordinator")
        old_task = next(t for t in before["tasks"] if t["id"] == tid)
        _stage(old_task, old)
        new_task = next(t for t in after["tasks"] if t["id"] == tid)
        require(new_task == {**old_task, "zones": [*old_task["zones"], *row["files"]]}
                and old["contract"] != new["contract"] and new["contract"] == task_contract(after, tid),
                "Zone extension: расширение не является монотонным")
        for role, ref in new["roles"].items():
            require(artifact_valid(store, ref), "Zone extension: повреждена карточка эпохи")
        require(_added_cards_valid(store, old, new), "Zone extension: эпоха добавила карточку вне ролей попытки")
        require(old["roles"]["critic1"] == new["roles"]["critic1"], "Zone extension: изменён первый критик")
        baseline_records(store, {"zoneExtensions": [row]})
    if rows:
        require(attempt["contract"] == rows[-1]["after"]["contract"]
                and attempt["roles"] == rows[-1]["after"]["roles"], "Zone extension: текущая эпоха изменена")
        saved = relevant[-1]["state"]["taskHistory"][tid][-1]
        frozen = {"id", "briefCount", "reviewPolicy", "authorization", "createdAt", "submitted",
                  "submissionHistory", "cardHistory", "lightBaseline", "testBaseline", *task_tiers.FIELDS}
        require(all(attempt.get(k) == saved.get(k) for k in frozen)
                and attempt.get("rounds", [])[:1] == saved.get("rounds", [])
                and attempt.get("writers", [])[:len(saved.get("writers", []))] == saved.get("writers", [])
                and attempt.get("actorHistory", [])[:len(saved.get("actorHistory", []))] == saved.get("actorHistory", [])
                and attempt.get("testIntentHistory", [])[:len(saved.get("testIntentHistory", []))] == saved.get("testIntentHistory", [])
                and all(state.get("actors", {}).get(aid) == actor
                        for aid, actor in relevant[0]["state"].get("actors", {}).items()),
                "Zone extension: потеряна исходная история после расширения")
    return True


def inspect(store, state, tid):
    task = next((t for t in state["tasks"] if t["id"] == tid), None)
    require(task is not None, "Zone extension: неизвестная задача")
    attempt = state["taskHistory"][tid][-1]
    _stage(task, attempt)
    history_valid(store, state, tid, attempt)
    return {"task": tid, "attempt": attempt["id"], "contract": attempt["contract"],
            "epoch": len(attempt.get("zoneExtensions", [])), "zones": task["zones"],
            "requirements": task["requirements"], "requiredFindingIds": sorted(review_policy.required_ids(attempt["rounds"][0])),
            "expectedJournalSha256": store.read_journal()["sha256"],
            "scope": "Exact additional files for existing requirements; no new authority or review round"}


def prepare(host, tid, report, *, actor):
    """Read-only validation and full proposed-plan advice."""
    host.build_ready()
    store, state = host.store, host.s
    budget = scale.check_value(report, state)
    from .team import _actor_valid, _history_valid, _record_valid, _writer_valid
    require(phase_receipts.enabled(state) and phase_receipts.suitable(participants.resolve(store, state, actor), "planner"),
            "Zone extension: нужен зарегистрированный реальный planner/coordinator --actor")
    task, attempt = host.task(tid), host.pipeline._attempt(tid) if hasattr(host, "pipeline") else (state["taskHistory"][tid][-1])
    require(_history_valid(store, state, tid), "Zone extension: повреждена история попытки")
    _stage(task, attempt)
    require_exact_files(store, attempt)
    require(all(_record_valid(store, r) for r in [attempt["submitted"], *attempt["rounds"]]),
            "Zone extension: повреждены исходная сдача или первый критик")
    require(all(_actor_valid(store, state, attempt, role) for role in ("worker", "critic1"))
            and _writer_valid(store, state, attempt, attempt["submitted"]["actor"])
            and attempt["rounds"][0]["actor"] == attempt["actors"]["critic1"]
            and attempt["rounds"][0]["basis"] == attempt["submitted"]["basis"]
            and _review_anchor(store, tid, attempt),
            "Zone extension: исходная независимая цепочка повреждена")
    from . import test_intent
    test_intent.require_revision_available(state, attempt)
    require(test_intent.required(state, attempt) and test_intent.history_valid(store, state, tid)
            and attempt["submitted"].get("testIntent") == attempt["rounds"][0].get("testIntent")
            and test_intent._has_red(test_intent._saved(store, attempt["testIntent"])["report"]),
            "Zone extension: нужен сохранённый автоматический контракт исходной эпохи")
    require(scale.enabled(state) or len(attempt.get("zoneExtensions", [])) < MAX_EXTENSIONS,
            "Zone extension: исчерпаны расширения")
    if attempt.get("repairPlans"):
        from . import repairs
        if attempt.get("zoneExtensions"):
            repairs.epoch_history(store, state, tid)
        else:
            repairs.read(store, state, tid)
    require(isinstance(report, dict) and set(report) == {"attempt", "contract", "reason", "files", "planReview"}
            and report["attempt"] == attempt["id"] and report["contract"] == attempt["contract"]
            and nonempty(report["reason"]) and (scale.enabled(state) or len(report["reason"]) <= 2400),
            "Zone extension: отчёт устарел или имеет неверную схему")
    files = report["files"]
    require(isinstance(files, list) and files and (scale.enabled(state) or len(files) <= MAX_FILES),
            "Zone extension: нужны точные файлы (прежняя policy: 1–32)")
    names = []
    for row in files:
        if budget is not None:
            budget.check(path="task-zone files", progress=len(names), progress_unit="items")
        require(isinstance(row, dict) and set(row) == {"path", "requirement", "reason"}
                and row["requirement"] in task["requirements"] and nonempty(row["reason"])
                and (scale.enabled(state) or len(row["reason"]) <= 1600),
                "Zone extension: файл требует существующего требования и обоснования")
        name = _exact_file(store, row["path"])
        from .team import _inside
        require(name not in names and not _inside(name, task["zones"]), "Zone extension: повтор или уже разрешённый файл")
        names.append(name)
    proposed = copy.deepcopy(state["tasks"])
    next(t for t in proposed if t["id"] == tid)["zones"].extend(names)
    validated = validate_plan(state, [definition(t) for t in proposed])
    require([definition(t) for t in validated] == [definition(t) for t in proposed], "Zone extension: план меняет другие границы")
    if state.get("policy", {}).get("designContract"):
        require(design_contract.valid_scenarios(store, state) and design_contract.valid_interfaces(store, state, check_sources=False),
                "Zone extension: повреждены сценарии или интерфейсы")
        design_contract.validate_plan_bindings(store, state, proposed)
    # The ordinary plan-review validator includes the delivery validator.
    size = planning.review(store, state, proposed, report["planReview"], persist=False)
    require(size.get("deliveryDigest") == (state.get("planReview") or {}).get("deliveryDigest"),
            "Zone extension: delivery-контракт менять нельзя")
    candidate = {**state, "tasks": proposed}
    affected = []
    for other in proposed:
        history = state.get("taskHistory", {}).get(other["id"], [])
        if other["id"] != tid and history and history[-1]["contract"] != task_contract(candidate, other["id"]):
            require(all(_unconsumed(other, a) for a in history), "Zone extension: зависимая задача уже использовала контракт")
            affected.append(other["id"])
    if state.get("policy", {}).get("integrationReview"):
        require(integration.choose_task(store, state, proposed) == state.get("integrationTask"),
                "Zone extension: нельзя менять финальную интеграционную задачу")
    baseline = _add_baseline(store, tid, attempt, names)
    return {"plan": proposed, "files": names, "descendants": affected, "addedBaseline": baseline,
            "advice": planning.analyze(store, state, proposed)}


def apply(host, tid, report, *, actor, expected_journal_sha256):
    """Caller holds the normal journal lock, just as for plan/submit/repair."""
    store, state = host.store, host.s
    journal = store.read_journal()
    require(journal["sha256"] == expected_journal_sha256 and digest(journal["last"]["state"]) == digest(state),
            "Zone extension: stale CAS; перечитайте журнал")
    proposal = prepare(host, tid, report, actor=actor)
    from .team import TaskPipeline, _artifact_json, _seal
    old = copy.deepcopy(state)
    attempt = state["taskHistory"][tid][-1]
    before = {"contract": attempt["contract"], "roles": copy.deepcopy(attempt["roles"])}
    pipeline = host.pipeline if hasattr(host, "pipeline") else TaskPipeline(host)
    state["tasks"] = proposal["plan"]
    state["planReview"] = planning.review(store, old, proposal["plan"], report["planReview"])
    attempt["contract"] = task_contract(state, tid)
    attempt["verification"] = None
    for role, ref in list(attempt["roles"].items()):
        if role == "critic1":
            continue
        card = _json(store, ref)
        card.update(contract=attempt["contract"], zones=host.task(tid)["zones"],
                    closure=[definition(t) for t in pipeline._closure(tid)],
                    zoneEpoch=len(attempt.get("zoneExtensions", [])) + 1, previousCard=copy.deepcopy(ref))
        attempt["roles"][role] = {**_artifact_json(store, "roles", card),
                                  "checks": ref["checks"], "specialization": ref["specialization"]}
    if review_policy.focused(attempt):
        # Focused cards appear on assignment, so critic2 has none yet: the new
        # epoch carries it, bound to the extended contract (see history_valid).
        for role in _missing_roles(attempt):
            pipeline._make_card(tid, attempt, role, (attempt.get("roleOverrides") or {}).get(role, {}))
    for descendant in proposal["descendants"]:
        pipeline._cards(descendant, state["taskHistory"][descendant][-1])
    state["gates"]["plan"] = {"passed": True, "basis": basis(store, state, "plan"),
                              "report": _artifact_json(store, "plans", [definition(t) for t in state["tasks"]])}
    phase_receipts.record(store, state, "planner", state["gates"]["plan"]["report"], actor=actor)
    attempt.setdefault("zoneExtensions", [])
    row = {"schema": 2 if scale.enabled(state) else 1,
           "run": state["run"], "task": tid, "attempt": attempt["id"],
           "epoch": len(attempt.get("zoneExtensions", [])) + 1, "actor": actor,
           "beforeSequence": journal["count"], "beforeState": digest(old), "afterState": digest(state),
           "before": before, "after": {"contract": attempt["contract"], "roles": copy.deepcopy(attempt["roles"])},
           "continuedActors": sorted(set(attempt["actors"].values()) | set(attempt["writers"])),
           "files": proposal["files"], "addedBaseline": proposal["addedBaseline"],
           "request": _artifact_json(store, "zone-extension-requests", report),
           "previousPlanReview": old.get("planReview"), "previousPlanGate": old["gates"]["plan"],
           "previous": (attempt.get("zoneExtensions") or [{}])[-1].get("record", {}).get("sha256")}
    receipt = _seal(store, row)
    attempt.setdefault("zoneExtensions", []).append(receipt)
    require_exact_files(store, attempt)
    store.append(state, EVENT + tid)
    history_valid(store, state, tid, attempt)
    require(planning.valid(store, state) and phase_receipts.valid(store, state, "planner"),
            "Zone extension: read-back плана не прошёл")
    return copy.deepcopy(receipt)
