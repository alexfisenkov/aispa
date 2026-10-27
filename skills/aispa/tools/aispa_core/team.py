"""Recorded native team, bounded criticism and source-bound task acceptance.

This module records declared native identities; it does not authenticate a
model or spawn agents. Role cards and decisions are immutable local artifacts.
Another attempt requires a new quoted owner brief or a separate source-bound
owner decision for one blocked task.
"""
import copy
import json
import uuid

from . import autonomy, timing, repairs, participants, integration, test_intent, mixed_evidence, review_policy, task_tiers, check_modes, owner_task_attempt
from . import findings as collected, focused_review, model_routes
from .packets import routing
from .contracts import (active, artifact_valid, definition, evidence_rows, nonempty,
                        require, task_basis, task_closure, task_contract, task_verified)
from .portable import match_name
from .store import Invalid, canonical_zone, digest, timestamp

ROLES = ("worker", "critic1", "critic2", "acceptor")
LENSES = {
    "worker": ["Результат реализован в объявленных зонах и проверен"],
    "critic1": ["Проверены логика, крайние случаи и доказательства результата"],
    "critic2": ["Независимо перепроверены результат и замечания первого раунда"],
    "acceptor": ["Результат задачи соответствует исходному запросу и ограничениям"],
}
# A lean critic also answers the former acceptor's question; drift is filed here.
ACCEPTANCE_LENS = LENSES["acceptor"][0]


def _object(value):
    require(isinstance(value, dict), "Нужен объект JSON")
    return value


def _json(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise Invalid("Нужен корректный JSON команды") from exc
    require(isinstance(value, dict), "Нужен объект JSON")
    return value


def _artifact_json(store, group, value):
    return store.artifact(group, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), ".json")


def _seal(store, record):
    result = copy.deepcopy(record)
    result["record"] = _artifact_json(store, "team-records", record)
    return result


def _sealed_valid(store, record):
    if not isinstance(record, dict) or not artifact_valid(store, record.get("record", {})):
        return False
    return _object(store.json(record["record"])) == {k: v for k, v in record.items() if k != "record"}


def _declared(attempt):
    """Class the plan gave the attempt; fast and max may run it under another."""
    return {"tier": attempt.get("plannedTier") or attempt.get("tier")}


def _card_roles(attempt):
    """Roles whose cards must exist: focused attempts create a card on assignment."""
    roles = task_tiers.roles(attempt)
    if review_policy.focused(attempt):
        return tuple(role for role in roles if role in attempt.get("roles", {}))
    return roles


def _latest(state, tid):
    history = state.get("taskHistory", {}).get(tid, [])
    require(history, "Для задачи ещё не подготовлены роли")
    return history[-1]


def _role_data(store, attempt, role):
    from . import task_zone_extension
    record = attempt.get("roles", {}).get(role)
    require(record and artifact_valid(store, record), "Повреждена карточка роли")
    card = _object(store.json(record))
    require(card.get("role") == role and card.get("attempt") == attempt["id"]
            and card.get("contract") == task_zone_extension.card_contract(attempt, role, record)
            and card.get("checks") == record.get("checks")
            and card.get("specialization") == record.get("specialization")
            and card.get("reviewPolicy", review_policy.LEGACY) == review_policy.mode(attempt)
            and all(card.get(key) == attempt.get(key) for key in task_tiers.FIELDS)
            and all(artifact_valid(store, item) for item in card.get("briefs", []))
            and artifact_valid(store, card.get("spec", {})),
            "Карточка роли не соответствует попытке")
    _card_closure(card, card.get("task"))
    deferred = card.get("deferredDependencies", [])
    require(deferred == attempt.get("deferredDependencies", []),
            "Карточка роли изменила объявленные отложенные зависимости")
    if attempt.get("zoneExtensions"):
        original_roles = attempt["zoneExtensions"][0]["before"]["roles"]
        # A focused critic2 card is born in a later epoch: epoch zero's worker card is its reference.
        original_ref = original_roles.get(role) or original_roles["worker"]
        require(artifact_valid(store, original_ref), "Повреждена исходная карточка эпохи")
        original = store.json(original_ref)
        require(original.get("deferredDependencies", []) == deferred
                and {t["id"] for t in _card_closure(original, card["task"])}
                    == {t["id"] for t in _card_closure(card, card["task"])},
                "Эпоха изменила состав зависимостей исходной карточки")
    return card


def _card_closure(card, tid):
    """Honor the sealed declaration, including old epochs that re-added it.

    Deferral is a card/attempt fact, not today's status of its predecessor.
    _role_data binds that declaration to the attempt and epoch-zero artifact.
    """
    deferred = card.get("deferredDependencies", [])
    closure = card.get("closure")
    require(isinstance(tid, str) and isinstance(deferred, list)
            and all(isinstance(t, str) and t for t in deferred)
            and deferred == sorted(set(deferred)) and tid not in deferred
            and isinstance(closure, list) and all(isinstance(t, dict) and isinstance(t.get("id"), str) for t in closure)
            and len({t["id"] for t in closure}) == len(closure) and tid in {t["id"] for t in closure},
            "Повреждён состав зависимостей карточки")
    return [t for t in closure if t["id"] not in deferred]


def _actor_valid(store, state, attempt, role, aid=None):
    from . import task_zone_extension
    aid = aid or attempt.get("actors", {}).get(role)
    actor = state.get("actors", {}).get(aid)
    if not actor or not _sealed_valid(store, actor):
        return False
    return (actor.get("id") == aid == digest([actor.get("runtime"), actor.get("nativeId"), actor.get("session")])
            and actor.get("attempt") == attempt["id"] and actor.get("role") == role
            and task_zone_extension.actor_card_valid(attempt, role, aid, actor.get("card"))
            and (role == "worker" or actor.get("fresh") is True))


def _history_valid(store, state, tid):
    history = state.get("taskHistory", {}).get(tid, [])
    if not history or len({a["id"] for a in history}) != len(history):
        return False
    if history[0].get("authorization") is not None:
        return False
    for attempt in history:
        from . import task_zone_extension
        try:
            task_zone_extension.history_valid(store, state, tid, attempt)
        except (Invalid, OSError, KeyError, ValueError, TypeError, IndexError):
            return False
        for role in attempt.get("roles", {}):
            _role_data(store, attempt, role)
        records = [attempt.get("submitted"), attempt.get("repair"), attempt.get("acceptance"), attempt.get("verifiedCompletion"),
                   *attempt.get("rounds", []), *attempt.get("submissionHistory", []), *attempt.get("repairHistory", [])]
        if any(record is not None and not _record_valid(store, record) for record in records):
            return False
        for revision in attempt.get("cardHistory", []):
            if not all(artifact_valid(store, card) for card in revision["roles"].values()):
                return False
        actor_ids = set(attempt.get("actors", {}).values()) | set(attempt.get("writers", []))
        for replacement in attempt.get("actorHistory", []):
            actor_ids.update((replacement["previous"], replacement["replacement"]))
        if any(not _sealed_valid(store, state.get("actors", {}).get(aid)) for aid in actor_ids):
            return False
    briefs = {b["id"]: (index + 1, b) for index, b in enumerate(state["briefs"])}
    for previous, attempt in zip(history, history[1:]):
        auth = attempt.get("authorization")
        if not auth or not _sealed_valid(store, auth) or auth.get("previousAttempt") != previous["id"]:
            return False
        if auth.get("kind") == "tier-escalation":
            if (not task_tiers.worker_only(previous) or task_tiers.worker_only(attempt)
                    or previous.get("rounds") or previous.get("acceptance")
                    or not nonempty(auth.get("reason"))
                    or auth.get("previousTier") != previous.get("tier") or auth.get("nextTier") != "heavy"):
                return False
            continue
        if auth.get("kind") == "check-mode":
            request = auth.get("request")
            if (previous.get("rounds") or previous.get("acceptance") or previous.get("verifiedCompletion")
                    or auth.get("mode") not in check_modes.MODES or not isinstance(request, dict)
                    or not artifact_valid(store, request)
                    or not any(row.get("request") == request and row.get("mode") == auth["mode"]
                               for row in state.get("checkModeHistory", []))):
                return False
            continue
        if auth.get("kind") == "extra-wave":
            if (not previous.get("terminal") or task_tiers.worker_only(previous) or task_tiers.worker_only(attempt)
                    or sum((a.get("authorization") or {}).get("kind") == "extra-wave" for a in history) != 1):
                return False
            continue
        if auth.get("kind") == owner_task_attempt.KIND:
            if not owner_task_attempt.authorization_valid(store, state, tid, previous, attempt, auth):
                return False
            uses = list(owner_task_attempt.decision_uses(state, auth["decisionId"]))
            if (sum(task_id == tid for task_id, _, _ in uses) != 1
                    or any((source_sha, quote) != (auth["source"]["sha256"], auth["quote"])
                           for _, source_sha, quote in uses)):
                return False
            continue
        index, source = briefs.get(auth.get("brief"), (0, None))
        if (source is None or source != auth.get("source") or not artifact_valid(store, source)
                or not previous["briefCount"] < index <= attempt["briefCount"]
                or not nonempty(auth.get("quote")) or auth["quote"] not in store.text(source)):
            return False
    return True


def _inside(path, zones):
    # Real Git paths are only case-folded like zones (Windows), never rejected
    # for reserved names; declared zones are fully canonicalised.
    name = match_name(path)
    return any(zone in ("", ".") or name == zone or name.startswith(zone + "/")
               for zone in (canonical_zone(z) for z in zones))


def _writer_valid(store, state, attempt, aid):
    return aid in attempt.get("writers", []) and _actor_valid(store, state, attempt, "worker", aid)


def _verification_valid(store, value, candidate):
    return bool(isinstance(value, dict) and value.get("exitCode") == 0 and not value.get("changedCode")
                and not any(value.get(k) for k in ("timedOut", "truncated", "redacted"))
                and value.get("candidate") == candidate and artifact_valid(store, value.get("output", {})))


