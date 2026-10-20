"""Public project handoff backed by existing docs and private execution receipts.

This module never creates or edits ordinary project documents. The host owns the
journal lock, saves the returned state, and supplies an authentic execution
resolver. A manifest is evidence of file/command provenance, not a semantic or
privacy certification of the prose written by a human or agent.
"""
import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import shutil
import subprocess
import sys

from . import carryover, discovery
from .contracts import artifact_valid, require, verified
from .portable import relative_name
from .store import Invalid, timestamp


SCHEMA = 1
CATEGORIES = ("quickstart", "architecture", "decisions", "glossary")
COMMANDS = ("install", "run", "check", "singleTest")
OPTIONAL = CATEGORIES[1:]
MAX_DOCS = 24
MAX_SOURCES = 12
MAX_BYTES = 1024 * 1024
MAX_TEXT = 1200
SHA = re.compile(r"[0-9a-f]{64}\Z")
HOST_PATH = re.compile(r"(?i)(?:/users/|/home/|[a-z]:[/\\]users[/\\]|\\users\\)")
PS_BARE = re.compile(r"[A-Za-z0-9_./\\:+=-]+\Z")
IS_WINDOWS = os.name == "nt"


def _need(condition, message):
    require(condition, "AISpa handoff: " + message)


def _safe_text(value, label, *, max_chars=MAX_TEXT):
    _need(isinstance(value, str) and value.strip() and len(value) <= max_chars,
          label + ": нужен короткий непустой текст")
    _need(not discovery.SECRET_CONTENT.search(value) and not carryover.SECRET.search(value),
          label + ": возможный секрет")
    _need(not HOST_PATH.search(value),
          label + ": локальный домашний путь нельзя публиковать")
    return value


def _tracked(store):
    raw = store.git("ls-files", "--cached", "-z")
    return {os.fsdecode(item) for item in raw.split(b"\0") if item}


def _project_file(store, row, tracked, *, document=False):
    _need(isinstance(row, dict), "неверная ссылка на файл")
    name = row.get("path")
    _need(isinstance(name, str), "путь файла должен быть строкой")
    try:
        canonical = relative_name(name)
    except ValueError as exc:
        raise Invalid("AISpa handoff: небезопасный путь файла") from exc
    _need(name == canonical and name in tracked and discovery._kind(name, explicit=True) is not None,
          "файл должен быть обычным отслеживаемым источником Git: " + name)
    if document:
        _need(Path(name).suffix.lower() == ".md", "документ должен быть Markdown")
    path = discovery._safe_path(store.root, name)
    _need(path.is_file() and path.stat().st_size <= MAX_BYTES, "файл отсутствует или слишком велик: " + name)
    raw = path.read_bytes()
    _need(hashlib.sha256(raw).hexdigest() == row.get("sha256"), "файл изменён: " + name)
    if document:
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise Invalid("AISpa handoff: документ должен быть UTF-8: " + name) from exc
        _need("\0" not in content and not discovery.SECRET_CONTENT.search(content)
              and not carryover.SECRET.search(content),
              "документ содержит двоичные данные или возможный секрет: " + name)
        _need(not HOST_PATH.search(content),
              "документ содержит локальный домашний путь: " + name)
        return content
    return None


def _section(content, heading):
    _safe_text(heading, "заголовок", max_chars=160)
    lines = content.splitlines()
    target = re.compile(r"^(#{1,6})\s+" + re.escape(heading.strip()) + r"\s*#*\s*$", re.I)
    matches = [(index, len(match.group(1))) for index, line in enumerate(lines)
               if (match := target.fullmatch(line))]
    _need(len(matches) == 1, "заголовок должен встречаться ровно один раз: " + heading)
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    body = "\n".join(lines[start + 1:end]).strip()
    _need(len(body) >= 20, "раздел документа пуст или слишком краток: " + heading)
    return body


def _ps_arg(arg):
    """A single literal PowerShell argv item, with no interpolation or splitting."""
    return arg if arg and PS_BARE.fullmatch(arg) else "'" + arg.replace("'", "''") + "'"


