"""Bounded, source-bound project study. The host owns locking and journal writes.

Collection is deterministic evidence, not an architecture inference. A native agent
supplies the observations; validation checks provenance and explicit uncertainty,
not the semantic truth of arbitrary prose. No collected command is executed.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

from .store import Invalid, digest, canonical_zone, NATIVE_RULE_INPUTS, native_rule_input
from .portable import is_link, match_name, relative_name
from . import docmeta, scale
from . import project_documents


SCHEMA = 1
AREAS = ("architecture", "data", "entrypoints", "dependencies", "commands", "conventions", "risks")
MAX_SOURCES = 48
MAX_FILE_BYTES = 262144
MAX_EXCERPT_CHARS = 6000
MAX_TOTAL_CHARS = 96000
MAX_OMITTED_DETAILS = 80
MAX_COMMANDS = 40
MAX_REPORT_CHARS = 48000
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "coverage", "__pycache__", "target",
             "archive", "archives", "history", "backup", "backups", "generated", "fixtures",
             "fixture", "testdata", "data", "datasets", "uploads", "media", "logs", "tmp", "temp",
             "students", "customers", "users", "private", "secrets", "credentials"}
MANIFESTS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "composer.json",
             "requirements.txt", "Makefile", "pytest.ini", "tox.ini", "setup.cfg"}
DOCUMENTS = {"readme.md", "architecture.md", "decisions.md", "contributing.md", "index.md",
             "data-dictionary.md", "glossary.md", "agents.md", "agents.override.md", "claude.md",
             "gemini.md", "testing.md", "conventions.md", "design.md"}
DOCUMENTS |= project_documents.RECOGNIZED_DOCUMENTS
SAFE_HIDDEN = {".editorconfig"} | NATIVE_RULE_INPUTS
CODE_NAMES = {"main", "app", "index", "server", "cli", "api", "routes", "router", "models",
              "schema", "database", "repository", "service", "store", "__main__", "__init__"}
CODE_EXTENSIONS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php"}
SECRET_NAME = re.compile(r"(?:^|[._-])(?:env|secret|secrets|credential|credentials|password|passwords|token|tokens|key|keys|private|customer|customers|student|students|user|users)(?:$|[._-])", re.I)
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b|"
    r"(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*[\"']?[^\s\"'$<{][^\s\"']{5,}", re.I)
COMMAND_START = re.compile(r"^(?:python(?:3(?:\.\d+)?)?|node|npm|pnpm|yarn|bun|pytest|uv|ruff|mypy|make|cargo|go|dotnet|swift|bundle|rake|php|composer|npx|tox|coverage)(?:\s|$)")


def _require(condition, text):
    if not condition:
        raise Invalid("AISpa discovery: " + text)


def _safe_path(root, name):
    try:
        path = Path(relative_name(name))
    except ValueError as exc:
        raise Invalid("AISpa discovery: нужен относительный путь источника") from exc
    result = root
    _require(not is_link(result), "ссылка вместо корня запрещена")
    for part in path.parts:
        result = result / part
        _require(not is_link(result), "символические ссылки запрещены: " + name)
    _require(result.resolve().is_relative_to(root.resolve()), "источник вне проекта")
    return result


def _kind(name, *, explicit=False):
    path = Path(name)
    lower = path.name.lower()
    if any(part.lower() in SKIP_DIRS for part in path.parts[:-1]):
        return None
    if any(part.startswith(".") for part in path.parts) and name not in SAFE_HIDDEN and not native_rule_input(name):
        return None
    if any(SECRET_NAME.search(part) for part in path.parts) or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".db", ".csv"}:
        return None
    if explicit and (path.suffix.lower() == ".md" or path.suffix.lower() in CODE_EXTENSIONS):
        return "document" if path.suffix.lower() == ".md" else "code"
    if path.name in MANIFESTS:
        return "manifest"
    if name in SAFE_HIDDEN or native_rule_input(name):
        return "configuration"
    if lower in DOCUMENTS or project_documents._role(name) or (path.parts[0].lower() in {"docs", "doc", "adr"} and path.suffix.lower() == ".md"):
        return "document"
    if "tests" not in path.parts and "test" not in path.parts and path.stem.lower() in CODE_NAMES and path.suffix.lower() in CODE_EXTENSIONS:
        return "code"
    return None


def _commands(name, text):
    """Extract literal commands only; never derive a runner from a package name."""
    commands = []
    def add(value, origin):
        if isinstance(value, str) and value.strip() and len(value) <= 2000 and not value.rstrip().endswith("\\"):
            row = {"command": value.strip(), "origin": origin}
            if row not in commands:
                commands.append(row)
    filename = Path(name).name
    if filename == "package.json":
        try:
            value = json.loads(text)
            for key, command in sorted(value.get("scripts", {}).items()):
                add(command, "scripts." + key)
        except (ValueError, AttributeError, TypeError):
            pass
    elif filename == "pyproject.toml":
        try:
            value = tomllib.loads(text)
            # project.scripts are import targets, not shell test commands.
            envs = value.get("tool", {}).get("hatch", {}).get("envs", {})
            for env, config in sorted(envs.items()):
                for key, command in sorted(config.get("scripts", {}).items()):
                    if isinstance(command, list):
                        for item in command:
                            add(item, "tool.hatch.envs." + env + ".scripts." + key)
                    else:
                        add(command, "tool.hatch.envs." + env + ".scripts." + key)
        except (ValueError, AttributeError, TypeError):
            pass
    elif filename == "Makefile":
        for line in text.splitlines():
            if line.startswith("\t"):
                add(line.strip().lstrip("@-+"), "recipe")
    elif filename == "tox.ini":
        in_commands = False
        for line in text.splitlines():
            if re.match(r"\s*commands\s*=", line):
                in_commands = True
                add(line.split("=", 1)[1], "commands")
            elif in_commands and line[:1].isspace():
                add(line.strip(), "commands")
            elif line.strip():
                in_commands = False
    if filename.lower().endswith(".md"):
        fenced = False
        shell_fence = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                language = stripped[3:].strip().lower()
                shell_fence = not fenced and language in {"sh", "shell", "bash", "zsh", "console"}
                fenced = not fenced
                continue
            candidate = stripped[2:] if stripped.startswith("$ ") else stripped
            if shell_fence and COMMAND_START.match(candidate):
                add(candidate, "document-shell")
            for inline in re.findall(r"`([^`\n]+)`", line):
                if COMMAND_START.match(inline):
                    add(inline, "document-inline")
    return commands


def _names(store):
    top = Path(os.fsdecode(store.git("rev-parse", "--show-toplevel")).strip()).resolve()
    _require(top == store.root, "root должен быть корнем Git-репозитория")
    names = set(store.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    ignored = set(store.git("ls-files", "--cached", "--ignored", "--exclude-standard", "-z").split(b"\0"))
    return sorted(os.fsdecode(raw) for raw in names - ignored if raw)


def _included_paths(store, paths, names):
    _require(isinstance(paths, list) and all(isinstance(name, str) for name in paths), "include должен быть списком относительных файлов")
    result = []
    for name in paths:
        try:
            canonical = relative_name(name)
        except ValueError as exc:
            raise Invalid("AISpa discovery: небезопасный include") from exc
        parts = Path(canonical).parts
        _require(all(not part.startswith(".") for part in parts),
                 "небезопасный include: нужен обычный относительный файл")
        _require(_kind(canonical, explicit=True) is not None, "небезопасный или неподдержанный include: " + canonical)
        _require(canonical in names, "include отсутствует или исключён Git: " + canonical)
        _require(_safe_path(store.root, canonical).is_file(), "include отсутствует или не является файлом: " + canonical)
        result.append(canonical)
    return sorted(set(result))


def _retain(budget, value, *, path):
    """Account retained page values before keeping another source in memory."""
    if budget is None:
        return
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False).iterencode(value):
        size += len(part.encode("utf-8"))
        budget.check_memory(size * 4, path=path)
    budget.base_memory += size * 4


def _scan(store, paths=None, *, cursor=None, scalable=False, index=False):
    budget = scale.Budget() if scalable else None
    max_sources = scale._positive("AISPA_DISCOVERY_PAGE_SOURCES", MAX_SOURCES) if scalable else MAX_SOURCES
    max_file_bytes = budget.max_bytes if scalable else MAX_FILE_BYTES
    max_excerpt = scale._positive("AISPA_DISCOVERY_EXCERPT_CHARS", MAX_TOTAL_CHARS) if scalable else MAX_EXCERPT_CHARS
    max_total = scale._positive("AISPA_DISCOVERY_PAGE_CHARS", MAX_TOTAL_CHARS) if scalable else MAX_TOTAL_CHARS
    position, character, binding = 0, 0, None
    if cursor is not None:
        _require(scalable and isinstance(cursor, str), "cursor requires scalableInputs")
        try:
            binding, left, right = cursor.split(":")
            position, character = int(left), int(right)
            _require(position >= 0 and character >= 0 and str(position) == left and str(character) == right,
                     "invalid discovery cursor")
        except (ValueError, AttributeError) as error:
            raise Invalid("AISpa discovery: invalid cursor") from error
    next_position = None
    next_character = 0
    sources, omitted, fingerprints = [], [], []
    counts, used = Counter(), 0
    names = _names(store)
    included = _included_paths(store, [] if paths is None else paths, names)
    candidates = []
    groups = {kind: [] for kind in ("document", "manifest", "code", "configuration")}
    for name in names:
        kind = _kind(name, explicit=name in included)
        if kind:
            groups[kind].append((name, kind))
    for items in groups.values():
        items.sort(key=lambda item: (item[0] not in included, len(Path(item[0]).parts), item[0]))
    # Round-robin prevents a long docs tree from consuming every code/manifest slot.
    for index in range(max((len(items) for items in groups.values()), default=0)):
        for items in groups.values():
            if index < len(items):
                candidates.append(items[index])
    if scalable:
        # All requested safe includes precede opportunistic navigation sources.
        candidates.sort(key=lambda item: item[0] not in included)
        _retain(budget, {"names": names, "included": included, "candidates": candidates}, path="discovery selection")
    for candidate_index, (name, kind) in enumerate(candidates):
        # The previous full source is transient; only its page excerpt remains.
        raw = text = None
        if budget:
            budget.check(0, path=name)
        item = {"path": name, "kind": kind}
        try:
            path = _safe_path(store.root, name)
            if not path.is_file():
                item["status"] = "missing"
            else:
                stat = path.stat()
                item["bytes"] = stat.st_size
                if not scalable and stat.st_size > max_file_bytes:
                    item.update(status="oversized", mtimeNs=stat.st_mtime_ns)
                else:
                    # Size cap also applies if a writer grows a file after stat.
                    if scalable:
                        raw = scale._read(path, budget)
                    else:
                        with path.open("rb") as stream:
                            raw = stream.read(max_file_bytes + 1)
                    if len(raw) > max_file_bytes:
                        item.update(status="oversized", mtimeNs=stat.st_mtime_ns)
                    else:
                        item["sha256"] = hashlib.sha256(raw).hexdigest()
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeError:
                            item["status"] = "non-utf8"
                        else:
                            if "\0" in text:
                                item["status"] = "binary"
                            elif SECRET_CONTENT.search(text):
                                item["status"] = "sensitive-content"
                            else:
                                item["status"] = "safe"
        except (OSError, Invalid) as error:
            if isinstance(error, Invalid) and str(error).startswith("RESOURCE_LIMIT "):
                raise
            item["status"] = "unavailable-or-symlink"
        _require(name not in included or item["status"] in {"safe", "oversized"},
                 "include нельзя безопасно прочитать: " + name + " (" + item["status"] + ")")
        _retain(budget, item, path="discovery fingerprints:" + name)
        fingerprints.append(item)
        reason = item["status"] if item["status"] != "safe" else (
            "previous-page" if candidate_index < position else "source-limit" if len(sources) >= max_sources else "excerpt-budget" if used >= max_total or next_position is not None else None)
        if reason:
            if scalable and item["status"] == "safe" and candidate_index >= position and next_position is None:
                next_position = candidate_index
            counts[reason] += 1
            if len(omitted) < MAX_OMITTED_DETAILS:
                omitted.append({"path": name, "reason": reason})
            continue
        offset = character if scalable and candidate_index == position else 0
        _require(offset < len(text) or not text, "discovery cursor offset outside source")
        limit = min(max_excerpt, max_total - used)
        excerpt = text[offset:offset + limit]
        if scalable and offset + len(excerpt) < len(text):
            next_position, next_character = candidate_index, offset + len(excerpt)
        used += len(excerpt)
        commands = _commands(name, text)
        source = {k: v for k, v in item.items() if k != "status"}
        source.update(excerpt=excerpt, omittedChars=max(0, len(text) - len(excerpt)),
                      **({"excerptStart": offset, "excerptEnd": offset + len(excerpt), "totalChars": len(text)} if scalable else {}),
                      commands=commands[:MAX_COMMANDS], omittedCommands=max(0, len(commands) - MAX_COMMANDS))
        _retain(budget, source, path="discovery page:" + name)
        sources.append(source)
    limits = {"sources": max_sources, "fileBytes": max_file_bytes, "excerptChars": max_excerpt,
              "totalExcerptChars": max_total, "omittedDetails": MAX_OMITTED_DETAILS, "commandsPerSource": MAX_COMMANDS}
    scan_digest = digest(fingerprints)
    pagination = {}
    if scalable:
        page_digest = digest({"scan": scan_digest, "included": included, "limits": limits})
        _require(binding is None or binding == page_digest, "stale or foreign discovery cursor")
        _require(position < len(candidates) or position == 0, "discovery cursor outside selection")
        pagination = {"pagination": {"total": len(candidates), "omitted": len(candidates) - next_position if next_position is not None else 0,
                       "cursor": f"{page_digest}:{next_position}:{next_character}" if next_position is not None else None,
                       "digest": page_digest, "start": position, "character": character}}
    # Focused runs keep a short digest per scanned file, so that later checks can leave out the files
    # the plan's tasks write themselves. Not part of any basis.
    fingerprint_index = {"fingerprintIndex": {row["path"]: digest(row)[:16] for row in fingerprints}} if index else {}
    return {**pagination, **fingerprint_index, "sources": sources, "scanDigest": scan_digest, "limits": limits, "includedPaths": included,
            "omitted": {"counts": dict(sorted(counts.items())), "details": omitted,
                        "unlisted": sum(counts.values()) - len(omitted),
                        "unselectedPaths": len(names) - len(candidates)},
            "scope": "Git tracked and nonignored files; allowlisted docs, manifests, configuration and code names plus explicit safe includes. "
                     "Hidden, secret, data, vendor, generated, archive and symlink paths excluded. "
                     "Omitted candidate content is fingerprinted within file-size limits; oversized files use size and mtime.",
            "candidateCount": len(candidates)}


def _artifact(store, value):
    _safe_path(store.run, "discovery")
    return store.artifact("discovery", (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(), ".json")


def _read(store, artifact):
    _require(isinstance(artifact, dict), "нет сохранённого артефакта")
    name = artifact.get("file")
    _require(isinstance(name, str) and name.startswith("discovery/") and name.endswith(".json"), "недопустимый артефакт")
    path = _safe_path(store.run, name)
    raw = scale.read_file(path, expected_sha256=artifact.get("sha256"))
    return json.loads(raw)


def _collection(store, state):
    ref = state.get("discoveryCollection")
    saved = _read(store, ref)
    _require(saved.get("schema") == SCHEMA and saved.get("run") == state["run"], "чужой снимок исследования")
    _require(saved.get("basis") == ref.get("basis") == digest({"run": state["run"], "scan": saved.get("scanDigest"), "limits": saved.get("limits"), "includedPaths": saved.get("includedPaths", []), **({"pagination": saved["pagination"]} if "pagination" in saved else {})}),
             "неверная основа исследования")
    return saved


def _focused(state):
    return state.get("policy", {}).get("focusedReview") is True


def _plan_zones(state):
    """Canonical write zones of the tasks of the current plan."""
    zones = []
    for task in state.get("tasks", []):
        for zone in task.get("zones", []):
            try:
                zones.append(canonical_zone(zone))
            except Invalid:
                continue
    return zones


def _outside(index, zones):
    return {path: row for path, row in index.items()
            if not any(zone in ("", ".") or match_name(path) == zone or match_name(path).startswith(zone + "/")
                       for zone in zones)}


def _inputs_unchanged(state, saved, scan):
    """The study inputs are the same. Focused runs leave out the files the plan's tasks write: that
    work is the run's own and is covered by the proofs of those tasks, not by a new study."""
    if saved["limits"] != scan["limits"]:
        return False
    if saved["scanDigest"] == scan["scanDigest"]:
        return True
    old, new = saved.get("fingerprintIndex"), scan.get("fingerprintIndex")
    zones = _plan_zones(state) if _focused(state) else []
    if not zones or not isinstance(old, dict) or not isinstance(new, dict):
        return False
    return _outside(old, zones) == _outside(new, zones)


