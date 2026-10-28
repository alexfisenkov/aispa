"""Two helpers the release updater needs from the AISpa 0.x store.

Only Invalid and atomic_json are kept, unchanged from AISpa 0.4.0: updates.py
imports them, and the 0.x first installer loads updates.py from a release tag.
"""
import json
import os
from pathlib import Path
import tempfile


class Invalid(ValueError):
    pass


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".aispa-write-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
