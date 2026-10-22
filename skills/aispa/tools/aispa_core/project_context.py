"""Confined project bootstrap and derived context; the journal remains canonical.

The host supplies the native runtime, owns the Journal lock and appends state.
initialize links instructions once; refresh never modifies project instructions.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from .store import Invalid, digest
from .portable import is_link, relative_name
from . import autonomy, carryover, docmeta, ideas


RUNTIMES = ("codex", "claude-code", "cursor", "openclaw", "gemini", "antigravity")
MEMORY_LIMIT = 8000
BEGIN = b"<!-- AISPA_CONTEXT_BEGIN_V1 -->"
END = b"<!-- AISPA_CONTEXT_END_V1 -->"
_LEGACY_POINTER = BEGIN + b"\n" + (
    "## AISpa: проектный контекст\n\n"
    "Перед работой прочитайте `.aispa/context/INDEX.md` и `.aispa/context/memory.md`.\n"
    "Бриф, спецификация, план, решения и прогресс: `.aispa/context/`.\n"
    "Это производные представления: источники истины — неизменные источники и\n"
    "журнал `.aispa/runs/`; их ссылки и контрольные суммы — в `source-map.json`.\n"
    "Не редактируйте представления вручную; изменения фиксируются движком AISpa.\n"
    "Правила проекта вне этого блока сохраняют силу.\n"
).encode() + END
POINTER = BEGIN + b"\n" + (
    "## AISpa: проектный контекст\n\n"
    "Если `.aispa/context/INDEX.md` существует, прочитайте его и `.aispa/context/memory.md`.\n"
    "В чистом Git-клоне приватного `.aispa/context/` может не быть: тогда читайте\n"
    "существующие `README.md`, публичные документы в `docs/` и корневые правила проекта.\n"
    "Отсутствующие файлы не считайте доказательством отсутствия контекста;\n"
    "сверяйте фактические файлы и сохранённый бриф доступного прогона.\n"
    "При локальном прогоне бриф, спецификация, план, решения и прогресс — в `.aispa/context/`.\n"
    "Это производные представления: источники истины — неизменные источники и\n"
    "журнал `.aispa/runs/`; их ссылки и контрольные суммы — в `source-map.json`.\n"
    "Не редактируйте представления вручную; изменения фиксируются движком AISpa.\n"
    "Правила проекта вне этого блока сохраняют силу.\n"
).encode() + END
CURSOR_RULE = ("---\ndescription: AISpa project context\nalwaysApply: true\n---\n\n".encode()
               + POINTER + b"\n")
_LEGACY_CURSOR_RULE = ("---\ndescription: AISpa project context\nalwaysApply: true\n---\n\n".encode()
                       + _LEGACY_POINTER + b"\n")
ANTIGRAVITY_RULE = (b"---\ntrigger: always_on\n---\n\n" + POINTER + b"\n")
MANAGED_RULES = {".cursor/rules/aispa.mdc": (CURSOR_RULE, _LEGACY_CURSOR_RULE),
                 ".agents/rules/aispa.md": (ANTIGRAVITY_RULE,)}
VIEWS = ("INDEX.md", "brief.md", "spec.md", "plan.md", "decisions.md", "progress.md",
         "memory.md", "requirements.json", "project-study.md", "carryover.md", "idea.md", "knowledge.md", "documents.yaml", "source-map.json")
VIEW_SUMMARIES = {
    "INDEX.md": "Навигация по контексту, правила чтения и карта существующих файлов проекта.",
    "brief.md": "Точный текст сохранённых брифов владельца и ссылки на неизменные оригиналы.",
    "spec.md": "Текущая спецификация и ссылка на её неизменный источник, либо отметка об отсутствии.",
    "plan.md": "Текущие задачи, зависимости, зоны, критерии и ссылка на исходный план.",
    "decisions.md": "Записанные решения агента и владельца, включая изменения объёма требований.",
    "progress.md": "Текущие статусы, блокеры, следующие действия, роли, участники и попытки задач.",
    "memory.md": "Краткая ориентация в текущем прогоне и ссылки на полные источники и остатки.",
    "project-study.md": "Сохранённое изучение проекта, источники исследования и ограничения его свежести.",
    "carryover.md": "Происхождение истории, решений и незакрытых обязательств прошлых прогонов.",
    "idea.md": "Разбор идеи: пользователь, проблема, результат, альтернативы, предположения и сценарии отказа.",
    "knowledge.md": "Маршрут к проверенным командам, ловушкам, причинам решений и словарю; адресное чтение по текущим исходникам.",
}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "coverage", "__pycache__",
             "tests", "test", "fixtures", "fixture", "history", "archive", "archives"}
MANIFESTS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Gemfile",
             "composer.json", "Package.swift", "requirements.txt", "Makefile", "Dockerfile"}
ENTRYPOINTS = {"main.py", "app.py", "manage.py", "__main__.py", "main.go", "main.rs",
               "main.ts", "main.tsx", "index.ts", "index.tsx", "index.js", "server.js",
               "server.ts", "app.ts", "app.tsx", "page.tsx"}
DOCUMENTS = {"readme.md", "architecture.md", "decisions.md", "plan.md", "spec.md",
             "index.md", "contributing.md", "data-dictionary.md"}


def _path(root, relative):
    """Reject every symlink component, including an in-root link and dangling links."""
    try:
        name = Path(relative_name(relative.as_posix() if isinstance(relative, Path) else relative))
    except ValueError as exc:
        raise Invalid("AISpa context: нужен относительный путь без переходов ..") from exc
    current = root
    if is_link(current):
        raise Invalid("AISpa context: ссылка вместо корня запрещена")
    for part in name.parts:
        current = current / part
        if is_link(current):
            raise Invalid("AISpa context: символическая ссылка запрещена: " + str(name))
        if current != root / name and current.exists() and not current.is_dir():
            raise Invalid("AISpa context: родительский каталог занят файлом: " + str(name))
    if not current.resolve().is_relative_to(root.resolve()):
        raise Invalid("AISpa context: путь выходит за корень проекта")
    return current


def _write(path, data):
    if path.exists() and path.read_bytes() == data:
        return
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".aispa-context-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _block(value):
    # A fence longer than any content fence keeps user text unambiguous.
    text = _json(value)
    fence = "`" * max(3, max((len(x) for x in text.split() if set(x) == {"`"}), default=0) + 1)
    return fence + "json\n" + text + fence + "\n"


PROJECT_ROLE_LABELS = {"interview": "Интервью", "intent": "Замысел проекта", "spec": "Спецификация",
                       "plan": "План разработки", "index": "Карта проекта"}
PROJECT_STATUS_LABELS = {"fresh": "Актуален", "changed": "Изменён — перечитать", "missing": "Файл не найден",
                         "ambiguous": "Несколько кандидатов — файл не выбран", "absent": "Не сопоставлен",
                         "unsafe": "Недопустимый источник", "unbound": "Найден — ещё не сопоставлен"}


def _table_text(value):
    text = str(value).replace("\\", "\\\\").replace("\r", " ").replace("\n", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in "|[]*_`":
        text = text.replace(char, "\\" + char)
    return text


def _project_link(store, row):
    """Link only selected, safe files that still exist; never synthesize a path."""
    name = row.get("path")
    if not isinstance(name, str) or row.get("status") not in ("fresh", "changed"):
        return None
    try:
        path = _path(store.root, name)
        if not path.is_file():
            return None
    except (Invalid, OSError):
        return None
    relative = Path(os.path.relpath(path, store.home / "context")).as_posix()
    return "[" + _table_text(name) + "](" + quote(relative, safe="/") + ")"


def _project_document_map(store, state):
    """Render navigation to adopted originals, without copying their content."""
    if state.get("policy", {}).get("projectDocuments") is not True:
        return "", "", None
    from . import project_documents
    document_map = project_documents.view(store, state)
    roles = document_map.get("roles", {})
    lines = ["## Исходные документы проекта", "",
             "Это обычные файлы проекта. Открывайте оригинал нужного документа по ссылке; "
             "он может выполнять несколько ролей. Интервью и замысел сохраняют решения между прогонами, "
             "но текущий объём работы определяет бриф прогона.", "",
             "| Роль | Файл проекта | Назначение | Когда читать | Состояние |",
             "| --- | --- | --- | --- | --- |"]
    for role, label in PROJECT_ROLE_LABELS.items():
        row = roles.get(role, {})
        status = PROJECT_STATUS_LABELS.get(row.get("status"), "Не определено")
        link = _project_link(store, row)
        path = link or _table_text(row.get("path") or "—")
        if row.get("status") in ("ambiguous", "unbound") or (row.get("status") == "unsafe" and not row.get("path")):
            candidates = row.get("candidates", [])
            names = [candidate.get("path", "") if isinstance(candidate, dict) else candidate
                     for candidate in candidates]
            names = [name for name in names if isinstance(name, str) and name]
            path = "Не выбран" + (": " + "; ".join(_table_text(name) for name in names[:5]) if names else "")
            if len(names) > 5:
                path += "; ещё " + str(len(names) - 5) + " — в project-documents"
        lines.append("| " + " | ".join((label, path, _table_text(row.get("purpose", "—")),
                                        _table_text(row.get("whenRead", "—")), status)) + " |")
    lines += ["", "Актуальность означает совпадение с сохранённой версией файла, а не приёмку результата. "
              "При изменении оригинала перечитайте его и обновите сопоставление. "
              "Если путь известен, открывайте его напрямую: весь каталог на каждой задаче не нужен.", ""]
    intent = roles.get("intent", {})
    status = PROJECT_STATUS_LABELS.get(intent.get("status"), "Не определено")
    pointer = "Замысел проекта: " + (_project_link(store, intent) or "[карта исходных документов](INDEX.md)") + "; " + status + ".\n"
    if len(pointer) > 256:
        pointer = "Замысел проекта: [карта исходных документов](INDEX.md); " + status + ".\n"
    return "\n".join(lines) + "\n", pointer, document_map


def inventory(root):
    """Inventory names only: never read configs, secrets, tests or historical copies."""
    if is_link(root):
        raise Invalid("AISpa context: ссылка вместо корня запрещена")
    from .project_documents import RECOGNIZED_DOCUMENTS
    document_names = DOCUMENTS | set(RECOGNIZED_DOCUMENTS)
    result = {"zones": [], "manifests": [], "entrypoints": [], "documents": [], "instructions": []}
    for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md"):
        path = root / name
        if not is_link(path) and path.is_file():
            result["instructions"].append(name)
    def walk(folder, depth=0):
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            if is_link(path) or path.name.startswith("."):
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_dir():
                if depth == 0:
                    result["zones"].append(rel)
                if depth < 2 and path.name.lower() not in SKIP_DIRS:
                    walk(path, depth + 1)
            elif path.is_file():
                if path.name in MANIFESTS:
                    result["manifests"].append(rel)
                if path.name in ENTRYPOINTS:
                    result["entrypoints"].append(rel)
                if path.name.casefold().replace("_", "-").replace(" ", "-") in document_names:
                    result["documents"].append(rel)
    walk(root)
    result["scope"] = "Names only, depth <= 3; hidden, generated, fixture and history trees excluded."
    return result


def _rules(existing, target):
    if BEGIN in existing or END in existing:
        if existing.count(BEGIN) != 1 or existing.count(END) != 1:
            raise Invalid("AISpa context: изменён управляемый блок в " + target + "; содержимое сохранено")
        begin = existing.index(BEGIN)
        marker_end = existing.find(END, begin)
        if marker_end < 0:
            raise Invalid("AISpa context: изменён управляемый блок в " + target + "; содержимое сохранено")
        end = marker_end + len(END)
        current = existing[begin:end]
        if current == POINTER:
            return existing
        if current == _LEGACY_POINTER:
            return existing[:begin] + POINTER + existing[end:]
        raise Invalid("AISpa context: изменён управляемый блок в " + target + "; содержимое сохранено")
    return existing + (b"\n\n" if existing and not existing.endswith(b"\n") else b"\n" if existing else b"") + POINTER + b"\n"


def _new_agents(root, found):
    def names(key):
        return ", ".join("`" + n + "`" for n in found[key]) or "не обнаружены при первичной инвентаризации"
    return ("# Работа над проектом " + root.name.replace("\n", " ") + "\n\n"
            "Назначение и критерий готовности определяются сохранённым брифом и требованиями AISpa.\n"
            "Сначала сверяйте задачу с действующим брифом, спецификацией и планом.\n\n"
            "Первичная карта фактически существовавших файлов:\n\n"
            "- Документы: " + names("documents") + ".\n"
            "- Манифесты: " + names("manifests") + ".\n"
            "- Точки входа: " + names("entrypoints") + ".\n"
            "- Каталоги: " + names("zones") + ".\n\n"
            "Стек и команды проверки не выводятся из названий: подтвердите их по коду и документации.\n"
            "Сохраняйте существующую структуру; создавайте прикладные каталоги только по плану.\n"
            "Незавершённое и блокеры фиксируются в журнале и `.aispa/context/progress.md`.\n").encode()


def initialize(store, state, runtime):
    """Inventory, safely link runtime instructions, then derive local context.

    Returns context info and sets state['projectContext']; does not append events.
    Repeated calls preserve all bytes when no changes are needed.
    """
    if runtime not in RUNTIMES:
        raise Invalid("AISpa context: runtime должен быть одним из " + ", ".join(RUNTIMES))
    previous = (state.get("projectContext", {}).get("runtime"), state.get("runtime"))
    if any(value and value != runtime for value in previous):
        raise Invalid("AISpa context: смена среды существующего прогона требует явной миграции")
    found = inventory(store.root)  # Must precede any writes.
    targets = ["AGENTS.md"]
    if runtime == "codex" and ((store.root / "AGENTS.override.md").exists()
                               or is_link(store.root / "AGENTS.override.md")):
        targets.append("AGENTS.override.md")
    if runtime == "claude-code":
        targets.append("CLAUDE.md")
    if runtime == "cursor":
        targets.append(".cursor/rules/aispa.mdc")
    if runtime in ("gemini", "antigravity"):
        targets.append("GEMINI.md")
    if runtime == "antigravity":
        targets.append(".agents/rules/aispa.md")
    edits = []
    for target in targets:
        path = _path(store.root, target)
        if path.exists() and not path.is_file():
            raise Invalid("AISpa context: файл правил занят каталогом: " + target)
        before = path.read_bytes() if path.exists() else None
        if target in MANAGED_RULES:
            accepted = MANAGED_RULES[target]
            if before is not None and before not in accepted:
                raise Invalid("AISpa context: чужой или изменённый " + target + "; содержимое сохранено")
            after = accepted[0]
        else:
            base = before if before is not None else _new_agents(store.root, found) if target == "AGENTS.md" else b""
            after = _rules(base, target)
        backup = None
        if before is not None and before != after:
            backup = _path(store.root, ".aispa/backups/" + target + "." + hashlib.sha256(before).hexdigest() + ".bak")
            if backup.exists() and backup.read_bytes() != before:
                raise Invalid("AISpa context: конфликт резервной копии " + target)
        edits.append((path, before, after, backup))
    # Preflight hidden paths too; do not leave rule edits on an unsafe context path.
    for name in VIEWS:
        path = _path(store.root, ".aispa/context/" + name)
        if path.exists() and not path.is_file():
            raise Invalid("AISpa context: файл контекста занят каталогом: " + name)
    for path, before, after, backup in edits:
        if backup is not None:
            _write(backup, before)
        if before != after:
            _write(path, after)
    state["projectContext"] = {"schema": 1, "runtime": runtime, "initialized": True,
                               "instructionFiles": targets}
    result = refresh(store, state)
    result.update(instructionFiles=targets,
                  changedInstructionFiles=[path.relative_to(store.root).as_posix()
                                           for path, before, after, _ in edits if before != after])
    return result


def _journal(store, state):
    if store.run is None:
        raise Invalid("AISpa context: нет активного прогона")
    try:
        relative = store.run.relative_to(store.root)
    except ValueError as exc:
        raise Invalid("AISpa context: прогон вне проекта") from exc
    path = _path(store.root, relative / "events.jsonl")
    try:
        journal = store.read_journal()
        row = journal["last"]
        if journal["count"] != row["sequence"] or row["state"]["run"] != state["run"]:
            raise Invalid("AISpa context: журнал другого прогона")
        return {"file": path.relative_to(store.root).as_posix(), "sha256": journal["sha256"],
                "sequence": row["sequence"], "event": row["event"], "eventDigest": row["hash"],
                "recordedStateDigest": digest(row["state"])}
    except (KeyError, TypeError) as exc:
        raise Invalid("AISpa context: повреждён журнал") from exc


def _source(store, artifact, sources, kind, read=False):
    name = artifact.get("file", "")
    if any(part.startswith(".env") or part.lower().endswith((".pem", ".key")) for part in Path(name).parts):
        raise Invalid("AISpa context: секретный файл не может быть источником контекста")
    # Validate relative name independently before joining it with the run path.
    path = _path(store.run, name)
    path = _path(store.root, path.relative_to(store.root))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
        raise Invalid("AISpa context: изменён неизменный источник " + name)
    relative = path.relative_to(store.root).as_posix()
    sources.append({"kind": kind, "file": relative, "sha256": artifact["sha256"]})
    relative_link = Path(os.path.relpath(path, store.home / "context")).as_posix()
    link = "[" + name.replace("]", "\\]") + "](" + quote(relative_link, safe="/") + ")"
    return link, store.text(artifact) if read else None


def _next(state):
    if state.get("status") != "running":
        return ["Проверить причину остановки и записать разрешённое продолжение."]
    if not state.get("briefs"):
        return ["Сохранить исходный бриф владельца."]
    if not state.get("sealed"):
        return ["Сверить требования с брифом и зафиксировать обязательное ядро."]
    if not state.get("spec"):
        if state.get("policy", {}).get("discovery") and not state.get("discovery"):
            return ["Изучить проект через discover и сохранить наблюдения по источникам: discover --file."]
        return ["Подготовить спецификацию по действующему брифу."]
    if not state.get("tasks"):
        return ["Проверить покрытие брифа и записать план."]
    pending = [t for t in state["tasks"] if t.get("status") in ("pending", "running")]
    if pending:
        return [str(t.get("id")) + ": " + str(t.get("phase", t.get("status"))) for t in pending]
    if any(t.get("status") == "blocked" for t in state["tasks"]):
        return ["Сохранить блокеры; продолжать доступные независимые задачи."]
    if state.get("policy", {}).get("deliveryDocs") and not state.get("handoff"):
        return ["Подготовить отслеживаемые документы передачи проекта, проверить команды и записать handoff до чистого клона."]
    return ["Провести актуальные сверки волн и итоговую приёмку; готовность определяет движок."]


def refresh(store, state):
    """Refresh hidden derived views only; caller holds the lock and persists state."""
    runtime = state.get("projectContext", {}).get("runtime", state.get("runtime"))
    if runtime not in RUNTIMES:
        raise Invalid("AISpa context: сначала initialize с явной средой исполнения")
    paths = {name: _path(store.root, ".aispa/context/" + name) for name in VIEWS}
    journal = _journal(store, state)
    sources = []
    lead = "Производное представление; канон — журнал и неизменные источники из [source-map.json](source-map.json).\n\n"
    brief = "# Бриф\n\n" + lead
    for item in state.get("briefs", []):
        link, text = _source(store, item, sources, "brief", read=True)
        brief += "## " + str(item.get("id", "Источник")) + "\n\nИсточник: " + link + "\n\n" + text + "\n\n"
    spec = "# Спецификация\n\n" + lead
    if state.get("spec"):
        link, text = _source(store, state["spec"], sources, "spec", read=True)
        spec += "Источник: " + link + "\n\n" + text
    else:
        spec += "Спецификация ещё не записана.\n"
    plan_report = state.get("gates", {}).get("plan", {}).get("report")
    plan = "# Текущий план\n\n" + lead
    if plan_report:
        link, _ = _source(store, plan_report, sources, "plan")
        plan += "Исходный план: " + link + "\n\n"
    plan += _block(state.get("tasks", []))
    if state.get("planReview"):
        link, _ = _source(store, state["planReview"]["report"], sources, "plan-review")
        plan += "\nРазбор размера и сохранённые границы: " + link + "\n"
    decisions = {"agentDecisions": state.get("notes", []), "decisions": state.get("decisions", []),
                 "ownerScopeDecisions": [{"requirement": r["id"], "decision": r["decision"]}
                                         for r in state.get("requirements", []) if r.get("decision")]}
    blockers = [{"task": t.get("id"), "note": t.get("note")} for t in state.get("tasks", [])
                if t.get("status") == "blocked"]
    progress_data = {key: state.get(key) for key in ("stage", "status", "waiting", "remaining", "activity")}
    autonomy_contract = autonomy.view(state)
    if autonomy_contract is not None:
        progress_data["autonomy"] = autonomy_contract
    progress_data.update(runtime=runtime, blockers=blockers, nextSteps=_next(state),
                         tasks=state.get("tasks", []), actors=state.get("actors", {}),
                         taskHistory=state.get("taskHistory", {}),
                         projectIntegrationReview=state.get("projectIntegrationReview"),
                         projectIntegrationReviewHistory=state.get("projectIntegrationReviewHistory", []))
    progress = "# Прогресс и роли\n\n" + lead + _block(progress_data)
    for tid, attempts in state.get("taskHistory", {}).items():
        for attempt in attempts:
            for role, artifact in attempt.get("roles", {}).items():
                link, _ = _source(store, artifact, sources, "role")
                progress += "- " + str(tid) + " / " + str(attempt.get("id")) + " / " + str(role) + ": " + link + "\n"
    inherited = carryover.view(store, state)
    carry_text = "# Память между прогонами\n\n" + lead
    carry_text += "Историческая приёмка не подтверждает текущий код. Сохранённый остаток не добавляется в текущий объём молча.\n\n"
    if inherited.get("handoff"):
        link, _ = _source(store, inherited["handoff"], sources, "handoff")
        carry_text += "Полные источники и происхождение: " + link + "\n\n"
        for receipt in state["carryover"]["decisions"]:
            _source(store, receipt, sources, "carryover-decision")
    carry_text += _block({key: inherited[key] for key in ("summary", "items", "decisions", "runs")})
    study = "# Изучение проекта\n\n" + lead
    if state.get("discovery"):
        link, text = _source(store, state["discovery"], sources, "discovery", read=True)
        study += "Записанное исследование: " + link + "\n\n" + text + "\n"
        link, _ = _source(store, state["discovery"]["collection"], sources, "discovery-collection")
        study += "\nСобранные фрагменты и пропуски: " + link + "\n"
        study += "\nЭто снимок изучения. Перед новым планом проверьте свежесть командой discovery.\n"
    else:
        study += "Исследование ещё не записано; инвентаризация имён его не заменяет.\n"
    idea_text = "# Идея и сценарии продукта\n\n" + lead
    if state.get("ideaReview"):
        link, _ = _source(store, state["ideaReview"], sources, "idea-review")
        idea_text += "Полный разбор: " + link + "\n\n"
    idea_text += _block(ideas.view(store, state))
    learned = state.get("knowledge", {})
    knowledge_text = ("# Практические знания проекта\n\n" + lead +
        "Читать адресно: `knowledge --path <путь>` или `knowledge --kind command|trap|decision|glossary`.\n"
        "Команда проверяет свежесть источников. Историческая проверка команды не означает текущий PASS.\n"
        "Фактические исполнения: `commands --purpose install|run|check|test --limit 8`; "
        "точный argv и доказательство — `commands --receipt <sha256>`. Не загружайте весь журнал.\n"
        "Для нового автора: `context --kind knowledge --write`; читать фактический код, а не план.\n\n")
    if state.get("handoff"):
        link, _ = _source(store, state["handoff"]["artifact"], sources, "project-handoff")
        knowledge_text += "Передача обычных Git-документов проверена записью: " + link + "\n"
    if learned.get("inherited"):
        link, _ = _source(store, learned["inherited"], sources, "knowledge-handoff")
        knowledge_text += "Переданные знания с историей: " + link + "\n"
    for item in learned.get("records", []):
        link, _ = _source(store, item, sources, "knowledge-record")
        knowledge_text += "Запись знаний: " + link + "\n"
    if not learned.get("records") and not learned.get("inherited"):
        knowledge_text += "Проверенные знания ещё не записаны.\n"
    found = inventory(store.root)
    project_map, intent_pointer, document_map = _project_document_map(store, state)
    map_lead = ("Эта страница — навигация. Канон проекта — сопоставленные исходные документы; "
                "канон текущего прогона — журнал и неизменные источники из [source-map.json](source-map.json).\n\n"
                if document_map is not None else lead)
    generated_intro = ("## Представления текущего прогона\n\n"
                       "Эти файлы создаёт движок AISpa. Они доступны только для чтения; не редактируйте их вручную.\n\n"
                       if document_map is not None else "")
    index = ("# Контекст проекта AISpa\n\n" + map_lead + project_map + generated_intro +
             "Читайте [краткую память](memory.md), затем нужный документ:\n\n"
             "- [Бриф](brief.md), [требования](requirements.json), [спецификация](spec.md).\n"
             "- [План](plan.md), [решения](decisions.md), [прогресс и роли](progress.md).\n"
             "- [Изучение проекта](project-study.md), [память между прогонами](carryover.md).\n"
             "- [Разбор идеи](idea.md), [практические знания](knowledge.md); читать по текущему этапу.\n"
             "- [Краткий каталог документов](documents.yaml): назначение, ссылки и свежесть.\n"
             "- [Происхождение и свежесть](source-map.json).\n\n"
             "YAML-заголовки и каталог помогают выбрать нужный файл; они не заменяют его чтение.\n"
             "Перед использованием каталога проверьте текущие хеши командой documents; устаревшее резюме не используйте.\n"
             "После изменения источника обновите его описание через document или documents --refresh.\n"
             "Бриф, требования и обязательные проверки читайте полностью по их точным источникам.\n\n"
             "Правки этих файлов не меняют канон и будут заменены при обновлении.\n"
             "Не объявляйте проверки пройденными по статусам этого представления.\n\n"
             "## Фактическая карта проекта\n\n" + _block(found))
    def short(value, limit=300):
        text = str(value).replace("\n", " ")
        return text if len(text) <= limit else text[:limit] + "… (полностью в связанных документах)"
    memory = ("# Краткая память AISpa\n\n" + lead +
              "Проект: " + short(state.get("title")) + "\n"
              "Среда: " + runtime + "; этап: " + short(state.get("stage")) +
              "; статус: " + short(state.get("status")) + ".\n\n"
              + ("Общение с владельцем:\n" + _block(autonomy_contract) + "\n" if autonomy_contract is not None else "") +
              "Полные данные: [бриф](brief.md), [спецификация](spec.md), [требования](requirements.json), "
              "[план](plan.md), [решения](decisions.md), [прогресс и роли](progress.md).\n"
              "Сжатие памяти не удаляет канонические записи.\n" + intent_pointer + "\n"
              "Прошлые прогоны: [история и остатки](carryover.md); незакрытых пунктов: " + str(inherited["summary"]["outstanding"]) + ".\n"
              "Архитектура, команды и неизвестное: [изучение проекта](project-study.md).\n\n"
              "Разбор идеи: [idea.md](idea.md). Знания о коде: [knowledge.md](knowledge.md), читать адресно.\n"
              "Для поиска неизвестных путей: [каталог документов](documents.yaml); при известных путях он не нужен.\n"
              "Каталог и YAML — навигация, а не доказательство выполнения требований.\n\n"
              "Требований: " + str(len(state.get("requirements", []))) + "; задач: " + str(len(state.get("tasks", []))) +
              "; решений агента: " + str(len(state.get("notes", []))) + ".\n\n"
              "Следующие действия (до 8; весь список в progress.md):\n" +
              "\n".join("- " + short(x) for x in _next(state)[:8]) + "\n\n"
              "Блокеры (до 5; все в progress.md):\n" +
              ("\n".join("- " + short(x) for x in blockers[:5]) or "- Нет записанных блокеров задач.") +
              "\nОжидание: " + short(state.get("waiting")) + "\n")
    content = {"INDEX.md": index, "brief.md": brief, "spec.md": spec, "plan.md": plan,
               "decisions.md": "# Решения\n\n" + lead + _block(decisions), "progress.md": progress,
               "memory.md": memory, "requirements.json": _json({"run": state["run"], "requirements": state.get("requirements", [])})}
    content.update({"project-study.md": study, "carryover.md": carry_text, "idea.md": idea_text, "knowledge.md": knowledge_text})
    for name, summary in VIEW_SUMMARIES.items():
        content[name] = docmeta.render(content[name], kind="derived-context", summary=summary)
    if len(content["memory.md"]) > MEMORY_LIMIT:
        raise Invalid("AISpa context: краткая память вместе с YAML превысила установленный предел")
    documents = docmeta.catalog(store, state)
    # These immutable sources have already been checked by _source. Do not
    # mutate state here: refresh derives views after the event was committed.
    canonical = [{"path": row["file"], "kind": row["kind"], "sourceSha256": row["sha256"],
                  "status": "fresh", "immutable": True}
                 for row in sources if row["kind"] in ("brief", "spec")]
    documents.update(canonicalSources=canonical[:32], canonicalSourceCount=len(canonical),
                     canonicalSourcesOmitted=max(0, len(canonical) - 32), derived=True,
                     readingRule="Navigation only. Check live freshness with documents; read original briefs and required evidence in full.")
    # JSON is a YAML 1.2 subset; no parser dependency is needed for this view.
    content["documents.yaml"] = _json(documents)
    source_map = {"schema": 1, "derived": True, "run": state["run"], "runtime": runtime,
                  "stateDigest": digest(state), "journal": journal, "sources": sources,
                  "inventory": found, "projectDocuments": document_map, "views": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in content.items()}}
    for name, text in content.items():
        _write(paths[name], text.encode())
    # Last write is the commit marker: interrupted refreshes are detectably stale.
    _write(paths["source-map.json"], _json(source_map).encode())
    return info(store, state)


def info(store, state):
    """Return provenance and freshness without changing files or the journal."""
    path = _path(store.root, ".aispa/context/source-map.json")
    if not path.exists():
        return {"initialized": False, "fresh": False, "path": ".aispa/context/INDEX.md"}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        journal = _journal(store, state)
        fresh = (saved["run"] == state["run"] and saved["stateDigest"] == digest(state)
                 and saved["journal"] == journal and journal["recordedStateDigest"] == digest(state))
        if state.get("policy", {}).get("projectDocuments") is True:
            from . import project_documents
            fresh = fresh and saved.get("projectDocuments") == project_documents.view(store, state)
        for name in VIEWS[:-1]:
            view = _path(store.root, ".aispa/context/" + name)
            fresh = fresh and view.is_file() and hashlib.sha256(view.read_bytes()).hexdigest() == saved["views"].get(name)
        for source in saved["sources"]:
            source_path = _path(store.root, source["file"])
            if not source_path.is_relative_to(store.run) or any(part.startswith(".env") for part in source_path.parts):
                raise Invalid("AISpa context: недопустимый источник в source-map.json")
            fresh = fresh and source_path.is_file() and hashlib.sha256(source_path.read_bytes()).hexdigest() == source["sha256"]
        return {"initialized": True, "fresh": bool(fresh), "runtime": saved["runtime"],
                "path": ".aispa/context/INDEX.md", "run": saved["run"],
                "stateDigest": saved["stateDigest"], "eventDigest": saved["journal"]["eventDigest"]}
    except (OSError, KeyError, ValueError) as exc:
        raise Invalid("AISpa context: не удалось проверить происхождение представлений") from exc
