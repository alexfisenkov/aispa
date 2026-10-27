#!/usr/bin/env python3
"""Measurement helpers for the AISpa 0.5.0 build. No model is ever started here.

    measure.py pair --brief F --start-tree D --out D --aispa-cmd CMD --autopilot-cmd CMD [--runtime R] [--limit-seconds N]
    measure.py compare --pair pair.json [--blind verdict.json]
    measure.py blind-package --repo D --commit SHA --brief F [--brief F ...] --out D
    measure.py integrity snapshot --dir D --out FILE | integrity verify --dir D --snapshot FILE

`pair` runs two commands one after another, each inside its own temporary git
repository with the same starting tree. A command receives AISPA_MEASURE_BRIEF,
AISPA_MEASURE_REPO and AISPA_MEASURE_EVENTS (a JSONL file where it, or its
wrapper, appends {"event": "question"|"stop"}). Tokens come only from the JSON
the environment returned on stdout; anything else is "unknown", never an estimate.
Standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SKILLS = ("aispa", "autopilot")
FORBIDDEN_NAMES = ("spec.md", "manifest.md", "interfaces.md")
HIDDEN_DIR = ".autopilot"
TOKEN_FIELDS = (("input_tokens", "input"), ("output_tokens", "output"),
                ("cache_creation_input_tokens", "cacheCreation"), ("cache_read_input_tokens", "cacheRead"))
MAX_STDOUT_BYTES = 4 * 1024 * 1024
UNKNOWN = "unknown"
WORSE, NOT_WORSE, NO_DATA = "хуже", "не хуже", "нет данных"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=measure", "-c", "user.email=measure@localhost",
                           *args], capture_output=True, text=True, check=check)


# --- pair ------------------------------------------------------------------

def parse_tokens(text):
    """Sum of the environment's own usage numbers, or "unknown". Nothing is estimated."""
    usages = []
    try:
        whole = json.loads(text)
        items = [whole] if isinstance(whole, dict) else []
    except ValueError:
        items = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                items.append(value)
    with_usage = [item for item in items if isinstance(item.get("usage"), dict)]
    turns = [item for item in with_usage if item.get("type") == "turn.completed"]
    usages = [item["usage"] for item in (turns or with_usage[-1:])]
    totals = {}
    for usage in usages:
        for source, name in TOKEN_FIELDS:
            value = usage.get(source)
            if type(value) is int and value >= 0:
                totals[name] = totals.get(name, 0) + value
    return {**totals, "total": sum(totals.values())} if totals else UNKNOWN


def count_events(path):
    """(questions, stops) from the events file; both "unknown" when the command wrote none."""
    if not path.is_file():
        return UNKNOWN, UNKNOWN
    names = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            names.append(json.loads(line).get("event"))
        except (ValueError, AttributeError):
            continue
    return names.count("question"), names.count("stop")