def collect(store, state, paths=None, *, metadata_receipt=None, cursor=None, reuse_receipt=None):
    """Save/reuse bounded evidence; paths=None retains prior explicit includes.

    An explicit [] clears includes. Mutate state only, never execute or append.
    metadata_receipt, when supplied, receives routing-index registration results;
    those results do not change the saved discovery evidence or its basis.
    reuse_receipt, when supplied, gets "reused": whether the saved collection was returned because the
    inputs did not change; the caller then has nothing new to record (no event).
    """
    _require(metadata_receipt is None or isinstance(metadata_receipt, dict), "metadata_receipt должен быть объектом")
    _require(reuse_receipt is None or isinstance(reuse_receipt, dict), "reuse_receipt должен быть объектом")
    if reuse_receipt is not None:
        reuse_receipt["reused"] = False
    def index_documents(sources):
        result = docmeta.collect_sources(store, state, sources)
        if metadata_receipt is not None:
            metadata_receipt.clear()
            metadata_receipt.update(result)
    if paths is None and state.get("discoveryCollection"):
        paths = _collection(store, state).get("includedPaths", [])
    if state.get("policy", {}).get("projectDocuments"):
        # Read bound analogues even when their filenames are not in the default
        # catalog. Missing/stale references remain visible in project-documents.
        mapped = project_documents.view(store, state)["roles"].values()
        paths = sorted(set(paths or []) | {row["path"] for row in mapped if row.get("status") in ("fresh", "changed")})
    scan = _scan(store, paths, cursor=cursor, scalable=scale.enabled(state), index=_focused(state))
    basis = digest({"run": state["run"], "scan": scan["scanDigest"], "limits": scan["limits"], "includedPaths": scan["includedPaths"], **({"pagination": scan["pagination"]} if "pagination" in scan else {})})
    if state.get("discoveryCollection"):
        try:
            previous = _collection(store, state)
            same_page = (previous.get("includedPaths") == scan["includedPaths"]
                         and {k: previous.get("pagination", {}).get(k) for k in ("start", "character")}
                         == {k: scan.get("pagination", {}).get(k) for k in ("start", "character")})
            if previous["basis"] == basis or same_page and _inputs_unchanged(state, previous, scan):
                index_documents(previous["sources"])
                if reuse_receipt is not None:
                    reuse_receipt["reused"] = True
                return previous
        except (OSError, ValueError, TypeError):
            pass
    saved = {"schema": SCHEMA, "run": state["run"], "basis": basis, **scan}
    scale.check_value(saved, state)
    state["discoveryCollection"] = {**_artifact(store, saved), "basis": basis}
    index_documents(saved["sources"])
    return saved


