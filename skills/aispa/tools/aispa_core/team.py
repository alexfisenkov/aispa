"""Recorded native team, bounded criticism and source-bound task acceptance.

This module records declared native identities; it does not authenticate a
model or spawn agents. Role cards and decisions are immutable local artifacts.
Only a new quoted owner brief can open another attempt after consumed reviews.
"""
import copy
import json
import uuid

from . import timing, repairs, participants, integration, test_intent, review_policy, task_tiers
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
    return _json(store.text(record["record"])) == {k: v for k, v in record.items() if k != "record"}


def _latest(state, tid):
    history = state.get("taskHistory", {}).get(tid, [])
    require(history, "Для задачи ещё не подготовлены роли")
    return history[-1]


def _role_data(store, attempt, role):
    record = attempt.get("roles", {}).get(role)
    require(record and artifact_valid(store, record), "Повреждена карточка роли")
    card = _json(store.text(record))
    require(card.get("role") == role and card.get("attempt") == attempt["id"]
            and card.get("contract") == attempt.get("contract")
            and card.get("checks") == record.get("checks")
            and card.get("specialization") == record.get("specialization")
            and card.get("reviewPolicy", review_policy.LEGACY) == review_policy.mode(attempt)
            and all(card.get(key) == attempt.get(key) for key in task_tiers.FIELDS)
            and all(artifact_valid(store, item) for item in card.get("briefs", []))
            and artifact_valid(store, card.get("spec", {})),
            "Карточка роли не соответствует попытке")
    return card


def _actor_valid(store, state, attempt, role, aid=None):
    aid = aid or attempt.get("actors", {}).get(role)
    actor = state.get("actors", {}).get(aid)
    if not actor or not _sealed_valid(store, actor):
        return False
    return (actor.get("id") == aid == digest([actor.get("runtime"), actor.get("nativeId"), actor.get("session")])
            and actor.get("attempt") == attempt["id"] and actor.get("role") == role
            and actor.get("card") == attempt["roles"][role]["sha256"]
            and (role == "worker" or actor.get("fresh") is True))


def _history_valid(store, state, tid):
    history = state.get("taskHistory", {}).get(tid, [])
    if not history or len({a["id"] for a in history}) != len(history):
        return False
    if history[0].get("authorization") is not None:
        return False
    for attempt in history:
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
    if not _sealed_valid(store, record) or not artifact_valid(store, record.get("report", {})):
        return False
    if not all(artifact_valid(store, item) for item in record.get("artifacts", [])):
        return False
    if record.get("testIntent") and not artifact_valid(store, record["testIntent"]):
        return False
    report = _json(store.text(record["report"]))
    return (report.get("basis") == record.get("basis") and report.get("reviewer") == record.get("actor")
            and _verification_valid(store, record.get("verification"), record.get("basis")))


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
    expected_count = 1 if adaptive and first.get("passed") else 2
    require(len(rounds) == expected_count and final.get("passed") is True,
            "До приёмки нужен PASS последнего обязательного критика")
    roles = ["worker", "critic1"] + (["critic2"] if len(rounds) == 2 else [])
    if require_acceptor and not review_policy.lean(attempt):
        roles.append("acceptor")
    for role in task_tiers.roles(attempt):
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
            report = _json(store.text(record["report"]))
            failures = [row for row in report.get("checks", []) if row.get("status") != "passed"]
            failures += [row for row in report.get("branchReview", {}).get("axes", []) if row.get("status") != "passed"]
            clean = not failures and not report.get("unmapped") and not review_policy.required_ids(report)
            clean = clean and not any(row.get("status") == "unresolved" for row in report.get("resolutions", []))
            value = review_policy.verdict(report, clean=clean, critic=True)
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
        required = review_policy.required_ids(first)
        require(set(repair.get("addresses", [])) == required, "Исправление потеряло обязательные замечания")
        if state.get("policy", {}).get("repairDiagnosis"):
            require(repair.get("diagnosis") == repairs.worker_guard(store, state, tid, repair["actor"]),
                    "Исправление потеряло диагностику")
    if len(rounds) == 2:
        require(final.get("previousReport") == first["report"]["sha256"], "Второй раунд не связан с первым")
        resolutions = _json(store.text(final["report"])).get("resolutions", [])
        require({row["id"] for row in resolutions} == review_policy.required_ids(first)
                and all(row["status"] in ("resolved", "rejected") for row in resolutions),
                "Второй критик не закрыл обязательные замечания")
    if state.get("policy", {}).get("testIntent"):
        require(attempt.get("testIntent") and all(record.get("testIntent") == attempt["testIntent"] for record in records)
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
    report = _json(store.text(accepted["report"]))
    expected = {(task["id"], c) for c in card["checks"]}
    expected |= {(r["id"], c) for r in card["constraints"] for c in r["acceptance"]}
    artifacts, failures = evidence_rows(store, report, expected)
    if failures or report.get("unmapped") or artifacts != accepted.get("artifacts"):
        return None
    if set((row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])) != expected:
        return None
    if state.get("policy", {}).get("testIntent"):
        if (not attempt.get("testIntent") or accepted.get("testIntent") != attempt["testIntent"]
                or not test_intent.history_valid(store, state, task["id"])):
            return None
    if task.get("proof") != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    inherited = accepted.get("inheritedProofs")
    ancestors = {t["id"] for t in card["closure"]} - {task["id"]}
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
    if state.get("policy", {}).get("testIntent") and accepted.get("testIntent") != attempt.get("testIntent"):
        return None
    if review_policy.adaptive(attempt):
        report = _json(store.text(accepted["report"]))
        clean = (not report.get("unmapped") and not review_policy.required_ids(report)
                 and all(r.get("status") == "passed" for r in report.get("checks", []))
                 and all(r.get("result") == "aligned" for r in report.get("trace", [])))
        if review_policy.verdict(report, clean=clean) != "PASS" or accepted.get("verdict") != "PASS":
            return None
    proof = task.get("proof", {})
    if proof != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    coverage = {(row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])}
    expected = {(t["id"], c) for t in cards["acceptor"]["closure"] for c in t["criteria"]}
    expected |= {(r["id"], c) for r in cards["acceptor"]["constraints"] for c in r["acceptance"]}
    expected |= {(tid, c) for c in cards["acceptor"]["checks"]}
    if coverage != expected:
        return None
    inherited = accepted.get("inheritedProofs")
    closure_ids = {t["id"] for t in cards["acceptor"]["closure"]} - {tid}
    if not isinstance(inherited, list) or len(inherited) != len(closure_ids) or {p["task"] for p in inherited} != closure_ids:
        return None
    return inherited


