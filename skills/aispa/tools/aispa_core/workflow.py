"""Domain operations. No publishing, agent spawning or external messages."""
import json
import math
import os
from pathlib import Path
import re
import time

from .contracts import (active, add_requirements, artifact_valid, basis, definition,
                        evidence_rows, gate_valid, plan_basis, report_header, require,
                        validate_plan, verified, task_basis, task_verified, task_closure)
from .store import Invalid, timestamp
from . import timing, phase_receipts, execution_obligations, measured_estimate
from . import autonomy, project_documents, preflight
from .team import TaskPipeline, _history_valid, historical_proof_valid, task_proof_valid
from . import check_modes, project_context, discovery, carryover, cleanroom, blind, process_runner, ideas, planning, knowledge, product, handoff, executions, briefmap, design_contract, participants, integration, test_intent, task_tiers, owner_task_attempt
from . import final_repairs

WAVES_UNCHECKED = "В этой политике волны не сверяются; бриф проверяют на входе и в слепой приёмке"


SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                    r"sk-[A-Za-z0-9_-]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
                    r"-----BEGIN [^-]*PRIVATE KEY-----)")


def read_input(path, *, scalable=False):
    from . import scale
    text = scale.read_file(path, allow_manifest=scalable).decode("utf-8-sig")
    require(bool(text.strip()), "Входной файл пуст")
    require(not SECRET.search(text), "Обнаружен возможный секрет; подготовьте обезличенный входной файл")
    return text


