"""Recorded native team, two critic rounds and source-bound task acceptance.

This module records declared native identities; it does not authenticate a
model or spawn agents. Role cards and decisions are immutable local artifacts.
Only a new quoted owner brief can open another attempt after consumed reviews.
"""
import copy
import json
import uuid

from . import timing
from .packets import routing
from .contracts import (active, artifact_valid, definition, evidence_rows, nonempty,
                        require, task_basis, task_closure, task_contract, task_verified)
from .store import Invalid, digest, timestamp

ROLES = ("worker", "critic1", "critic2", "acceptor")
LENSES = {
    "worker": ["Результат реализован в объявленных зонах и проверен"],
    "critic1": ["Проверены логика, крайние случаи и доказательства результата"],
    "critic2": ["Независимо перепроверены результат и замечания первого раунда"],
    "acceptor": ["Результат задачи соответствует исходному запросу и ограничениям"],
}


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
        records = [attempt.get("submitted"), attempt.get("repair"), attempt.get("acceptance"),
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
        index, source = briefs.get(auth.get("brief"), (0, None))
        if (source is None or source != auth.get("source") or not artifact_valid(store, source)
                or not previous["briefCount"] < index <= attempt["briefCount"]
                or not nonempty(auth.get("quote")) or auth["quote"] not in store.text(source)):
            return False
    return True


def _writer_valid(store, state, attempt, aid):
    return aid in attempt.get("writers", []) and _actor_valid(store, state, attempt, "worker", aid)


def _verification_valid(store, value, candidate):
    return bool(isinstance(value, dict) and value.get("exitCode") == 0 and not value.get("changedCode")
                and value.get("candidate") == candidate and artifact_valid(store, value.get("output", {})))


def _record_valid(store, record):
    if not _sealed_valid(store, record) or not artifact_valid(store, record.get("report", {})):
        return False
    if not all(artifact_valid(store, item) for item in record.get("artifacts", [])):
        return False
    report = _json(store.text(record["report"]))
    return (report.get("basis") == record.get("basis") and report.get("reviewer") == record.get("actor")
            and _verification_valid(store, record.get("verification"), record.get("basis")))


def _proof_ref(state, tid):
    attempt = _latest(state, tid)
    acceptance = attempt.get("acceptance") or {}
    return {"task": tid, "attempt": attempt["id"], "report": acceptance.get("report", {}).get("sha256")}


def _historical_one(store, state, tid):
    task = next((t for t in state["tasks"] if t["id"] == tid), None)
    if not task or task.get("status") != "completed":
        return None
    attempt = _latest(state, tid)
    if not _history_valid(store, state, tid):
        return None
    accepted = attempt.get("acceptance")
    if attempt.get("terminal") or not accepted or not accepted.get("passed"):
        return None
    cards = {role: _role_data(store, attempt, role) for role in ROLES}
    if not all(_actor_valid(store, state, attempt, role) for role in ROLES):
        return None
    if (len(set(attempt["actors"].values())) != len(ROLES)
            or set(attempt.get("writers", [])) & {attempt["actors"][r] for r in ROLES if r != "worker"}):
        return None
    submitted, repair, rounds = attempt.get("submitted"), attempt.get("repair"), attempt.get("rounds", [])
    if len(rounds) != 2 or [r.get("number") for r in rounds] != [1, 2]:
        return None
    first, second = rounds
    if not submitted or not all(_record_valid(store, r) for r in (submitted, first, second, accepted)):
        return None
    if not second.get("passed") or not _writer_valid(store, state, attempt, submitted.get("actor")):
        return None
    if first.get("actor") != attempt["actors"]["critic1"] or second.get("actor") != attempt["actors"]["critic2"]:
        return None
    if first["basis"] != submitted["basis"] or second.get("previousReport") != first["report"]["sha256"]:
        return None
    if first.get("passed"):
        if repair is not None or second.get("repairReceipt") is not None or second["basis"] != first["basis"]:
            return None
    else:
        if (not repair or not _record_valid(store, repair) or not _writer_valid(store, state, attempt, repair.get("actor"))
                or repair.get("previousReport") != first["report"]["sha256"]
                or second.get("repairReceipt") != repair["report"]["sha256"] or second["basis"] != repair["basis"]):
            return None
    if (accepted.get("actor") != attempt["actors"]["acceptor"] or accepted["basis"] != second["basis"]
            or accepted.get("previousReport") != second["report"]["sha256"]):
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
        return (attempt["contract"] == task_contract(state, tid)
                and attempt["acceptance"]["basis"] == task_basis(store, state, tid)
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
            accepted = _latest(state, descendant)["acceptance"]
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
        return {"id": uuid.uuid4().hex, "briefCount": len(self.s["briefs"]), "contract": None,
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
        require(not attempt["rounds"], "Карточки заморожены после первого критика")
        task = self.host.task(tid)
        require(custom is None or isinstance(custom, dict) and set(custom) <= set(ROLES), "Неизвестная роль команды")
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
                   "attempt": attempt["id"], "contract": attempt["contract"], "title": task["title"],
                   "zones": task["zones"], "readZones": task.get("readZones", []),
                   "closure": [definition(t) for t in task_closure(self.s, tid)],
                   "sources": self._sources(tid),
                   "constraints": [copy.deepcopy(r) for r in active(self.s) if r["kind"] == "constraint"],
                   "briefs": copy.deepcopy(self.s["briefs"]), "spec": copy.deepcopy(self.s["spec"])}
        for role in ROLES:
            override = (custom or {}).get(role, {})
            require(isinstance(override, dict), "Настройка роли должна быть объектом")
            specialization = override.get("specialization", f"{role}: {task['title']}")
            checks = override.get("checks", [])
            require(nonempty(specialization) and isinstance(checks, list) and all(nonempty(c) for c in checks),
                    "Роли нужны специализация и строковые проверки")
            all_checks = list(dict.fromkeys(task["criteria"] + LENSES[role] + checks))
            card = {**context, "role": role, "specialization": specialization, "checks": all_checks,
                    "routing": routing(self.s, task, role, attempt, self.store), "discovery": self.s.get("discovery"),
                    "freshContext": role != "worker", "maximumCriticRounds": 2}
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
        require(not attempt["terminal"] and not attempt["acceptance"], "Попытка уже завершена")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт роли устарел; подготовьте команду")
        _role_data(self.store, attempt, role)
        aid = digest([runtime, native_id, session])
        previous = attempt["actors"].get(role)
        if previous:
            if role == "worker":
                require(not attempt["rounds"] or len(attempt["rounds"]) == 1 and not attempt["rounds"][0]["passed"],
                        "Заменить исполнителя можно до критики или при исправлении первого раунда")
            elif role.startswith("critic"):
                require(len(attempt["rounds"]) < int(role[-1]), "Критика после его отчёта нельзя заменить")
            else:
                require(attempt["acceptance"] is None, "Приёмщика после отчёта нельзя заменить")
        require(aid not in self.s.setdefault("actors", {}), "Этот native actor уже использован в другой роли или попытке")
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
        if role == "acceptor":
            expected |= {(task["id"], c) for task in task_closure(self.s, tid) for c in task["criteria"]}
            expected |= {(r["id"], c) for r in active(self.s) if r["kind"] == "constraint" for c in r["acceptance"]}
        return expected

    def _live(self, tid, *, allow_blocked=False):
        self.host.build_ready()
        task, attempt = self.host.task(tid), self._attempt(tid)
        require(task["status"] in (("running", "blocked") if allow_blocked else ("running",)), "Задача не выполняется")
        require(not attempt["terminal"] and not attempt["acceptance"], "Попытка закрыта; нужна новая команда владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in ROLES:
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
        artifacts, failures = evidence_rows(self.store, report, self.expected_checks(tid, role))
        return text, report, artifacts, failures

    def _record(self, tid, role, text, report, artifacts, **extra):
        attempt = self._attempt(tid)
        return _seal(self.store, {"actor": attempt["actors"][role], "basis": report["basis"],
                                 "report": self.store.artifact("reviews", text.encode(), ".json"),
                                 "artifacts": artifacts, "verification": copy.deepcopy(attempt["verification"]),
                                 **extra})

    def submit(self, tid, path):
        task, attempt = self._live(tid)
        require(not attempt["rounds"], "После начала критики используйте repair; повторная сдача не сбрасывает раунды")
        text, report, artifacts, failures = self._read(tid, "worker", path)
        require(not failures and not report["unmapped"], "Исполнитель ещё не подтвердил все проверки")
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
            require(finding.get("severity") in ("blocking", "note") and nonempty(finding.get("text"))
                    and nonempty(finding.get("criterion")) and finding["criterion"] in {c for _r, c in expected}, "Некорректное замечание критика")
        return findings

    def _resolutions(self, report, first):
        expected = {f["id"] for f in first["findings"] if f["severity"] == "blocking"}
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
        require(seen == expected, "Второй критик не разобрал все блокирующие замечания")
        return artifacts, unresolved

    def review(self, tid, number, path):
        task, attempt = self._live(tid)
        require(type(number) is int and number in (1, 2) and number == len(attempt["rounds"]) + 1,
                "Критика идёт ровно в два последовательных раунда")
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
                require(report.get("repairReceipt") == repair["report"]["sha256"] and report["basis"] == repair["basis"],
                        "Второй раунд не связан с исправленным кандидатом")
            found, unresolved = self._resolutions(report, first)
            artifacts.extend(found)
            extra.update(previousReport=first["report"]["sha256"], repairReceipt=repair["report"]["sha256"] if repair else None)
        passed = not failures and not report["unmapped"] and not unresolved and not any(f["severity"] == "blocking" for f in findings)
        attempt["rounds"].append(self._record(tid, role, text, report, artifacts, passed=passed, **extra))
        if number == 1:
            task["phase"] = "critic_2" if passed else "repair"
            self.host.save("task-critic-1:" + tid)
        elif passed:
            task["phase"] = "task_acceptance"
            self.host.save("task-critic-2:" + tid)
        else:
            attempt["terminal"] = True
            task["phase"] = "critic_2"
            self.host.block(tid, "Второй критик не принял задачу; третий раунд автоматически не разрешён")
        return passed

    def repair(self, tid, path):
        task, attempt = self._live(tid, allow_blocked=True)
        require(len(attempt["rounds"]) == 1 and not attempt["rounds"][0]["passed"],
                "Исправление допускается между первым и вторым критиком")
        first = attempt["rounds"][0]
        require(_record_valid(self.store, first), "Повреждён отчёт первого критика")
        text, report, artifacts, failures = self._read(tid, "worker", path)
        require(report.get("previousReport") == first["report"]["sha256"], "Исправление не связано с первым отчётом")
        expected = {f["id"] for f in first["findings"] if f["severity"] == "blocking"}
        addressed = report.get("addresses")
        require(isinstance(addressed, list) and all(nonempty(f) for f in addressed)
                and len(addressed) == len(set(addressed)) and set(addressed) == expected,
                "Исправление должно разобрать все блокирующие замечания")
        require(not failures and not report["unmapped"], "Исправление не прошло проверки исполнителя")
        if attempt["repair"]:
            attempt["repairHistory"].append(attempt["repair"])
        attempt["repair"] = self._record(tid, "worker", text, report, artifacts, passed=True,
                                         previousReport=first["report"]["sha256"], addresses=addressed)
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
        require(len(attempt["rounds"]) == 2 and attempt["rounds"][1]["passed"], "До приёмки обязательны два критика")
        second = attempt["rounds"][1]
        require(all(_record_valid(self.store, record) for record in [attempt["submitted"], *attempt["rounds"],
                    *([attempt["repair"]] if attempt["repair"] else [])]), "Повреждена цепочка исполнителя или критиков")
        require(all(_actor_valid(self.store, self.s, attempt, role) for role in ROLES)
                and len(set(attempt["actors"].values())) == len(ROLES), "Повреждена независимая команда задачи")
        text, report, artifacts, failures = self._read(tid, "acceptor", path)
        require(report["basis"] == second["basis"], "После второго критика изменён результат; автоматический третий раунд запрещён")
        require(report.get("previousReport") == second["report"]["sha256"], "Приёмка не связана со вторым критиком")
        inherited = self.inherited_proofs(tid)
        reported = report.get("inheritedProofs")
        require(isinstance(reported, list) and all(isinstance(row, dict) for row in reported) and sorted(reported, key=lambda r: r.get("task", "")) == sorted(inherited, key=lambda r: r["task"]),
                "Нужны точные ссылки на принятые доказательства всех зависимостей")
        require(all(historical_proof_valid(self.store, self.s, ref["task"]) for ref in inherited), "Повреждена цепочка приёмки зависимости")
        found, aligned = self._trace(tid, report)
        artifacts.extend(found)
        passed = not failures and not report["unmapped"] and aligned
        accepted = self._record(tid, "acceptor", text, report, artifacts, passed=passed,
            attempt=attempt["id"], previousReport=second["report"]["sha256"], inheritedProofs=inherited,
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
        self.s["taskHistory"][tid].append(new)
        # A withdrawn task can be reactivated under its original ID. Its
        # historical budget is retained even while absent from the live plan.
        task = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        if task is not None:
            timing.stop(task, timestamp())
            task.update(status="pending", phase="implementation")
            for key in ("proof", "note", "finishedAt"):
                task.pop(key, None)
        self.host.save("owner-task-attempt:" + tid)
        return new["id"]

    def start_guard(self, tid):
        attempt = self._attempt(tid)
        require(not attempt["rounds"] and not attempt["terminal"] and not attempt["acceptance"],
                "Раунды уже использованы; продолжайте repair или получите новую команду владельца")
        require(attempt["contract"] == task_contract(self.s, tid), "Контракт команды устарел")
        for role in ROLES:
            _role_data(self.store, attempt, role)

    def reset_guard(self, tid):
        affected = {tid}
        while True:
            next_set = affected | {t["id"] for t in self.s["tasks"] if set(t["dependsOn"]) & affected}
            if affected == next_set:
                break
            affected = next_set
        for current in affected:
            attempt = self._attempt(current)
            require(not attempt["rounds"] and not attempt["terminal"], "Reset не может обнулить бюджет критиков")

    def validate_replan(self, new_tasks):
        known = {t["id"]: t for t in self.s["tasks"]}
        proposed = {t["id"]: t for t in new_tasks}
        active_ids = {r["id"] for r in active(self.s)}
        next_state = {**self.s, "tasks": new_tasks}
        for tid, history in self.s.get("taskHistory", {}).items():
            consumed = any(attempt["rounds"] for attempt in history)
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
            if tid in proposed and latest["rounds"]:
                require(latest["contract"] == task_contract(next_state, tid),
                        "Контракт после первого критика заморожен; нужна новая попытка владельца")
