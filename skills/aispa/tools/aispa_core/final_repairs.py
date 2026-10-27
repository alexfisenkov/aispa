"""Final repairs of branch findings and blind-acceptance divergences: state["finalRepairs"].

Only runs with the ``focusedReview`` policy have them. A repair never enters
state["tasks"] and never changes the plan basis. One repair of kind
``findings`` fixes the confirmed warnings and the branch reviewers' blocking
findings; repairs of kind ``blind`` fix what the blind acceptance found
diverging (at most two cycles). Each repair round is checked by a fresh
participant who reads only the diff between candidates and the item list.

The candidate chain ``state["candidateChain"]`` starts at the code the branch
reviewers saw (with the task progress of that moment) and grows by one checked
candidate per repair round; acceptance needs it to end at the current code.
"""
import copy
import json

from .contracts import nonempty, require
from .store import canonical_zone, digest, timestamp

MAX_ROUNDS = 2
MAX_BLIND_REPAIRS = 2
KINDS = ("findings", "blind")
FINISHED = ("passed", "exhausted")  # both are in measured_estimate.REPAIR_FINISHED
ITEM_STATUSES = ("fixed", "not_fixed")


def enabled(state):
    return state.get("policy", {}).get("focusedReview") is True


def repairs(state):
    rows = state.get("finalRepairs") or []
    require(isinstance(rows, list), "Повреждён список finalRepairs")
    return rows


def chain(state):
    rows = state.get("candidateChain") or []
    require(isinstance(rows, list), "Повреждена цепочка кандидатов")
    return rows


def repaired(state):
    """True once a repair has started the chain: task proofs then follow the chain, not live code."""
    return enabled(state) and bool(state.get("candidateChain"))


def unfinished(state):
    return next((r for r in repairs(state) if r.get("status") not in FINISHED), None)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _snapshot(store):
    records = store.code_records()
    return {"code": digest(records), "records": store.artifact("final-repair-records", _json({"records": records}), ".json")}


def _records(store, snapshot):
    from .contracts import artifact_valid
    require(isinstance(snapshot, dict) and artifact_valid(store, snapshot.get("records", {})),
            "Снимок кандидата ремонта отсутствует или изменён")
    records = store.json(snapshot["records"]).get("records")
    require(isinstance(records, list) and digest(records) == snapshot.get("code"), "Снимок кандидата ремонта повреждён")
    return records


def _progress(state):
    return [{"id": t["id"], "status": t["status"], "proof": t.get("proof")} for t in state["tasks"]]


def _zones(zones):
    require(isinstance(zones, list) and zones and all(nonempty(z) for z in zones), "Ремонту нужен непустой список зон")
    result = sorted({canonical_zone(z) for z in zones})
    require(all(z not in ("", ".") for z in result), "Зона ремонта должна быть конкретным путём, а не всем деревом")
    return result


def _start_chain(store, state):
    """The first repair binds the branch-reviewed candidate and the task proofs current at it."""
    from . import integration
    from .check_modes import is_deferred
    from .team import task_proof_valid
    require(integration.branch_reviewed(store, state),
            "Ремонт открывается после проверки всей ветки по всем линзам режима и разбора всех предупреждений")
    require(all(is_deferred(state, t, store) or t["status"] == "blocked"
                or t["status"] == "completed" and task_proof_valid(store, state, t["id"]) for t in state["tasks"]),
            "До ремонта нужны текущие доказательства всех задач")
    base = _snapshot(store)
    state["candidateChain"] = [{"source": "branch", **base, "progress": _progress(state),
                                "integrationBasis": integration.context(store, state)["basis"], "at": timestamp()}]
    return base