def _closure_expected(card, tid):
    expected = {(t["id"], c) for t in card["closure"] for c in t["criteria"]}
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
    if state.get("policy", {}).get("testIntent") and accepted.get("testIntent") != attempt.get("testIntent"):
        return None
    card = _role_data(store, attempt, role)
    report = _json(store.text(accepted["report"]))
    if (not all(row.get("result") == "aligned" for row in report.get("trace", []))
            or {row.get("requirement") for row in report.get("trace", [])}
            != {r["id"] for r in _card_sources(card)}):
        return None
    if task.get("proof") != {key: accepted[key] for key in ("attempt", "basis", "report", "artifacts", "inheritedProofs")}:
        return None
    coverage = {(row["requirement"], row["criterion"]) for row in accepted.get("coverage", [])}
    if coverage != _closure_expected(card, tid):
        return None
    inherited = accepted.get("inheritedProofs")
    closure_ids = {t["id"] for t in card["closure"]} - {tid}
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
        if state.get("policy", {}).get("testIntent"):
            repaired = bool(attempt.get("rounds") and not attempt["rounds"][0]["passed"] and attempt.get("repair"))
            if not test_intent.valid(store, state, tid, allow_source_changes=repaired):
                return False
            receipt = attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt.get("acceptance")
            accepted = _json(store.text(receipt["report"]))
            if accepted.get("testSourceChanges", []) != test_intent.source_changes(store, state, tid):
                return False
        return (attempt["contract"] == task_contract(state, tid)
                and (attempt.get("verifiedCompletion") if task_tiers.worker_only(attempt) else attempt["acceptance"])["basis"] == task_basis(store, state, tid)
                and task_verified(store, state, tid))
    except (Invalid, KeyError, TypeError, ValueError, OSError):
        return False