def _ps_join(argv):
    _need(isinstance(argv, list) and argv and all(isinstance(arg, str) and arg
          and '"' not in arg and "\r" not in arg and "\n" not in arg for arg in argv),
          "PowerShell 5.1 не сохраняет пустой аргумент, двойную кавычку или перенос строки")
    rendered = [_ps_arg(arg) for arg in argv]
    return ("& " if rendered[0].startswith("'") else "") + " ".join(rendered)


def _python_runner(runner):
    if runner == "py -3":
        if not IS_WINDOWS:
            return False
        launcher = shutil.which("py")
        if not launcher:
            return False
        try:
            probe = subprocess.run([launcher, "-3", "-c",
                                    "import json,sys; print(json.dumps(sys.executable, ensure_ascii=True))"],
                                   capture_output=True, text=True, timeout=5, check=False)
            selected = json.loads(probe.stdout.strip()) if probe.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        return isinstance(selected, str) and Path(selected).resolve() == Path(sys.executable).resolve()
    if runner not in ("python", "python3"):
        return False
    path = shutil.which(runner)
    return bool(path and Path(path).resolve() == Path(sys.executable).resolve())


def _display(command, runner):
    _need(isinstance(command, list) and command and len(command) <= 64
          and all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 2000 for arg in command)
          and bool(command[0]),
          "неверный argv команды")
    if runner is None:
        try:
            relative_name(command[0])
        except ValueError as exc:
            raise Invalid("AISpa handoff: исполняемый файл должен иметь переносимое относительное имя") from exc
        _need(command[0][0].isalnum() or command[0][0] == ".",
              "имя исполняемого файла должно начинаться с буквы, цифры или точки")
        _need(not PureWindowsPath(command[0]).drive and not command[0].startswith("/"),
              "абсолютный исполняемый путь нельзя публиковать")
        _need(not any(HOST_PATH.search(arg) for arg in command),
              "локальный исполняемый путь требует переносимого Python runner")
        return _ps_join(command) if IS_WINDOWS else shlex.join(command)
    actual_python = (shutil.which(command[0]) if "/" not in command[0] and "\\" not in command[0]
                     else command[0])
    _need(_python_runner(runner) and actual_python is not None
          and Path(actual_python).resolve() == Path(sys.executable).resolve(),
          "Python runner не соответствует проверенному интерпретатору")
    display_argv = runner.split() + command[1:]
    display = _ps_join(display_argv) if IS_WINDOWS else shlex.join(display_argv)
    _need(not HOST_PATH.search(display), "переносимая команда содержит локальный домашний путь")
    return display


