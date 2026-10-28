"""Strict support selection from the installed package, with source receipts."""
import hashlib
import html
import json
from pathlib import Path
import re

from .portable import is_link, relative_name
from .store import Invalid, digest
from .updates import _version_text

MAX_BYTES = 262144
SHA = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[a-z][a-z0-9-]{0,79}\Z")


def _need(condition, message):
    if not condition:
        raise Invalid("AISpa FAQ: " + message)


def _file(root, name):
    try:
        safe = relative_name(name)
    except (TypeError, ValueError) as error:
        raise Invalid("AISpa FAQ: unsafe source path") from error
    _need(safe == name and not is_link(root), "unsafe source root/path")
    path = root
    for part in Path(safe).parts:
        path = path / part
        _need(not is_link(path), "source links are forbidden")
    _need(path.is_file() and path.stat().st_size <= MAX_BYTES, "source missing or too large")
    import os  # local: the rest of this file stays byte for byte as in 0.5
    import stat
    try:
        # No link may be swapped in between the checks above and the read; a FIFO or device swapped in
        # opens without blocking (O_NONBLOCK) and is refused by fstat on the descriptor actually opened.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            _need(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES, "source is not a regular file or too large")
            data = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise Invalid("AISpa read: missing or unreadable input") from error
    _need(len(data) <= MAX_BYTES, "source grew beyond byte budget")
    return data


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 12000


def select(package, *, version, ids=None, expected_sha256=None):
    """Detect an exact package version; never apply newer FAQ to an old pin."""
    package = Path(package).absolute()
    version_raw = _file(package, "tools/aispa_core/__init__.py")
    try:
        detected = _version_text(version_raw)
    except (UnicodeError, ValueError) as error:
        raise Invalid("AISpa FAQ: invalid detected package version") from error
    _need(isinstance(version, str) and version == detected, "requested version differs from detected package")
    raw = _file(package, "references/support-faq.json")
    canonical_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        _need(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256)
              and expected_sha256 == canonical_sha, "canonical SHA mismatch")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as error:
        raise Invalid("AISpa FAQ: malformed canonical JSON") from error
    _need(isinstance(value, dict) and set(value) == {"schema", "packageVersion", "source", "axes", "entries"}
          and type(value["schema"]) is int and value["schema"] == 1 and value["packageVersion"] == version,
          "unsupported schema/version or unexpected private metadata")
    axes = {"questions": ["auto", "important", "interview"], "checks": ["fast", "balanced", "max"],
            "interviewScopes": ["compact", "standard", "deep"], "independent": True}
    _need(isinstance(value["axes"], dict) and value["axes"] == axes
          and value["axes"].get("independent") is True, "mode axes must stay independent")
    source = value["source"]
    _need(isinstance(source, dict) and set(source) == {"repository", "files"}
          and source["repository"] == "alexfisenkov/aispa" and isinstance(source["files"], list)
          and 0 < len(source["files"]) <= 48, "invalid source binding")
    seen_sources = set()
    for row in source["files"]:
        _need(isinstance(row, dict) and set(row) == {"path", "sha256"}
              and isinstance(row["path"], str) and row["path"] not in seen_sources
              and row["path"] != "references/support-faq.json"
              and isinstance(row["sha256"], str) and SHA.fullmatch(row["sha256"]), "invalid source record")
        _need(hashlib.sha256(_file(package, row["path"])).hexdigest() == row["sha256"], "stale package source: " + row["path"])
        seen_sources.add(row["path"])
    _need("tools/aispa_core/__init__.py" in seen_sources, "detected version source must be bound")
    entries = value["entries"]
    _need(isinstance(entries, list) and 0 < len(entries) <= 128, "invalid entry list")
    by_id = {}
    for entry in entries:
        _need(isinstance(entry, dict) and set(entry) == {"id", "question", "answer"}
              and isinstance(entry["id"], str) and ID.fullmatch(entry["id"])
              and entry["id"] not in by_id and _text(entry["question"]) and _text(entry["answer"]),
              "invalid, duplicate or mixed-version FAQ entry")
        by_id[entry["id"]] = entry
    if ids is None:
        ids = list(by_id)
    _need(isinstance(ids, list) and ids and all(isinstance(key, str) for key in ids)
          and len(set(ids)) == len(ids) and set(ids) <= set(by_id), "unknown or duplicate selected ID")
    source_sha = digest(source)
    result = {"schema": 1, "packageVersion": version, "canonicalSha256": canonical_sha,
              "source": source, "sourceSha256": source_sha, "axes": axes,
              "entries": [dict(by_id[key]) for key in ids]}
    return result


def render(selected, *, format="markdown"):
    """Render visible Q/A and a separate generation receipt, never self-hash."""
    _need(format in ("markdown", "html"), "unsupported rendering format")
    entries = selected["entries"]
    if format == "html":
        text = '<section aria-label="AISpa — ответы на вопросы">\n<h2>AISpa — ответы на вопросы</h2>\n'
        for row in entries:
            text += '<h3>' + html.escape(row["question"]) + '</h3>\n<p>' + html.escape(row["answer"]).replace("\n", "<br>\n") + '</p>\n'
        text += '</section>\n'
    else:
        text = "# AISpa — ответы на вопросы\n\n" + "".join("## " + row["question"] + "\n\n" + row["answer"] + "\n\n" for row in entries)
    return {"text": text, "receipt": {"schema": 1, "packageVersion": selected["packageVersion"],
            "canonicalSha256": selected["canonicalSha256"], "sourceSha256": selected["sourceSha256"],
            "sourceFiles": selected["source"]["files"], "entryIds": [row["id"] for row in entries],
            "format": format, "outputSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "delivery": "generated-only; not a published Release or remote-consumer receipt"}}
