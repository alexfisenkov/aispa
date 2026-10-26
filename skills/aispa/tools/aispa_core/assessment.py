"""Bounded static filesystem assessment, independent of runs and execution."""
import ast
import hashlib
import math
import os
from pathlib import Path
import stat
import time

from . import discovery
from .portable import is_link
from .store import Invalid


# Audit selection is deliberately broader than discovery's representative
# language/entrypoint selection. Exclude sensitive/opaque formats before read;
# all other ordinary files get bounded text inspection, whatever their suffix.
EXCLUDED_EXTENSIONS = frozenset({
    ".pem", ".key", ".p8", ".p12", ".pfx", ".keystore", ".jks", ".der", ".crt", ".cer",
    ".db", ".db3", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".csv", ".tsv",
    ".log", ".jsonl", ".ndjson", ".parquet", ".arrow", ".feather", ".avro", ".h5", ".hdf5",
    ".npy", ".npz", ".pickle", ".pkl", ".pt", ".pth", ".ckpt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic", ".heif", ".ico", ".svg",
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".aiff",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".a", ".lib", ".class", ".pyc", ".pyo", ".wasm", ".bin",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
})


def _ordinary_kind(name):
    path = Path(name)
    if (path.suffix.lower() in EXCLUDED_EXTENSIONS
            or any(part.startswith(".") or discovery.SECRET_NAME.search(part) for part in path.parts)):
        return None
    # Keep useful existing labels, without using their absence as exclusion.
    # Unknown text is not claimed to have passed a parser for its language.
    return discovery._kind(name, explicit=True) or "text"


def _positive(value, label, *, zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (value < 0 if zero else value <= 0)):
        raise Invalid("AISpa assessment: invalid " + label)