def _validate(store, state, report, command_receipt, *, recording=False):
    _need(isinstance(report, dict) and type(report.get("schema")) is int and report["schema"] == SCHEMA
          and report.get("profile") in ("small", "standard"), "неверная схема или профиль")
    _need(set(report) == {"schema", "profile", "documents", "notApplicable", "commands", "obligations"}
          | ({"decisionDetails"} if "decisionDetails" in report else set()),
          "неизвестные поля handoff manifest")
    _need(callable(command_receipt), "нужен проверяющий сохранённых execution receipts")
    _need((not recording or state.get("status") == "running")
          and all(task.get("status") in ("completed", "blocked") for task in state.get("tasks", [])),
          "сначала завершите или явно заблокируйте задачи")
    docs = report.get("documents")
    _need(isinstance(docs, list) and 1 <= len(docs) <= MAX_DOCS, "нужны существующие документы")
    tracked = _tracked(store)
    content_by_path, sections, document_refs = {}, {}, []
    for item in docs:
        _need(isinstance(item, dict) and set(item) == {"path", "sha256", "sources", "covers"},
              "неверная запись документа")
        name = item.get("path")
        _need(isinstance(name, str), "путь документа должен быть строкой")
        _need(name not in content_by_path, "документ указан повторно")
        content = _project_file(store, item, tracked, document=True)
        sources = item.get("sources")
        _need(isinstance(sources, list) and 1 <= len(sources) <= MAX_SOURCES,
              "нужны исходные файлы для документа")
        seen, project_sources = set(), 0
        for source in sources:
            _need(isinstance(source, dict), "неверный source mapping")
            if set(source) == {"path", "sha256"}:
                _need(isinstance(source.get("path"), str), "путь source mapping должен быть строкой")
                _need(source.get("path") != name and source.get("path") not in seen,
                      "документ не может ссылаться только на себя")
                _project_file(store, source, tracked)
                seen.add(source["path"])
                project_sources += 1
            else:
                artifact = source.get("artifact")
                _need(set(source) == {"kind", "artifact"} and source["kind"] == "evidence"
                      and isinstance(artifact, dict) and set(artifact) == {"file", "sha256"}
                      and isinstance(artifact["file"], str) and artifact["file"].startswith("evidence/")
                      and artifact_valid(store, artifact), "неверная ссылка на свидетельство")
                _need(artifact["file"] not in seen, "источник повторяется")
                seen.add(artifact["file"])
        _need(project_sources > 0, "каждому публичному документу нужен доступный в Git источник")
        covers = item.get("covers")
        _need(isinstance(covers, dict) and covers and set(covers) <= set(CATEGORIES),
              "неверные категории документа")
        for category, heading in covers.items():
            _need(isinstance(heading, str), "заголовок категории должен быть строкой")
            key = (name, heading)
            section = sections.setdefault(key, _section(content, heading))
            _need(section.strip(), "пустой раздел")
        content_by_path[name] = content
        document_refs.append(copy.deepcopy(item))
    coverage = {category: [(doc["path"], heading) for doc in docs
                            for key, heading in doc["covers"].items() if key == category]
                for category in CATEGORIES}
    _need(coverage["quickstart"], "нужен раздел quickstart")
    na = report.get("notApplicable", {})
    _need(isinstance(na, dict) and set(na) <= set(OPTIONAL), "неверный список неприменимого")
    for category in OPTIONAL:
        _need(bool(coverage[category]) != (category in na),
              "категорию нужно раскрыть в документе или обоснованно исключить: " + category)
        if category in na:
            _need(report["profile"] == "small", "N/A категорий допустим только для малого проекта")
            _safe_text(na[category], "обоснование " + category)
            _need(any(na[category] in content for content in content_by_path.values()),
                  "обоснование N/A должно быть доступно в Git-документе: " + category)
    if coverage["decisions"]:
        details = report.get("decisionDetails")
        _need(isinstance(details, dict) and set(details) == {"choice", "alternatives", "consequences"},
              "решение должно назвать выбор, альтернативы и последствия")
        bodies = "\n".join(sections[key] for key in coverage["decisions"])
        for key, phrase in details.items():
            _safe_text(phrase, "решение " + key)
            _need(phrase in bodies, "деталь решения отсутствует в документе: " + key)
    else:
        _need("decisionDetails" not in report, "детали решения без раздела")
    commands = report.get("commands")
    _need(isinstance(commands, dict) and set(commands) == set(COMMANDS),
          "нужны install, run, check и singleTest")
    current_code = store.code()
    command_rows = {}
    for purpose in COMMANDS:
        entry = commands[purpose]
        _need(isinstance(entry, dict), "неверная команда: " + purpose)
        if "notApplicable" in entry:
            _need(purpose != "check" and set(entry) == {"notApplicable"},
                  "check всегда требует реального выполнения")
            _safe_text(entry["notApplicable"], "N/A " + purpose)
            _need(any(entry["notApplicable"] in sections[key] for key in coverage["quickstart"]),
                  "обоснование N/A команды должно быть в quickstart: " + purpose)
            command_rows[purpose] = copy.deepcopy(entry)
            continue
        _need(set(entry) == {"receipt", "document", "heading", "display", "runner"},
              "неверная привязка команды: " + purpose)
        _need(isinstance(entry["document"], str) and isinstance(entry["heading"], str),
              "документ и заголовок команды должны быть строками")
        _need(entry["document"] in content_by_path, "команда ссылается на чужой документ")
        section = sections.get((entry["document"], entry["heading"]))
        _need(section is not None, "команда должна быть в объявленном разделе")
        ref = entry["receipt"]
        _need(isinstance(ref, dict) and isinstance(ref.get("sha256"), str)
              and SHA.fullmatch(ref["sha256"]), "неверная ссылка на execution")
        receipt = command_receipt(ref)
        expected_purpose = "test" if purpose == "singleTest" else purpose
        _need(isinstance(receipt, dict) and receipt.get("successful") is True
              and receipt.get("exitCode") == 0 and receipt.get("purpose") == expected_purpose,
              "команда не имеет успешной подлинной квитанции: " + purpose)
        if purpose == "check":
            _need(receipt.get("kind") == "project" and receipt.get("code") == current_code,
                  "check должен быть успешной проверкой текущего кода")
            _need(verified(store, state), "нет действительной общей проверки проекта")
        display = _display(receipt.get("command"), entry["runner"])
        _need(entry["display"] == display and display in section,
              "документ должен содержать точную исполняемую команду: " + purpose)
        command_rows[purpose] = {**copy.deepcopy(entry),
                                 "historical": purpose != "check",
                                 "recheckNeeded": purpose != "check" or receipt.get("code") != current_code}
    obligations = report.get("obligations")
    _need(isinstance(obligations, list) and len(obligations) <= 128, "нужен явный список обязательств")
    blocked = {task["id"] for task in state.get("tasks", []) if task.get("status") == "blocked"}
    mapped = set()
    for item in obligations:
        _need(isinstance(item, dict) and set(item) == {"status", "text", "document", "heading", "task"}
              and item["status"] in ("open", "blocked"), "неверная запись обязательства")
        _safe_text(item["text"], "обязательство")
        _need(isinstance(item["document"], str) and isinstance(item["heading"], str)
              and (item["task"] is None or isinstance(item["task"], str)),
              "поля ссылки обязательства должны быть строками")
        _need(item["document"] in content_by_path, "обязательство ссылается на чужой документ")
        section = _section(content_by_path[item["document"]], item["heading"])
        _need(item["text"] in section, "обязательство отсутствует в документе")
        if item["task"] is not None:
            _need(item["status"] == "blocked" and item["task"] in blocked
                  and item["task"] not in mapped, "неверная привязка блокера")
            mapped.add(item["task"])
    _need(mapped == blocked, "каждая заблокированная задача должна быть документирована")
    return {"schema": SCHEMA, "run": state["run"], "profile": report["profile"],
            "code": current_code, "documents": document_refs, "notApplicable": copy.deepcopy(na),
            "decisionDetails": copy.deepcopy(report.get("decisionDetails")),
            "commands": command_rows, "obligations": copy.deepcopy(obligations)}