def _record_valid(store, record):
    from .verified_reads import current_scope
    def check():
        if not _sealed_valid(store, record) or not artifact_valid(store, record.get("report", {})):
            return False
        if not all(artifact_valid(store, item) for item in record.get("artifacts", [])):
            return False
        if record.get("testIntent") and not artifact_valid(store, record["testIntent"]):
            return False
        if not mixed_evidence.record_valid(store, record):
            return False
        report = _object(store.json(record["report"]))
        return (report.get("basis") == record.get("basis") and report.get("reviewer") == record.get("actor")
                and _verification_valid(store, record.get("verification"), record.get("basis")))
    scope = current_scope()
    return scope.verdict(store, "record_valid", record, check) if scope is not None else check()


def _proof_ref(state, tid):
    attempt = _latest(state, tid)
    acceptance = attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt.get("acceptance")
    acceptance = acceptance or {}
    return {"task": tid, "attempt": attempt["id"], "report": acceptance.get("report", {}).get("sha256")}


def acceptance_chain(store, state, tid, attempt, *, require_acceptor=True):
    """Validate the executed critic chain once for live and historical consumers.

    This does not assert current code or dependency acceptance; those checks
    belong to the caller. It returns the actual final critic, never a proxy.
    """
    rounds = attempt.get("rounds", [])
    require(1 <= len(rounds) <= 2 and [r.get("number") for r in rounds] == list(range(1, len(rounds) + 1)),
            "Нет последовательной цепочки критиков")
    first, final = rounds[0], rounds[-1]
    adaptive = review_policy.adaptive(attempt)
    policy = review_policy.mode(attempt)
    expected_count = 1 if adaptive and first.get("passed") and attempt.get("secondRound") is not True else 2
    require(len(rounds) == expected_count and final.get("passed") is True,
            "До приёмки нужен PASS последнего обязательного критика")
    roles = ["worker", "critic1"] + (["critic2"] if len(rounds) == 2 else [])
    if require_acceptor and not review_policy.lean(attempt):
        roles.append("acceptor")
    for role in sorted(set(_card_roles(attempt)) | set(roles)):
        _role_data(store, attempt, role)
    require(all(_actor_valid(store, state, attempt, role) for role in roles),
            "Повреждена независимая команда задачи")
    if adaptive and len(rounds) == 1:
        require(not attempt.get("actors", {}).get("critic2"), "После первого PASS второй critic actor не требуется")
    actor_ids = [attempt["actors"][role] for role in roles]
    reviewers = {attempt["actors"][role] for role in roles if role != "worker"}
    require(len(set(actor_ids)) == len(actor_ids) and not set(attempt.get("writers", [])) & reviewers,
            "Критики и приёмщик должны быть независимы от исполнителей")
    submitted, repair = attempt.get("submitted"), attempt.get("repair")
    records = [submitted, *rounds, *([repair] if repair else [])]
    require(all(_record_valid(store, record) for record in records), "Повреждена цепочка исполнителя или критиков")
    require(_writer_valid(store, state, attempt, submitted.get("actor"))
            and first["basis"] == submitted["basis"], "Первый критик потерял исходную сдачу")
    for number, record in enumerate(rounds, 1):
        require(record["actor"] == attempt["actors"]["critic" + str(number)], "Отчёт критика подписан другим actor")
        if adaptive:
            report = _object(store.json(record["report"]))
            failures = [row for row in report.get("checks", []) if row.get("status") != "passed"]
            failures += [row for row in report.get("branchReview", {}).get("axes", []) if row.get("status") != "passed"]
            clean = not failures and not report.get("unmapped") and not review_policy.required_ids(report, policy)
            clean = clean and not any(row.get("status") == "unresolved" for row in report.get("resolutions", []))
            value = review_policy.verdict(report, clean=clean, critic=True, policy=policy)
            require(record.get("verdict") == value and record["passed"] == (value == "PASS")
                    and record.get("findings") == report.get("findings"), "Сохранённый verdict критика расходится с отчётом")
        if state.get("policy", {}).get("integrationReview"):
            require(integration.record_valid(store, state, tid, "critic" + str(number), record),
                    "Повреждено общее ревью выполненного раунда")
    if first["passed"]:
        require(repair is None, "Неожиданное исправление после PASS")
        if len(rounds) == 2:
            require(final.get("repairReceipt") is None and final["basis"] == first["basis"],
                    "После PASS первого критика изменён кандидат")
    else:
        require(repair and _writer_valid(store, state, attempt, repair.get("actor"))
                and repair.get("previousReport") == first["report"]["sha256"]
                and final.get("repairReceipt") == repair["report"]["sha256"] and final["basis"] == repair["basis"],
                "Второй критик не связан с исправлением первого отчёта")
        required = review_policy.required_ids(first, policy)
        require(set(repair.get("addresses", [])) == required, "Исправление потеряло обязательные замечания")
        if state.get("policy", {}).get("repairDiagnosis"):
            require(repair.get("diagnosis") == repairs.worker_guard(store, state, tid, repair["actor"]),
                    "Исправление потеряло диагностику")
    if len(rounds) == 2:
        require(final.get("previousReport") == first["report"]["sha256"], "Второй раунд не связан с первым")
        resolutions = _object(store.json(final["report"])).get("resolutions", [])
        require({row["id"] for row in resolutions} == review_policy.required_ids(first, policy)
                and all(row["status"] in ("resolved", "rejected") for row in resolutions),
                "Второй критик не закрыл обязательные замечания")
    if test_intent.required(state, attempt):
        require(attempt.get("testIntent") and test_intent.chain_matches(store, attempt, submitted, rounds, repair)
                and test_intent.history_valid(store, state, tid), "Повреждена история тестового контракта")
    return final


def _historical_light(store, state, task, attempt):
    """Worker verification is deliberately not acceptance or critic approval."""
    accepted = attempt.get("verifiedCompletion")
    if (attempt.get("terminal") or attempt.get("rounds") or attempt.get("acceptance")
            or set(attempt.get("roles", {})) != {"worker"}
            or set(attempt.get("actors", {})) != {"worker"}
            or not accepted or not _record_valid(store, accepted)
            or accepted.get("passed") is not True
            or accepted.get("completionKind") != "verified_completion"
            or accepted.get("attempt") != attempt["id"]
            or not _writer_valid(store, state, attempt, accepted.get("actor"))):
        return None
    if accepted.get("lightBaseline") != attempt.get("lightBaseline") or not artifact_valid(store, accepted.get("lightBaseline", {})):
        return None
    submitted = attempt.get("submitted")
    if (not _record_valid(store, submitted) or submitted.get("basis") != accepted["basis"]
            or submitted.get("report", {}).get("sha256") != accepted["report"]["sha256"]):
        return None
    card = _role_data(store, attempt, "worker")
    report = _object(store.json(accepted["report"]))
    expected = {(task["id"], c) for c in card["checks"]}
    expected |= {(r["id"], c) for r in card["constraints"] for c in r["acceptance"]}
    artifacts, failures = evidence_rows(store, report, expected)
    intent_ref = accepted.get("testIntent")
    extra_artifacts, extra_failures = (mixed_evidence.check_contract(store, test_intent._saved(store, intent_ref), intent_ref, report)
                                      if intent_ref else ([], []))
    artifacts.extend(extra_artifacts)
    failures.extend(extra_failures)
    if failures or report.get("unmapped") or artifacts != accepted.get("artifacts"):
        return None
    if set((row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])) != expected:
        return None
    if test_intent.required(state, attempt):
        if (not attempt.get("testIntent") or accepted.get("testIntent") != attempt["testIntent"]
                or not test_intent.history_valid(store, state, task["id"])):
            return None
    if task.get("proof") != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    inherited = accepted.get("inheritedProofs")
    ancestors = {t["id"] for t in _card_closure(card, task["id"])} - {task["id"]}
    if not isinstance(inherited, list) or len(inherited) != len(ancestors) or {p["task"] for p in inherited} != ancestors:
        return None
    return inherited


def _historical_one(store, state, tid):
    task = next((t for t in state["tasks"] if t["id"] == tid), None)
    if not task or task.get("status") != "completed":
        return None
    attempt = _latest(state, tid)
    if not _history_valid(store, state, tid):
        return None
    if task_tiers.worker_only(attempt):
        return _historical_light(store, state, task, attempt)
    accepted = attempt.get("acceptance")
    if attempt.get("terminal") or not accepted or not accepted.get("passed"):
        return None
    if review_policy.lean(attempt):
        return _historical_lean(store, state, task, attempt, accepted)
    cards = {role: _role_data(store, attempt, role) for role in ROLES}
    final = acceptance_chain(store, state, tid, attempt)
    if (not _record_valid(store, accepted) or accepted.get("actor") != attempt["actors"]["acceptor"]
            or accepted["basis"] != final["basis"] or accepted.get("previousReport") != final["report"]["sha256"]):
        return None
    if test_intent.required(state, attempt) and accepted.get("testIntent") != attempt.get("testIntent"):
        return None
    if review_policy.adaptive(attempt):
        report = _object(store.json(accepted["report"]))
        clean = (not report.get("unmapped") and not review_policy.required_ids(report)
                 and all(r.get("status") == "passed" for r in report.get("checks", []))
                 and all(r.get("result") == "aligned" for r in report.get("trace", [])))
        if review_policy.verdict(report, clean=clean) != "PASS" or accepted.get("verdict") != "PASS":
            return None
    proof = task.get("proof", {})
    if proof != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    coverage = {(row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])}
    expected = _closure_expected(cards["acceptor"], tid)
    if coverage != expected:
        return None
    inherited = accepted.get("inheritedProofs")
    closure_ids = {t["id"] for t in _card_closure(cards["acceptor"], tid)} - {tid}
    if not isinstance(inherited, list) or len(inherited) != len(closure_ids) or {p["task"] for p in inherited} != closure_ids:
        return None
    return inherited


def _closure_expected(card, tid, ancestors=None):
    """Lean: all closure criteria. Focused: only ancestors whose files the attempt changed."""
    closure = _card_closure(card, tid)
    if ancestors is not None:
        closure = [t for t in closure if t["id"] == tid or t["id"] in set(ancestors)]
    expected = {(t["id"], c) for t in closure for c in t["criteria"]}
    expected |= {(r["id"], c) for r in card["constraints"] for c in r["acceptance"]}
    return expected | {(tid, c) for c in card["checks"]}