def assess(root, *, max_entries=1000, max_depth=4, max_file_bytes=131072,
           max_total_bytes=1048576, max_excerpt_chars=2000, timeout=2):
    """Read ordinary safe files only; commands are literal findings, never run.

    POSIX traversal pins directory descriptors and opens children with NOFOLLOW.
    Hosts without descriptor-relative traversal use reparse/link checks and
    checked file identity; concurrent hostile tree replacement is not a sandbox.
    """
    options = dict(max_entries=max_entries, max_depth=max_depth, max_file_bytes=max_file_bytes,
                   max_total_bytes=max_total_bytes, max_excerpt_chars=max_excerpt_chars)
    for key, value in options.items():
        _positive(value, key, zero=key == "max_depth")
        if not isinstance(value, int):
            raise Invalid("AISpa assessment: integer required for " + key)
    _positive(timeout, "timeout")
    deadline = time.monotonic() + timeout
    requested = Path(root).absolute()
    if is_link(requested) or not requested.is_dir():
        raise Invalid("AISpa assessment: root must be an ordinary existing directory")
    physical = requested.resolve()
    result = {"schema": 1, "mode": "audit-only", "root": str(physical),
              "facts": [], "commands": [], "executedCommands": [], "suspicions": [],
              "proposedRepairs": [], "unknowns": [
                  "Static filesystem evidence does not verify runtime behavior, tests, dependencies or security.",
                  "Repair requires the original owner's repair request and existing host permissions."],
              "repair": {"authorized": False, "requiresOwnerRepairRequest": True},
              "limits": {**options, "timeout": timeout}, "omissions": [],
              "complete": True, "observed": {"entries": 0, "bytes": 0}}
    omitted_count = 0
    def omit(name, reason):
        nonlocal omitted_count
        result["complete"] = False
        omitted_count += 1
        if len(result["omissions"]) < 80:
            result["omissions"].append({"path": name, "reason": reason})

    def expired():
        return time.monotonic() >= deadline

    def record(name, metadata, opener):
        if not stat.S_ISREG(metadata.st_mode):
            omit(name, "special file or link excluded")
            return
        kind = _ordinary_kind(name)
        if kind is None:
            omit(name, "sensitive, private, media or opaque file type/name excluded")
            return
        if metadata.st_size > max_file_bytes:
            omit(name, "per-file byte budget")
            return
        remaining = max_total_bytes - result["observed"]["bytes"]
        if metadata.st_size > remaining:
            omit(name, "total byte budget")
            return
        descriptor = None
        try:
            descriptor = opener()
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)):
                omit(name, "file identity changed before reading")
                return
            chunks, count = [], 0
            while count <= min(max_file_bytes, remaining):
                if expired():
                    omit(name, "deadline while reading")
                    return
                chunk = os.read(descriptor, min(65536, min(max_file_bytes, remaining) - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                result["observed"]["bytes"] += len(chunk)
                if count > min(max_file_bytes, remaining):
                    omit(name, "file grew beyond byte budget")
                    return
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                omit(name, "file changed during read")
                return
            data = b"".join(chunks)
        except (OSError, ValueError):
            omit(name, "permission or filesystem read failure")
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)
        fact = {"path": name, "kind": kind, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        result["facts"].append(fact)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            fact["kind"] = "uninterpreted"
            omit(name, "non-UTF-8 content excluded from interpretation")
            return
        if any((ord(char) < 32 and char not in "\n\r\t\f") or 127 <= ord(char) <= 159 for char in text):
            fact["kind"] = "uninterpreted"
            omit(name, "binary/control content excluded from interpretation")
            return
        if discovery.SECRET_CONTENT.search(text):
            omit(name, "suspected inline secret: excerpt, commands and findings omitted")
            return
        fact["excerpt"] = text[:max_excerpt_chars]
        if len(text) > max_excerpt_chars:
            omit(name, "excerpt character budget")
        for command in discovery._commands(name, text):
            if len(result["commands"]) >= discovery.MAX_COMMANDS:
                omit(name, "command presentation budget")
                break
            result["commands"].append({"path": name, **command, "executed": False})
        if name.endswith(".py"):
            try:
                ast.parse(text, filename=name)
            except (SyntaxError, ValueError, RecursionError):
                finding = {"path": name, "reason": "Possible Python syntax error; static inspection only"}
                result["suspicions"].append(finding)
                result["proposedRepairs"].append({**finding, "requiresOwnerRepairRequest": True})

    descriptor_mode = (os.open in os.supports_dir_fd and os.scandir in os.supports_fd
                       and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))
    if not descriptor_mode:
        result["unknowns"].append("Host lacks descriptor-relative traversal; concurrent hostile tree replacement is outside this static audit guarantee.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    frames = []
    def enter(path, prefix, depth, parent_fd=None, child=None):
        fd = None
        try:
            if descriptor_mode:
                fd = os.open(child if parent_fd is not None else path,
                             flags | os.O_DIRECTORY, **({"dir_fd": parent_fd} if parent_fd is not None else {}))
                iterator = os.scandir(fd)
            else:
                if is_link(path) or not path.is_dir() or not path.resolve().is_relative_to(physical):
                    omit(prefix, "link or escape excluded")
                    return
                iterator = os.scandir(path)
            frames.append((path, prefix, depth, fd, iterator))
        except OSError:
            if fd is not None:
                os.close(fd)
            omit(prefix, "permission or directory access failure")
    enter(physical, "", 0)
    try:
        while frames:
            if expired():
                omit("", "deadline traversal budget")
                break
            if result["observed"]["entries"] >= max_entries:
                omit("", "entry traversal budget")
                break
            path, prefix, depth, fd, iterator = frames[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                iterator.close()
                if fd is not None:
                    os.close(fd)
                frames.pop()
                continue
            except OSError:
                omit(prefix, "directory enumeration failed")
                break
            result["observed"]["entries"] += 1
            name = prefix + "/" + entry.name if prefix else entry.name
            child = path / entry.name
            if (entry.name.startswith(".") or entry.name.lower() in discovery.SKIP_DIRS
                    or discovery.SECRET_NAME.search(entry.name)):
                omit(name, "hidden, generated, private or sensitive path excluded")
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
                if not descriptor_mode:
                    # Windows DirEntry.stat has zero device/inode fields.
                    # A fresh nofollow stat can be compared to the opened fd.
                    metadata = os.stat(child, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or (not descriptor_mode and is_link(child)):
                    omit(name, "link or reparse point excluded")
                elif stat.S_ISDIR(metadata.st_mode):
                    if depth >= max_depth:
                        omit(name, "depth traversal budget")
                    else:
                        enter(child, name, depth + 1, fd, entry.name)
                else:
                    if descriptor_mode:
                        record(name, metadata, lambda n=entry.name, parent=fd: os.open(n, flags, dir_fd=parent))
                    else:
                        record(name, metadata, lambda p=child: os.open(p, flags))
            except OSError:
                omit(name, "permission or metadata read failure")
    finally:
        for _, _, _, fd, iterator in reversed(frames):
            iterator.close()
            if fd is not None:
                os.close(fd)
    result["facts"].sort(key=lambda row: row["path"])
    result["omittedCount"] = omitted_count
    return result