def make_repo(start_tree, parent, skill):
    repo = Path(tempfile.mkdtemp(prefix="measure-%s-" % skill, dir=parent))
    shutil.copytree(start_tree, repo, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start tree", "--allow-empty")
    return repo, git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def run_command(command, repo, env, limit):
    """(exit code | "not-started", seconds, timed_out, stdout text)."""
    began = time.monotonic()
    try:
        process = subprocess.Popen(shlex.split(command), cwd=repo, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except (OSError, ValueError):
        return "not-started", 0.0, False, ""
    timed_out = False
    try:
        out, _ = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            process.kill()
        out, _ = process.communicate()
    seconds = time.monotonic() - began
    return process.returncode, round(min(seconds, limit) if timed_out else seconds, 3), timed_out, \
        out[:MAX_STDOUT_BYTES].decode("utf-8", errors="replace")


def measure_one(skill, command, args, out, brief_copy):
    work = Path(args.workdir) if args.workdir else out / "work"
    work.mkdir(parents=True, exist_ok=True)
    repo, tree = make_repo(Path(args.start_tree), work, skill)
    events = out / ("events-%s.jsonl" % skill)
    env = {**os.environ, "AISPA_MEASURE_BRIEF": str(brief_copy), "AISPA_MEASURE_REPO": str(repo),
           "AISPA_MEASURE_EVENTS": str(events)}
    code, seconds, timed_out, stdout = run_command(command, repo, env, args.limit_seconds)
    (out / ("%s-stdout.txt" % skill)).write_text(stdout, encoding="utf-8")
    questions, stops = count_events(events)
    return {"skill": skill, "runtime": args.runtime, "repo": str(repo), "startTree": tree, "exitCode": code,
            "seconds": seconds, "timedOut": timed_out, "questions": questions, "stops": stops,
            "tokens": parse_tokens(stdout)}


def pair(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    brief_copy = out / "brief.md"
    shutil.copyfile(args.brief, brief_copy)
    record = {"brief": {"sha256": sha256_file(brief_copy)}, "limitSeconds": args.limit_seconds}
    for skill, command in zip(SKILLS, (args.aispa_cmd, args.autopilot_cmd)):
        record[skill] = measure_one(skill, command, args, out, brief_copy)
    record["sameStartTree"] = record["aispa"]["startTree"] == record["autopilot"]["startTree"]
    (out / "pair.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({skill: {k: record[skill][k] for k in ("exitCode", "seconds", "timedOut", "questions", "stops")}
                      for skill in SKILLS}, ensure_ascii=False))
    return 0 if record["sameStartTree"] else 1


# --- compare ---------------------------------------------------------------

def _row(axis, mine, theirs, worse):
    if mine is None or theirs is None:
        return {"axis": axis, "aispa": mine, "autopilot": theirs, "verdict": NO_DATA}
    return {"axis": axis, "aispa": mine, "autopilot": theirs, "verdict": WORSE if worse(mine, theirs) else NOT_WORSE}


def _interruptions(side):
    values = (side.get("questions"), side.get("stops"))
    return sum(values) if all(type(v) is int for v in values) else None


def _tokens(side):
    tokens = side.get("tokens")
    return tokens.get("total") if isinstance(tokens, dict) and type(tokens.get("total")) is int else None


def compare_axes(record, blind):
    mine, theirs = record["aispa"], record["autopilot"]
    def unmet(who):
        value = ((blind or {}).get(who) or {}).get("unmet")
        return value if type(value) is int else None
    slower = lambda a, b: (bool(mine.get("timedOut")) and not theirs.get("timedOut")) or (
        bool(mine.get("timedOut")) == bool(theirs.get("timedOut")) and a > b)
    return [_row("brief", unmet("aispa"), unmet("autopilot"), lambda a, b: a > b),
            _row("interruptions", _interruptions(mine), _interruptions(theirs), lambda a, b: a > b),
            _row("time", mine.get("seconds"), theirs.get("seconds"), slower),
            _row("tokens", _tokens(mine), _tokens(theirs), lambda a, b: a > b)]


def verdict(axes):
    by_axis = {row["axis"]: row["verdict"] for row in axes}
    if WORSE in by_axis.values():
        return "не выполнено"
    required = ("brief", "interruptions", "time")
    return "выполнено" if all(by_axis[a] == NOT_WORSE for a in required) else "нет данных"


def compare(args):
    record = json.loads(Path(args.pair).read_text(encoding="utf-8"))
    blind = json.loads(Path(args.blind).read_text(encoding="utf-8")) if args.blind else None
    axes = compare_axes(record, blind)
    result = {"axes": axes, "worse": [row for row in axes if row["verdict"] == WORSE], "r02": verdict(axes)}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["r02"] == "выполнено" else 1


# --- blind package -----------------------------------------------------------

class Refused(Exception):
    pass


def leaked(path):
    parts = Path(path).parts
    return HIDDEN_DIR in parts or (len(parts) == 1 and parts[0] in FORBIDDEN_NAMES)


def clone_commit(repo, commit, target):
    sha = git(repo, "rev-parse", "--verify", "--quiet", commit + "^{commit}", check=False).stdout.strip()
    if not sha:
        raise Refused("Коммит не найден в репозитории")
    git(target.parent, "init", "-q", "-b", "main", str(target))
    fetched = git(target, "fetch", "-q", "--depth", "1", "file://" + str(Path(repo).resolve()), sha, check=False)
    if fetched.returncode:
        raise Refused("Не удалось получить коммит одним снимком")
    git(target, "checkout", "-q", "--detach", "FETCH_HEAD")
    (target / ".git" / "FETCH_HEAD").unlink(missing_ok=True)  # It names the source path.
    bad = [p for p in git(target, "ls-files").stdout.splitlines() if leaked(p)]
    if bad:
        raise Refused("Коммит содержит материалы плана сборки, судье их показывать нельзя")


def copy_briefs(files, target):
    target.mkdir()
    for name in files:
        source = Path(name)
        if not source.is_file() or source.name in FORBIDDEN_NAMES or (target / source.name).exists():
            raise Refused("Файл брифа не подходит: нужен существующий файл, не из плана сборки, имена без повторов")
        shutil.copyfile(source, target / source.name)


def blind_package(args):
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print("Папка судьи уже занята: %s" % out.name)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".judge-", dir=out.parent))
    try:
        clone_commit(args.repo, args.commit, staging / "repo")
        copy_briefs(args.brief, staging / "brief")
        found = [p for p in staging.rglob("*") if ".git" not in p.relative_to(staging).parts and (
            p.name == HIDDEN_DIR or p.name in FORBIDDEN_NAMES and p.parent in (staging, staging / "brief", staging / "repo"))]
        if found:
            raise Refused("В папке судьи оказались материалы плана сборки")
        if out.exists():
            out.rmdir()
        os.replace(staging, out)
    except Refused as error:
        shutil.rmtree(staging, ignore_errors=True)
        print(str(error))
        return 1
    except subprocess.CalledProcessError as error:
        shutil.rmtree(staging, ignore_errors=True)
        print("Git не смог подготовить пакет (команда: %s)" % " ".join(error.cmd[5:7]))
        return 1
    print(json.dumps({"repo": "repo", "brief": sorted(p.name for p in (out / "brief").iterdir())}))
    return 0


