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
from . import timing
from .team import TaskPipeline, task_proof_valid
from . import project_context, discovery, carryover, cleanroom, blind, process_runner, ideas, planning, knowledge, product, handoff, executions, briefmap, design_contract, participants, integration, test_intent


SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                    r"sk-[A-Za-z0-9_-]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
                    r"-----BEGIN [^-]*PRIVATE KEY-----)")


def read_input(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    require(bool(text.strip()), "Входной файл пуст")
    require(not SECRET.search(text), "Обнаружен возможный секрет; подготовьте обезличенный входной файл")
    return text


class Workflow:
    read_input = staticmethod(read_input)

    def __init__(self, store, state):
        self.store, self.s = store, state
        self.pipeline = TaskPipeline(self)

    def save(self, event, at=None):
        at = at or timestamp()
        for task in self.s.get("tasks", []):
            timing.sync_phase(task, at, self.s.get("status") == "running")
        self.store.append(self.s, event)
        if self.s.get("projectContext", {}).get("initialized"):
            try:
                project_context.refresh(self.store, self.s)
            except (OSError, Invalid) as error:
                raise Invalid("Событие записано, но память проекта не обновилась. Выполните context-refresh без повтора операции") from error

    def running(self, *, allow_wait=False):
        require(not self.s.get("journalRecovery", {}).get("pending"),
                "После восстановления сначала проверьте и согласуйте состояние: journal-reconcile")
        require(self.s["status"] == "running", "Прогон закрыт или приостановлен; сначала resume")
        require(allow_wait or not (isinstance(self.s.get("waiting"), dict)
                                  and self.s["waiting"].get("kind") == "owner_question"),
                "Ожидается ответ владельца; сначала сохраните факт ответа командой answered")

    def brief(self, path):
        self.running()
        text = read_input(path)
        artifact = self.store.artifact("briefs", text.encode(), ".md")
        artifact["id"] = "B%03d" % (len(self.s["briefs"]) + 1)
        self.s["briefs"].append(artifact)
        self.s["sealed"] = False
        self.s["stage"] = "brief"
        self.save("brief-added:" + artifact["id"])
        return artifact

    def requirements(self, path):
        self.running()
        additions = add_requirements(self.store, self.s, json.loads(read_input(path)))
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
        require(self.s["briefs"] and active(self.s), "Нужны бриф и действующие требования")
        if self.s.get("policy", {}).get("sourceAccounting"):
            require(briefmap.valid(self.store, self.s, actor_check=lambda s, a, r: participants.actor_check(self.store, s, a, r)),
                    "Каждая часть исходного брифа должна пройти независимый разбор и сопоставление: brief-map")
        if self.s.get("policy", {}).get("ideaReview"):
            require(ideas.valid(self.store, self.s), "До фиксации брифа нужен актуальный разбор идеи: idea --file")
        core = set(self.s.get("core", [])) | {r["id"] for r in active(self.s) if r["core"]}
        require(core & {r["id"] for r in active(self.s)}, "До разработки определите обязательное ядро")
        self.s["core"] = sorted(core)
        self.s["sealed"] = True
        self.s["stage"] = "spec"
        self.save("brief-sealed")

    def spec(self, path):
        self.running()
        require(self.s.get("sealed"), "Сначала зафиксируйте бриф и обязательное ядро")
        if self.s.get("policy", {}).get("ideaReview"):
            require(ideas.valid(self.store, self.s), "Разбор идеи отсутствует или устарел; обновите idea --file")
        if self.s.get("policy", {}).get("discovery"):
            require(discovery.valid(self.store, self.s), "Сначала изучите проект: discover и проверенный отчёт discover --file")
        self.s["spec"] = self.store.artifact("specs", read_input(path).encode(), ".md")
        self.save("spec-recorded")

    def coverage(self, path):
        self.running()
        require(self.s["spec"] and self.s.get("sealed"), "Нет спецификации для действующего брифа")
        text = read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "coverage")
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
        self.save("coverage-reviewed")
        return passed

    def plan(self, path, review_path=None):
        self.running()
        if self.s.get("policy", {}).get("discovery"):
            require(discovery.valid(self.store, self.s), "Исследование проекта отсутствует или устарело; обновите discover")
        carryover.guard_current(self.store, self.s)
        require(gate_valid(self.store, self.s, "coverage"), "Покрытие брифа не подтверждено или устарело")
        text = read_input(path)
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
        size_review = planning.review(self.store, self.s, tasks, json.loads(read_input(review_path))) if review_path else None
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
        self.s["stage"] = "build"
        self.save("plan-validated")

    def task(self, tid):
        result = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        require(result is not None, "Неизвестная задача")
        return result

    def build_ready(self):
        self.running()
        require(self.s.get("sealed"), "Бриф изменён; нужна повторная фиксация")
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
        self.pipeline.start_guard(tid)
        require(context == basis(self.store, self.s, "task", tid), "Пакет контекста задачи устарел")
        closure = {t["id"] for t in task_closure(self.s, tid)} - {tid}
        require(all(task_proof_valid(self.store, self.s, dep, allowed_task_ids=closure) for dep in task["dependsOn"]),
                "Зависимости задачи не приняты или доказательства устарели; включите актуального преемника в dependsOn")
        entry = self.s.setdefault("waveEntries", {}).get(str(task["wave"]))
        opening = not entry or entry["plan"] != plan_basis(self.s)
        for prior in {t["wave"] for t in self.s["tasks"] if t["wave"] < task["wave"]}:
            require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"] if t["wave"] == prior),
                    "Предыдущая волна ещё выполняется")
            review = self.s.get("waves", {}).get(str(prior))
            require(review and review["passed"] and review["plan"] == plan_basis(self.s)
                    and artifact_valid(self.store, review["report"])
                    and all(artifact_valid(self.store, a) for a in review.get("artifacts", [])),
                    "Нет актуальной сверки всего брифа после предыдущей волны")
            if opening and prior == max(t["wave"] for t in self.s["tasks"] if t["wave"] < task["wave"]):
                require(review["basis"] == basis(self.store, self.s, "wave"), "Последняя сверка волны устарела; повторите общую проверку")
        if opening:
            self.s["waveEntries"][str(task["wave"])] = {"plan": plan_basis(self.s), "openedAt": timestamp()}
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

    def ready(self):
        """Addressed scheduler: ready peers only; never bypass the wave gate."""
        self.build_ready()
        unfinished = [t for t in self.s["tasks"] if t["status"] not in ("completed", "blocked")]
        wave = min((t["wave"] for t in unfinished), default=None)
        tasks = []
        for task in unfinished:
            if task["wave"] != wave or task["status"] != "pending":
                continue
            closure = {t["id"] for t in task_closure(self.s, task["id"])} - {task["id"]}
            if all(task_proof_valid(self.store, self.s, dep, allowed_task_ids=closure) for dep in task["dependsOn"]):
                prior = [w for w in {t["wave"] for t in self.s["tasks"]} if w < wave]
                if any(not self.s.get("waves", {}).get(str(w), {}).get("passed") for w in prior):
                    continue
                tasks.append({"id": task["id"], "title": task["title"],
                              "context": basis(self.store, self.s, "task", task["id"])})
        return {"wave": wave, "tasks": tasks, "startRechecksAllGuards": True}

    def wait(self, note):
        self.running()
        require(not self.s.get("sealed") and not self.s.get("tasks"), "Вопросы допустимы только при подготовке брифа")
        require(note.strip() and not SECRET.search(note), "Нужно безопасное описание ожидаемого ответа")
        at = timestamp()
        timing.stop(self.s, at)
        self.s["waiting"] = {"kind": "owner_question", "note": note, "since": at}
        self.save("owner-answer-awaited", at)
        return self.s["waiting"]

    def answered(self, path):
        self.running(allow_wait=True)
        require(isinstance(self.s.get("waiting"), dict) and self.s["waiting"].get("kind") == "owner_question",
                "Нет ожидаемого ответа владельца")
        text = read_input(path)
        artifact = self.store.artifact("briefs", text.encode(), ".md")
        artifact["id"] = "B%03d" % (len(self.s["briefs"]) + 1)
        self.s["briefs"].append(artifact)
        self.s["sealed"], self.s["stage"] = False, "brief"
        self.s["waiting"] = None
        at = timestamp()
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
        self.s["waves"] = {}
        self.s["waveEntries"] = {}
        self.save("tasks-reset:" + ",".join(sorted(affected)), at)

    def verify(self, command, timeout, task_id=None):
        self.running()
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
        self.save("verification-started")
        try:
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            raw, rc, timed_out, truncated, redacted = process_runner.run(
                command, self.store.root, environment, timeout, max_output=1048576,
                secret_pattern=SECRET, secrets=secrets)
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
        self.build_ready()
        require(verified(self.store, self.s), "Нет актуальной общей проверки кода перед сверкой волны")
        require(all(task_proof_valid(self.store, self.s, t["id"]) for t in self.s["tasks"] if t["status"] == "completed"),
                "Приёмка одной из завершённых задач отсутствует, повреждена или устарела")
        members = [t for t in self.s["tasks"] if t["wave"] == number]
        require(members and all(t["status"] in ("completed", "blocked") for t in members), "Волна ещё не закончилась")
        text = read_input(path); report = json.loads(text)
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

    def handoff_ready(self):
        if self.s.get("policy", {}).get("integrationReview"):
            require(integration.proof_valid(self.store, self.s),
                    "До сдачи нужны два общих code/security-ревью в финальной задаче и её приёмка")
        if self.s.get("policy", {}).get("deliveryDocs"):
            require(handoff.valid(self.store, self.s, command_receipt=lambda ref: executions.resolve(self.store, self.s, ref)),
                    "До чистого клона и приёмки подготовьте и проверьте отслеживаемые документы проекта: handoff --file")

    def acceptance(self, path):
        self.build_ready()
        self.handoff_ready()
        if self.s.get("policy", {}).get("cleanClone"):
            require(cleanroom.valid(self.store, self.s), "Для итоговой приёмки нужен актуальный успешный запуск из чистого клона")
            require(blind.valid(self.store, self.s), "Сначала нужны независимые наблюдения из чистого клона")
        require(verified(self.store, self.s), "Нет успешной проверки текущего кода")
        require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"]), "Есть незавершённые задачи без результата или блокера")
        require(all(task_proof_valid(self.store, self.s, t["id"]) for t in self.s["tasks"] if t["status"] == "completed"),
                "Не все завершённые задачи имеют действительную цепочку приёмки")
        for number in {t["wave"] for t in self.s["tasks"]}:
            checkpoint = self.s.get("waves", {}).get(str(number))
            require(checkpoint and checkpoint["passed"] and checkpoint["plan"] == plan_basis(self.s)
                    and artifact_valid(self.store, checkpoint["report"])
                    and all(artifact_valid(self.store, a) for a in checkpoint.get("artifacts", [])),
                    "Не все волны прошли сверку с полным брифом")
        last_wave = max(t["wave"] for t in self.s["tasks"])
        require(self.s["waves"][str(last_wave)]["basis"] == basis(self.store, self.s, "wave"),
                "Итоговая сверка последней волны устарела")
        text = read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "acceptance")
        if self.s.get("policy", {}).get("cleanClone"):
            require(report.get("cleanrun") == self.s["cleanrun"]["artifact"]["sha256"], "Приёмщик должен сослаться на точную проверку чистого клона")
        expected = {(r["id"], c) for r in active(self.s) for c in r["acceptance"]}
        artifacts, failures = evidence_rows(self.store, report, expected)
        if self.s.get("policy", {}).get("cleanClone"):
            failures.extend(blind.mapping(self.store, self.s, report))
        missing = {f["requirement"] for f in failures}
        core = set(self.s["core"]) & {r["id"] for r in active(self.s)}
        passed = not report["unmapped"] and None not in missing and not core & missing
        core_tasks = [t for t in self.s["tasks"] if set(t["requirements"]) & core]
        passed = passed and all(t["status"] == "completed" for t in core_tasks)
        self.s["gates"]["acceptance"] = {
            "passed": passed, "basis": report["basis"], "failures": failures,
            "unmapped": report["unmapped"], "artifacts": artifacts,
            "report": self.store.artifact("reviews", text.encode(), ".json")}
        self.s["stage"] = "acceptance"
        self.save("acceptance-reviewed")
        return passed

    def finish(self):
        self.build_ready()
        self.handoff_ready()
        require(gate_valid(self.store, self.s, "acceptance"), "Обязательное ядро не принято или доказательства устарели")
        require(verified(self.store, self.s), "Проверка текущего кода устарела")
        require(self.store.clean(), "Сначала сохраните изменения проекта в Git")
        if self.s.get("policy", {}).get("projectKnowledge"):
            require(knowledge.valid(self.store, self.s), "До сдачи сохраните проверенные знания о готовом коде: knowledge-record")
        self.store.git("rev-parse", "HEAD")
        for task in self.s["tasks"]:
            if task["status"] == "completed":
                require(task_proof_valid(self.store, self.s, task["id"]), "Устарела или повреждена цепочка проверки задачи")
                proof = task.get("proof", {})
                require(artifact_valid(self.store, proof.get("report", {})) and all(
                    artifact_valid(self.store, a) for a in proof.get("artifacts", [])), "Повреждены доказательства задачи")
        residuals = self.s["gates"]["acceptance"]["failures"]
        excluded = [r["id"] for r in self.s["requirements"] if r["scope"] != "active"]
        blocked = [t["id"] for t in self.s["tasks"] if t["status"] == "blocked"]
        inherited = carryover.unresolved(self.store, self.s)
        self.s["status"] = "delivered_with_remaining" if residuals or excluded or blocked or inherited else "accepted"
        self.s["remaining"] = {"checks": residuals, "excluded": excluded, "blockedTasks": blocked,
                               "inherited": [{"id": item["id"], "status": item["status"], "text": item.get("requirement", {}).get("text", "")} for item in inherited]}
        timing.stop(self.s, timestamp(), completed=True)
        self.save("delivery-recorded")

    def close(self, status, note):
        require(status in ("paused", "blocked", "cancelled"), "Неизвестная причина остановки")
        require(bool(note.strip()), "Нужна причина остановки")
        at = timestamp()
        timing.stop(self.s, at)
        for task in self.s["tasks"]:
            timing.stop(task, at)
        self.s.pop("activity", None)
        self.s["status"], self.s["waiting"] = status, note
        self.save("run-" + status, at)

    def resume(self):
        require(not self.s.get("journalRecovery", {}).get("pending"),
                "После восстановления сначала journal-verify и journal-reconcile")
        require(self.s["status"] != "running", "Прогон уже выполняется")
        self.s["status"], self.s["waiting"] = "running", None
        at = timestamp()
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
