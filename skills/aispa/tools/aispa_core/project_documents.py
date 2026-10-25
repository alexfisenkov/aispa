"""Journal-owned links to ordinary project documents, with immutable evidence.

The caller owns the journal lock and event. This module never writes ordinary
project documents, imports old scope, executes document text, or asks for approval.
"""
from . import scale
import copy
import hashlib
import json
from pathlib import Path
import re

from .portable import is_link, relative_name
from .store import Invalid, confined, digest
from .verified_reads import ResourceLimit, current_scope

SCHEMA = 1
ROLES = ("interview", "intent", "spec", "plan", "index")
ALIASES = {
    "interview": {"interview", "интервью", "вопросы-и-ответы"},
    "intent": {"intent", "замысел", "замысел-проекта"},
    "spec": {"spec", "specification", "спецификация", "техническое-задание"},
    "plan": {"plan", "план", "план-разработки"},
    "index": {"index", "индекс", "карта-файлов", "карта-проекта"},
}
RECOGNIZED_DOCUMENTS = {name + ".md" for names in ALIASES.values() for name in names}
PURPOSES = {
    "interview": ("Исходные вопросы и ответы", "При уточнении смысла и происхождения требований"),
    "intent": ("Постоянный замысел проекта: зачем, для кого и ограничения", "В начале каждого запуска и перед изменением направления проекта"),
    "spec": ("Спецификация проекта; требования нового задания определяются отдельно", "Перед новой спецификацией и при проверке совместимости"),
    "plan": ("План проекта; задачи нового запуска формируются отдельно", "Перед планированием, чтобы учесть зависимости и прежние решения"),
    "index": ("Пользовательская карта проекта", "При поиске файлов; известные файлы читать адресно"),
}
MAX_FILE_BYTES = 262144
MAX_CANDIDATES = 128
_HASH = re.compile(r"^[a-f0-9]{64}$")
CREDENTIAL = re.compile(r"(?:\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
                        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
                        r"(?i:\b(?:cookie|authorization|session(?:id|_id|_token)?|access[_-]?token)"
                        r"\s*[:=]\s*[\"']?(?:Bearer\s+|Basic\s+)?[A-Za-z0-9_./+=;-]{16,}))")


def _require(condition, text):
    if not condition:
        raise Invalid("AISpa project documents: " + text)


def _name(value):
    try:
        value = relative_name(value)
    except ValueError as error:
        raise Invalid("AISpa project documents: нужен относительный путь внутри проекта") from error
    # Reuse the discovery boundary lazily: discovery can use our alias registry.
    from . import discovery
    _require(Path(value).suffix.casefold() == ".md"
             and discovery._kind(value, explicit=True) == "document",
             "нужен обычный безопасный Markdown, не скрытое представление: " + value)
    return value


def _source(store, name, names=None):
    """Read bounded safe bytes; return no bytes for missing or forbidden input."""
    from . import discovery
    name = _name(name)
    path = discovery._safe_path(store.root, name)
    if not path.exists():
        return {"path": name, "status": "missing"}, None
    if names is None:
        names = set(discovery._names(store))
    if name not in names:
        return {"path": name, "status": "unsafe", "reason": "ignored-by-git"}, None
    if not path.is_file():
        return {"path": name, "status": "unsafe", "reason": "not-a-file"}, None
    maximum = scale.Budget().max_bytes if getattr(store, "scale_enabled", False) else MAX_FILE_BYTES
    if getattr(store, "scale_enabled", False):
        scale.Budget().check(path.stat().st_size, path=name)
    if path.stat().st_size > maximum:
        return {"path": name, "status": "unsafe", "reason": "oversized"}, None
    scope = current_scope()
    if scope is not None:
        raw = scope.artifact(path, None, literal=True, maximum=maximum)
        discovery._safe_path(store.root, name)
    else:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        return {"path": name, "status": "unsafe", "reason": "oversized"}, None
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return {"path": name, "status": "unsafe", "reason": "non-utf8"}, None
    if not text.lstrip("\ufeff").strip():
        return {"path": name, "status": "unsafe", "reason": "empty"}, None
    if "\0" in text or discovery.SECRET_CONTENT.search(text) or CREDENTIAL.search(text):
        return {"path": name, "status": "unsafe", "reason": "binary-or-sensitive"}, None
    sha = scope.value_hash(raw) if scope is not None else hashlib.sha256(raw).hexdigest()
    return {"path": name, "status": "safe", "sha256": sha, "bytes": len(raw)}, raw