def _historical_lean(store, state, task, attempt, accepted):
    """The final passing critic is the task acceptance; nothing else signs it."""
    tid = task["id"]
    final = acceptance_chain(store, state, tid, attempt, require_acceptor=False)
    role = "critic" + str(final["number"])
    if (not _sealed_valid(store, accepted) or accepted.get("completionKind") != "critic_acceptance"
            or accepted.get("round") != final["number"] or accepted.get("attempt") != attempt["id"]
            or any(accepted.get(key) != final.get(key) for key in ("actor", "basis", "report", "verification", "artifacts"))
            or accepted.get("verdict") != "PASS" or final.get("verdict") != "PASS"):
        return None
    if not _record_valid(store, accepted) or accepted["actor"] != attempt["actors"][role]:
        return None
    if test_intent.required(state, attempt) and accepted.get("testIntent") != attempt.get("testIntent"):
        return None
    card = _role_data(store, attempt, role)
    report = _object(store.json(accepted["report"]))
    if (not all(row.get("result") == "aligned" for row in report.get("trace", []))
            or {row.get("requirement") for row in report.get("trace", [])}
            != {r["id"] for r in _card_sources(card)}):
        return None
    if task.get("proof") != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    coverage = {(row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])}
    ancestors = focused_review.sealed_ancestors(store, state, tid, attempt, accepted) if review_policy.focused(attempt) else None
    if coverage != _closure_expected(card, tid, ancestors):
        return None
    inherited = accepted.get("inheritedProofs")
    closure_ids = {t["id"] for t in _card_closure(card, tid)} - {tid}
    if (not isinstance(inherited, list) or len(inherited) != len(closure_ids)
            or {p["task"] for p in inherited} != closure_ids
            or not isinstance(report.get("inheritedProofs"), list)
            or sorted(report["inheritedProofs"], key=lambda r: json.dumps(r, sort_keys=True))
            != sorted(inherited, key=lambda r: json.dumps(r, sort_keys=True))):
        return None
    return inherited


def _card_sources(card):
    return card.get("sources", [])


def historical_proof_valid(store, state, tid):
    """Validate the entire recorded acceptance DAG without claiming current code."""
    try:
        visiting, completed = set(), set()
        stack = [(tid, False)]
        while stack:
            current, closing = stack.pop()
            if closing:
                visiting.remove(current)
                completed.add(current)
                continue
            if current in completed:
                continue
            if current in visiting:
                return False
            inherited = _historical_one(store, state, current)
            if inherited is None:
                return False
            visiting.add(current)
            stack.append((current, True))
            for ref in inherited:
                if ref != _proof_ref(state, ref["task"]):
                    return False
                stack.append((ref["task"], False))
        return True
    except (Invalid, KeyError, TypeError, ValueError, OSError):
        return False


def direct_proof_current(store, state, tid):
    """Current task proof, with no recursive fallback through descendants."""
    try:
        if not historical_proof_valid(store, state, tid):
            return False
        attempt = _latest(state, tid)
        if test_intent.required(state, attempt):
            repaired = bool(attempt.get("rounds") and not attempt["rounds"][0]["passed"] and attempt.get("repair"))
            if not test_intent.valid(store, state, tid, allow_source_changes=repaired):
                return False
            receipt = attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt.get("acceptance")
            accepted = _object(store.json(receipt["report"]))
            if accepted.get("testSourceChanges", []) != test_intent.source_changes(store, state, tid):
                return False
        return (attempt["contract"] == task_contract(state, tid)
                and (attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt["acceptance"])["basis"] == task_basis(store, state, tid)
                and task_verified(store, state, tid))
    except (Invalid, KeyError, TypeError, ValueError, OSError):
        return False


def proof_provenance(store, state, tid, allowed_task_ids=None):
    """Return the actual valid confirmer, under the existing proof guards."""
    if direct_proof_current(store, state, tid):
        attempt = _latest(state, tid)
        receipt = attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt["acceptance"]
        return {"kind": "direct", "task": tid, "attempt": attempt["id"], "recordedAt": store.receipt_time(receipt)}
    if not historical_proof_valid(store, state, tid):
        return None
    from . import final_repairs
    if final_repairs.chain_proof(store, state, tid):
        # Focused: after a final repair the proof stands at the branch-reviewed candidate; checked repairs cover the rest.
        attempt = _latest(state, tid)
        receipt = attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt["acceptance"]
        return {"kind": "repair-chain", "task": tid, "attempt": attempt["id"], "recordedAt": store.receipt_time(receipt)}
    allowed = set(allowed_task_ids) if allowed_task_ids is not None else {t["id"] for t in state["tasks"]}
    for candidate in state["tasks"]:
        descendant = candidate["id"]
        if descendant == tid or descendant not in allowed or candidate.get("status") != "completed":
            continue
        try:
            closure = {t["id"] for t in task_closure(state, descendant)}
            if tid not in closure or not direct_proof_current(store, state, descendant):
                continue
            descendant_attempt = _latest(state, descendant)
            if task_tiers.worker_only(descendant_attempt):
                continue  # A worker receipt never re-attests changed ancestor code.
            accepted = descendant_attempt["acceptance"]
            if _proof_ref(state, tid) not in accepted["inheritedProofs"]:
                continue
            coverage = {(row["requirement"], row["criterion"]) for row in accepted["coverage"]}
            ancestor = next(t for t in state["tasks"] if t["id"] == tid)
            expected = {(tid, c) for c in ancestor["criteria"]}
            expected |= {(r["id"], c) for r in active(state) if r["kind"] == "constraint" for c in r["acceptance"]}
            if expected <= coverage:
                return {"kind": "descendant", "task": descendant, "attempt": descendant_attempt["id"],
                        "recordedAt": store.receipt_time(accepted)}
        except (Invalid, KeyError, TypeError, ValueError, OSError):
            continue
    return None


def task_proof_valid(store, state, tid, allowed_task_ids=None):
    """Boolean compatibility API for the same provenance/acceptance predicate."""
    return proof_provenance(store, state, tid, allowed_task_ids) is not None