def _report(saved, report, state=None):
    if state is not None:
        scale.check_value(report, state)
    _require(isinstance(report, dict), "отчёт должен быть объектом")
    _require(state is not None and scale.enabled(state) or len(json.dumps(report, ensure_ascii=False)) <= MAX_REPORT_CHARS, "отчёт слишком большой; используйте ссылки")
    _require(report.get("basis") == saved["basis"], "отчёт относится к другому снимку")
    _require(isinstance(report.get("summary"), str) and report["summary"].strip(), "нужно краткое резюме исследования")
    observations, uncertainties = report.get("observations"), report.get("uncertainties")
    _require(isinstance(observations, list) and isinstance(uncertainties, list), "нужны observations и uncertainties")
    _require(observations or not saved["sources"], "существующие источники требуют хотя бы одного наблюдения")
    known = {item["path"]: item for item in saved["sources"]}
    covered = set()
    result = []
    for row in observations:
        _require(isinstance(row, dict) and row.get("area") in AREAS, "неизвестная область наблюдения")
        _require(isinstance(row.get("text"), str) and row["text"].strip(), "нет текста наблюдения")
        refs = row.get("sources")
        _require(isinstance(refs, list) and refs, "наблюдению нужны источники")
        clean_refs = []
        for ref in refs:
            _require(isinstance(ref, dict) and isinstance(ref.get("path"), str), "неверная ссылка на источник")
            source = known.get(ref["path"])
            _require(source is not None and ref.get("sha256") == source["sha256"], "источник отсутствует в сборе или изменён")
            if "excerpt" in ref:
                _require(isinstance(ref["excerpt"], str) and ref["excerpt"] and ref["excerpt"] in source["excerpt"], "цитата отсутствует в собранном фрагменте")
            clean_refs.append({key: ref[key] for key in ("path", "sha256", "excerpt") if key in ref})
        clean = {"area": row["area"], "text": row["text"], "sources": clean_refs}
        if row["area"] == "commands" or "command" in row:
            command = row.get("command")
            _require(isinstance(command, str) and any(command == candidate["command"] for ref in refs
                    for candidate in known[ref["path"]]["commands"]), "команда не подтверждена источником")
            clean["command"] = command
        result.append(clean)
        covered.add(row["area"])
    unknown = []
    for row in uncertainties:
        _require(isinstance(row, dict) and row.get("area") in AREAS and isinstance(row.get("text"), str)
                 and row["text"].strip(), "неопределённость должна назвать область и причину")
        unknown.append({"area": row["area"], "text": row["text"]})
        covered.add(row["area"])
    _require(set(AREAS) <= covered, "исследованы не все области: " + ", ".join(sorted(set(AREAS) - covered)))
    return {"basis": saved["basis"], "summary": report["summary"], "observations": result, "uncertainties": unknown}