def _metadata(store, name, names):
    try:
        return _source(store, name, names)[0]
    except (Invalid, OSError):
        return {"path": name, "status": "unsafe", "reason": "unavailable-or-unsafe-path"}


def _role(name):
    path = Path(name)
    if path.suffix.casefold() != ".md":
        return None
    if len(path.parts) > 1 and path.parts[0].casefold() not in {"docs", "doc", "документы"}:
        return None
    stem = path.stem.casefold().replace("_", "-").replace(" ", "-")
    return next((role for role in ROLES if stem in ALIASES[role]), None)


def discover(store):
    """Recognize names, not meaning; ambiguous or unsafe candidates remain visible."""
    from . import discovery
    names = set(discovery._names(store))
    candidates = {name for name in names if _role(name)}
    doc_roots = [p.name for p in store.root.iterdir()
                 if p.name.casefold() in {"docs", "doc", "документы"} and not is_link(p) and p.is_dir()]
    if doc_roots:
        # Git lists ignored descendants without following symlink directories.
        # Read names only, so an ignored nested intent is visible as an obstacle.
        import os
        ignored = store.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", *doc_roots)
        candidates |= {os.fsdecode(raw) for raw in ignored.split(b"\0") if raw and _role(os.fsdecode(raw))}
    # Include ignored root/docs names as obstacles without opening them. In
    # particular an ignored INTENT.md must never be mistaken for no intent.
    for folder in (store.root, store.root / "docs", store.root / "doc", store.root / "документы"):
        if is_link(folder) or not folder.is_dir():
            continue
        for path in folder.iterdir():
            name = path.relative_to(store.root).as_posix()
            if _role(name):
                candidates.add(name)
    _require(getattr(store, "scale_enabled", False) or len(candidates) <= MAX_CANDIDATES, "слишком много кандидатов; укажите канон адресно")
    roles = {role: [] for role in ROLES}
    for name in sorted(candidates):
        roles[_role(name)].append(_metadata(store, name, names))
    return {"schema": SCHEMA, "roles": roles}


def _registry(state):
    value = state.get("projectDocuments", {"schema": SCHEMA, "bindings": {}, "history": []})
    _require(isinstance(value, dict) and value.get("schema") == SCHEMA
             and isinstance(value.get("bindings"), dict) and isinstance(value.get("history"), list),
             "повреждён реестр документов")
    _require(set(value["bindings"]) <= set(ROLES), "неизвестная роль документа")
    latest = {}
    for event in value["history"]:
        _require(isinstance(event, dict) and event.get("action") in {"bind", "inherit"}
                 and event.get("role") in ROLES and isinstance(event.get("binding"), dict),
                 "повреждена история привязок")
        latest[event["role"]] = event["binding"]
    _require(latest == value["bindings"], "привязки не совпадают с последними событиями истории")
    return value


def _artifact_bytes(base, ref, suffix, *, scalable=False):
    maximum = scale.Budget().max_bytes if scalable else MAX_FILE_BYTES + 8192
    _require(isinstance(ref, dict) and set(ref) == {"file", "sha256"}
             and isinstance(ref["file"], str) and ref["file"].startswith("project-documents/")
             and ref["file"].endswith(suffix) and isinstance(ref["sha256"], str)
             and _HASH.fullmatch(ref["sha256"]), "повреждена ссылка на снимок документа")
    path = confined(base, ref["file"])
    _require(path.is_file() and path.stat().st_size <= maximum, "снимок отсутствует или слишком велик")
    scope = current_scope()
    if scope is not None:
        # Legacy is literal bytes, even when Markdown begins with the reserved
        # transport marker. Scalable callers retain the sole scale decoder.
        raw = scope.artifact(path, ref["sha256"], allow_manifest=scalable,
                             literal=not scalable, maximum=maximum)
        confined(base, ref["file"])
        return raw
    if scalable:
        return scale.read_file(path, expected_sha256=ref["sha256"])
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum, "снимок слишком велик")
    _require(hashlib.sha256(raw).hexdigest() == ref["sha256"], "изменён неизменный снимок документа")
    return raw