def _items_for(store, state, kind):
    if kind == "findings":
        from . import integration
        require(not any(r["kind"] == "findings" for r in repairs(state)),
                "Ремонт находок ветки уже открывался: находки чинит одна задача ремонта")
        require(integration.branch_reviewed(store, state),
                "Ремонт открывается после проверки всей ветки по всем линзам режима и разбора всех предупреждений")
        return sorted(row["id"] for row in state.get("collectedFindings", []) if row.get("status") == "confirmed")
    from .contracts import gate_current
    used = sum(1 for r in repairs(state) if r["kind"] == "blind")
    require(used < MAX_BLIND_REPAIRS,
            "Циклов ремонта слепой приёмки не больше %d; третий не открывается: сдайте с остатками (finish)" % MAX_BLIND_REPAIRS)
    gate = state.get("gates", {}).get("acceptance")
    require(gate and gate.get("passed") is False and gate_current(store, state, "acceptance"),
            "Ремонт расхождений открывается после актуальной непройденной итоговой приёмки")
    require(all(r.get("acceptance", {}).get("sha256") != gate["report"]["sha256"] for r in repairs(state)),
            "По этой приёмке ремонт уже открывался")
    return repairable_items(gate)


def repairable_items(gate):
    """Requirements a blind repair can fix: failures bound to a requirement."""
    return sorted({f["requirement"] for f in gate.get("failures", [])
                   if isinstance(f, dict) and isinstance(f.get("requirement"), str)})


def open(store, state, kind, items, zones):
    """Open one repair; the caller saves the event."""
    require(enabled(state), "final-repair доступен только в прогонах с focusedReview")
    require(kind in KINDS, "Вид ремонта: findings или blind")
    require(unfinished(state) is None, "Сначала закончите открытый ремонт")
    zones = _zones(zones)
    expected = _items_for(store, state, kind)
    require(expected, "Нечего чинить: нет подтверждённых находок" if kind == "findings"
            else "Нечего чинить: у провала приёмки нет пунктов с требованием; "
                 "потерянное (unmapped) и несвязанное сдаётся с остатками: finish")
    if items is not None:
        require(isinstance(items, list) and len(items) == len(set(items)) and sorted(items) == expected,
                "Ремонт берёт все пункты сразу: " + ", ".join(expected))
    if chain(state):
        require(chain_current(store, state), "Цепочка кандидатов не заканчивается текущим кодом")
        base = {key: chain(state)[-1][key] for key in ("code", "records")}
    else:
        base = _start_chain(store, state)
    repair = {"id": "F%02d" % (len(repairs(state)) + 1), "kind": kind, "items": expected, "zones": zones,
              "base": base, "worker": None, "rounds": [], "status": "open", "openedAt": timestamp()}
    if kind == "blind":
        gate = state["gates"]["acceptance"]
        repair["acceptance"] = copy.deepcopy(gate["report"])
        repair["recheckOf"] = copy.deepcopy(state["blindReview"]["report"])
    state.setdefault("finalRepairs", []).append(repair)
    return {"repair": copy.deepcopy(repair), "packet": _worker_packet(store, state, repair)}


def _identity(state, role, runtime, native_id, session, model, reasoning, repair):
    from . import participants
    require(all(nonempty(v) for v in (runtime, native_id, session)), "Нужны реальные runtime, native ID и session")
    runtime, native_id, session = runtime.strip().casefold(), native_id.strip(), session.strip()
    participants.require_unused_identity(state, runtime, native_id)
    require(native_id not in used_identities(state), "Участник уже работал в ремонтах этого прогона; нужен свежий")
    return {"id": digest([runtime, native_id, session]), "runtime": runtime, "nativeId": native_id,
            "session": session, "role": "final-repair-" + role, "model": model, "reasoning": reasoning,
            "repair": repair["id"], "run": state["run"], "at": timestamp(), "identityEvidence": "host-declared"}


def used_identities(state):
    """IDs and native IDs of every repair participant: they may not review the branch or accept the result."""
    used = set()
    for repair in state.get("finalRepairs") or []:
        people = [repair.get("worker")] + [row.get("checker") for row in repair.get("rounds", [])]
        for person in people:
            if isinstance(person, dict):
                used.update(value for value in (person.get("id"), person.get("nativeId")) if isinstance(value, str))
    return used


def _route(state, role):
    from . import model_routes
    try:
        return model_routes.route(state, "worker" if role == "worker" else "integration-reviewer")
    except Exception:  # advice only; the table may not cover this runtime
        return None