def record(store, state, report):
    """Validate source-backed observations and store a reusable immutable report."""
    saved = _collection(store, state)
    current = _scan(store, saved.get("includedPaths", []), scalable=scale.enabled(state),
                    cursor=(f"{saved['pagination']['digest']}:{saved['pagination']['start']}:{saved['pagination']['character']}"
                            if saved.get("pagination", {}).get("start") or saved.get("pagination", {}).get("character") else None))
    _require(saved["scanDigest"] == current["scanDigest"] and saved["limits"] == current["limits"],
             "источники изменились; повторите discovery collect")
    normalized = _report(saved, report, state)
    value = {"schema": SCHEMA, "run": state["run"], "collection": state["discoveryCollection"], **normalized}
    old = state.get("discovery")
    if old:
        try:
            if _read(store, old) == value:
                return old
        except (OSError, ValueError, TypeError):
            pass
    snapshot = {**_artifact(store, value), "basis": saved["basis"], "collection": dict(state["discoveryCollection"])}
    state["discovery"] = snapshot
    return snapshot


def valid(store, state, *, check_sources=True):
    """Check integrity and optionally live freshness; build edits may stale a plan's snapshot."""
    try:
        ref = state.get("discovery")
        report = _read(store, ref)
        _require(report.get("schema") == SCHEMA and report.get("run") == state["run"], "чужой отчёт")
        collection_state = dict(state, discoveryCollection=ref.get("collection"))
        saved = _collection(store, collection_state)
        _require(report.get("collection") == ref["collection"] and report.get("basis") == ref.get("basis"), "связь отчёта нарушена")
        _report(saved, report, state)
        from . import phase_receipts
        if not phase_receipts.valid(store, state, "discovery"):
            return False
        if check_sources:
            if _focused(state) and "fingerprintIndex" in saved:
                # Per-file digests compare without a page cursor: a page is a view, the digests cover all files.
                scan = _scan(store, saved.get("includedPaths", []), scalable=scale.enabled(state), index=True)
                return _inputs_unchanged(state, saved, scan)
            scan = _scan(store, saved.get("includedPaths", []), scalable=scale.enabled(state),
                    cursor=(f"{saved['pagination']['digest']}:{saved['pagination']['start']}:{saved['pagination']['character']}"
                            if saved.get("pagination", {}).get("start") or saved.get("pagination", {}).get("character") else None))
            return saved["scanDigest"] == scan["scanDigest"] and saved["limits"] == scan["limits"]
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def view(store, state):
    """Compact reusable notes and exact references, deliberately without code excerpts."""
    integrity = valid(store, state, check_sources=False)
    result = {"collected": bool(state.get("discoveryCollection")), "recorded": bool(state.get("discovery")),
              "integrityValid": integrity, "fresh": valid(store, state) if integrity else False}
    ref = state.get("discovery") if integrity else None
    collection_ref = ref.get("collection") if ref else state.get("discoveryCollection")
    if collection_ref:
        try:
            saved = _collection(store, dict(state, discoveryCollection=collection_ref))
            result.update(basis=saved["basis"], collection=collection_ref, omitted=saved["omitted"],
                          scope=saved["scope"], limits=saved["limits"], candidateCount=saved["candidateCount"], includedPaths=saved.get("includedPaths", []),
                          sources=[{k: v for k, v in row.items() if k not in {"excerpt", "commands"}} for row in saved["sources"]])
        except (OSError, ValueError, TypeError, KeyError):
            result["collectionValid"] = False
    if ref:
        report = _read(store, ref)
        result.update(artifact=ref, summary=report["summary"], observations=report["observations"], uncertainties=report["uncertainties"])
    return result
