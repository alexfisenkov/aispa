"""Durations of finished tasks and stages from earlier runs on this computer.

The file holds numbers and labels only: kind, class, seconds, runtime, date.
It lives outside any repository. A broken, foreign or unreadable file is never
fatal: it is not used, a note says so, and writing is skipped.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile
import time

SCHEMA = 1
MAX_PER_CLASS = 200
KINDS = ("task", "stage")
FIELDS = ("kind", "class", "seconds", "runtime", "recordedAt")
MAX_SECONDS = 30 * 24 * 3600
MAX_FILE_BYTES = 4 * 1024 * 1024
LOCK_WAIT_SECONDS = 5.0


def path():
    configured = os.environ.get("AISPA_CALIBRATION_FILE")
    if configured:
        return Path(configured)
    return Path(os.path.expanduser("~")) / ".aispa" / "calibration.json"


def _in_temp_dir(root):
    """True when the project root lies inside the system temp directory (symlinks resolved on both sides)."""
    try:
        return Path(root).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _default_path_forbidden(root):
    """A run living in the temp dir (tests, scratch work) must not write the user's own ~/.aispa file.

    An explicit AISPA_CALIBRATION_FILE is always honoured; an unknown root is not a reason to refuse.
    """
    return root is not None and not os.environ.get("AISPA_CALIBRATION_FILE") and _in_temp_dir(root)


def valid(row):
    """Exactly the allowed fields with sane values; extra keys are not valid input here."""
    if not isinstance(row, dict) or any(key not in row for key in FIELDS):
        return False
    seconds = row["seconds"]
    if row["kind"] not in KINDS or type(seconds) not in (int, float) or not math.isfinite(seconds):
        return False
    if not 0 < seconds <= MAX_SECONDS:
        return False
    for key in ("class", "runtime", "recordedAt"):
        if not isinstance(row[key], str) or not row[key] or len(row[key]) > 64:
            return False
    try:
        datetime.fromisoformat(row["recordedAt"])
    except ValueError:
        return False
    return True


def _clean(row):
    return {key: row[key] for key in FIELDS}


def _read():
    """(rows, notes, writable). A missing file is normal; anything else odd is a note."""
    target = path()
    try:
        if not target.exists():
            return [], [], True
        if target.stat().st_size > MAX_FILE_BYTES:
            return [], ["Файл калибровки слишком велик; прошлые замеры не используются"], False
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return [], ["Файл калибровки не читается или повреждён; прошлые замеры не используются"], False
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("samples"), list):
        return [], ["Файл калибровки другой версии; прошлые замеры не используются"], False
    return [_clean(row) for row in value["samples"] if valid(row)], [], True


def inspect():
    """Samples plus human-readable warnings; never raises."""
    rows, notes, _ = _read()
    return {"samples": rows, "notes": notes}


def load():
    return _read()[0]


@contextmanager
def _locked(target):
    """Best-effort exclusive lock beside the file; the replace below is atomic anyway."""
    lock = target.with_name(target.name + ".lock")
    handle = open(lock, "a+")
    try:
        try:
            import fcntl
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
        except ImportError:
            pass
        yield
    finally:
        handle.close()


def record(sample, root=None):
    """Append one sample; True only when it was durably written.

    `root` is the project root of the run the sample comes from: when it lies in the system
    temp directory and no AISPA_CALIBRATION_FILE is set, nothing is written (and nothing is created).
    """
    if not valid(sample) or _default_path_forbidden(root):
        return False
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _locked(target):
            rows, notes, writable = _read()
            if not writable:
                return False
            rows.append(_clean(sample))
            kept, counts = [], {}
            for row in reversed(rows):
                key = (row["kind"], row["class"])
                counts[key] = counts.get(key, 0) + 1
                if counts[key] <= MAX_PER_CLASS:
                    kept.append(row)
            kept.reverse()
            descriptor, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"schema": SCHEMA, "samples": kept}, stream, ensure_ascii=False)
                os.replace(name, target)
            except BaseException:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
        return True
    except OSError:
        return False