def assign(store, state, role, *, runtime, native_id, session, model=None, reasoning=None):
    from .team import _seal
    repair = unfinished(state)
    require(repair is not None, "Нет открытого ремонта")
    require(role in ("worker", "checker"), "Роль ремонта: worker или checker")
    if role == "worker":
        require(repair["worker"] is None, "Исполнитель ремонта уже назначен")
    else:
        current = repair["rounds"][-1] if repair["rounds"] else None
        require(current is not None and current.get("check") is None, "Проверяющего назначают после сдачи ремонта")
        require(current.get("checker") is None, "Проверяющий этого круга уже назначен")
    person = _seal(store, _identity(state, role, runtime, native_id, session, model, reasoning, repair))
    if role == "worker":
        repair["worker"] = person
        packet = _worker_packet(store, state, repair)
    else:
        repair["rounds"][-1]["checker"] = person
        packet = _checker_packet(store, state, repair)
    return {"repair": repair["id"], "role": role, "actor": person["id"], "route": _route(state, role), "packet": packet}


def _item_rows(store, state, repair):
    if repair["kind"] == "findings":
        rows = {row["id"]: row for row in state.get("collectedFindings", [])}
        return [copy.deepcopy(rows[i]) for i in repair["items"] if i in rows]
    from . import blind
    return blind.item_quotes(store, state, repair["items"])


def _worker_packet(store, state, repair):
    return {"repair": repair["id"], "kind": repair["kind"], "items": _item_rows(store, state, repair),
            "zones": repair["zones"], "baseCode": repair["base"]["code"], "maximumRounds": MAX_ROUNDS,
            "template": {"repair": repair["id"], "worker": "", "summary": "",
                         "items": [{"id": i, "note": ""} for i in repair["items"]]}}


def _base(repair):
    done = [row for row in repair["rounds"] if row.get("check")]
    return done[-1]["submission"]["candidate"] if done else repair["base"]


def _changes(store, before, after_records):
    from . import integration
    return integration._changes(_records(store, before), after_records)


def _checker_packet(store, state, repair):
    current = repair["rounds"][-1]
    candidate = current["submission"]["candidate"]
    changes = _changes(store, _base(repair), _records(store, candidate))
    return {"repair": repair["id"], "kind": repair["kind"], "round": current["number"],
            "items": _item_rows(store, state, repair), "zones": repair["zones"],
            "baseCode": _base(repair)["code"], "candidate": candidate["code"], "changes": changes,
            "reading": "Read the listed changes and the items only; the rest of the branch was already reviewed.",
            "template": {"repair": repair["id"], "checker": "", "independent": True, "candidate": candidate["code"],
                         "verdict": "", "items": [{"id": i, "status": "", "note": ""} for i in repair["items"]]}}


def _person(report, field, person):
    require(isinstance(person, dict) and report.get(field) in (person["id"], person["nativeId"]),
            "Отчёт подписан не назначенным участником ремонта")


def submit(store, state, report):
    repair = unfinished(state)
    require(repair is not None, "Нет открытого ремонта")
    require(isinstance(report, dict) and report.get("repair") == repair["id"], "Отчёт относится к другому ремонту")
    require(repair["worker"] is not None, "Сначала назначьте исполнителя ремонта")
    _person(report, "worker", repair["worker"])
    require(repair["status"] == "open", "Ремонт уже сдан и ждёт проверки")
    require(len(repair["rounds"]) < MAX_ROUNDS, "Кругов ремонта не больше %d" % MAX_ROUNDS)
    notes = report.get("items")
    require(isinstance(notes, list) and sorted(row.get("id") for row in notes if isinstance(row, dict)) == repair["items"]
            and all(nonempty(row.get("note")) for row in notes), "Исполнитель описывает каждый пункт ремонта")
    candidate = _snapshot(store)
    changes = _changes(store, _base(repair), _records(store, candidate))
    require(changes, "Ремонт не изменил ни одного файла")
    from .integration import _covers
    outside = [row["path"] for row in changes if not any(_covers(zone, row["path"]) for zone in repair["zones"])]
    require(not outside, "Ремонт изменил файлы вне своих зон: " + ", ".join(outside[:8]))
    ref = store.artifact("final-repairs", _json(report), ".json")
    number = len(repair["rounds"]) + 1
    repair["rounds"].append({"number": number, "submission": {"report": ref, "candidate": candidate,
                                                              "changed": len(changes), "at": timestamp()},
                             "checker": None, "check": None})
    repair["status"] = "submitted"
    return {"repair": repair["id"], "round": number, "candidate": candidate["code"],
            "changes": [row["path"] for row in changes], "next": "final-repair assign --role checker"}


