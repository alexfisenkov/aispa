"""Work versus idle: a long silence in the journal is work only if project files changed.

An executor can write code for hours without a journal event. So a gap between
two events longer than IDLE_CAP_SECONDS counts as work for at most the cap, or,
when project files (outside .aispa/) changed inside the gap, until the last
change plus the cap. The mark is written once to state["workActivity"].
The currently open gap is checked at most once per REPROBE_SECONDS.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import time

IDLE_CAP_SECONDS = 2700
REPROBE_SECONDS = 300
LIST_TIMEOUT_SECONDS = 20
MAX_FILES = 200000


def parse(value):
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _seconds(start, end):
    return (parse(end) - parse(start)).total_seconds()


def counted_seconds(gap_from, gap_to, last_change_at):
    """Seconds of the gap that count as work."""
    gap = max(0.0, _seconds(gap_from, gap_to))
    if gap <= IDLE_CAP_SECONDS:
        return gap
    changed = _seconds(gap_from, last_change_at) if last_change_at else 0.0
    return min(gap, max(0.0, changed) + IDLE_CAP_SECONDS)


def _project_files(root):
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, timeout=LIST_TIMEOUT_SECONDS)
    if result.returncode:
        raise OSError("git ls-files failed")
    for raw in result.stdout.split(b"\0")[:MAX_FILES]:
        name = os.fsdecode(raw)
        if name and name != ".aispa" and not name.startswith(".aispa/"):
            yield name


def _stat_all(root):
    base = Path(root)
    for name in _project_files(root):
        try:
            yield name, (base / name).stat().st_mtime
        except OSError:
            continue


def latest_change(root, since, until, *, lister=None):
    """ISO time of the newest project-file change after `since` (capped at `until`), else None.

    Raises OSError when the project files cannot be listed: the caller must not
    read that as "nothing changed".
    """
    newest = max((mtime for _, mtime in (lister or _stat_all)(root)), default=None)
    if newest is None:
        return None
    moment = datetime.fromtimestamp(newest, tz=timezone.utc)
    if moment <= parse(since):
        return None
    return min(moment, parse(until)).isoformat()


def make_probe(root, *, lister=None):
    """A probe(since, until) bound to a project root."""
    return lambda since, until: latest_change(root, since, until, lister=lister)


class ThrottledProbe:
    """Asks the wrapped probe at most once per REPROBE_SECONDS; repeats the last answer in between."""

    def __init__(self, probe, clock=time.monotonic):
        self.probe, self.clock, self.asked, self.answer, self.since = probe, clock, None, None, None

    def __call__(self, since, until):
        now = self.clock()
        if self.asked is None or since != self.since or now - self.asked >= REPROBE_SECONDS:
            self.answer, self.asked, self.since = self.probe(since, until), now, since
        return self.answer


def record_gap(state, previous_at, at, probe):
    """Write the mark for a closed long gap. True when a new mark was added."""
    if not previous_at or _seconds(previous_at, at) <= IDLE_CAP_SECONDS:
        return False
    rows = state.get("workActivity")
    if any(isinstance(row, dict) and row.get("gapFrom") == previous_at for row in rows or []):
        return False
    try:
        changed = probe(previous_at, at)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False  # Unknown is not idle: leave the gap counted as work.
    state.setdefault("workActivity", []).append({"gapFrom": previous_at, "gapTo": at, "lastChangeAt": changed})
    return True


def _segment(row):
    try:
        counted = counted_seconds(row["gapFrom"], row["gapTo"], row.get("lastChangeAt"))
        start = parse(row["gapFrom"]) + timedelta(seconds=counted)
        end = parse(row["gapTo"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return (start, end) if end > start else None


def open_gap(state, at, probe=None):
    """The unclosed gap since the last journal event, as a workActivity-shaped row, or None."""
    previous = state.get("updatedAt")
    if (probe is None or not previous or state.get("status") != "running"
            or isinstance(state.get("waiting"), dict) and state["waiting"].get("kind")):
        return None
    try:
        if _seconds(previous, at) <= IDLE_CAP_SECONDS:
            return None
        return {"gapFrom": previous, "gapTo": at, "lastChangeAt": probe(previous, at)}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def uncounted_segments(state, at, probe=None):
    """[(start, end)] of idle time: closed marks plus, with a probe, the open gap."""
    rows = [row for row in state.get("workActivity") or [] if isinstance(row, dict)]
    pending = open_gap(state, at, probe)
    segments = [_segment(row) for row in rows + ([pending] if pending else [])]
    return [segment for segment in segments if segment]


def idle_seconds(state, at, probe=None):
    return sum((end - start).total_seconds() for start, end in uncounted_segments(state, at, probe))


def overlap_seconds(segments, start, end):
    """Seconds of [start, end] covered by the given idle segments."""
    total = 0.0
    for left, right in segments:
        total += max(0.0, (min(right, end) - max(left, start)).total_seconds())
    return total
