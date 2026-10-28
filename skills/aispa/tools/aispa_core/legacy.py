"""A run of AISpa 0.x in a project (.aispa/), read only.

0.x kept one journal per run: .aispa/active.json names the run, every line of
.aispa/runs/<run>/events.jsonl carries the whole state, and init copied the
package that started the run into .aispa/runs/<run>/runtime/ — that copy can
still finish the run after the installed skill moved on to 1.x.
Nothing here writes, moves or deletes anything.
"""
import json
from pathlib import Path

FINISHED = ("accepted", "delivered_with_remaining", "cancelled")
TAIL = 1 << 20  # last lines are enough; a state line is far below a megabyte


def _last_state(journal):
    """State of the last complete line (a crash may leave a torn last line)."""
    with journal.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL))
        lines = f.read().splitlines()
    if size > TAIL:
        lines = lines[1:]  # the first one is cut by the seek
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("state"), dict):
            return row["state"]
    return None


def old_run(root):
    """None when the project has no .aispa/; otherwise what a 1.x agent needs to decide."""
    home = Path(root) / ".aispa"
    if not home.is_dir():
        return None
    info = {"path": str(home), "run": None, "status": None, "stage": None, "title": None,
            "version": None, "finished": None, "skill": None, "cli": None}
    try:
        run = json.loads((home / "active.json").read_text(encoding="utf-8")).get("run")
    except (OSError, ValueError, AttributeError):
        return info
    if not isinstance(run, str) or not run or "/" in run or "\\" in run or run.startswith("."):
        return info
    info["run"] = run
    folder = home / "runs" / run
    try:
        state = _last_state(folder / "events.jsonl")
    except OSError:
        state = None
    if state:
        for key in ("status", "stage", "title", "version"):
            value = state.get(key)
            info[key] = value if isinstance(value, str) else None
        if info["status"]:
            info["finished"] = info["status"] in FINISHED
    runtime = folder / "runtime"
    if (runtime / "SKILL.md").is_file() and (runtime / "tools" / "aispa.py").is_file():
        info["skill"] = str(runtime / "SKILL.md")
        info["cli"] = str(runtime / "tools" / "aispa.py")
    return info