def task_proof_valid(store, state, tid, allowed_task_ids=None):
    """Accept a fresh proof or a fresh explicit descendant rechecking this task."""
    if direct_proof_current(store, state, tid):
        return True
    if not historical_proof_valid(store, state, tid):
        return False
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
                return True
        except (Invalid, KeyError, TypeError, ValueError, OSError):
            continue
    return False


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
        return {**({key: task[key] for key in task_tiers.FIELDS if key in task} if task_tiers.enabled(self.s) else {}),"id": uuid.uuid4().hex, "briefCount": len(self.s["briefs"]), "contract": None,
                "reviewPolicy": review_policy.selected(self.s),
                "roles": {}, "actors": {}, "writers": [], "actorHistory": [], "rounds": [], "submitted": None, "repair": None,
                "acceptance": None, "terminal": False, "authorization": authorization,
                "createdAt": timestamp(), "cardHistory": [], "submissionHistory": [], "repairHistory": []}

    def _sources(self, tid):
        requirements = {r["id"]: r for r in self.s["requirements"]}
        ids = {rid for task in task_closure(self.s, tid) for rid in task["requirements"]}
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
        return [_proof_ref(self.s, task["id"]) for task in task_closure(self.s, tid) if task["id"] != tid]

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
        context = {"schema": 1, "project": self.s["title"], "run": self.s["run"], "task": tid,
                   "reviewPolicy": review_policy.mode(attempt),
                   **{key: attempt[key] for key in task_tiers.FIELDS if key in attempt},
                   "attempt": attempt["id"], "contract": attempt["contract"], "title": task["title"],
                   "zones": task["zones"], "readZones": task.get("readZones", []),
                   "closure": [definition(t) for t in task_closure(self.s, tid)],
                   "sources": self._sources(tid),
                   "constraints": [copy.deepcopy(r) for r in active(self.s) if r["kind"] == "constraint"],
                   "briefs": copy.deepcopy(self.s["briefs"]), "spec": copy.deepcopy(self.s["spec"])}
        for role in task_tiers.roles(attempt):
            override = (custom or {}).get(role, {})
            require(isinstance(override, dict), "Настройка роли должна быть объектом")
            specialization = override.get("specialization", f"{role}: {task['title']}")
            checks = override.get("checks", [])
            require(nonempty(specialization) and isinstance(checks, list) and all(nonempty(c) for c in checks),
                    "Роли нужны специализация и строковые проверки")
            lenses = LENSES[role] + (LENSES["acceptor"] if review_policy.lean(attempt) and role.startswith("critic") else [])
            all_checks = list(dict.fromkeys(task["criteria"] + lenses + checks))
            if self.s.get("policy", {}).get("testIntent") and role.startswith("critic"):
                all_checks.append("Тестовый контракт и изменения тестов действительно проверяют критерии задачи")
            if self.s.get("policy", {}).get("errorBoundaryChecks") and role != "worker":
                all_checks.append("Проверены применимые отказные границы failureChecks, сохранность данных и реальные отрицательные сценарии")
            if self.s.get("policy", {}).get("integrationReview") and tid == self.s.get("integrationTask") and role.startswith("critic"):
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
            if attempt["contract"] != contract or not attempt["roles"]:
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

    def assign(self, tid, role, runtime, native_id, session, fresh=True, model=None):
        self.host.build_ready()
        require(role in ROLES, "Неизвестная роль")
        require(all(nonempty(v) for v in (runtime, native_id, session)), "Нужны runtime, native ID и session")
        require(model is None or nonempty(model), "Модель должна быть реальным ID среды или отсутствовать")
        runtime, native_id, session = runtime.strip().casefold(), native_id.strip(), session.strip()
        require(type(fresh) is bool and (role == "worker" or fresh), "Проверяющему нужен новый независимый контекст")
        attempt = self._attempt(tid)
        require(role in task_tiers.roles(attempt), "Роль не предусмотрена классом задачи и политикой проверки")
        require(not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"), "Попытка уже завершена")
        if role == "critic2" and review_policy.adaptive(attempt):
            require(review_policy.second_allowed(attempt), "Второй критик назначается только после первого WARN/BLOCK/FAIL")
            first, repair = attempt["rounds"][0], attempt.get("repair")
            require(repair and _record_valid(self.store, repair) and repair.get("passed") is True
                    and _writer_valid(self.store, self.s, attempt, repair.get("actor")),
                    "До назначения critic2 нужен действительный receipt исправления")
            repaired_report = _json(self.store.text(repair["report"]))
            required = review_policy.required_ids(first)
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
        actor = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session, "fresh": fresh,
                 "task": tid, "attempt": attempt["id"], "role": role,
                 "card": attempt["roles"][role]["sha256"], "model": model, "at": timestamp()}
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
        if role == "acceptor" or role.startswith("critic") and review_policy.lean(attempt):
            expected |= {(task["id"], c) for task in task_closure(self.s, tid) for c in task["criteria"]}
            expected |= {(r["id"], c) for r in active(self.s) if r["kind"] == "constraint" for c in r["acceptance"]}
        return expected

    def _live(self, tid, *, allow_blocked=False):
        self.host.build_ready()
        task, attempt = self.host.task(tid), self._attempt(tid)
        require(task["status"] in (("running", "blocked") if allow_blocked else ("running",)), "Задача не выполняется")
        require(not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"), "Попытка закрыта; нужна новая команда владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in task_tiers.roles(attempt):
            _role_data(self.store, attempt, role)
        return task, attempt

    def _read(self, tid, role, path):
        attempt = self._attempt(tid)
        require(_actor_valid(self.store, self.s, attempt, role), "Нужен назначенный actor с действующей карточкой")
        text = self.host.read_input(path)
        report = _json(text)
        candidate = task_basis(self.store, self.s, tid)
        require(report.get("basis") == candidate, "Отчёт устарел или относится к другому состоянию задачи")
        require(report.get("reviewer") == attempt["actors"][role], "Отчёт подписан другим actor")
        require(isinstance(report.get("unmapped"), list), "Нужен список unmapped")
        if role != "worker":
            require(report.get("independent") is True, "Проверяющему нужен независимый контекст")
        require(task_verified(self.store, self.s, tid), "Нет успешной адресной проверки текущей задачи")
        if self.s.get("policy", {}).get("testIntent") and role != "worker":
            repaired = bool(attempt.get("rounds") and not attempt["rounds"][0]["passed"] and attempt.get("repair"))
            require(test_intent.valid(self.store, self.s, tid, allow_source_changes=repaired),
                    "Тестовый контракт отсутствует, повреждён или не подтверждён реальным RED/GREEN")
            changes = test_intent.source_changes(self.store, self.s, tid)
            require(report.get("testSourceChanges", []) == changes,
                    "Критик должен явно сверить изменения тестовых источников после RED")
        artifacts, failures = evidence_rows(self.store, report, self.expected_checks(tid, role))
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
                                    if self.s.get("policy", {}).get("testIntent") else {}),
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

    def _findings(self, report, expected):
        findings = report.get("findings")
        require(isinstance(findings, list), "Нужен список findings, в том числе пустой")
        seen = set()
        for finding in findings:
            require(isinstance(finding, dict) and nonempty(finding.get("id")) and finding["id"] not in seen,
                    "Нужны уникальные ID замечаний")
            seen.add(finding["id"])
            require(finding.get("severity") in ("blocking", "warning", "note") and nonempty(finding.get("text"))
                    and nonempty(finding.get("criterion")) and finding["criterion"] in {c for _r, c in expected}, "Некорректное замечание критика")
        return findings

    def _resolutions(self, report, first):
        expected = review_policy.required_ids(first)
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
        text, report, artifacts, failures = self._read(tid, role, path)
        findings = self._findings(report, self.expected_checks(tid, role))
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
            found, unresolved = self._resolutions(report, first)
            artifacts.extend(found)
            extra.update(previousReport=first["report"]["sha256"], repairReceipt=repair["report"]["sha256"] if repair else None)
        inherited = None
        if review_policy.lean(attempt):
            inherited = self._inherited_report(tid, report)
            found, aligned = self._trace(tid, report)
            artifacts.extend(found)
            if not aligned:
                require(any(f["severity"] in ("blocking", "warning") and f["criterion"] == ACCEPTANCE_LENS for f in findings),
                        "Расхождение trace требует обязательного замечания по соответствию исходному запросу")
                failures.append({"criterion": ACCEPTANCE_LENS})
        passed = not failures and not report["unmapped"] and not unresolved and not review_policy.required_ids(report)
        if review_policy.adaptive(attempt):
            required_findings = [f for f in findings if f["severity"] in ("blocking", "warning")]
            require(all(any(f["criterion"] == row.get("criterion", "Вся ветка: " + row.get("axis", ""))
                            for f in required_findings) for row in failures),
                    "Каждая проваленная проверка требует обязательного замечания с ID")
            # An unresolved ID from round 1 is already a recorded obligation;
            # critic2 need not invent a duplicate new finding to block it.
            extra["verdict"] = review_policy.verdict(report, clean=passed, critic=not unresolved)
            passed = extra["verdict"] == "PASS"
        attempt["rounds"].append(self._record(tid, role, text, report, artifacts, passed=passed, **extra))
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
            self.host.block(tid, "Второй критик не принял задачу; третий раунд автоматически не разрешён")
        return passed

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
        diagnosis = repairs.worker_guard(self.store, self.s, tid) if self.s.get("policy", {}).get("repairDiagnosis") else None
        text, report, artifacts, failures = self._read(tid, "worker", path)
        require(report.get("previousReport") == first["report"]["sha256"], "Исправление не связано с первым отчётом")
        expected = review_policy.required_ids(first)
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
            self.host.block(tid, "Приёмщик обнаружил расхождение с запросом или неподтверждённый критерий")
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
            # Light keeps its exact-file rule; standard tolerates directory peers.
            peer = _inside(row["path"], peers) if task_tiers.is_standard(attempt) else row["path"] in peers
            if not _inside(row["path"], task["zones"]) and peer:
                continue
            if task_tiers.is_standard(attempt):
                # Standard owns whole declared zones, still never a sensitive path.
                require(_inside(row["path"], task["zones"]), "Изменение вышло за зоны standard: " + row["path"])
                require(not task_tiers.sensitive_path(row["path"]),
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
        self.s["taskHistory"][tid].append(new)
        # A withdrawn task can be reactivated under its original ID. Its
        # historical budget is retained even while absent from the live plan.
        task = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        if task is not None:
            timing.stop(task, timestamp())
            task.update(status="pending", phase="implementation")
            for key in ("proof", "note", "finishedAt", "phaseTiming"):
                task.pop(key, None)
        self.host.save("owner-task-attempt:" + tid)
        return new["id"]

    def start_guard(self, tid):
        attempt = self._attempt(tid)
        require(not attempt["rounds"] and not attempt["terminal"] and not attempt["acceptance"] and not attempt.get("verifiedCompletion"),
                "Раунды уже использованы; продолжайте repair или получите новую команду владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in task_tiers.roles(attempt):
            _role_data(self.store, attempt, role)
        if task_tiers.worker_only(attempt) and not attempt.get("lightBaseline"):
            # A restart never forgets writes made since the attempt began.
            attempt["lightBaseline"] = _artifact_json(self.store, "team-records", {"records": self.store.code_records()})

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
        known = {t["id"]: t for t in self.s["tasks"]}
        proposed = {t["id"]: t for t in new_tasks}
        active_ids = {r["id"] for r in active(self.s)}
        next_state = {**self.s, "tasks": new_tasks}
        # A retained promotion may still have its original light declaration
        # in the immutable plan; its latest attempt supplies the effective tier.
        heavy_coverage = {rid for task in new_tasks
                          if not task_tiers.worker_only(task) or
                          (task["id"] in known and definition(task) == definition(known[task["id"]])
                           and not task_tiers.worker_only((self.s.get("taskHistory", {}).get(task["id"]) or [{}])[-1]))
                          for rid in task["requirements"]}
        for tid, history in self.s.get("taskHistory", {}).items():
            consumed = any(attempt["rounds"] for attempt in history)
            if task_tiers.enabled(self.s):
                consumed = consumed or any(not task_tiers.worker_only(a) or a.get("actors") or a.get("submitted") or a.get("verifiedCompletion") for a in history) or known.get(tid, {}).get("startedAt") or known.get(tid, {}).get("status") == "running"
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
                    if not task_tiers.worker_only(historical) and historical.get("roles"):
                        card = _role_data(self.store, historical, "worker")
                        prior = next((t for t in card["closure"] if t["id"] == tid), None)
                        require(prior is not None, "Историческая heavy-карточка потеряла исходную задачу")
                        obligations.update(prior["requirements"])
                require(obligations.intersection(active_ids) <= heavy_coverage,
                        "Активные исторические heavy-требования должны оставаться покрыты heavy-задачей")
            if tid in proposed and task_tiers.enabled(self.s):
                if task_tiers.worker_only(latest):
                    require(all(proposed[tid].get(key) == latest.get(key) for key in task_tiers.FIELDS),
                            "Классификация light заморожена; повышение выполняется через promote-task")
                # A promotion overrides the original plan declaration without
                # rewriting its immutable plan-review artifact.
                escalated = any(a.get("authorization", {}).get("kind") == "tier-escalation" for a in history if a.get("authorization"))
                require(not task_tiers.worker_only(proposed[tid]) or all(task_tiers.worker_only(a) for a in history) or
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