def _validate_binding(base, binding, role, *, scalable=False):
    _require(isinstance(binding, dict) and set(binding) == {"id", "role", "path", "acceptedSha256", "reason", "originRun", "snapshot", "manifest"},
             "повреждена привязка документа")
    _require(binding["role"] == role and isinstance(binding["reason"], str) and binding["reason"].strip()
             and isinstance(binding["originRun"], str), "повреждена роль или причина привязки")
    _name(binding["path"])
    expected = {key: value for key, value in binding.items() if key != "manifest"}
    try:
        raw_manifest = _artifact_bytes(base, binding["manifest"], ".json", scalable=scalable)
        scope = current_scope()
        saved = scope.json(raw_manifest) if scope is not None else json.loads(raw_manifest.decode("utf-8"))
    except ResourceLimit:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise Invalid("AISpa project documents: повреждён манифест привязки") from error
    _require(saved == {"schema": SCHEMA, "binding": expected}, "привязка не совпадает с сохранённым манифестом")
    raw = _artifact_bytes(base, binding["snapshot"], ".md", scalable=scalable)
    scope = current_scope()
    sha = scope.value_hash(raw) if scope is not None else hashlib.sha256(raw).hexdigest()
    _require(binding["acceptedSha256"] == sha
             and binding["id"] == digest({"role": role, "path": binding["path"], "sha256": binding["acceptedSha256"]}),
             "привязка не совпадает с принятым источником")
    return raw


def _save(store, role, path, raw, reason, origin_run):
    sha = hashlib.sha256(raw).hexdigest()
    binding = {"id": digest({"role": role, "path": path, "sha256": sha}), "role": role,
               "path": path, "acceptedSha256": sha, "reason": reason, "originRun": origin_run,
               "snapshot": store.artifact("project-documents", raw, ".md")}
    body = json.dumps({"schema": SCHEMA, "binding": binding}, ensure_ascii=False, sort_keys=True).encode()
    binding["manifest"] = store.artifact("project-documents", body, ".json")
    return binding


def _validate_history(base, registry, *, scalable=False):
    checked = set()
    for event in registry["history"]:
        binding = event["binding"]
        key = json.dumps(binding, ensure_ascii=False, sort_keys=True)
        if key not in checked:
            _validate_binding(base, binding, event["role"], scalable=scalable)
            checked.add(key)


def bind(store, state, role, path, expected_sha256, reason):
    """Select a source already read by the agent, without another owner approval."""
    _require(role in ROLES, "неизвестная роль документа")
    path = _name(path)
    _require(isinstance(expected_sha256, str) and _HASH.fullmatch(expected_sha256), "нужен SHA-256 прочитанного источника")
    _require(isinstance(reason, str) and bool(reason.strip()) and len(reason) <= 1600,
             "нужно краткое предметное обоснование привязки")
    from . import discovery
    _require(not discovery.SECRET_CONTENT.search(reason) and not CREDENTIAL.search(reason),
             "причина содержит чувствительные данные")
    registry = _registry(state)
    _validate_history(store.run, registry, scalable=scale.enabled(state))
    metadata, raw = _source(store, path)
    _require(metadata["status"] == "safe", "источник недоступен или небезопасен: " + path)
    _require(metadata["sha256"] == expected_sha256, "источник изменился после чтения; перечитайте его")
    previous = registry["bindings"].get(role)
    if previous:
        _validate_binding(store.run, previous, role, scalable=scale.enabled(state))
        if previous["path"] == path and previous["acceptedSha256"] == expected_sha256:
            return copy.deepcopy(previous)
    binding = _save(store, role, path, raw, reason, state["run"])
    updated = copy.deepcopy(registry)
    updated["bindings"][role] = binding
    updated["history"].append({"action": "bind", "role": role, "binding": copy.deepcopy(binding)})
    state["projectDocuments"] = updated
    return copy.deepcopy(binding)