def check(store, state, report):
    """A fresh participant checks the round by its diff; the checked candidate joins the chain."""
    from . import findings
    repair = unfinished(state)
    require(repair is not None and repair["rounds"], "Нет сданного ремонта")
    current = repair["rounds"][-1]
    require(current.get("check") is None and current.get("checker") is not None,
            "Сначала назначьте свежего проверяющего этого круга")
    require(isinstance(report, dict) and report.get("repair") == repair["id"], "Отчёт относится к другому ремонту")
    _person(report, "checker", current["checker"])
    require(report.get("independent") is True, "Проверяющему ремонта нужен независимый контекст")
    candidate = current["submission"]["candidate"]
    require(report.get("candidate") == candidate["code"] == store.code(),
            "После сдачи ремонта код изменился; проверяют только сданного кандидата")
    rows = report.get("items")
    require(isinstance(rows, list) and sorted(row.get("id") for row in rows if isinstance(row, dict)) == repair["items"]
            and all(row.get("status") in ITEM_STATUSES and nonempty(row.get("note")) for row in rows),
            "Проверяющий отмечает каждый пункт: fixed или not_fixed с причиной")
    fixed = sorted(row["id"] for row in rows if row["status"] == "fixed")
    open_ids = sorted(set(repair["items"]) - set(fixed))
    verdict = "PASS" if not open_ids else "FAIL"
    require(report.get("verdict") == verdict, "verdict должен быть PASS только когда исправлен каждый пункт")
    at = timestamp()
    current["check"] = {"report": store.artifact("final-repairs", _json(report), ".json"), "verdict": verdict,
                        "fixed": fixed, "notFixed": open_ids, "at": at}
    state["candidateChain"].append({"source": "repair", "repair": repair["id"], "round": current["number"],
                                    "code": candidate["code"], "records": candidate["records"],
                                    "checker": current["checker"]["id"], "at": at})
    if verdict == "PASS" or current["number"] >= MAX_ROUNDS:
        repair["status"] = "passed" if verdict == "PASS" else "exhausted"
        repair["finishedAt"] = at
        if repair["kind"] == "findings":
            decisions = [{"id": i, "status": "fixed", "note": "Исправлено: " + repair["id"]} for i in fixed]
            decisions += [{"id": i, "status": "remaining", "note": "Не исправлено за %d круга" % MAX_ROUNDS}
                          for i in open_ids]
            findings.triage(state, decisions, current["checker"]["id"])
    else:
        repair["status"] = "open"
    return {"repair": repair["id"], "round": current["number"], "verdict": verdict, "status": repair["status"],
            "notFixed": open_ids, "ok": verdict == "PASS"}


def chain_current(store, state):
    """True when every repair is finished and the chain of checked candidates ends at the current code."""
    if not enabled(state):
        return True
    try:
        rows, done = chain(state), repairs(state)
        if not done:
            return not rows
        if any(r.get("status") not in FINISHED for r in done) or not rows or rows[0].get("source") != "branch":
            return False
        checked = {(r["id"], row["number"]): row["submission"]["candidate"]["code"]
                   for r in done for row in r["rounds"] if row.get("check")}
        for row in rows[1:]:
            if checked.get((row.get("repair"), row.get("round"))) != row.get("code"):
                return False
        _records(store, rows[-1])
        return rows[-1]["code"] == store.code()
    except Exception:
        return False


