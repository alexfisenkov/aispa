"""Commit-bound worktree snapshots with portable Git text/EOL semantics.

Only CRLF -> LF text equivalence is accepted; custom content filters are never
executed. Raw binary changes, staged differences and hidden tracked edits fail.
"""
import hashlib
import os
from pathlib import Path
import stat

from .portable import relative_name, is_link, executable
from .store import Invalid


def _require(condition, message):
    if not condition:
        raise Invalid(message)


def _tree(root, git):
    entries = []
    for raw in git(root, "ls-tree", "-rz", "--full-tree", "HEAD").stdout.split(b"\0"):
        if raw:
            meta, name = raw.split(b"\t", 1)
            mode, kind, oid = meta.split()
            entries.append((os.fsdecode(name), mode, kind, oid))
    return entries


def ensure_clean_index(root, git):
    """Compare the index directly; don't run user clean/textconv filters."""
    expected = sorted((name, mode, oid) for name, mode, _kind, oid in _tree(root, git))
    actual = []
    for raw in git(root, "ls-files", "--stage", "-z").stdout.split(b"\0"):
        if raw:
            meta, name = raw.split(b"\t", 1)
            mode, oid, stage = meta.split()
            _require(stage == b"0", "Clean-clone: в индексе есть незавершённый конфликт")
            actual.append((os.fsdecode(name), mode, oid))
    _require(sorted(actual) == expected, "Clean-clone требует сохранения staged изменений в commit")
    _require(not git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout,
             "Clean-clone требует сохранения unignored файлов в commit")


def _attributes(root, names, git):
    if not names:
        return {}
    raw = git(root, "check-attr", "-z", "--stdin", "filter", "working-tree-encoding", "ident", "text",
              input_data=b"\0".join(os.fsencode(name) for name in names) + b"\0").stdout.split(b"\0")
    values = {}
    for index in range(0, len(raw) - 1, 3):
        name, key, value = os.fsdecode(raw[index]), raw[index + 1].decode(), raw[index + 2].decode()
        values.setdefault(name, {})[key] = value
        if key != "text":
            _require(value in ("unspecified", "unset"),
                     "Clean-clone: внешние Git filters, ident и working-tree-encoding пока не поддерживаются")
    return values


def _normalized_chunks(path):
    pending = b""
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            chunk = pending + chunk
            if chunk.endswith(b"\r"):
                pending, chunk = b"\r", chunk[:-1]
            else:
                pending = b""
            yield chunk.replace(b"\r\n", b"\n")
    if pending:
        yield pending


def _hashes(path, object_format, secret_pattern):
    raw_hash = hashlib.sha256()
    git_hash = hashlib.new(object_format)
    size = path.stat().st_size
    git_hash.update(("blob %d\0" % size).encode())
    tail, binary, read_size = b"", False, 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            raw_hash.update(chunk); git_hash.update(chunk)
            read_size += len(chunk)
            binary = binary or b"\0" in chunk
            if secret_pattern is not None:
                _require(not secret_pattern.search((tail + chunk).decode("utf-8", "replace")),
                         "Git tree содержит возможный секрет")
                tail = chunk[-4096:]
    _require(read_size == size, "Файл изменился во время проверки clean-clone")
    return raw_hash.hexdigest(), git_hash.hexdigest().encode(), binary


def records(root, git, *, secret_pattern=None, credential_name=lambda _name: False):
    entries = _tree(root, git)
    names = [name for name, *_rest in entries]
    if os.name == "nt":
        _require(len({name.casefold() for name in names}) == len(names), "Git tree содержит конфликт регистра имён Windows")
    # Do not give invalid/device names even to the attribute reader.
    for name, mode, kind, _oid in entries:
        try:
            _require(relative_name(name) == name, "Неканонический путь Git")
        except ValueError as error:
            raise Invalid("Путь Git несовместим с Windows/macOS") from error
        _require(not any(part.casefold() in (".git", ".aispa") for part in Path(name).parts), "Служебный путь в Git tree")
        _require(kind == b"blob" and mode in (b"100644", b"100755") and name != ".gitmodules",
                 "Clean-clone пока не поддерживает submodules и tracked symlinks")
        _require(not credential_name(name), "Git tree содержит файл с возможными credentials")
    attrs = _attributes(root, names, git)
    object_format = git(root, "rev-parse", "--show-object-format").stdout.decode().strip()
    result = []
    for name, mode, _kind, oid in entries:
        path = root
        for part in Path(name).parts:
            path /= part
            _require(not is_link(path), "Ссылка или junction в clean-clone")
        _require(path.is_file() and stat.S_ISREG(path.stat().st_mode), "В clean-clone отсутствует обычный tracked-файл")
        sha, raw_oid, binary = _hashes(path, object_format, secret_pattern)
        if raw_oid != oid:
            _require(not binary and attrs.get(name, {}).get("text") != "unset", "Рабочий файл отличается от committed HEAD")
            length = 0
            for chunk in _normalized_chunks(path):
                length += len(chunk)
            canonical = hashlib.new(object_format)
            normalized = hashlib.sha256()
            canonical.update(("blob %d\0" % length).encode())
            for chunk in _normalized_chunks(path):
                canonical.update(chunk)
                normalized.update(chunk)
            _require(canonical.hexdigest().encode() == oid, "Рабочий файл отличается от committed HEAD")
            sha = normalized.hexdigest()
        _require(os.name == "nt" or executable(path) == (mode == b"100755"),
                 "Execute-бит файла отличается от committed HEAD")
        result.append([name, {"sha256": sha, "executable": mode == b"100755"}])
    return sorted(result)