def record(store, state, report, *, command_receipt):
    """Validate an existing handoff and stage an immutable record in state."""
    body = _validate(store, state, report, command_receipt, recording=True)
    body["at"] = timestamp()
    raw = (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _need(len(raw) <= MAX_BYTES, "handoff manifest слишком велик")
    ref = store.artifact("handoff", raw, ".json")
    state["handoff"] = {"schema": SCHEMA, "run": state["run"], "code": body["code"], "artifact": ref}
    return copy.deepcopy(state["handoff"])


def valid(store, state, *, command_receipt):
    """Recheck immutable record, docs, sources, and authentic execution receipts."""
    try:
        row = state.get("handoff")
        if not isinstance(row, dict) or row.get("schema") != SCHEMA or row.get("run") != state.get("run"):
            return False
        ref = row.get("artifact")
        if not artifact_valid(store, ref):
            return False
        path = store.run / ref["file"]
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            return False
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("run") != state["run"] or body.get("code") != row["code"]:
            return False
        report = {key: copy.deepcopy(body[key]) for key in
                  ("schema", "profile", "documents", "notApplicable", "commands", "obligations")}
        if body.get("decisionDetails") is not None:
            report["decisionDetails"] = copy.deepcopy(body["decisionDetails"])
        for command in report["commands"].values():
            command.pop("historical", None)
            command.pop("recheckNeeded", None)
        expected = _validate(store, state, report, command_receipt)
        expected["at"] = body.get("at")
        return expected == body and row["code"] == store.code()
    except (Invalid, ValueError, KeyError, TypeError, OSError):
        return False