def tasks_settled(store, state):
    """After a repair, task proofs stand as they were at the branch-reviewed candidate."""
    from .contracts import artifact_valid
    try:
        base = chain(state)[0]
        return bool(base["progress"] == _progress(state) and all(
            artifact_valid(store, t["proof"]["report"]) and all(artifact_valid(store, a) for a in t["proof"].get("artifacts", []))
            for t in state["tasks"] if t["status"] == "completed"))
    except Exception:
        return False


def chain_proof(store, state, tid):
    """A completed task's proof after a final repair: recorded at the chain base, unchanged since, chain current."""
    if not repaired(state):
        return False
    try:
        base = {row["id"]: row for row in chain(state)[0]["progress"]}
        return (base[tid]["status"] == "completed" and tasks_settled(store, state)
                and chain_current(store, state))
    except Exception:
        return False


def findings_settled(state):
    """No warning is waiting for a decision or for the repair."""
    return not any(row.get("status") in ("open", "confirmed") for row in state.get("collectedFindings", []))


def pending_recheck(state):
    """The last blind repair, finished and not yet rechecked by a fresh acceptor."""
    rows = [r for r in state.get("finalRepairs") or [] if r.get("kind") == "blind"]
    if not rows or rows[-1].get("status") not in FINISHED:
        return None
    saved = state.get("blindReview") or {}
    return None if saved.get("repair") == rows[-1]["id"] else rows[-1]


def blind_exhausted(state):
    """Both blind repair cycles were spent and the last one was rechecked: finish delivers the remainder."""
    rows = [r for r in state.get("finalRepairs") or [] if r.get("kind") == "blind"]
    return (enabled(state) and len(rows) >= MAX_BLIND_REPAIRS and rows[-1].get("status") in FINISHED
            and (state.get("blindReview") or {}).get("repair") == rows[-1]["id"])


def blind_settled(state):
    """Focused finish may deliver a failed acceptance with its list: both cycles spent, or nothing to repair.

    A failure without requirement-bound items (only unmapped requests or
    observations tied to no requirement) has no item a blind repair could fix,
    so it goes to the remainder at once instead of leaving the run without an
    outcome. Product-flow and other gate vetoes are checked by the caller.
    """
    if not enabled(state) or unfinished(state) is not None or pending_recheck(state) is not None:
        return False
    if blind_exhausted(state):
        return True
    gate = (state.get("gates") or {}).get("acceptance") or {}
    if gate.get("passed") is not False or repairable_items(gate):
        return False
    loose = [f for f in gate.get("failures", []) if isinstance(f, dict) and f.get("requirement") is None
             and f.get("kind") != "product-flow"]
    return bool(gate.get("unmapped") or loose)


def remaining_findings(state):
    return [{"id": row["id"], "criterion": row["criterion"], "note": row["note"], "source": row["source"]}
            for row in state.get("collectedFindings", []) if row.get("status") == "remaining"]


def execute(store, state, flow, args):
    """CLI `final-repair open|assign|submit|check`: returns the receipt; each step saves its own event."""
    action = args.repair_action
    flow.build_ready()
    if action == "open":
        data = json.loads(flow.read_input(args.file))
        require(isinstance(data, dict), "Нужен объект JSON: kind, items, zones")
        receipt = open(store, state, data.get("kind"), data.get("items"), data.get("zones"))
        flow.save("final-repair-opened:" + receipt["repair"]["id"])
        return receipt
    if action == "assign":
        receipt = assign(store, state, args.role, runtime=args.runtime, native_id=args.native_id,
                         session=args.session, model=args.model, reasoning=args.reasoning)
        flow.save("final-repair-assigned:" + receipt["repair"])
        return receipt
    report = json.loads(flow.read_input(args.file))
    if action == "submit":
        receipt = submit(store, state, report)
        flow.save("final-repair-submitted:" + receipt["repair"])
        return receipt
    require(action == "check", "Шаг final-repair: open, assign, submit или check")
    receipt = check(store, state, report)
    flow.save("final-repair-checked:" + receipt["repair"])
    return receipt