def initialize(store, state, previous=None):
    """Inherit selected source bytes without accepting drift or old run scope."""
    _require("projectDocuments" not in state, "реестр уже инициализирован")
    registry = {"schema": SCHEMA, "bindings": {}, "history": []}
    if previous and previous.get("projectDocuments"):
        old = _registry(previous)
        old_run = confined(store.home, "runs/" + previous["run"])
        _validate_history(old_run, old, scalable=scale.enabled(previous))
        for role, binding in old["bindings"].items():
            raw = _validate_binding(old_run, binding, role, scalable=scale.enabled(previous))
            saved = _save(store, role, binding["path"], raw, binding["reason"], binding["originRun"])
            registry["bindings"][role] = saved
            registry["history"].append({"action": "inherit", "role": role,
                                        "fromRun": previous["run"], "binding": copy.deepcopy(saved)})
    state["projectDocuments"] = registry
    found = discover(store)
    for role, candidates in found["roles"].items():
        if role not in registry["bindings"] and len(candidates) == 1 and candidates[0]["status"] == "safe":
            row = candidates[0]
            bind(store, state, role, row["path"], row["sha256"], "Единственный документ с распознанным именем; содержание остаётся исходным источником.")
    return view(store, state)


def view(store, state):
    """Compact read-only file map. Actual contents stay in their ordinary file."""
    registry = _registry(state)
    _validate_history(store.run, registry, scalable=scale.enabled(state))
    found = discover(store)
    from . import discovery
    names = set(discovery._names(store))
    roles = {}
    for role in ROLES:
        candidates = found["roles"][role]
        row = {"role": role, "purpose": PURPOSES[role][0], "whenRead": PURPOSES[role][1], "candidates": candidates}
        binding = registry["bindings"].get(role)
        if binding:
            _validate_binding(store.run, binding, role, scalable=scale.enabled(state))
            metadata = _metadata(store, binding["path"], names)
            status = metadata["status"]
            if status == "safe":
                status = "fresh" if metadata["sha256"] == binding["acceptedSha256"] else "changed"
            row.update(metadata, status=status, acceptedSha256=binding["acceptedSha256"], id=binding["id"],
                       reason=binding["reason"], snapshot=copy.deepcopy(binding["snapshot"]))
        else:
            status = "ambiguous" if len(candidates) > 1 else (
                "unsafe" if candidates and candidates[0]["status"] == "unsafe" else "unbound" if candidates else "absent")
            row["status"] = status
        roles[role] = row
    return {"schema": SCHEMA, "roles": roles}


def fingerprint(store, state):
    rows = view(store, state)["roles"]
    return digest({role: {key: row[key] for key in ("id", "path", "status", "sha256", "acceptedSha256", "candidates") if key in row}
                   for role, row in rows.items()})


def accepted_basis(state):
    """Accepted intent identity only; freshness is checked separately against disk."""
    binding = _registry(state)["bindings"].get("intent")
    return digest({key: binding[key] for key in ("id", "path", "acceptedSha256")} if binding else None)


def required_paths(state):
    binding = _registry(state)["bindings"].get("intent")
    return [binding["path"]] if binding else []


def intent_reference(store, state):
    """Read the selected intent only; task packets need no full discovery catalog."""
    registry = _registry(state)
    _validate_history(store.run, registry, scalable=scale.enabled(state))
    binding = registry["bindings"].get("intent")
    if not binding:
        return None
    _validate_binding(store.run, binding, "intent", scalable=scale.enabled(state))
    row = _metadata(store, binding["path"], None)
    if row["status"] == "safe":
        row["status"] = "fresh" if row["sha256"] == binding["acceptedSha256"] else "changed"
    return {**row, "id": binding["id"], "acceptedSha256": binding["acceptedSha256"],
            "snapshot": copy.deepcopy(binding["snapshot"]),
            "authority": "project context, not a new task or permission; current scope comes from the new brief"}


def require_fresh_intent(store, state, *, require=True):
    if not require or not state.get("policy", {}).get("projectDocuments"):
        return
    row = intent_reference(store, state) or {"status": "unbound"}
    _require(row["status"] == "fresh", "замысел проекта должен быть выбран и актуален (" + row["status"]
             + "); изучите существующий канон или создайте один INTENT.md и привяжите прочитанный SHA")