# --- integrity ---------------------------------------------------------------

def scan(directory):
    files, base = {}, Path(directory)
    for root, dirs, names in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            path = Path(root) / name
            rel = path.relative_to(base).as_posix()
            if path.is_symlink():
                files[rel] = {"link": os.readlink(path)}
            elif path.is_file():
                files[rel] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return {"schema": 1, "fileCount": len(files), "files": files, "events": files.get("events.jsonl")}


def integrity(args):
    directory = Path(args.dir)
    if not directory.is_dir():
        print("Каталог не найден")
        return 2
    current = scan(directory)
    if args.action == "snapshot":
        Path(args.out).write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({"fileCount": current["fileCount"]}))
        return 0
    old = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))["files"]
    now = current["files"]
    result = {"changed": sorted(k for k in old.keys() & now.keys() if old[k] != now[k]),
              "added": sorted(now.keys() - old.keys()), "removed": sorted(old.keys() - now.keys()),
              "fileCount": current["fileCount"]}
    result["same"] = not (result["changed"] or result["added"] or result["removed"])
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["same"] else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair")
    p.add_argument("--brief", required=True)
    p.add_argument("--start-tree", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--aispa-cmd", required=True)
    p.add_argument("--autopilot-cmd", required=True)
    p.add_argument("--runtime", default=UNKNOWN)
    p.add_argument("--limit-seconds", type=float, default=5400.0)
    p.add_argument("--workdir")
    p.set_defaults(handler=pair)
    c = sub.add_parser("compare")
    c.add_argument("--pair", required=True)
    c.add_argument("--blind")
    c.set_defaults(handler=compare)
    b = sub.add_parser("blind-package")
    b.add_argument("--repo", required=True)
    b.add_argument("--commit", required=True)
    b.add_argument("--brief", action="append", required=True)
    b.add_argument("--out", required=True)
    b.set_defaults(handler=blind_package)
    i = sub.add_parser("integrity")
    i.add_argument("action", choices=("snapshot", "verify"))
    i.add_argument("--dir", required=True)
    i.add_argument("--out")
    i.add_argument("--snapshot")
    i.set_defaults(handler=integrity)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "integrity" and (args.out if args.action == "snapshot" else args.snapshot) is None:
        build_parser().error("integrity %s требует %s" % (args.action, "--out" if args.action == "snapshot" else "--snapshot"))
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
