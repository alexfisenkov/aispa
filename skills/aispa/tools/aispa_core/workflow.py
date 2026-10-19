"""Domain operations. No publishing, agent spawning or external messages."""
import json
from pathlib import Path
import re
import subprocess
import time

from .contracts import (active, add_requirements, artifact_valid, basis, definition,
                        evidence_rows, gate_valid, plan_basis, report_header, require,
                        validate_plan, verified)
from .store import Invalid, timestamp


SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                    r"sk-[A-Za-z0-9_-]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
                    r"-----BEGIN [^-]*PRIVATE KEY-----)")


def read_input(path):
    text = Path(path).read_text(encoding="utf-8")
    require(bool(text.strip()), "Входной файл пуст")
    require(not SECRET.search(text), "Обнаружен возможный секрет; подготовьте обезличенный входной файл")
    return text


class Workflow:
    def __init__(self, store, state):
        self.store, self.s = store, state

    def save(self, event):
        self.store.append(self.s, event)

    def running(self):
        require(self.s["status"] == "running", "Прогон закрыт или приостановлен; сначала resume")

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
        req["scope"] = {"drop": "dropped", "defer": "deferred", "restore": "active"}[action]
        req["decision"] = {"brief": brief, "quote": quote, "action": action}
        self.s["sealed"] = False
        self.save("owner-scope-decision:" + rid)

    def seal(self):
        self.running()
        require(self.s["briefs"] and active(self.s), "Нужны бриф и действующие требования")
        core = set(self.s.get("core", [])) | {r["id"] for r in active(self.s) if r["core"]}
        require(core & {r["id"] for r in active(self.s)}, "До разработки определите обязательное ядро")
        self.s["core"] = sorted(core)
        self.s["sealed"] = True
        self.s["stage"] = "spec"
        self.save("brief-sealed")

    def spec(self, path):
        self.running()
        require(self.s.get("sealed"), "Сначала зафиксируйте бриф и обязательное ядро")
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
        self.s["gates"]["coverage"] = {"passed": passed, "basis": report["basis"],
                                         "report": self.store.artifact("reviews", text.encode(), ".json")}
        self.save("coverage-reviewed")
        return passed

    def plan(self, path):
        self.running()
        require(gate_valid(self.store, self.s, "coverage"), "Покрытие брифа не подтверждено или устарело")
        text = read_input(path)
        tasks = validate_plan(self.s, json.loads(text))
        old = {t["id"]: t for t in self.s["tasks"]}
        contract = basis(self.store, self.s, "coverage")
        for task in tasks:
            previous = old.get(task["id"])
            task["origin"] = contract
            if previous and definition(previous) == definition(task) and previous.get("origin") == contract:
                for key in ("status", "proof", "note"):
                    if key in previous:
                        task[key] = previous[key]
        self.s["tasks"] = tasks
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
        require(gate_valid(self.store, self.s, "coverage") and gate_valid(self.store, self.s, "plan"),
                "Проверки брифа или плана отсутствуют, провалены либо устарели")

    def start(self, tid, context):
        self.build_ready()
        task = self.task(tid)
        require(task["status"] in ("pending", "blocked"), "Задача уже запущена или завершена")
        require(context == basis(self.store, self.s, "task", tid), "Пакет контекста задачи устарел")
        require(all(self.task(dep)["status"] == "completed" for dep in task["dependsOn"]), "Зависимости задачи ещё не завершены")
        for prior in {t["wave"] for t in self.s["tasks"] if t["wave"] < task["wave"]}:
            require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"] if t["wave"] == prior),
                    "Предыдущая волна ещё выполняется")
            review = self.s.get("waves", {}).get(str(prior))
            require(review and review["passed"] and review["plan"] == plan_basis(self.s)
                    and artifact_valid(self.store, review["report"])
                    and all(artifact_valid(self.store, a) for a in review.get("artifacts", [])),
                    "Нет актуальной сверки всего брифа после предыдущей волны")
        task["status"] = "running"
        task.pop("proof", None)
        self.save("task-started:" + tid)

    def block(self, tid, note):
        self.running()
        require(bool(note.strip()), "Нужна причина препятствия")
        task = self.task(tid)
        task["status"], task["note"] = "blocked", note
        task.pop("proof", None)
        self.save("task-blocked:" + tid)

    def reset(self, tid):
        self.running()
        affected = {tid}; self.task(tid)
        while True:
            next_set = affected | {t["id"] for t in self.s["tasks"] if set(t["dependsOn"]) & affected}
            if next_set == affected:
                break
            affected = next_set
        for task in self.s["tasks"]:
            if task["id"] in affected:
                task["status"] = "pending"
                task.pop("proof", None)
        self.s["waves"] = {}
        self.save("tasks-reset:" + ",".join(sorted(affected)))

    def verify(self, command, timeout):
        self.running()
        require(command and all(isinstance(x, str) for x in command), "Нужна команда проверки после --")
        require(not SECRET.search(" ".join(command)), "Секреты нельзя передавать аргументами команды")
        code = self.store.code()
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=self.store.root, capture_output=True, timeout=timeout)
            raw, rc = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as e:
            raw, rc = (e.stdout or b"") + (e.stderr or b"") + b"\nAISPA: timeout\n", 124
        except OSError:
            raise Invalid("Не удалось запустить команду проверки")
        output = raw.decode("utf-8", "replace")
        if SECRET.search(output):
            output = "AISpa withheld the entire output because it may contain a secret.\n"
            rc = 125
        artifact = self.store.artifact("evidence", output.encode() or b"(no output)\n", ".log")
        self.s["verification"] = {"command": command, "exitCode": rc, "code": code,
                                  "changedCode": code != self.store.code(), "output": artifact,
                                  "durationSeconds": round(time.monotonic() - started, 3), "at": timestamp()}
        self.save("verification-executed")
        return verified(self.store, self.s)

    def complete_task(self, tid, path):
        self.build_ready()
        task = self.task(tid)
        require(task["status"] == "running", "Задача не выполняется")
        require(verified(self.store, self.s), "Нет успешной проверки текущего кода")
        text = read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "task", tid)
        artifacts, failures = evidence_rows(self.store, report, {(tid, c) for c in task["criteria"]})
        require(not failures and not report["unmapped"], "Задача не прошла проверку всех критериев")
        task["status"] = "completed"
        task["proof"] = {"basis": report["basis"], "report": self.store.artifact("reviews", text.encode(), ".json"),
                         "artifacts": artifacts}
        self.save("task-completed:" + tid)

    def wave(self, number, path):
        self.build_ready()
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

    def acceptance(self, path):
        self.build_ready()
        require(verified(self.store, self.s), "Нет успешной проверки текущего кода")
        require(all(t["status"] in ("completed", "blocked") for t in self.s["tasks"]), "Есть незавершённые задачи без результата или блокера")
        for number in {t["wave"] for t in self.s["tasks"]}:
            checkpoint = self.s.get("waves", {}).get(str(number))
            require(checkpoint and checkpoint["passed"] and checkpoint["plan"] == plan_basis(self.s)
                    and artifact_valid(self.store, checkpoint["report"])
                    and all(artifact_valid(self.store, a) for a in checkpoint.get("artifacts", [])),
                    "Не все волны прошли сверку с полным брифом")
        text = read_input(path); report = json.loads(text)
        report_header(self.store, self.s, report, "acceptance")
        expected = {(r["id"], c) for r in active(self.s) for c in r["acceptance"]}
        artifacts, failures = evidence_rows(self.store, report, expected)
        missing = {f["requirement"] for f in failures}
        core = set(self.s["core"]) & {r["id"] for r in active(self.s)}
        passed = not report["unmapped"] and not core & missing
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
        require(gate_valid(self.store, self.s, "acceptance"), "Обязательное ядро не принято или доказательства устарели")
        require(verified(self.store, self.s), "Проверка текущего кода устарела")
        require(self.store.clean(), "Сначала сохраните изменения проекта в Git")
        self.store.git("rev-parse", "HEAD")
        for task in self.s["tasks"]:
            if task["status"] == "completed":
                proof = task.get("proof", {})
                require(artifact_valid(self.store, proof.get("report", {})) and all(
                    artifact_valid(self.store, a) for a in proof.get("artifacts", [])), "Повреждены доказательства задачи")
        residuals = self.s["gates"]["acceptance"]["failures"]
        excluded = [r["id"] for r in self.s["requirements"] if r["scope"] != "active"]
        blocked = [t["id"] for t in self.s["tasks"] if t["status"] == "blocked"]
        self.s["status"] = "delivered_with_remaining" if residuals or excluded or blocked else "accepted"
        self.s["remaining"] = {"checks": residuals, "excluded": excluded, "blockedTasks": blocked}
        self.s["finishedAt"] = timestamp()
        self.save("delivery-recorded")

    def close(self, status, note):
        require(status in ("paused", "blocked", "cancelled"), "Неизвестная причина остановки")
        require(bool(note.strip()), "Нужна причина остановки")
        self.s["status"], self.s["waiting"] = status, note
        self.save("run-" + status)

    def resume(self):
        require(self.s["status"] != "running", "Прогон уже выполняется")
        self.s["status"], self.s["waiting"] = "running", None
        self.s.pop("finishedAt", None)
        self.s["gates"].pop("acceptance", None)
        self.save("run-resumed")

    def decision(self, text):
        self.running()
        require(bool(text.strip()), "Пустое решение")
        require(not SECRET.search(text), "В решении обнаружен возможный секрет")
        self.s["notes"].append({"at": timestamp(), "text": text})
        self.save("agent-decision-recorded")
