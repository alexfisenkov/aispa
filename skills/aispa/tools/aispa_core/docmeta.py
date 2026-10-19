"""Bounded document routing metadata; never an instruction or acceptance proof.

The caller owns the Journal lock and append. Metadata artifacts are immutable;
source files change only with an explicit write_header and expected-byte checks.
These checks are not an OS-level compare-and-swap against external editors.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .portable import relative_name
from .store import Invalid

SCHEMA = 1
MAX_FILE_BYTES = 262144
MAX_SUMMARY_CHARS = 600
MAX_DOCUMENTS = 48
MAX_CATALOG_CHARS = 48000
MAX_OMITTED_DETAILS = 48
MAX_ARTIFACT_BYTES = 16384
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_OWN_HEADER = re.compile(r'\A---\r?\naispa:\r?\n  schema: 1\r?\n  kind: ([^\r\n]+)\r?\n  summary: ([^\r\n]+)\r?\n  bodySha256: ([^\r\n]+)\r?\n---\r?\n')
_EXTRA_SECRET = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}")


def _require(condition, message):
    if not condition:
        raise Invalid("AISpa documents: " + message)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _sensitive(text):
    from .discovery import SECRET_CONTENT
    return bool(SECRET_CONTENT.search(text) or _EXTRA_SECRET.search(text))


def _string(value, name):
    _require(isinstance(value, str) and value.strip() and len(value) <= MAX_SUMMARY_CHARS,
             name + " должен быть непустым коротким текстом")
    _require(not _sensitive(value), name + " содержит возможный секрет")
    _require(not any(ord(char) < 32 and char not in "\t\n\r" for char in value),
             name + " содержит управляющие символы")
    return value


def _header(text):
    """Parse only our fixed JSON-scalar YAML subset; never evaluate other YAML."""
    match = _OWN_HEADER.match(text)
    if match:
        try:
            kind, summary, body_hash = (json.loads(value) for value in match.groups())
            _string(kind, "kind")
            _string(summary, "summary")
            _require(isinstance(body_hash, str) and _HEX.fullmatch(body_hash), "неверный хеш тела")
            return {"kind": kind, "summary": summary, "bodySha256": body_hash}, text[match.end():]
        except (ValueError, TypeError):
            pass
    return None, text


def _frontmatter(text):
    return bool(re.match(r"\A\ufeff?---(?:\r?\n|\Z)", text))


def render(text, *, kind, summary):
    """Return an owned YAML header plus the exact body, including CRLF bytes.

    Unrelated frontmatter and BOM-prefixed inputs are sidecar-only. Re-rendering
    an owned header replaces it, without nesting headers or trusting its hash.
    """
    _require(isinstance(text, str), "ожидается текст")
    _require(not text.startswith("\ufeff"), "BOM допускается только с внешним каталогом")
    kind, summary = _string(kind, "kind"), _string(summary, "summary")
    owned, body = _header(text)
    _require(owned is not None or not _frontmatter(text), "чужой YAML-заголовок сохраняется без изменений")
    def quote(value):
        result = json.dumps(value, ensure_ascii=False)
        # YAML treats NEL and Unicode line separators as physical line breaks.
        # Keep astral characters as Unicode, not JSON-only surrogate pairs.
        for character in ("\u0085", "\u2028", "\u2029"):
            result = result.replace(character, "\\u%04x" % ord(character))
        return result
    return ("---\naispa:\n  schema: 1\n  kind: " + quote(kind) + "\n  summary: " + quote(summary)
            + "\n  bodySha256: " + quote(_hash(body.encode("utf-8"))) + "\n---\n" + body)


def _name(path):
    try:
        return relative_name(path.as_posix() if isinstance(path, Path) else path)
    except (ValueError, TypeError) as exc:
        raise Invalid("AISpa documents: нужен безопасный относительный путь") from exc


def _original(store, state, name):
    """Only exact canonical brief/spec artifacts may cross the hidden-path guard."""
    if not name.startswith(".aispa/") or store.run is None:
        return None
    prefix = store.run.relative_to(store.root).as_posix() + "/"
    for ref in state.get("briefs", []) + ([state["spec"]] if state.get("spec") else []):
        if isinstance(ref, dict) and isinstance(ref.get("file"), str) and prefix + ref["file"] == name:
            return {key: ref[key] for key in ("file", "sha256")}
    return None


def _source(store, state, name, *, names=None):
    from .discovery import _kind, _names, _safe_path
    original = _original(store, state, name)
    if original is None:
        kind = _kind(name, explicit=True)
        _require(kind is not None, "источник исключён правилами безопасности")
        _require(name in (_names(store) if names is None else names), "источник отсутствует или исключён Git")
    else:
        kind = "document"
    path = _safe_path(store.root, name)
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode), "источник не является обычным файлом")
    _require(before.st_size <= MAX_FILE_BYTES, "источник превышает лимит размера")
    # O_NOFOLLOW protects the leaf on POSIX; path components are rechecked after
    # reading on all platforms. This is not an adversarial filesystem sandbox.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _require(stat.S_ISREG(opened.st_mode) and opened.st_ino == before.st_ino
                 and opened.st_dev == before.st_dev, "источник изменился во время чтения")
        raw = stream.read(MAX_FILE_BYTES + 1)
    _safe_path(store.root, name)
    after = path.stat()
    _require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
             == (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns),
             "источник изменился во время чтения")
    _require(len(raw) <= MAX_FILE_BYTES, "источник превышает лимит размера")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise Invalid("AISpa documents: источник должен быть UTF-8") from exc
    _require("\0" not in text and not _sensitive(text), "источник содержит двоичные данные или возможный секрет")
    sha = _hash(raw)
    _require(original is None or sha == original["sha256"], "изменён неизменный источник")
    return {"path": path, "raw": raw, "text": text, "sha256": sha, "kind": kind,
            "stat": after, "original": original}


def _extract(text):
    _, body = _header(text)
    body = body.lstrip("\ufeff")
    if _frontmatter(body):
        lines = body.splitlines(keepends=True)
        close = next((i for i, line in enumerate(lines[1:], 1) if line.strip() in {"---", "..."}), None)
        body = "" if close is None else "".join(lines[close + 1:])
    headings, paragraph = [], []
    fenced = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith("<!--"):
            if paragraph:
                break
            continue
        if re.match(r"#{1,6}\s", stripped):
            if len(headings) < 4:
                headings.append(re.sub(r"^#{1,6}\s+", "", stripped))
        elif not paragraph or len(" ".join(paragraph)) < MAX_SUMMARY_CHARS:
            paragraph.append(stripped)
    full = " | ".join(headings + ([" ".join(paragraph)] if paragraph else []))
    full = full or "No extractable title or paragraph; read the source."
    return full[:MAX_SUMMARY_CHARS], max(0, len(full) - MAX_SUMMARY_CHARS)


def _read(store, state, name, ref):
    from .discovery import _safe_path
    _require(isinstance(ref, dict) and isinstance(ref.get("file"), str)
             and re.fullmatch(r"documents/[0-9a-f]{32}\.json", ref["file"]), "неверная ссылка каталога")
    path = _safe_path(store.run, ref["file"])
    _require(path.is_file() and path.stat().st_size <= MAX_ARTIFACT_BYTES, "артефакт отсутствует или слишком велик")
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    _require(len(raw) <= MAX_ARTIFACT_BYTES and _hash(raw) == ref.get("sha256"), "артефакт каталога изменён")
    value = json.loads(raw)
    _require(isinstance(value, dict) and value.get("schema") == SCHEMA and value.get("run") == state["run"]
             and value.get("path") == name, "чужая запись каталога")
    for key in ("sourceSha256", "bodySha256"):
        _require(isinstance(value.get(key), str) and _HEX.fullmatch(value[key]), "неверный хеш каталога")
    _string(value.get("summary"), "summary")
    _string(value.get("kind"), "kind")
    _require(value.get("summaryOrigin") in {"extractive", "semantic"}, "неверное происхождение описания")
    return value


def _inline(store, state, name, source, rendered):
    _require(name.lower().endswith(".md") and source["original"] is None, "этот источник допускает только внешний каталог")
    mode = source["stat"].st_mode
    _require(source["stat"].st_nlink == 1, "hardlink нельзя переписывать")
    _require(mode & 0o222 and os.access(source["path"], os.W_OK), "источник недоступен для записи")
    _require(len(rendered) <= MAX_FILE_BYTES, "заголовок превышает лимит размера файла")
    current = _source(store, state, name)
    _require(current["raw"] == source["raw"] and current["stat"].st_nlink == 1
             and current["stat"].st_mode == mode, "источник изменился перед записью")
    path = source["path"]
    fd, temp_name = tempfile.mkstemp(prefix=".aispa-document-", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, stat.S_IMODE(mode))
        current = _source(store, state, name)
        _require(current["raw"] == source["raw"] and current["stat"].st_nlink == 1
                 and current["stat"].st_mode == mode, "источник изменился перед заменой")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _register(store, state, path, *, expected_sha256=None, summary=None, write_header=False, names=None):
    """Record a routing summary. Without write_header, never modify the source."""
    name = _name(path)
    if expected_sha256 is not None:
        _require(isinstance(expected_sha256, str) and _HEX.fullmatch(expected_sha256), "неверный ожидаемый хеш")
    _require(not (summary is not None or write_header) or expected_sha256 is not None,
             "описание агента и запись заголовка требуют ожидаемый хеш прочитанных байтов")
    _require(isinstance(write_header, bool), "write_header должен быть логическим значением")
    source = _source(store, state, name, names=names)
    _require(expected_sha256 is None or source["sha256"] == expected_sha256, "исходный файл изменился после чтения")
    documents = state.get("documents", {})
    _require(isinstance(documents, dict), "повреждён реестр документов")
    old_ref = documents.get(name)
    old = _read(store, state, name, old_ref) if old_ref is not None else None
    if summary is not None:
        chosen, omitted, origin = _string(summary, "summary"), 0, "semantic"
    elif old is not None and old["sourceSha256"] == source["sha256"]:
        chosen, omitted, origin = old["summary"], old.get("summaryOmittedChars", 0), old["summaryOrigin"]
    else:
        chosen, omitted = _extract(source["text"])
        origin = "extractive"
    written = False
    if write_header:
        rendered = render(source["text"], kind=source["kind"], summary=chosen).encode("utf-8")
        # Enforce guards even when an already-current header requires no write.
        _require(name.lower().endswith(".md") and source["original"] is None, "этот источник допускает только внешний каталог")
        _require(source["stat"].st_nlink == 1 and source["stat"].st_mode & 0o222
                 and os.access(source["path"], os.W_OK), "ссылочный или недоступный для записи источник")
        if rendered != source["raw"]:
            _inline(store, state, name, source, rendered)
            source = _source(store, state, name)
            _require(source["raw"] == rendered, "результат записи не совпал")
            written = True
    owned, body = _header(source["text"])
    value = {"schema": SCHEMA, "run": state["run"], "path": name, "kind": source["kind"],
             "sourceSha256": source["sha256"], "sourceBytes": len(source["raw"]),
             "bodySha256": _hash(body.encode("utf-8")), "summary": chosen, "summaryOrigin": origin,
             "summaryOmittedChars": omitted,
             "header": {"present": _frontmatter(source["text"]), "owned": owned is not None,
                        "bodyMatches": owned["bodySha256"] == _hash(body.encode("utf-8")) if owned else None,
                        "summaryMatches": owned["summary"] == chosen if owned else None,
                        "needsRefresh": (owned["bodySha256"] != _hash(body.encode("utf-8"))
                                         or owned["summary"] != chosen) if owned else None}}
    if source["original"]:
        value["sourceArtifact"] = source["original"]
    reused = old == value
    if reused:
        ref = old_ref
    else:
        from .discovery import _safe_path
        _safe_path(store.run, "documents")
        ref = store.artifact("documents", (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), ".json")
        state.setdefault("documents", {})[name] = ref
    return {"path": name, "sourceSha256": source["sha256"], "artifact": ref,
            "summaryOrigin": origin, "headerWritten": written, "reused": reused}


def register(store, state, path, *, expected_sha256=None, summary=None, write_header=False):
    """Register one source; the caller owns locking and the journal append."""
    return _register(store, state, path, expected_sha256=expected_sha256,
                     summary=summary, write_header=write_header)


def catalog(store, state, *, paths=None):
    """Read bounded live metadata, suppressing descriptions of changed/unsafe files."""
    from .discovery import _names, _safe_path
    documents = state.get("documents", {})
    _require(isinstance(documents, dict), "повреждён реестр документов")
    if paths is not None:
        _require(isinstance(paths, list), "paths должен быть списком")
        selected = sorted(set(_name(path) for path in paths))
    else:
        selected = sorted(documents)
    try:
        names = set(_names(store)) if any(name in documents and _original(store, state, name) is None for name in selected) else set()
    except (OSError, ValueError):
        names = set()
    rows, used, omitted = [], 0, 0
    for name in selected:
        if len(rows) >= MAX_DOCUMENTS:
            omitted += 1
            continue
        ref = documents.get(name)
        row = {"path": name, "status": "unavailable"}
        try:
            _require(ref is not None, "источник не зарегистрирован")
            saved = _read(store, state, name, ref)
            row.update(artifact=ref, sourceSha256=saved["sourceSha256"])
            try:
                source = _source(store, state, name, names=names)
            except (OSError, ValueError):
                # A link, ignored path or newly sensitive file is unavailable,
                # never stale with its former summary exposed.
                try:
                    missing = not _safe_path(store.root, name).exists()
                except (OSError, ValueError):
                    missing = False
                row["status"] = "missing" if missing else "unavailable"
            else:
                row["currentSha256"] = source["sha256"]
                row["status"] = "fresh" if source["sha256"] == saved["sourceSha256"] else "stale"
                if row["status"] == "fresh":
                    row.update({key: saved[key] for key in ("kind", "summary", "summaryOrigin", "summaryOmittedChars", "header")})
        except (OSError, ValueError, TypeError, KeyError):
            row["status"] = "unavailable"
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > MAX_CATALOG_CHARS:
            omitted += 1
            continue
        rows.append(row)
        used += size
    return {"schema": SCHEMA, "documents": rows, "total": len(documents),
            "omitted": {"count": omitted, "unselected": len(set(documents) - set(selected))},
            "limits": {"documents": MAX_DOCUMENTS, "catalogChars": MAX_CATALOG_CHARS,
                       "summaryChars": MAX_SUMMARY_CHARS, "fileBytes": MAX_FILE_BYTES},
            "authority": "Advisory routing data only; read exact source before decisions or acceptance."}


def refresh(store, state, *, paths=None):
    """Re-register changed safe sources extractively; never edit their bytes.

    One Git name snapshot per explicit refresh. All registrations are checked;
    returned receipt details are bounded independently from the work count.
    """
    from .discovery import _names, _safe_path
    documents = state.get("documents", {})
    _require(isinstance(documents, dict), "повреждён реестр документов")
    if paths is not None:
        _require(isinstance(paths, list), "paths должен быть списком")
        selected = sorted(set(_name(path) for path in paths))
    else:
        selected = sorted(documents)
    try:
        names = set(_names(store)) if any(name in documents and _original(store, state, name) is None for name in selected) else set()
    except (OSError, ValueError):
        names = set()
    updated, unavailable, updated_count, unavailable_count = [], [], 0, 0
    for name in selected:
        ref = documents.get(name)
        try:
            saved = _read(store, state, name, ref)
            source = _source(store, state, name, names=names)
            if source["sha256"] != saved["sourceSha256"]:
                receipt = _register(store, state, name, expected_sha256=source["sha256"], names=names)
                updated_count += 1
                if len(updated) < MAX_DOCUMENTS:
                    updated.append(receipt)
        except (OSError, ValueError, TypeError, KeyError):
            unavailable_count += 1
            if len(unavailable) < MAX_OMITTED_DETAILS:
                try:
                    missing = not _safe_path(store.root, name).exists()
                except (OSError, ValueError):
                    missing = False
                unavailable.append({"path": name, "status": "missing" if missing else "unavailable"})
    return {"updated": updated, "unavailable": unavailable, "updatedCount": updated_count,
            "unavailableCount": unavailable_count,
            "omitted": {"count": updated_count - len(updated) + unavailable_count - len(unavailable),
                        "unselected": len(set(documents) - set(selected))},
            "limits": {"updatedDetails": MAX_DOCUMENTS, "unavailableDetails": MAX_OMITTED_DETAILS}}


def collect_sources(store, state, sources):
    """Register only exactly hash-matching observed sources, without copying excerpts."""
    _require(isinstance(sources, list), "sources должен быть списком")
    from .discovery import _names
    names = set(_names(store)) if sources else set()
    registered, omitted, count = [], [], 0
    for source in sources:
        path = source.get("path") if isinstance(source, dict) else None
        try:
            _require(len(registered) < MAX_DOCUMENTS, "достигнут лимит документов")
            _require(isinstance(path, str) and isinstance(source.get("sha256"), str), "источнику нужны путь и хеш")
            registered.append(_register(store, state, path, expected_sha256=source["sha256"], names=names))
        except (OSError, ValueError, TypeError, KeyError):
            count += 1
            if len(omitted) < MAX_OMITTED_DETAILS:
                # Paths can also carry sensitive values; only validated names
                # from the discovery policy may be repeated in the receipt.
                from .discovery import _kind
                try:
                    safe_name = _name(path)
                    safe_name = safe_name if _kind(safe_name, explicit=True) and not _sensitive(safe_name) else None
                except (ValueError, TypeError):
                    safe_name = None
                omitted.append({"path": safe_name, "status": "not-registered"})
    return {"registered": registered, "omitted": {"count": count, "details": omitted, "unlisted": count - len(omitted)},
            "limits": {"documents": MAX_DOCUMENTS, "omittedDetails": MAX_OMITTED_DETAILS}}