class TaskPipeline:
    def __init__(self, host):
        self.host = host
        self.store, self.s = host.store, host.s

    def _attempt(self, tid):
        self.host.task(tid)
        require(_history_valid(self.store, self.s, tid), "Повреждена история разрешений на попытки")
        return _latest(self.s, tid)

    def _new(self, tid, authorization=None):
        task = next((t for t in self.s["tasks"] if t["id"] == tid), {})
        attempt = {**({key: task[key] for key in task_tiers.FIELDS if key in task} if task_tiers.enabled(self.s) else {}),"id": uuid.uuid4().hex, "briefCount": len(self.s["briefs"]), "contract": None,
                "reviewPolicy": review_policy.selected(self.s),
                "roles": {}, "actors": {}, "writers": [], "actorHistory": [], "rounds": [], "submitted": None, "repair": None,
                "acceptance": None, "terminal": False, "authorization": authorization,
                "createdAt": timestamp(), "cardHistory": [], "submissionHistory": [], "repairHistory": []}
        if authorization is None or review_policy.focused(attempt) and task:
            self._mode_class(attempt, task, self.s.get("taskHistory", {}).get(tid, []))
        return attempt

    def _mode_class(self, attempt, task, history):
        """Fast and max set the class of an attempt; the plan keeps its own and
        the declared class survives as plannedTier. A promotion stays heavy
        unless the owner chose fast."""
        if not (task_tiers.enabled(self.s) and check_modes.enabled(self.s)) or not task:
            return
        if review_policy.focused(attempt):
            promoted = any((a.get("authorization") or {}).get("kind") == "tier-escalation" for a in [*history, attempt])
            focused_review.freeze(self.s, attempt, task, self.store, promoted=promoted)
            return
        mode = check_modes.current(self.s)
        for key in ("plannedTier", "checkMode"):
            attempt.pop(key, None)
        attempt.update({key: task[key] for key in task_tiers.FIELDS if key in task})
        promoted = next((a for a in reversed(history) if (a.get("authorization") or {}).get("kind") == "tier-escalation"), None)
        if promoted and mode != "fast":
            attempt.update(tier="heavy", tierReason=promoted.get("tierReason", attempt.get("tierReason")))
            return
        planned, target = task.get("tier", "heavy"), check_modes.effective_tier(self.s, task.get("tier"))
        if target != planned:
            attempt.update(plannedTier=planned, tier=target, checkMode=mode)

    def _deferred_ids(self):
        return check_modes.deferred_ids(self.s, self.store)

    def _skipped(self, tid):
        history = self.s.get("taskHistory", {}).get(tid) or [{}]
        return set(history[-1].get("deferredDependencies", []))

    def _closure(self, tid, skip=None):
        """Closure without deferred dependencies recorded on the attempt."""
        skip = self._skipped(tid) if skip is None else skip
        return [t for t in task_closure(self.s, tid) if t["id"] == tid or t["id"] not in skip]

    def _sources(self, tid, skip=None):
        requirements = {r["id"]: r for r in self.s["requirements"]}
        ids = {rid for task in self._closure(tid, skip) for rid in task["requirements"]}
        ids |= {r["id"] for r in active(self.s) if r["kind"] == "constraint"}
        originals = {}
        for rid in sorted(ids):
            req = requirements[rid]
            if req["kind"] == "addition":
                req = requirements[req["parent"]]
            originals[req["id"]] = req
        return [copy.deepcopy(originals[rid]) for rid in sorted(originals)]

    def trace_requirements(self, tid):
        return self._sources(tid)

    def inherited_proofs(self, tid):
        return [_proof_ref(self.s, task["id"]) for task in self._closure(tid) if task["id"] != tid]

    def _cards(self, tid, attempt, custom=None):
        require(not attempt["rounds"] and not attempt.get("verifiedCompletion"), "Карточки заморожены после проверки")
        task = self.host.task(tid)
        require(custom is None or isinstance(custom, dict) and set(custom) <= set(task_tiers.roles(attempt)), "Неизвестная роль команды")
        for override in (custom or {}).values():
            require(isinstance(override, dict) and nonempty(override.get("specialization", "default"))
                    and isinstance(override.get("checks", []), list)
                    and all(nonempty(c) for c in override.get("checks", [])), "Некорректная специализация роли")
        if attempt["roles"]:
            attempt["cardHistory"].append({"contract": attempt["contract"], "roles": copy.deepcopy(attempt["roles"]),
                                            "actors": copy.deepcopy(attempt["actors"]), "writers": list(attempt["writers"])})
        if attempt["submitted"]:
            attempt["submissionHistory"].append(attempt["submitted"])
        attempt.update(contract=task_contract(self.s, tid), briefCount=len(self.s["briefs"]),
                       roles={}, actors={}, writers=[], submitted=None, repair=None,
                       acceptance=None, verification=None)
        deferred = sorted(self._deferred_ids() & {t["id"] for t in task_closure(self.s, tid)} - {tid})
        if deferred or "deferredDependencies" in attempt:
            attempt["deferredDependencies"] = deferred
        if review_policy.focused(attempt):
            # Focused: a card appears when its role is assigned (_ensure_card).
            attempt.pop("roleOverrides", None)
            if custom:
                attempt["roleOverrides"] = copy.deepcopy(custom)
            return
        for role in task_tiers.roles(attempt):
            self._make_card(tid, attempt, role, (custom or {}).get(role, {}))

    def _ensure_card(self, tid, attempt, role):
        """Focused: create the one card of an assigned role; recreated only by a new task_contract."""
        if role in attempt["roles"]:
            return False
        require(role in task_tiers.roles(attempt), "Роль не предусмотрена классом задачи и политикой проверки")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт роли устарел; подготовьте команду")
        require(not attempt.get("zoneExtensions"), "После расширения зон карточки фиксированы эпохой; нужна новая попытка")
        self._make_card(tid, attempt, role, (attempt.get("roleOverrides") or {}).get(role, {}))
        return True

    def _make_card(self, tid, attempt, role, override):
        task = self.host.task(tid)
        deferred = attempt.get("deferredDependencies", [])
        context = {"schema": 1, "project": self.s["title"], "run": self.s["run"], "task": tid,
                   **({"autonomy": autonomy.view(self.s)} if autonomy.enabled(self.s) else {}),
                   "reviewPolicy": review_policy.mode(attempt),
                   **{key: attempt[key] for key in task_tiers.FIELDS if key in attempt},
                   "attempt": attempt["id"], "contract": attempt["contract"], "title": task["title"],
                   "zones": task["zones"], "readZones": task.get("readZones", []),
                   "closure": [definition(t) for t in self._closure(tid, deferred)],
                   **({"deferredDependencies": deferred} if deferred else {}),
                   "sources": self._sources(tid, deferred),
                   "constraints": [copy.deepcopy(r) for r in active(self.s) if r["kind"] == "constraint"],
                   "briefs": copy.deepcopy(self.s["briefs"]), "spec": copy.deepcopy(self.s["spec"])}
        require(isinstance(override, dict), "Настройка роли должна быть объектом")
        specialization = override.get("specialization", f"{role}: {task['title']}")
        checks = override.get("checks", [])
        require(nonempty(specialization) and isinstance(checks, list) and all(nonempty(c) for c in checks),
                "Роли нужны специализация и строковые проверки")
        lenses = LENSES[role] + (LENSES["acceptor"] if review_policy.lean(attempt) and role.startswith("critic") else [])
        all_checks = list(dict.fromkeys(task["criteria"] + lenses + checks))
        if test_intent.required(self.s, attempt) and role.startswith("critic"):
            all_checks.append("Тестовый контракт и изменения тестов действительно проверяют критерии задачи")
            if self.s.get("policy", {}).get("mixedEvidence"):
                all_checks.append("Выбор automated/browser/process обоснован; наблюдения и свидетельства каждого способа действительно проверяют критерии")
        if self.s.get("policy", {}).get("errorBoundaryChecks") and role != "worker":
            all_checks.append("Проверены применимые отказные границы failureChecks, сохранность данных и реальные отрицательные сценарии")
        # Focused: the whole branch belongs to the lens reviewers; the final critic checks its attempt only.
        if (self.s.get("policy", {}).get("integrationReview") and tid == self.s.get("integrationTask")
                and role.startswith("critic") and not focused_review.enabled(self.s)):
            all_checks += ["Вся ветка: " + axis for axis in integration.AXES]
        card = {**context, "role": role, "specialization": specialization, "checks": all_checks,
                "routing": routing(self.s, task, role, attempt, self.store), "discovery": self.s.get("discovery"),
                "freshContext": role != "worker", "maximumCriticRounds": 0 if task_tiers.worker_only(attempt) else 2}
        artifact = _artifact_json(self.store, "roles", card)
        attempt["roles"][role] = {**artifact, "checks": all_checks, "specialization": specialization}

    def prepare(self):
        """Create/rebind initial cards after plan; never reset a consumed budget."""
        self.host.running()
        self.s.setdefault("taskHistory", {})
        self.s.setdefault("actors", {})
        for task in self.s["tasks"]:
            tid = task["id"]
            history = self.s["taskHistory"].setdefault(tid, [])
            if not history:
                history.append(self._new(tid))
            attempt = history[-1]
            contract = task_contract(self.s, tid)
            if review_policy.focused(attempt):
                # No cards here: each appears when its role is assigned.
                if attempt["contract"] != contract:
                    require(not attempt["rounds"], "Контракт после критика меняется только через новую попытку владельца")
                    self._cards(tid, attempt)
                if attempt.get("authorization") is None and not (
                        attempt["roles"] or attempt["actors"] or attempt["writers"] or attempt["submitted"]
                        or attempt["rounds"] or attempt.get("verifiedCompletion") or attempt["terminal"]):
                    # A replan may give the task dependants: the weight follows the plan until work starts.
                    self._mode_class(attempt, task, history[:-1])
            elif attempt["contract"] != contract or not attempt["roles"]:
                require(not attempt["rounds"], "Контракт после критика меняется только через новую попытку владельца")
                self._cards(tid, attempt)
            task.setdefault("phase", "implementation")
        self.host.save("team-prepared")
        return {t["id"]: copy.deepcopy(self._attempt(t["id"])["roles"]) for t in self.s["tasks"]}

    def team(self, path=None):
        self.host.build_ready()
        self.prepare()
        if path is not None:
            config = _json(self.host.read_input(path))
            tasks = config.get("tasks")
            require(isinstance(tasks, dict) and tasks, "Нужен объект tasks со специализациями ролей")
            for tid, custom in tasks.items():
                require(isinstance(custom, dict) and set(custom) <= set(ROLES), "Неизвестная роль команды")
                require(not self._attempt(tid)["rounds"], "Специализация после первого критика заморожена")
                self._cards(tid, self._attempt(tid), custom)
            self.host.save("team-specialized")
        return {t["id"]: copy.deepcopy(self._attempt(t["id"])["roles"]) for t in self.s["tasks"]}

    def assign(self, tid, role, runtime, native_id, session, fresh=True, model=None, reasoning=None, model_evidence=None):
        self.host.build_ready()
        require(role in ROLES, "Неизвестная роль")
        require(all(nonempty(v) for v in (runtime, native_id, session)), "Нужны runtime, native ID и session")
        require(model is None or nonempty(model), "Модель должна быть реальным ID среды или отсутствовать")
        runtime, native_id, session = runtime.strip().casefold(), native_id.strip(), session.strip()
        require(type(fresh) is bool and (role == "worker" or fresh), "Проверяющему нужен новый независимый контекст")
        attempt = self._attempt(tid)
        require(role in task_tiers.roles(attempt), "Роль не предусмотрена классом задачи и политикой проверки")
        require(not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"), "Попытка уже завершена")
        policy = review_policy.mode(attempt)
        if role == "critic2" and review_policy.adaptive(attempt) and attempt.get("secondRound") is True and (
                len(attempt["rounds"]) == 1 and attempt["rounds"][0]["passed"]):
            # max: the mandatory second fresh review of a heavy task after a first PASS.
            require(attempt["rounds"][0]["basis"] == task_basis(self.store, self.s, tid)
                    and task_verified(self.store, self.s, tid),
                    "До второго критика кандидат должен оставаться тем, что принял первый, и проверенным")
        elif role == "critic2" and review_policy.adaptive(attempt):
            require(review_policy.second_allowed(attempt), "Второй критик назначается только после первого WARN/BLOCK/FAIL")
            first, repair = attempt["rounds"][0], attempt.get("repair")
            require(repair and _record_valid(self.store, repair) and repair.get("passed") is True
                    and _writer_valid(self.store, self.s, attempt, repair.get("actor")),
                    "До назначения critic2 нужен действительный receipt исправления")
            repaired_report = _json(self.store.text(repair["report"]))
            required = review_policy.required_ids(first, policy)
            addressed = repair.get("addresses")
            require(repair.get("previousReport") == first["report"]["sha256"]
                    and repaired_report.get("previousReport") == first["report"]["sha256"]
                    and isinstance(addressed, list) and all(nonempty(fid) for fid in addressed)
                    and len(addressed) == len(set(addressed)) and set(addressed) == required
                    and repaired_report.get("addresses") == addressed,
                    "Receipt исправления не связан со всеми замечаниями первого критика")
            require(repair["basis"] == task_basis(self.store, self.s, tid)
                    and task_verified(self.store, self.s, tid),
                    "До назначения critic2 исправленный кандидат должен оставаться актуальным и проверенным")
            if self.s.get("policy", {}).get("repairDiagnosis"):
                require(repair.get("diagnosis") == repairs.worker_guard(self.store, self.s, tid, repair["actor"]),
                        "Receipt исправления не связан с действительной диагностикой")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт роли устарел; подготовьте команду")
        if review_policy.focused(attempt):
            self._ensure_card(tid, attempt, role)
        _role_data(self.store, attempt, role)
        aid = digest([runtime, native_id, session])
        previous = attempt["actors"].get(role)
        if previous:
            if role == "worker":
                require(not attempt["rounds"] or len(attempt["rounds"]) == 1 and not attempt["rounds"][0]["passed"],
                        "Заменить исполнителя можно до критики или при исправлении первого раунда")
                if attempt["rounds"] and self.s.get("policy", {}).get("repairDiagnosis"):
                    repairs.assignment_guard(self.store, self.s, tid, fresh=fresh, model=model)
            elif role.startswith("critic"):
                require(len(attempt["rounds"]) < int(role[-1]), "Критика после его отчёта нельзя заменить")
            else:
                require(attempt["acceptance"] is None, "Приёмщика после отчёта нельзя заменить")
        require(aid not in self.s.setdefault("actors", {}), "Этот native actor уже использован в другой роли или попытке")
        if self.s.get("policy", {}).get("allRoleUsage"):
            participants.require_unused_identity(self.s, runtime, native_id)
        route = model_routes.enforce(self.store, self.s, role, runtime, native_id, session, model, reasoning,
                                     model_evidence, task=self.host.task(tid), attempt=attempt, save=self.host.save)
        actor = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session, "fresh": fresh,
                 "task": tid, "attempt": attempt["id"], "role": role,
                 "card": attempt["roles"][role]["sha256"], "model": model, "at": timestamp()}
        actor.update(participants.model_metadata(self.store, self.s, runtime, native_id, session,
                     model=model, reasoning=reasoning, evidence=model_evidence))
        if route:
            model_routes.confirm(route, role, runtime, actor)
            actor["routeClass"] = route["routeClass"]
        participants.require_model_route(self.store, self.s, actor,
                                          sensitive=task_tiers.sensitive_task(self.s, self.host.task(tid)))
        self.s["actors"][aid] = _seal(self.store, actor)
        if previous:
            attempt["actorHistory"].append({"role": role, "previous": previous, "replacement": aid, "at": timestamp()})
        attempt["actors"][role] = aid
        if role == "worker":
            attempt["writers"].append(aid)
            if previous and not attempt["rounds"] and attempt["submitted"]:
                attempt["submissionHistory"].append(attempt["submitted"])
                attempt["submitted"] = None
                self.host.task(tid)["phase"] = "implementation"
            elif previous and attempt["rounds"]:
                self.host.task(tid)["phase"] = "repair"
        self.host.save("actor-assigned:" + tid + ":" + role)
        return aid

    def expected_checks(self, tid, role):
        attempt = self._attempt(tid)
        card = _role_data(self.store, attempt, role)
        expected = {(tid, criterion) for criterion in card["checks"]}
        if role == "worker" and task_tiers.worker_only(attempt):
            expected |= {(r["id"], c) for r in active(self.s) if r["kind"] == "constraint" for c in r["acceptance"]}
        if role.startswith("critic") and review_policy.focused(attempt):
            # Own criteria and project constraints; an accepted ancestor only when this attempt changed its files.
            changed = set(focused_review.changed_ancestors(self.store, self.s, tid, attempt))
            expected |= {(task["id"], c) for task in self._closure(tid) if task["id"] in changed for c in task["criteria"]}
            expected |= {(r["id"], c) for r in active(self.s) if r["kind"] == "constraint" for c in r["acceptance"]}
        elif role == "acceptor" or role.startswith("critic") and review_policy.lean(attempt):
            expected |= {(task["id"], c) for task in self._closure(tid) for c in task["criteria"]}
            expected |= {(r["id"], c) for r in active(self.s) if r["kind"] == "constraint" for c in r["acceptance"]}
        return expected

    def _live(self, tid, *, allow_blocked=False):
        self.host.build_ready()
        task, attempt = self.host.task(tid), self._attempt(tid)
        require(task["status"] in (("running", "blocked") if allow_blocked else ("running",)), "Задача не выполняется")
        require(not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"), "Попытка закрыта; нужна новая команда владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in _card_roles(attempt):
            _role_data(self.store, attempt, role)
        return task, attempt

    def _read(self, tid, role, path):
        attempt = self._attempt(tid)
        require(_actor_valid(self.store, self.s, attempt, role), "Нужен назначенный actor с действующей карточкой")
        participants.require_model_route(self.store, self.s,
            participants.resolve(self.store, self.s, attempt["actors"][role]),
            sensitive=task_tiers.sensitive_task(self.s, self.host.task(tid)))
        text = self.host.read_input(path)
        report = _json(text)
        candidate = task_basis(self.store, self.s, tid)
        require(report.get("basis") == candidate, "Отчёт устарел или относится к другому состоянию задачи")
        require(report.get("reviewer") == attempt["actors"][role], "Отчёт подписан другим actor")
        require(isinstance(report.get("unmapped"), list), "Нужен список unmapped")
        if role != "worker":
            require(report.get("independent") is True, "Проверяющему нужен независимый контекст")
        require(task_verified(self.store, self.s, tid), "Нет успешной адресной проверки текущей задачи")
        model_routes.require_script_receipt(self.s, self.host.task(tid), attempt)
        if test_intent.required(self.s, attempt) and role != "worker":
            repaired = bool(attempt.get("rounds") and not attempt["rounds"][0]["passed"] and attempt.get("repair"))
            require(test_intent.valid(self.store, self.s, tid, allow_source_changes=repaired),
                    "Тестовый контракт отсутствует, повреждён или не подтверждён реальным RED/GREEN")
            changes = test_intent.source_changes(self.store, self.s, tid)
            require(report.get("testSourceChanges", []) == changes,
                    "Критик должен явно сверить изменения тестовых источников после RED")
        artifacts, failures = evidence_rows(self.store, report, self.expected_checks(tid, role))
        route_artifacts, route_failures = mixed_evidence.check(self.store, self.s, tid, report)
        artifacts.extend(route_artifacts)
        failures.extend(route_failures)
        if self.s.get("policy", {}).get("integrationReview") and role.startswith("critic"):
            extra_artifacts, extra_failures = integration.check(self.store, self.s, tid, role, report)
            artifacts.extend(extra_artifacts)
            failures.extend(extra_failures)
            if extra_failures:
                blocking = [f for f in self._findings(report, self.expected_checks(tid, role))
                            if f["severity"] == "blocking"]
                require(all(any(f.get("criterion") == "Вся ветка: " + row["axis"] for f in blocking)
                            for row in extra_failures), "Каждое проваленное направление общего ревью требует блокирующее замечание")
        return text, report, artifacts, failures

    def _record(self, tid, role, text, report, artifacts, **extra):
        attempt = self._attempt(tid)
        return _seal(self.store, {"actor": attempt["actors"][role], "basis": report["basis"],
                                 "report": self.store.artifact("reviews", text.encode(), ".json"),
                                 "artifacts": artifacts, "verification": copy.deepcopy(attempt["verification"]),
                                 **({"testIntent": copy.deepcopy(attempt.get("testIntent"))}
                                    if test_intent.required(self.s, attempt) else {}),
                                 **extra})

    def submit(self, tid, path):
        task, attempt = self._live(tid)
        require(not attempt["rounds"], "После начала критики используйте repair; повторная сдача не сбрасывает раунды")
        require(test_intent.valid(self.store, self.s, tid),
                "До сдачи нужен сохранённый тестовый контракт: реальный RED→GREEN либо адресное обоснование неприменимости")
        text, report, artifacts, failures = self._read(tid, "worker", path)
        require(not failures and not report["unmapped"], "Исполнитель ещё не подтвердил все проверки")
        if task_tiers.worker_only(attempt):
            findings = self._findings(report, self.expected_checks(tid, "worker")) if "findings" in report else []
            require(report.get("verdict", "PASS") == "PASS"
                    and not report.get("branchReview")
                    and not any(f.get("severity") in ("warning", "blocking") for f in findings),
                    "Нерешённые замечания или non-PASS требуют исправления либо promote-task")
            inherited = self.inherited_proofs(tid)
            require(all(direct_proof_current(self.store, self.s, ref["task"]) for ref in inherited),
                    "Лёгкая задача не перепроверяет устаревший код зависимостей")
            require(report.get("testSourceChanges", []) == test_intent.source_changes(self.store, self.s, tid),
                    "Исполнитель должен сверить изменения тестовых источников")
            changes = self._light_changes(tid, attempt)
            if attempt["submitted"]:
                attempt["submissionHistory"].append(attempt["submitted"])
            attempt["submitted"] = self._record(tid, "worker", text, report, artifacts, passed=True)
            completed = self._record(tid, "worker", text, report, artifacts, passed=True,
                completionKind="verified_completion", attempt=attempt["id"], inheritedProofs=inherited,
                lightBaseline=copy.deepcopy(attempt["lightBaseline"]), changes=changes,
                coverage=[{"requirement": rid, "criterion": c} for rid, c in sorted(self.expected_checks(tid, "worker"))])
            attempt["verifiedCompletion"] = completed
            task.update(status="completed", phase="verified_completion", completionKind="verified_completion")
            task["proof"] = {key: completed[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}
            task.pop("note", None)
            timing.stop(task, timestamp(), completed=True)
            self.host.save("task-verified-completed:" + tid)
            return copy.deepcopy(completed)
        if attempt["submitted"]:
            attempt["submissionHistory"].append(attempt["submitted"])
        attempt["submitted"] = self._record(tid, "worker", text, report, artifacts, passed=True)
        task["phase"] = "critic_1"
        self.host.save("task-submitted:" + tid)
        return copy.deepcopy(attempt["submitted"])

    def _findings(self, report, expected, policy=None):
        findings = report.get("findings")
        require(isinstance(findings, list), "Нужен список findings, в том числе пустой")
        seen = set()
        for finding in findings:
            require(isinstance(finding, dict) and nonempty(finding.get("id")) and finding["id"] not in seen,
                    "Нужны уникальные ID замечаний")
            seen.add(finding["id"])
            require(finding.get("severity") in ("blocking", "warning", "note") and nonempty(finding.get("text"))
                    and nonempty(finding.get("criterion")) and finding["criterion"] in {c for _r, c in expected}, "Некорректное замечание критика")
        return review_policy.check_findings(findings, policy)

    def _resolutions(self, report, first, policy=None):
        expected = review_policy.required_ids(first, policy)
        rows = report.get("resolutions")
        require(isinstance(rows, list), "Второму критику нужен список resolutions")
        seen, artifacts, unresolved = set(), [], False
        for row in rows:
            require(isinstance(row, dict) and nonempty(row.get("id")) and row["id"] in expected and row["id"] not in seen,
                    "Неизвестное или повторное замечание в resolutions")
            seen.add(row["id"])
            require(row.get("status") in ("resolved", "rejected", "unresolved") and nonempty(row.get("note")),
                    "Нужны статус и объяснение решения по замечанию")
            if row["status"] == "unresolved":
                unresolved = True
            else:
                found, _ = evidence_rows(self.store, {"checks": [{"requirement": "finding", "criterion": row["id"],
                    "status": "passed", "note": row["note"], "artifact": row.get("artifact")}]}, {("finding", row["id"])})
                artifacts.extend(found)
        require(seen == expected, "Второй критик не разобрал все обязательные замечания")
        return artifacts, unresolved

    def review(self, tid, number, path):
        task, attempt = self._live(tid)
        require(type(number) is int and number in (1, 2) and number == len(attempt["rounds"]) + 1,
                "Критика ограничена двумя последовательными раундами")
        if number == 2:
            require(review_policy.second_allowed(attempt), "После первого PASS второй критик не требуется")
        submitted = attempt["submitted"]
        require(submitted and _record_valid(self.store, submitted), "Нет целой сдачи исполнителя")
        role = "critic" + str(number)
        policy = review_policy.mode(attempt)
        text, report, artifacts, failures = self._read(tid, role, path)
        findings = self._findings(report, self.expected_checks(tid, role), policy)
        extra = {"number": number, "findings": findings}
        unresolved = False
        if number == 1:
            require(report["basis"] == submitted["basis"], "После сдачи код изменился; сдайте исполнение заново")
        else:
            first = attempt["rounds"][0]
            require(_record_valid(self.store, first), "Повреждён отчёт первого критика")
            require(report.get("previousReport") == first["report"]["sha256"], "Второй раунд не связан с первым отчётом")
            repair = attempt.get("repair")
            if first["passed"]:
                require(repair is None and report.get("repairReceipt") is None, "Неожиданное исправление после PASS")
                require(report["basis"] == first["basis"], "После PASS первого критика изменён кандидат")
            else:
                require(repair and _record_valid(self.store, repair), "После замечаний нужен receipt исправления")
                if self.s.get("policy", {}).get("repairDiagnosis"):
                    require(repair.get("diagnosis") == repairs.worker_guard(self.store, self.s, tid, repair["actor"]),
                            "Исправление не связано с действительной диагностикой")
                require(report.get("repairReceipt") == repair["report"]["sha256"] and report["basis"] == repair["basis"],
                        "Второй раунд не связан с исправленным кандидатом")
            found, unresolved = self._resolutions(report, first, policy)
            artifacts.extend(found)
            extra.update(previousReport=first["report"]["sha256"], repairReceipt=repair["report"]["sha256"] if repair else None)
        inherited = None
        if review_policy.lean(attempt):
            inherited = self._inherited_report(tid, report)
            found, aligned = self._trace(tid, report)
            artifacts.extend(found)
            if not aligned:
                require(any(f["severity"] in review_policy.required_severities(policy) and f["criterion"] == ACCEPTANCE_LENS
                            for f in findings),
                        "Расхождение trace требует обязательного замечания по соответствию исходному запросу"
                        if policy != review_policy.FOCUSED else
                        "Расхождение trace — blocking с blockingClass brief_drift по соответствию исходному запросу")
                failures.append({"criterion": ACCEPTANCE_LENS})
        passed = not failures and not report["unmapped"] and not unresolved and not review_policy.required_ids(report, policy)
        if review_policy.adaptive(attempt):
            required_findings = [f for f in findings if f["severity"] in review_policy.required_severities(policy)]
            require(all(any(f["criterion"] == row.get("criterion", "Вся ветка: " + row.get("axis", ""))
                            for f in required_findings) for row in failures),
                    "Каждая проваленная проверка требует обязательного замечания с ID")
            # An unresolved ID from round 1 is already a recorded obligation;
            # critic2 need not invent a duplicate new finding to block it.
            extra["verdict"] = review_policy.verdict(report, clean=passed, critic=not unresolved, policy=policy)
            passed = extra["verdict"] == "PASS"
        attempt["rounds"].append(self._record(tid, role, text, report, artifacts, passed=passed, **extra))
        if review_policy.focused(attempt):
            # Warnings never stop the task; they wait for the branch review.
            sealed = attempt["rounds"][-1]["report"]
            collected.collect(self.s, {"kind": "task", "task": tid, "attempt": attempt["id"], "round": number,
                                       "report": {"file": sealed["file"], "sha256": sealed["sha256"]}}, findings)
        if passed and number == 1 and attempt.get("secondRound") is True:
            task["phase"] = "critic_2"
            self.host.save("task-critic-1:" + tid)
            return True
        if passed and review_policy.lean(attempt):
            return self._critic_acceptance(tid, task, attempt, number, inherited)
        if number == 1:
            task["phase"] = ("task_acceptance" if review_policy.adaptive(attempt) else "critic_2") if passed else "repair"
            self.host.save("task-critic-1:" + tid)
        elif passed:
            task["phase"] = "task_acceptance"
            self.host.save("task-critic-2:" + tid)
        else:
            attempt["terminal"] = True
            task["phase"] = "critic_2"
            self._block(tid, attempt, "Второй критик не принял задачу; третий раунд автоматически не разрешён")
        return passed

    def _block(self, tid, attempt, note):
        """After the one extra wave a refusal defers the task to the next run."""
        history = self.s.get("taskHistory", {}).get(tid) or []
        if check_modes.enabled(self.s) and any((a.get("authorization") or {}).get("kind") == "extra-wave" for a in history):
            self.host.block(tid, "Отложено в остатки следующего прогона после дополнительной волны: " + note)
            self.host.task(tid)["deferred"] = check_modes.is_deferred(self.s, self.host.task(tid), self.store)
            self._recard_dependents(tid)
            self.host.save("task-deferred:" + tid)
        else:
            self.host.block(tid, note)

    def _recard_dependents(self, tid):
        """Idle tasks built on a deferred task get cards without its criteria."""
        for other in self.s["tasks"]:
            history = self.s.get("taskHistory", {}).get(other["id"])
            if other["id"] == tid or other.get("status") != "pending" or not history:
                continue
            attempt = history[-1]
            # A focused idle attempt has no cards yet, but its deferred set still changes.
            if (tid in {t["id"] for t in task_closure(self.s, other["id"])}
                    and (attempt.get("roles") or review_policy.focused(attempt))
                    and not (attempt["actors"] or attempt["writers"] or attempt["submitted"] or attempt["rounds"]
                             or attempt.get("verifiedCompletion") or attempt["terminal"])):
                self._cards(other["id"], attempt)

    def _inherited_report(self, tid, report):
        inherited = self.inherited_proofs(tid)
        reported = report.get("inheritedProofs")
        require(isinstance(reported, list) and all(isinstance(row, dict) for row in reported)
                and sorted(reported, key=lambda r: r.get("task", "")) == sorted(inherited, key=lambda r: r["task"]),
                "Нужны точные ссылки на принятые доказательства всех зависимостей")
        require(all(historical_proof_valid(self.store, self.s, ref["task"]) for ref in inherited),
                "Повреждена цепочка приёмки зависимости")
        return inherited

    def _critic_acceptance(self, tid, task, attempt, number, inherited):
        """Lean: the passing critic's sealed round becomes the task acceptance."""
        final = attempt["rounds"][-1]
        role = "critic" + str(number)
        accepted = _seal(self.store, {
            **{key: copy.deepcopy(final[key]) for key in ("actor", "basis", "report", "artifacts", "verification",
                                                          "testIntent") if key in final},
            "passed": True, "verdict": "PASS", "completionKind": "critic_acceptance", "round": number,
            "attempt": attempt["id"], "inheritedProofs": inherited,
            **(focused_review.acceptance_snapshot(self.store, self.s, tid, attempt) if review_policy.focused(attempt) else {}),
            "coverage": [{"requirement": rid, "criterion": c} for rid, c in sorted(self.expected_checks(tid, role))]})
        attempt["acceptance"] = accepted
        task.update(status="completed", phase="critic_" + str(number), completionKind="critic_acceptance")
        task["proof"] = {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}
        task.pop("note", None)
        timing.stop(task, timestamp(), completed=True)
        self.host.save("task-critic-accepted:" + tid)
        return True

    def repair(self, tid, path):
        task, attempt = self._live(tid, allow_blocked=True)
        require(len(attempt["rounds"]) == 1 and not attempt["rounds"][0]["passed"],
                "Исправление допускается между первым и вторым критиком")
        first = attempt["rounds"][0]
        require(_record_valid(self.store, first), "Повреждён отчёт первого критика")
        if attempt.get("zoneExtensions"):
            require(test_intent.valid(self.store, self.s, tid),
                    "Расширенной эпохе нужен новый сохранённый RED/GREEN до ремонта")
        diagnosis = repairs.worker_guard(self.store, self.s, tid) if self.s.get("policy", {}).get("repairDiagnosis") else None
        text, report, artifacts, failures = self._read(tid, "worker", path)
        require(report.get("previousReport") == first["report"]["sha256"], "Исправление не связано с первым отчётом")
        expected = review_policy.required_ids(first, review_policy.mode(attempt))
        addressed = report.get("addresses")
        require(isinstance(addressed, list) and all(nonempty(f) for f in addressed)
                and len(addressed) == len(set(addressed)) and set(addressed) == expected,
                "Исправление должно разобрать все обязательные замечания")
        require(not failures and not report["unmapped"], "Исправление не прошло проверки исполнителя")
        if attempt["repair"]:
            attempt["repairHistory"].append(attempt["repair"])
        attempt["repair"] = self._record(tid, "worker", text, report, artifacts, passed=True,
                                         previousReport=first["report"]["sha256"], addresses=addressed,
                                         **({"diagnosis": diagnosis} if diagnosis else {}))
        task["status"], task["phase"] = "running", "critic_2"
        timing.start(task, timestamp())
        self.host.save("task-repaired:" + tid)
        return copy.deepcopy(attempt["repair"])

    def _trace(self, tid, report):
        expected = {r["id"]: r for r in self._sources(tid)}
        rows = report.get("trace")
        require(isinstance(rows, list), "Приёмщику нужен trace к исходным словам владельца")
        briefs = {b["id"]: b for b in self.s["briefs"]}
        seen, artifacts, aligned = set(), [], True
        for row in rows:
            require(isinstance(row, dict) and nonempty(row.get("requirement")) and row["requirement"] in expected and row["requirement"] not in seen,
                    "Неизвестная или повторная строка trace")
            rid = row["requirement"]
            seen.add(rid)
            req = expected[rid]
            require(row.get("brief") == req["brief"] and row.get("quote") == req["quote"]
                    and artifact_valid(self.store, briefs[req["brief"]])
                    and row["quote"] in self.store.text(briefs[req["brief"]]), "Trace должен содержать точную исходную цитату")
            require(row.get("result") in ("aligned", "drift") and nonempty(row.get("note")), "Нужны результат и объяснение trace")
            aligned = aligned and row["result"] == "aligned"
            found, _ = evidence_rows(self.store, {"checks": [{"requirement": rid, "criterion": "trace", "status": "passed",
                "note": row["note"], "artifact": row.get("artifact")}]}, {(rid, "trace")})
            artifacts.extend(found)
        require(seen == set(expected), "Не все исходные требования и ограничения сверены с результатом")
        return artifacts, aligned

    def accept(self, tid, path):
        task, attempt = self._live(tid)
        require(not review_policy.lean(attempt), "В lean задачу закрывает PASS критика; отдельный приёмщик не назначается")
        final = acceptance_chain(self.store, self.s, tid, attempt)
        text, report, artifacts, failures = self._read(tid, "acceptor", path)
        require(report["basis"] == final["basis"], "После последнего критика изменён результат; автоматический дополнительный раунд запрещён")
        require(report.get("previousReport") == final["report"]["sha256"], "Приёмка не связана с последним обязательным критиком")
        inherited = self.inherited_proofs(tid)
        reported = report.get("inheritedProofs")
        require(isinstance(reported, list) and all(isinstance(row, dict) for row in reported) and sorted(reported, key=lambda r: r.get("task", "")) == sorted(inherited, key=lambda r: r["task"]),
                "Нужны точные ссылки на принятые доказательства всех зависимостей")
        require(all(historical_proof_valid(self.store, self.s, ref["task"]) for ref in inherited), "Повреждена цепочка приёмки зависимости")
        found, aligned = self._trace(tid, report)
        artifacts.extend(found)
        passed = not failures and not report["unmapped"] and aligned
        extra = {}
        if review_policy.adaptive(attempt):
            if "findings" in report:
                self._findings(report, self.expected_checks(tid, "acceptor"))
                passed = passed and not review_policy.required_ids(report)
            extra["verdict"] = review_policy.verdict(report, clean=passed)
            passed = extra["verdict"] == "PASS"
        accepted = self._record(tid, "acceptor", text, report, artifacts, passed=passed,
            attempt=attempt["id"], previousReport=final["report"]["sha256"], inheritedProofs=inherited, **extra,
            coverage=[{"requirement": rid, "criterion": c} for rid, c in sorted(self.expected_checks(tid, "acceptor"))])
        attempt["acceptance"] = accepted
        task["phase"] = "task_acceptance"
        if passed:
            task["status"] = "completed"
            task["proof"] = {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}
            task.pop("note", None)
            timing.stop(task, timestamp(), completed=True)
            self.host.save("task-accepted:" + tid)
        else:
            attempt["terminal"] = True
            self._block(tid, attempt, "Приёмщик обнаружил расхождение с запросом или неподтверждённый критерий")
        return passed

    def _light_changes(self, tid, attempt):
        baseline = attempt.get("lightBaseline")
        require(baseline and artifact_valid(self.store, baseline), "Нет исходного снимка лёгкой задачи")
        task = self.host.task(tid)
        changes = integration._changes(_json(self.store.text(baseline))["records"], self.store.code_records())
        # Other currently executing tasks may own separate files, but their
        # writes cannot enlarge this task's declared, exact-file write set.
        peers = {z for t in self.s["tasks"] if t["id"] != tid
                 and (t["status"] == "running" or t["status"] == "completed"
                      and direct_proof_current(self.store, self.s, t["id"])) for z in t["zones"]}
        owned = []
        for row in changes:
            # Light keeps its exact-file rule; standard and fast tolerate directory peers.
            focused_heavy = review_policy.focused(attempt) and attempt.get("tier") == "heavy"
            wide = task_tiers.is_standard(attempt) or task_tiers.is_fast(attempt) or focused_heavy
            peer = _inside(row["path"], peers) if wide else row["path"] in peers
            if not _inside(row["path"], task["zones"]) and peer:
                continue
            if wide:
                # Standard owns whole declared zones, still never a sensitive path;
                # fast is the owner's choice and checks only the zones.
                require(_inside(row["path"], task["zones"]),
                        "Изменение вышло за зоны " + attempt["tier"] + ": " + row["path"])
                # A focused heavy task without a critic (fast mode) is heavy by the engine's own rules.
                require(task_tiers.is_fast(attempt) or focused_heavy or not task_tiers.sensitive_path(row["path"]),
                        "Чувствительный путь требует promote-task: " + row["path"])
                for value in (row["before"], row["after"]):
                    require(value is None or value.get("missing") or "sha256" in value,
                            "Символические ссылки требуют promote-task: " + row["path"])
                owned.append(row)
                continue
            require(row["path"] in task["zones"] and task_tiers.safe_zone(row["path"], attempt["changeKind"]),
                    "Изменение вышло за безопасные границы light; выполните promote-task")
            for value in (row["before"], row["after"]):
                require(value is None or "sha256" in value and not value.get("executable"),
                        "Ссылки и исполняемые файлы требуют promote-task")
            owned.append(row)
        return owned

    def promote(self, tid, reason):
        """Escalate only a light attempt; never replenish a heavy review budget."""
        self.host.running()
        old = self._attempt(tid)
        require(task_tiers.enabled(self.s) and task_tiers.worker_only(old)
                and not old.get("rounds") and not old.get("acceptance"),
                "Повышение доступно только для light или standard без использованной критики")
        require(nonempty(reason) and len(reason) <= 2000, "Объясните обнаруженный риск")
        auth = _seal(self.store, {"kind": "tier-escalation", "previousTier": old["tier"], "nextTier": "heavy",
            "previousAttempt": old["id"], "reason": reason, "at": timestamp()})
        new = self._new(tid, auth)
        new.update(tier="heavy", tierReason=reason)
        if review_policy.focused(new):
            focused_review.freeze(self.s, new, self.host.task(tid), self.store, promoted=True)
            new["tierReason"] = reason
        self.s["taskHistory"][tid].append(new)
        task = self.host.task(tid)
        timing.stop(task, timestamp())
        task.update(status="pending", phase="implementation")
        for key in ("proof", "note", "finishedAt", "phaseTiming", "completionKind"):
            task.pop(key, None)
        self._cards(tid, new)
        self.host.save("task-promoted:" + tid)
        return new["id"]

    def new_attempt(self, tid, brief_id, quote):
        self.host.running()
        require(_history_valid(self.store, self.s, tid), "Неизвестная задача или повреждена история попыток")
        old = _latest(self.s, tid)
        index = next((i for i, brief in enumerate(self.s["briefs"]) if brief["id"] == brief_id), None)
        require(index is not None and index + 1 > old["briefCount"], "Новая попытка требует нового дополнения владельца")
        brief = self.s["briefs"][index]
        require(nonempty(quote) and artifact_valid(self.store, brief) and quote in self.store.text(brief),
                "Нужна точная цитата из нового дополнения владельца")
        authorization = _seal(self.store, {"brief": brief_id, "quote": quote, "source": brief,
                                            "previousAttempt": old["id"], "at": timestamp()})
        new = self._new(tid, authorization)
        new.update({key: old[key] for key in task_tiers.FIELDS if key in old})
        task = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        if task is not None and check_modes.enabled(self.s) and (check_modes.current(self.s) != "balanced" or "plannedTier" in old):
            self._mode_class(new, task, self.s["taskHistory"][tid])
        self.s["taskHistory"][tid].append(new)
        # A withdrawn task can be reactivated under its original ID. Its
        # historical budget is retained even while absent from the live plan.
        if task is not None:
            timing.stop(task, timestamp())
            task.update(status="pending", phase="implementation")
            for key in ("proof", "note", "finishedAt", "phaseTiming", "deferred"):
                task.pop(key, None)
        self.host.save("owner-task-attempt:" + tid)
        return new["id"]

    def owner_new_attempt(self, tid, *, owner_source_file, source_sha256, owner_decision_id, quote):
        """Retry one blocked task under a fresh local owner decision record."""
        from .contracts import validate_plan
        self.host.running()
        task = self.host.task(tid)
        require(self.s.get("sealed") is True and task.get("status") == "blocked",
                "Новая попытка по решению владельца доступна только заблокированной задаче с закрытым объёмом")
        require(_history_valid(self.store, self.s, tid), "Повреждена история разрешений на попытки")
        old = _latest(self.s, tid)
        require(old.get("contract") == task_contract(self.s, tid), "Контракт задачи устарел")
        # Validate the current plan before writing any new source or team record.
        candidate = validate_plan(self.s, [definition(row) for row in self.s["tasks"]])
        require([definition(row) for row in candidate] == [definition(row) for row in self.s["tasks"]],
                "Текущий план не соответствует допустимому объёму")
        raw = owner_task_attempt.read_source(owner_source_file, source_sha256, quote, owner_decision_id)
        for used_task, used_sha, used_quote in owner_task_attempt.decision_uses(self.s, owner_decision_id):
            require(_history_valid(self.store, self.s, used_task),
                    "Повреждена прежняя история этого решения владельца")
            require(used_task != tid, "Этот ID решения уже использован для новой попытки задачи")
            require((used_sha, used_quote) == (source_sha256, quote),
                    "ID решения связан с другим источником или цитатой")
        source = self.store.artifact("owner-attempt-requests", raw, ".txt")
        auth = _seal(self.store, {"kind": owner_task_attempt.KIND, "run": self.s["run"],
                                  "task": tid, "previousAttempt": old["id"],
                                  "source": source, "quote": quote,
                                  "decisionId": owner_decision_id, "at": timestamp()})
        new = self._new(tid, auth)
        new.update({key: copy.deepcopy(old[key]) for key in (*task_tiers.FIELDS, "plannedTier", "checkMode")
                    if key in old})
        if check_modes.enabled(self.s) and (check_modes.current(self.s) != "balanced" or "plannedTier" in old):
            self._mode_class(new, task, self.s["taskHistory"][tid])
        self.s["taskHistory"][tid].append(new)
        timing.stop(task, timestamp())
        task.update(status="pending", phase="implementation")
        for key in ("proof", "note", "finishedAt", "phaseTiming", "deferred", "completionKind"):
            task.pop(key, None)
        self._cards(tid, new)
        require(_history_valid(self.store, self.s, tid), "Новая попытка не прошла проверку истории")
        self.host.save("owner-task-attempt:" + tid)
        return new["id"]

    def switch_check_mode(self, mode, words):
        """Owner's words (kept verbatim, no brief): done work stays; idle tasks get a new attempt."""
        self.host.running()
        row = check_modes.record_switch(self.store, self.s, mode, words)
        affected = []
        for task in self.s["tasks"]:
            tid, history = task["id"], self.s.get("taskHistory", {}).get(task["id"])
            if task["status"] not in ("pending", "running", "blocked") or not history:
                continue
            old = history[-1]
            if (old["terminal"] or old["rounds"] or old["submitted"] or old.get("verifiedCompletion") or old["acceptance"]
                    or not task_tiers.enabled(self.s)):
                continue
            require(_history_valid(self.store, self.s, tid), "Повреждена история разрешений на попытки")
            auth = _seal(self.store, {"kind": "check-mode", "previousAttempt": old["id"],
                                      "request": row["request"], "mode": mode, "at": timestamp()})
            new = self._new(tid, auth)
            self._mode_class(new, task, history)
            frozen = ("tier", "criticRoles", "secondRound")
            if all(new.get(key, "heavy" if key == "tier" else None) == old.get(key, "heavy" if key == "tier" else None)
                   for key in frozen):
                continue
            if old.get("lightBaseline"):
                new["lightBaseline"] = copy.deepcopy(old["lightBaseline"])
            history.append(new)
            if task["status"] != "blocked":  # an ordinary obstacle stays; only the attempt changes class
                timing.stop(task, timestamp())
                task.update(status="pending", phase="implementation")
                for key in ("proof", "note", "finishedAt", "phaseTiming", "completionKind"):
                    task.pop(key, None)
            self._cards(tid, new)
            affected.append(tid)
        self.host.save("check-mode-changed")
        return affected

    def extra_wave(self, tid):
        """The one extra wave of max mode: no owner words, never repeated."""
        self.host.running()
        require(check_modes.is_max(self.s), "Дополнительная волна доступна только в режиме проверки max")
        old, task = self._attempt(tid), self.host.task(tid)
        history = self.s["taskHistory"][tid]
        refused = old.get("acceptance") and old["acceptance"].get("passed") is False
        require(old["terminal"] and task["status"] == "blocked" and not task_tiers.worker_only(old)
                and (refused or len(old["rounds"]) == 2 and not old["rounds"][-1]["passed"]),
                "Дополнительная волна доступна после исчерпанных кругов проверки заблокированной задачи")
        require(not any((a.get("authorization") or {}).get("kind") == "extra-wave" for a in history),
                "Дополнительная волна для задачи уже использована")
        auth = _seal(self.store, {"kind": "extra-wave", "previousAttempt": old["id"], "at": timestamp()})
        new = self._new(tid, auth)
        self._mode_class(new, task, history)
        new["tier"] = "heavy"
        if review_policy.focused(new):
            new["criticRoles"] = list(focused_review.FULL_ROLES)
        history.append(new)
        timing.stop(task, timestamp())
        task.update(status="pending", phase="implementation")
        for key in ("proof", "note", "finishedAt", "phaseTiming", "completionKind", "deferred"):
            task.pop(key, None)
        self._cards(tid, new)
        self.host.save("task-extra-wave:" + tid)
        return new["id"]

    def start_guard(self, tid):
        attempt = self._attempt(tid)
        require(not attempt["rounds"] and not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"),
                "Раунды уже использованы; продолжайте repair или получите новую команду владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in _card_roles(attempt):
            _role_data(self.store, attempt, role)
        current = sorted(self._deferred_ids() & {t["id"] for t in task_closure(self.s, tid)} - {tid})
        if current != attempt.get("deferredDependencies", []):
            require(not (attempt["actors"] or attempt["writers"] or attempt["submitted"]),
                    "Набор отложенных зависимостей изменился после назначения исполнителя; нужна новая команда владельца")
            self._cards(tid, attempt)
            return True
        if review_policy.focused(attempt):
            # Only the worker's card is needed to start; critics get theirs when assigned.
            self._ensure_card(tid, attempt, "worker")
        if (task_tiers.worker_only(attempt) or check_modes.enabled(self.s)) and not attempt.get("lightBaseline"):
            # A restart never forgets writes made since the attempt began; under
            # lean control every attempt keeps it so a later switch to a
            # worker-only class still sees writes outside the zones.
            attempt["lightBaseline"] = _artifact_json(self.store, "team-records", {"records": self.store.code_records()})
        return False

    def reset_guard(self, tid):
        affected = {tid}
        while True:
            next_set = affected | {t["id"] for t in self.s["tasks"] if set(t["dependsOn"]) & affected}
            if affected == next_set:
                break
            affected = next_set
        for current in affected:
            attempt = self._attempt(current)
            require(not attempt["rounds"] and not attempt["terminal"] and not attempt.get("verifiedCompletion"), "Reset не может обнулить бюджет проверки или завершение")

    def validate_replan(self, new_tasks):
        if focused_review.enabled(self.s):
            focused_review.validate_plan(self.store, self.s, new_tasks)
        known = {t["id"]: t for t in self.s["tasks"]}
        proposed = {t["id"]: t for t in new_tasks}
        active_ids = {r["id"] for r in active(self.s)}
        next_state = {**self.s, "tasks": new_tasks}
        # A retained promotion may still have its original light declaration
        # in the immutable plan; its latest attempt supplies the effective tier.
        heavy_coverage = {rid for task in new_tasks
                          if not task_tiers.worker_only(task) or
                          (task["id"] in known and definition(task) == definition(known[task["id"]])
                           and not task_tiers.worker_only(_declared((self.s.get("taskHistory", {}).get(task["id"]) or [{}])[-1])))
                          for rid in task["requirements"]}
        for tid, history in self.s.get("taskHistory", {}).items():
            consumed = any(attempt["rounds"] for attempt in history)
            if task_tiers.enabled(self.s):
                consumed = consumed or any(not task_tiers.worker_only(_declared(a)) or a.get("actors") or a.get("submitted") or a.get("verifiedCompletion") for a in history) or known.get(tid, {}).get("startedAt") or known.get(tid, {}).get("status") == "running"
            latest = history[-1]
            require(_history_valid(self.store, self.s, tid), "Повреждена история разрешений на попытки")
            authorized = latest["contract"] is None and latest.get("authorization") and _sealed_valid(self.store, latest["authorization"])
            if consumed and tid not in proposed and not authorized:
                previous = known.get(tid)
                if previous is None:
                    # Removed tasks still own their consumed review history.
                    # Recover their obligations from the last immutable card,
                    # so drop/remove/restore cannot hide them under a new ID.
                    historical = next((a for a in reversed(history) if a.get("roles")), None)
                    require(historical is not None, "У истории задачи отсутствует карточка")
                    card = _role_data(self.store, historical, "worker")
                    previous = next((task for task in card["closure"] if task["id"] == tid), None)
                    require(previous is not None, "Карточка не содержит исходную задачу")
                require(not (set(previous["requirements"]) & active_ids),
                        "Нельзя удалить/переименовать проверявшуюся задачу с действующим требованием")
            if task_tiers.enabled(self.s):
                obligations = set()
                for historical in history:
                    if not task_tiers.worker_only(_declared(historical)) and historical.get("roles"):
                        card = _role_data(self.store, historical, "worker")
                        prior = next((t for t in card["closure"] if t["id"] == tid), None)
                        require(prior is not None, "Историческая heavy-карточка потеряла исходную задачу")
                        obligations.update(prior["requirements"])
                require(obligations.intersection(active_ids) <= heavy_coverage,
                        "Активные исторические heavy-требования должны оставаться покрыты heavy-задачей")
            if tid in proposed and task_tiers.enabled(self.s):
                if task_tiers.worker_only(_declared(latest)):
                    require(all(proposed[tid].get(key) == latest.get(key) for key in task_tiers.FIELDS if key != "tier")
                            and proposed[tid].get("tier") == _declared(latest)["tier"],
                            "Классификация light заморожена; повышение выполняется через promote-task")
                # A promotion overrides the original plan declaration without
                # rewriting its immutable plan-review artifact.
                escalated = any(a.get("authorization", {}).get("kind") == "tier-escalation" for a in history if a.get("authorization"))
                require(not task_tiers.worker_only(proposed[tid]) or all(task_tiers.worker_only(_declared(a)) for a in history) or
                        escalated and tid in known and definition(proposed[tid]) == definition(known[tid]),
                        "Историю heavy нельзя понизить до light")
                authorization = latest.get("authorization") or {}
                assigned_history = any(revision.get("actors") or revision.get("writers")
                                       for revision in latest.get("cardHistory", []))
                promotion_replan = (authorization.get("kind") == "tier-escalation"
                    and _sealed_valid(self.store, authorization) and not task_tiers.worker_only(latest)
                    and known.get(tid, {}).get("status") == "pending" and not assigned_history
                    and not any(latest.get(key) for key in ("actors", "writers", "actorHistory", "rounds",
                        "submitted", "repair", "acceptance", "verifiedCompletion", "terminal")))
                if promotion_replan and latest["contract"] != task_contract(next_state, tid):
                    previous, candidate = known[tid], proposed[tid]
                    require(not task_tiers.worker_only(candidate), "После promote-task новый план должен объявлять heavy")
                    require(set(candidate["requirements"]) == set(previous["requirements"])
                            and all(set(previous.get(key, [])) <= set(candidate.get(key, []))
                                    for key in ("criteria", "zones", "readZones", "dependsOn")),
                            "Повышение может расширить зоны и проверки, но не менять требования или терять прежние обязательства")
                    require({"low": 0, "normal": 1, "high": 2}[candidate.get("risk", "normal")]
                            >= {"low": 0, "normal": 1, "high": 2}[previous.get("risk", "normal")],
                            "Повышение не может снижать риск")
                if (assigned_history or latest.get("actors") or latest.get("submitted") or latest.get("verifiedCompletion") or known.get(tid, {}).get("startedAt") or known.get(tid, {}).get("status") == "running") and not (authorized or promotion_replan):
                    require(latest["contract"] == task_contract(next_state, tid),
                            "Начатая классификация и контракт заморожены; сначала новая попытка или promote-task")
            if tid in proposed and latest["rounds"]:
                require(latest["contract"] == task_contract(next_state, tid),
                        "Контракт после первого критика заморожен; нужна новая попытка владельца")