class Workflow:
    def read_input(self, path):
        from . import scale
        self.store.scale_enabled = scale.enabled(self.s)
        return read_input(path, scalable=scale.enabled(self.s))

    def __init__(self, store, state):
        self.store, self.s = store, state
        from . import scale
        self.store.scale_enabled = scale.enabled(state)
        self.pipeline = TaskPipeline(self)
        # Idle marks and calibration samples exist only in runs with measuredTiming.
        self.tracker = measured_estimate.Tracker(state) if measured_estimate.enabled(state) else None

    def save(self, event, at=None):
        at = at or timestamp()
        from . import work_progress
        work_progress.sync(self.s, event, at)
        for task in self.s.get("tasks", []):
            timing.sync_phase(task, at, self.s.get("status") == "running" and not timing.waiting(self.s))
        samples = self.tracker.prepare(self.store, self.s, at) if self.tracker else []
        self.store.append(self.s, event)
        if samples:
            self.tracker.commit(samples)
        if self.s.get("projectContext", {}).get("initialized"):
            try:
                project_context.refresh(self.store, self.s)
            except (OSError, Invalid) as error:
                raise Invalid("Событие записано, но память проекта не обновилась. Выполните context-refresh без повтора операции") from error

    def progress_begin(self, phase):
        from . import work_progress
        self.running()
        at = timestamp()
        if work_progress.begin(self.s, phase, at):
            self.save("work-progress-began:" + phase, at)
        return work_progress.view(self.s, at)

    def focused_stage(self, phase):
        """Focused runs: the branch review and the blind acceptance start their measured stage by themselves.

        measured_estimate times the stages branchReview and blind by workProgress
        of the phases integration and blind; the engine begins them at the first
        branch context or review and at clean-prepare or blind. Idempotent while
        the phase runs; a waiting or closed run starts nothing.
        """
        if not self._focused() or self.s.get("status") != "running" or timing.waiting(self.s):
            return None
        return self.progress_begin(phase)

    def resource_admitted(self, kind, at=None):
        """Caller holds the journal lock; persist admission before native work."""
        at = at or timestamp()
        if not timing.resource_admitted(self.s, kind, at):
            return False
        self.save("resource-admitted:" + kind, at)
        return True

    def progress_end(self):
        from . import work_progress
        require(not self.s.get("journalRecovery", {}).get("pending"),
                "После восстановления сначала journal-verify и journal-reconcile")
        at = timestamp()
        if work_progress.end(self.s, at):
            self.save("work-progress-ended:" + self.s["workProgress"]["phase"], at)
        return work_progress.view(self.s, at)

    def running(self, *, allow_wait=False):
        require(not self.s.get("journalRecovery", {}).get("pending"),
                "После восстановления сначала проверьте и согласуйте состояние: journal-reconcile")
        require(self.s["status"] == "running", "Прогон закрыт или приостановлен; сначала resume")
        require(allow_wait or not (isinstance(self.s.get("waiting"), dict)
                                  and self.s["waiting"].get("kind") == "owner_question"),
                "Ожидается ответ владельца; сначала сохраните факт ответа командой answered")

    def check_mode(self, mode, path):
        """Owner's words for a mode change come as a file, never as a new brief."""
        self.running()
        return self.pipeline.switch_check_mode(mode, self.read_input(path))

    def owner_new_attempt(self, tid, *, owner_source_file, source_sha256, owner_decision_id, quote):
        """Open a single blocked task using an exact saved owner decision."""
        return self.pipeline.owner_new_attempt(
            tid, owner_source_file=owner_source_file, source_sha256=source_sha256,
            owner_decision_id=owner_decision_id, quote=quote)

    def brief(self, path):
        self.running()
        text = self.read_input(path)
        artifact = self.store.artifact("briefs", text.encode(), ".md")
        artifact["id"] = "B%03d" % (len(self.s["briefs"]) + 1)
        self.s["briefs"].append(artifact)
        self.s["sealed"] = False
        self.s["stage"] = "brief"
        self.save("brief-added:" + artifact["id"])
        return artifact

    def requirements(self, path):
        self.running()
        additions = add_requirements(self.store, self.s, json.loads(self.read_input(path)))
        self.s["requirements"].extend(additions)
        self.s["sealed"] = False
        self.save("requirements-added")

    def scope(self, rid, action, brief, quote):
        self.running()
        require(action in ("drop", "defer", "restore"), "Неизвестное решение о требовании")
        req = next((r for r in self.s["requirements"] if r["id"] == rid), None)
        source = next((b for b in self.s["briefs"] if b["id"] == brief), None)
        require(req is not None and source is not None, "Неизвестное требование или бриф")
        require(bool(quote.strip()) and quote in self.store.text(source), "Решение требует дословной цитаты из дополнения владельца")
        require(brief != req.get("brief"), "Изменение объёма требует нового дополнения к брифу")
        order = {item["id"]: index for index, item in enumerate(self.s["briefs"])}
        previous = req.get("decision", {}).get("brief", req.get("brief"))
        require(order[brief] > order.get(previous, -1), "Решение об объёме должно быть новее предыдущего решения по требованию")
        req["scope"] = {"drop": "dropped", "defer": "deferred", "restore": "active"}[action]
        req["decision"] = {"brief": brief, "quote": quote, "action": action}
        self.s["sealed"] = False
        self.save("owner-scope-decision:" + rid)

    def seal(self):
        self.running()
        preflight.require_adopted(self.store, self.s, purpose="seal")
        project_documents.require_fresh_intent(self.store, self.s)
        require(self.s["briefs"] and active(self.s), "Нужны бриф и действующие требования")
        if self.s.get("policy", {}).get("sourceAccounting"):
            require(briefmap.valid(self.store, self.s, actor_check=lambda s, a, r: participants.actor_check(self.store, s, a, r)),
                    "Каждая часть исходного брифа должна пройти независимый разбор и сопоставление: brief-map")
        if self.s.get("policy", {}).get("ideaReview"):
            require(ideas.valid(self.store, self.s) and phase_receipts.valid(self.store, self.s, "idea"),
                    "До фиксации брифа нужен актуальный разбор идеи с автором: idea --file")
        core = set(self.s.get("core", [])) | {r["id"] for r in active(self.s) if r["core"]}
        require(core & {r["id"] for r in active(self.s)}, "До разработки определите обязательное ядро")
        execution_seal = execution_obligations.seal(self.store, self.s)
        if execution_seal:
            self.s["executionSeal"] = execution_seal
        self.s["core"] = sorted(core)
        self.s["sealed"] = True
        self.s["stage"] = "spec"
        at = timestamp()
        first_closure = autonomy.close_briefing(self.s, at)
        self.save("brief-sealed", at)
        return first_closure

    def spec(self, path):
        self.running()
        project_documents.require_fresh_intent(self.store, self.s)
        require(self.s.get("sealed"), "Сначала зафиксируйте бриф и обязательное ядро")
        if self.s.get("policy", {}).get("ideaReview"):
            require(ideas.valid(self.store, self.s) and phase_receipts.valid(self.store, self.s, "idea"),
                    "Разбор идеи отсутствует, не атрибутирован или устарел; обновите idea --file")
        if self.s.get("policy", {}).get("discovery"):
            require(discovery.valid(self.store, self.s) and phase_receipts.valid(self.store, self.s, "discovery"),
                    "Сначала изучите проект: discover и проверенный отчёт discover --file с автором")
        self.s["spec"] = self.store.artifact("specs", self.read_input(path).encode(), ".md")
        self.save("spec-recorded")

    def coverage(self, path, actor=None):
        self.running()
        require(self.s["spec"] and self.s.get("sealed"), "Нет спецификации для действующего брифа")
        text = self.read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "coverage")
        author = phase_receipts.report_actor(self.store, self.s, "coverage", report, actor)
        rows = report.get("checks")
        require(isinstance(rows, list), "Нет checks")
        expected = {r["id"] for r in active(self.s)}
        seen, passed = set(), not report["unmapped"]
        spec = self.store.text(self.s["spec"])
        for row in rows:
            require(isinstance(row, dict), "Некорректная строка checks")
            rid = row.get("requirement")
            require(isinstance(rid, str) and rid in expected and rid not in seen, "Повторное или неизвестное требование")
            require(row.get("status") in ("covered", "missing", "partial"), "Некорректный статус покрытия")
            seen.add(rid)
            if row["status"] == "covered":
                quote = row.get("specQuote")
                require(isinstance(quote, str) and quote.strip() and quote in spec, "Нет подтверждающей цитаты из спецификации")
            else:
                passed = False
        require(seen == expected, "Не все требования проверены против спецификации")
        if self.s.get("policy", {}).get("productChallenge"):
            require(ideas.valid(self.store, self.s), "Разбор идеи устарел или повреждён")
            passed = product.coverage(self.s, report, spec,
                                      ideas._saved_report(self.store, self.s)["challenge"]) and passed
        self.s["gates"]["coverage"] = {"passed": passed, "basis": report["basis"],
                                         "report": self.store.artifact("reviews", text.encode(), ".json")}
        phase_receipts.record(self.store, self.s, "coverage", self.s["gates"]["coverage"]["report"],
                              actor=author, basis=report["basis"])
        self.save("coverage-reviewed")
        return passed

    def plan(self, path, review_path=None, actor=None):
        self.running()
        project_documents.require_fresh_intent(self.store, self.s)
        if self.s.get("policy", {}).get("discovery"):
            require(discovery.valid(self.store, self.s) and phase_receipts.valid(self.store, self.s, "discovery"),
                    "Исследование проекта отсутствует, не атрибутировано или устарело; обновите discover")
        carryover.guard_current(self.store, self.s)
        require(gate_valid(self.store, self.s, "coverage"), "Покрытие брифа не подтверждено или устарело")
        author = phase_receipts.authorize(self.store, self.s, "planner", actor)
        text = self.read_input(path)
        tasks = validate_plan(self.s, json.loads(text))
        if self.s.get("policy", {}).get("designContract"):
            require(design_contract.valid_scenarios(self.store, self.s),
                    "До плана нужны актуальные сценарии всех функций: scenarios")
            require(design_contract.valid_interfaces(self.store, self.s),
                    "До плана нужен актуальный контракт интерфейсов: interfaces")
            design_contract.validate_plan_bindings(self.store, self.s, tasks)
        self.pipeline.validate_replan(tasks)
        if self.s.get("policy", {}).get("planReview"):
            require(review_path is not None, "До плана нужен разбор размера задач: plan-check, затем plan --review")
        size_review = planning.review(self.store, self.s, tasks, json.loads(self.read_input(review_path))) if review_path else None
        integration_task = integration.choose_task(self.store, self.s, tasks) if self.s.get("policy", {}).get("integrationReview") else None
        old = {t["id"]: t for t in self.s["tasks"]}
        contract = basis(self.store, self.s, "coverage")
        for task in tasks:
            previous = old.get(task["id"])
            task["origin"] = contract
            if previous and definition(previous) == definition(task) and previous.get("origin") == contract:
                for key in ("status", "phase", "proof", "note", "phaseTiming", *timing.FIELDS):
                    if key in previous:
                        task[key] = previous[key]
            elif previous:
                timing.stop(previous, timestamp())
                for key in ("startedAt", "elapsedSeconds", "attempts"):
                    if key in previous:
                        task[key] = previous[key]
        self.s["tasks"] = tasks
        if integration_task:
            self.s["integrationTask"] = integration_task
        if size_review:
            self.s["planReview"] = size_review
        else:
            self.s.pop("planReview", None)
        self.pipeline.prepare()
        self.s["gates"]["plan"] = {"passed": True, "basis": basis(self.store, self.s, "plan"),
                                     "report": self.store.artifact("plans", text.encode(), ".json")}
        phase_receipts.record(self.store, self.s, "planner", self.s["gates"]["plan"]["report"], actor=author)
        self.s["stage"] = "build"
        self.save("plan-validated")

    def task(self, tid):
        result = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        require(result is not None, "Неизвестная задача")
        return result

    def _final_retry_revalidates_dependencies(self, task):
        """Only a started, owner-retried final heavy task may recheck stale ancestors."""
        tid = task["id"]
        if (not self.s.get("policy", {}).get("integrationReview")
                or self.s.get("integrationTask") != tid
                or type(task.get("attempts")) is not int or task["attempts"] < 1
                or not task.get("startedAt")):
            return False
        history = self.s.get("taskHistory", {}).get(tid, [])
        if len(history) < 2 or not _history_valid(self.store, self.s, tid):
            return False
        previous, current = history[-2:]
        authorization = current.get("authorization") or {}
        if (authorization.get("kind") != owner_task_attempt.KIND
                or authorization.get("previousAttempt") != previous.get("id")
                or current.get("tier", "heavy") != "heavy"
                or previous.get("tier", "heavy") != "heavy"
                or not previous.get("actors", {}).get("worker")):
            return False
        try:
            return integration.choose_task(self.store, self.s, self.s["tasks"]) == tid
        except (Invalid, KeyError, TypeError, ValueError, OSError):
            return False

    def _dependencies_ready(self, task, deferred):
        tid = task["id"]
        closure = {t["id"] for t in task_closure(self.s, tid)} - {tid}
        stale = [dep for dep in task["dependsOn"] if dep not in deferred
                 and not task_proof_valid(self.store, self.s, dep, allowed_task_ids=closure)]
        return not stale or (self._final_retry_revalidates_dependencies(task)
                             and all(historical_proof_valid(self.store, self.s, dep) for dep in stale))

    def build_ready(self):
        self.running()
        require(self.s.get("sealed"), "Бриф изменён; нужна повторная фиксация")
        if execution_obligations.enabled(self.s):
            execution_obligations.obligations(self.store, self.s, sealed=True)
        if self.s.get("policy", {}).get("sourceAccounting"):
            require(briefmap.valid(self.store, self.s, actor_check=lambda s, a, r: participants.actor_check(self.store, s, a, r)),
                    "Исходный разбор брифа отсутствует, повреждён или устарел")
        if self.s.get("policy", {}).get("designContract"):
            require(design_contract.valid_scenarios(self.store, self.s)
                    and design_contract.valid_interfaces(self.store, self.s, check_sources=False),
                    "Сохранённые сценарии или контракт интерфейсов повреждены либо устарели")
            require(all(artifact_valid(self.store, ref.get("snapshot", {})) for ref in self.s.get("ownerReferences", [])),
                    "Исходный материал владельца повреждён")
        require(gate_valid(self.store, self.s, "coverage") and gate_valid(self.store, self.s, "plan"),
                "Проверки брифа или плана отсутствуют, провалены либо устарели")
        if self.s.get("policy", {}).get("planReview"):
            require(planning.valid(self.store, self.s), "Разбор размера текущего плана отсутствует, повреждён или устарел")

    def start(self, tid, context):
        self.build_ready()
        carryover.guard_current(self.store, self.s)
        task = self.task(tid)
        require(task["status"] in ("pending", "blocked"), "Задача уже запущена или завершена")
        # Dependency proofs are read from the saved journal state, so they are checked before the guard
        # adds its (not yet saved) baseline to this task's attempt.
        deferred = self.pipeline._deferred_ids()
        require(self._dependencies_ready(task, deferred),
                "Зависимости задачи не приняты или доказательства устарели; включите актуального преемника в dependsOn")
        if self.pipeline.start_guard(tid):
            self.save("task-cards-refreshed:" + tid)
            raise Invalid("Карточки обновлены из-за отложенной зависимости; запросите пакет контекста задачи заново")
        require(context == basis(self.store, self.s, "task", tid), "Пакет контекста задачи устарел")
        entry = self.s.setdefault("waveEntries", {}).get(str(task["wave"]))
        opening = not entry or entry["plan"] != plan_basis(self.s)
        for prior in {t["wave"] for t in self.s["tasks"] if t["wave"] < task["wave"]}:
            require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"] if t["wave"] == prior),
                    "Предыдущая волна ещё выполняется")
            if self._wave_unchecked(prior):
                continue
            review = self.s.get("waves", {}).get(str(prior))
            require(review and review["passed"] and review["plan"] == plan_basis(self.s)
                    and artifact_valid(self.store, review["report"])
                    and all(artifact_valid(self.store, a) for a in review.get("artifacts", [])),
                    "Нет актуальной сверки всего брифа после предыдущей волны")
            if opening and prior == max(t["wave"] for t in self.s["tasks"] if t["wave"] < task["wave"]):
                require(review["basis"] == basis(self.store, self.s, "wave"), "Последняя сверка волны устарела; повторите общую проверку")
        if opening:
            self.s["waveEntries"][str(task["wave"])] = {
                "plan": plan_basis(self.s), "openedAt": timestamp(),
                **({"checkMode": check_modes.current(self.s)} if check_modes.enabled(self.s) else {})}
            if check_modes.is_fast(self.s):  # survives reset, which clears waveEntries
                mark = {"wave": task["wave"], "plan": plan_basis(self.s)}
                if mark not in self.s.setdefault("fastWaves", []):
                    self.s["fastWaves"].append(mark)
        if self.s.get("policy", {}).get("testIntent"):
            test_intent.capture_baseline(self.store, self.s, tid)
        at = timestamp()
        task["status"] = "running"
        task["phase"] = "implementation"
        timing.start(task, at)
        timing.start_phase(task, at)
        task["attempts"] = task.get("attempts", 0) + 1
        task.pop("proof", None)
        self.save("task-started:" + tid, at)

    def _wave_unchecked(self, number):
        return check_modes.wave_unchecked(self.s, number)

    def ready(self):
        """Addressed scheduler: ready peers only; never bypass the wave gate."""
        self.build_ready()
        unfinished = [t for t in self.s["tasks"] if t["status"] not in ("completed", "blocked")]
        wave = min((t["wave"] for t in unfinished), default=None)
        deferred = self.pipeline._deferred_ids()
        tasks = []
        for task in unfinished:
            if task["wave"] != wave or task["status"] != "pending":
                continue
            if self._dependencies_ready(task, deferred):
                prior = [w for w in {t["wave"] for t in self.s["tasks"]} if w < wave and not self._wave_unchecked(w)]
                if any(not self.s.get("waves", {}).get(str(w), {}).get("passed") for w in prior):
                    continue
                tasks.append({"id": task["id"], "title": task["title"],
                              "context": basis(self.store, self.s, "task", task["id"])})
        return {"wave": wave, "tasks": tasks, "startRechecksAllGuards": True}

    def wait(self, note):
        self.running()
        require(not self.s.get("sealed") and not self.s.get("tasks"), "Вопросы допустимы только при подготовке брифа")
        autonomy.require_questions_allowed(self.s)
        require(note.strip() and not SECRET.search(note), "Нужно безопасное описание ожидаемого ответа")
        at = timestamp()
        timing.stop(self.s, at)
        if preflight.enabled(self.s):
            timing.begin_wait(self.s, "owner_question", at)
        self.s["waiting"] = {"kind": "owner_question", "note": note, "since": at}
        self.save("owner-answer-awaited", at)
        return self.s["waiting"]

    def answered(self, path):
        self.running(allow_wait=True)
        require(isinstance(self.s.get("waiting"), dict) and self.s["waiting"].get("kind") == "owner_question",
                "Нет ожидаемого ответа владельца")
        text = self.read_input(path)
        artifact = self.store.artifact("briefs", text.encode(), ".md")
        artifact["id"] = "B%03d" % (len(self.s["briefs"]) + 1)
        self.s["briefs"].append(artifact)
        self.s["sealed"], self.s["stage"] = False, "brief"
        at = timestamp()
        if preflight.enabled(self.s):
            timing.end_wait(self.s, at)
        self.s["waiting"] = None
        timing.start(self.s, at)
        self.save("owner-answer-received", at)
        return {"waiting": None, "brief": artifact}

    def block(self, tid, note):
        self.running()
        require(bool(note.strip()), "Нужна причина препятствия")
        task = self.task(tid)
        at = timestamp()
        timing.stop(task, at)
        task["status"], task["note"] = "blocked", note
        task.pop("proof", None)
        self.save("task-blocked:" + tid, at)

    def reset(self, tid):
        self.running()
        self.pipeline.reset_guard(tid)
        affected = {tid}; self.task(tid)
        while True:
            next_set = affected | {t["id"] for t in self.s["tasks"] if set(t["dependsOn"]) & affected}
            if next_set == affected:
                break
            affected = next_set
        at = timestamp()
        for task in self.s["tasks"]:
            if task["id"] in affected:
                timing.stop(task, at)
                task.pop("finishedAt", None)
                task["status"] = "pending"
                task.pop("proof", None)
                task.pop("phaseTiming", None)
                task.pop("deferred", None)
        self.s["waves"] = {}
        self.s["waveEntries"] = {}
        self.save("tasks-reset:" + ",".join(sorted(affected)), at)

    def verify(self, command, timeout, task_id=None):
        self.running()
        managed = {}
        if preflight.enabled(self.s):
            from . import resources
            managed["resource_context"] = resources.context(self.store, self.s)
        executions.validate_command(command)
        require(command and all(isinstance(x, str) for x in command), "Нужна команда проверки после --")
        require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                and math.isfinite(timeout) and 0 < timeout <= 86400, "Нужен положительный конечный timeout")
        require(not SECRET.search(" ".join(command)), "Секреты нельзя передавать аргументами команды")
        secrets = [v for k, v in os.environ.items() if cleanroom.SENSITIVE_ENV.search(k) and len(v) >= 8]
        require(not any(value in " ".join(command) for value in secrets), "Секреты нельзя передавать аргументами команды")
        attempt = None
        if task_id:
            self.task(task_id)
            attempt = self.s.get("taskHistory", {}).get(task_id, [None])[-1]
            require(attempt is not None, "Сначала подготовьте команду задачи")
        code = task_basis(self.store, self.s, task_id) if task_id else self.store.code()
        started = time.monotonic()
        if attempt is not None:
            attempt["verification"] = None
        else:
            self.s["verification"] = None
        self.s["activity"] = {"kind": "verification", "task": task_id, "startedAt": timestamp()}
        start_event = "verification-started"
        self.save(start_event)
        if managed:
            def admitted_verification(kind):
                if self.resource_admitted(kind):
                    # Keep the strict start/completion event pair after the
                    # admission event, including this activity's task payload.
                    self.save(start_event)
            managed["on_admitted"] = admitted_verification
        try:
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            raw, rc, timed_out, truncated, redacted = process_runner.run(
                command, self.store.root, environment, timeout, max_output=1048576,
                secret_pattern=SECRET, secrets=secrets, **managed)
        except Invalid as error:
            from . import resources
            if not isinstance(error, resources.CapacityRefused):
                raise
            self.s.pop("activity", None)
            timing.begin_wait(self.s, "resource_wait", timestamp())
            self.s["waiting"].update(note=str(error), resourceKind=error.kind)
            self.save("resource-capacity-awaited")
            raise
        except OSError:
            self.s.pop("activity", None)
            self.save("verification-could-not-start")
            raise Invalid("Не удалось запустить команду проверки")
        output = raw.decode("utf-8", "replace")
        if redacted or SECRET.search(output):
            output = "AISpa withheld the entire output because it may contain a secret.\n"
            rc = 125
        artifact = self.store.artifact("evidence", output.encode() or b"(no output)\n", ".log")
        after = task_basis(self.store, self.s, task_id) if task_id else self.store.code()
        receipt = {"command": command, "exitCode": rc, "code": code, "changedCode": code != after,
                   "launchError": rc == 127 and raw == process_runner._START_FAILED,
                   "timedOut": timed_out, "truncated": truncated, "redacted": redacted,
                   "output": artifact, "durationSeconds": round(time.monotonic() - started, 3), "at": timestamp()}
        if attempt is not None:
            receipt.update(task=task_id, attempt=attempt["id"], candidate=code)
            attempt["verification"] = receipt
        else:
            self.s["verification"] = receipt
        self.s.pop("activity", None)
        self.save("verification-executed")
        return task_verified(self.store, self.s, task_id) if task_id else verified(self.store, self.s)

    def complete_task(self, tid, path):
        return self.pipeline.accept(tid, path)

    def wave(self, number, path):
        require(not self._focused(), WAVES_UNCHECKED)
        self.build_ready()
        require(verified(self.store, self.s), "Нет актуальной общей проверки кода перед сверкой волны")
        require(all(task_proof_valid(self.store, self.s, t["id"]) for t in self.s["tasks"] if t["status"] == "completed"),
                "Приёмка одной из завершённых задач отсутствует, повреждена или устарела")
        members = [t for t in self.s["tasks"] if t["wave"] == number]
        require(members and all(t["status"] in ("completed", "blocked") for t in members), "Волна ещё не закончилась")
        text = self.read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "wave")
        expected = {(r["id"], c) for r in active(self.s) for c in r["acceptance"]}
        artifacts, failures = evidence_rows(self.store, report, expected)
        delivered = {r["id"] for r in active(self.s) if all(
            t["status"] == "completed" for t in self.s["tasks"] if r["id"] in t["requirements"])}
        passed = not report["unmapped"] and not any(f["requirement"] in delivered for f in failures)
        self.s.setdefault("waves", {})[str(number)] = {
            "passed": passed, "plan": plan_basis(self.s), "basis": report["basis"],
            "report": self.store.artifact("reviews", text.encode(), ".json"), "artifacts": artifacts}
        self.save("wave-reviewed:" + str(number))
        return passed

    def _focused(self):
        return self.s.get("policy", {}).get("focusedReview") is True

    def _task_proofs_current(self):
        """After a final repair, task proofs stand at the branch-reviewed candidate and the chain covers the rest."""
        if final_repairs.repaired(self.s):
            return final_repairs.tasks_settled(self.store, self.s)
        return all(task_proof_valid(self.store, self.s, t["id"]) for t in self.s["tasks"] if t["status"] == "completed")

    def _branch_ready(self):
        """Focused, every mode: lenses recorded, warnings decided, repairs checked up to the current code."""
        if not self.s.get("policy", {}).get("integrationReview"):
            return
        require(integration.branch_reviewed(self.store, self.s),
                "До сдачи нужна проверка всей ветки: integration-review по каждой линзе режима ("
                + "; ".join("+".join(lens) for lens in integration.lenses(self.s))
                + "), ревьюер линзы behavior разбирает все открытые предупреждения")
        require(final_repairs.findings_settled(self.s),
                "Подтверждённые находки ветки ещё не исправлены: final-repair open --file (kind findings)")
        require(final_repairs.chain_current(self.store, self.s),
                "Цепочка кандидатов не заканчивается текущим кодом: каждое изменение после проверки ветки проверяет final-repair")
        require(integration.proof_valid(self.store, self.s),
                "Доказательства задач изменились после проверки ветки")

    def handoff_ready(self):
        if self._focused():
            self._branch_ready()
        elif self.s.get("policy", {}).get("integrationReview") and not check_modes.is_fast(self.s):
            require(integration.proof_valid(self.store, self.s),
                    "До сдачи нужны актуальное полное ревью финальной задачи по её политике и отдельная приёмка")
        if self.s.get("policy", {}).get("deliveryDocs"):
            require(handoff.valid(self.store, self.s, command_receipt=lambda ref: executions.resolve(self.store, self.s, ref)),
                    "До чистого клона и приёмки подготовьте и проверьте отслеживаемые документы проекта: handoff --file")

    def acceptance(self, path):
        self.build_ready()
        self.handoff_ready()
        require(final_repairs.chain_current(self.store, self.s),
                "Цепочка кандидатов не заканчивается текущим кодом: изменения после проверки ветки проверяет final-repair")
        if self.s.get("policy", {}).get("cleanClone"):
            require(cleanroom.valid(self.store, self.s), "Для итоговой приёмки нужен актуальный успешный запуск из чистого клона")
            require(blind.valid(self.store, self.s), "Сначала нужны независимые наблюдения из чистого клона")
        require(verified(self.store, self.s), "Нет успешной проверки текущего кода")
        require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"]), "Есть незавершённые задачи без результата или блокера")
        require(self._task_proofs_current(), "Не все завершённые задачи имеют действительную цепочку приёмки")
        text = self.read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "acceptance")
        author = phase_receipts.report_actor(self.store, self.s, "acceptance", report)
        if self.s.get("policy", {}).get("cleanClone"):
            require(report.get("cleanrun") == self.s["cleanrun"]["artifact"]["sha256"], "Приёмщик должен сослаться на точную проверку чистого клона")
        expected = {(r["id"], c) for r in active(self.s) for c in r["acceptance"]}
        artifacts, failures = evidence_rows(self.store, report, expected)
        criterion_artifacts = list(artifacts)
        if self.s.get("policy", {}).get("cleanClone"):
            failures.extend(blind.mapping(self.store, self.s, report))
        from . import product_flows
        flow_artifacts, flow_failures = product_flows.check(self.store, self.s, report, failures)
        artifacts.extend(flow_artifacts)
        failures.extend(flow_failures)
        execution_findings = [f for f in failures if f.get("kind") == "execution"]
        failures = [f for f in failures if f.get("kind") != "execution"]
        missing = {f["requirement"] for f in failures}
        core = set(self.s["core"]) & {r["id"] for r in active(self.s)}
        passed = not report["unmapped"] and None not in missing and not core & missing
        core_tasks = [t for t in self.s["tasks"] if set(t["requirements"]) & core]
        passed = passed and all(t["status"] == "completed" for t in core_tasks)
        passed = passed and not flow_failures
        # Max: a core task the owner's rules deferred goes to the remainder, not to a dead end; any other failure still stops the delivery.
        deferred_settled = False
        if not passed and not flow_failures and check_modes.is_max(self.s) and not report["unmapped"] and None not in missing:
            deferred = check_modes.deferred_ids(self.s, self.store)
            owned = {r for t in self.s["tasks"] if t["id"] in deferred for r in t["requirements"]}
            deferred_settled = bool(deferred and all(t["status"] == "completed" or t["id"] in deferred for t in core_tasks)
                                    and missing <= owned)
        # Reuse only a fully successful independent source-first final review of
        # one completed task. Intermediate waves retain their original checks.
        numbers = {t["wave"] for t in self.s["tasks"]}
        last_wave = max(numbers)
        reusable = bool(self.s.get("policy", {}).get("finalWaveReuse") and not check_modes.is_max(self.s) and
                        len(self.s["tasks"]) == 1 and len(numbers) == 1 and
                        self.s["tasks"][0]["status"] == "completed" and passed and
                        not failures and not report["unmapped"] and
                        self.s.get("policy", {}).get("cleanClone") and blind.valid(self.store, self.s))
        missing_wave = reusable and any("derivedFrom" in self.s.get("waves", {}).get(str(n), {}) for n in numbers)
        for number in numbers:
            if self._wave_unchecked(number):
                continue
            checkpoint = self.s.get("waves", {}).get(str(number))
            okay = bool(checkpoint and checkpoint["passed"] and checkpoint["plan"] == plan_basis(self.s)
                        and artifact_valid(self.store, checkpoint["report"])
                        and all(artifact_valid(self.store, a) for a in checkpoint.get("artifacts", [])))
            if number == last_wave:
                okay = okay and checkpoint["basis"] == basis(self.store, self.s, "wave")
            if not okay:
                require(reusable and number == last_wave, "Не все волны прошли актуальную сверку с полным брифом")
                missing_wave = True
        report_artifact = self.store.artifact("reviews", text.encode(), ".json")
        if missing_wave:
            wave_basis = basis(self.store, self.s, "wave")
            derived = {"kind": "derived-final-wave", "run": self.s["run"], "wave": last_wave,
                       "basis": wave_basis, "plan": plan_basis(self.s),
                       "derivedFrom": report_artifact["sha256"],
                       "blindReport": self.s["blindReview"]["report"]["sha256"],
                       "checks": report["checks"], "unmapped": [], "actor": author,
                       "predicate": "full-scope-delivered-requirements-passed"}
            ref = self.store.artifact("derived-wave-checkpoints", (json.dumps(derived, sort_keys=True) + "\n").encode(), ".json")
            self.s.setdefault("waves", {})[str(last_wave)] = {
                "passed": True, "plan": plan_basis(self.s), "basis": wave_basis,
                "report": ref, "artifacts": criterion_artifacts, "derivedFrom": report_artifact["sha256"],
                "blindReport": self.s["blindReview"]["report"]["sha256"]}
        self.s["gates"]["acceptance"] = {
            "passed": passed, "basis": report["basis"], "failures": failures,
            "unmapped": report["unmapped"], "artifacts": artifacts,
            "executionFindings": execution_findings,
            **({"deferredSettled": True} if deferred_settled else {}),
            "report": report_artifact}
        phase_receipts.record(self.store, self.s, "acceptance", report_artifact, actor=author, basis=report["basis"])
        self.s["stage"] = "acceptance"
        self.save("acceptance-reviewed")
        return passed

    def finish(self):
        self.build_ready()
        self.handoff_ready()
        # The owner's fast mode reports the problems at the end instead of repairing them first.
        require(gate_valid(self.store, self.s, "acceptance", settled=True), "Обязательное ядро не принято или доказательства устарели")
        require(verified(self.store, self.s), "Проверка текущего кода устарела")
        require(self.store.clean(), "Сначала сохраните изменения проекта в Git")
        if self.s.get("policy", {}).get("projectKnowledge"):
            require(knowledge.valid(self.store, self.s), "До сдачи сохраните проверенные знания о готовом коде: knowledge-record")
        self.store.git("rev-parse", "HEAD")
        repaired = final_repairs.repaired(self.s)
        require(not repaired or final_repairs.tasks_settled(self.store, self.s), "Устарела или повреждена цепочка проверки задачи")
        for task in self.s["tasks"]:
            if task["status"] == "completed":
                require(repaired or task_proof_valid(self.store, self.s, task["id"]), "Устарела или повреждена цепочка проверки задачи")
                proof = task.get("proof", {})
                require(artifact_valid(self.store, proof.get("report", {})) and all(
                    artifact_valid(self.store, a) for a in proof.get("artifacts", [])), "Повреждены доказательства задачи")
        residuals = self.s["gates"]["acceptance"]["failures"]
        excluded = [r["id"] for r in self.s["requirements"] if r["scope"] != "active"]
        blocked = [t["id"] for t in self.s["tasks"] if t["status"] == "blocked"]
        inherited = carryover.unresolved(self.store, self.s)
        inherited_execution = carryover.unresolved_execution(self.store, self.s)
        at = timestamp()
        execution_results = execution_obligations.evaluate(self.store, self.s, at)
        execution_remaining = [row for row in execution_results if row["status"] != "passed"]
        leftover = final_repairs.remaining_findings(self.s) if self._focused() else []
        self.s["status"] = "delivered_with_remaining" if residuals or excluded or blocked or inherited or execution_remaining or inherited_execution or leftover else "accepted"
        self.s["remaining"] = {"checks": residuals, "excluded": excluded, "blockedTasks": blocked,
                               "deferredTasks": sorted(check_modes.deferred_ids(self.s, self.store)),
                               "inherited": [{"id": item["id"], "status": item["status"], "text": item.get("requirement", {}).get("text", "")} for item in inherited]}
        if execution_obligations.enabled(self.s):
            self.s["executionResults"] = execution_results
            self.s["remaining"]["execution"] = execution_remaining
            receipt = {"run": self.s["run"], "event": "engine-finish", "at": at, "results": execution_results}
            ref = self.store.artifact("execution-results", (json.dumps(receipt, sort_keys=True) + "\n").encode(), ".json")
            self.s.setdefault("executionResultHistory", []).append(ref)
        if inherited_execution:
            self.s["remaining"]["inheritedExecution"] = inherited_execution
        if self._focused():
            self.s["remaining"]["findings"] = leftover
        if check_modes.enabled(self.s):
            gate = self.s["gates"]["acceptance"]
            # Focused: a failure after both blind repair cycles is delivered with its list in every mode.
            if (self._focused() or check_modes.is_fast(self.s)) and not gate.get("passed"):
                self.s["status"] = "delivered_with_remaining"
                if gate.get("unmapped"):
                    self.s["remaining"]["unmapped"] = gate["unmapped"]
            self.s["remaining"]["skippedChecks"] = check_modes.skipped_checks(self.s)
            self.s["remaining"]["checkModeChanges"] = check_modes.history_view(self.store, self.s)
        timing.stop(self.s, at, completed=True)
        self.save("delivery-recorded", at)

    def close(self, status, note):
        require(status in ("paused", "blocked", "cancelled"), "Неизвестная причина остановки")
        require(bool(note.strip()), "Нужна причина остановки")
        at = timestamp()
        if preflight.enabled(self.s) and status != "cancelled":
            timing.begin_wait(self.s, "blocked" if status == "blocked" else "paused", at)
        elif preflight.enabled(self.s) and timing.waiting(self.s):
            timing.stop(self.s.get("waitTiming", {}).get(self.s["waiting"]["kind"], {}), at, completed=True)
        timing.stop(self.s, at)
        for task in self.s["tasks"]:
            timing.stop(task, at)
        self.s.pop("activity", None)
        self.s["status"] = status
        if preflight.enabled(self.s):
            if status == "cancelled":
                self.s["waiting"] = {"kind": "cancelled", "note": note, "at": at}
                timing.stop(self.s, at, completed=True)
            else:
                self.s["waiting"]["note"] = note
        else:
            self.s["waiting"] = note
        self.save("run-" + status, at)

    def resume(self, host=None, expected_basis=None):
        require(not self.s.get("journalRecovery", {}).get("pending"),
                "После восстановления сначала journal-verify и journal-reconcile")
        if preflight.enabled(self.s):
            require(self.s["status"] in ("running", "paused", "blocked"), "Preflight: terminal run cannot resume")
            previous_session = self.s.get("hostSession")
            report = preflight.adopt(self, host, expected_basis=expected_basis, save=False)
            if self.s["status"] == "running":
                self.s.setdefault("continuations", []).append({"previousSession": previous_session,
                    "session": self.s["hostSession"], "source": host["source"], "basis": report["basis"], "at": timestamp()})
                self.save("run-continued")
                return report
        else:
            require(self.s["status"] != "running", "Прогон уже выполняется")
        self.s["status"] = "running"
        at = timestamp()
        if preflight.enabled(self.s):
            timing.end_wait(self.s, at)
        self.s["waiting"] = None
        timing.start(self.s, at)
        for task in self.s["tasks"]:
            if task["status"] == "running":
                timing.start(task, at)
        self.s.pop("activity", None)
        self.s["gates"].pop("acceptance", None)
        self.save("run-resumed", at)

    def decision(self, text):
        self.running()
        require(bool(text.strip()), "Пустое решение")
        require(not SECRET.search(text), "В решении обнаружен возможный секрет")
        self.s["notes"].append({"at": timestamp(), "text": text})
        self.save("agent-decision-recorded")
