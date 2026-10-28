#!/usr/bin/env python3
"""Parity measurement of AISpa 1.0 against Autopilot 2.2.0. No model is ever started here.

    measure.py pair --brief F --start-tree D --out D --aispa-cmd CMD --autopilot-cmd CMD [--runtime R]
                    [--mode run|continue] [--install project|global] [--request F] [--first aispa|autopilot]
                    [--autopilot-sha SHA] [--autopilot-src D] [--limit-seconds N]
                    [--codex-sessions D] [--claude-projects D]
    measure.py compare --pair pair.json [--pair ...] [--blind verdict.json [--blind ...]]
    measure.py blind-package --repo D --commit SHA --brief F [--brief F ...] --out D
    measure.py integrity snapshot --dir D --out FILE | integrity verify --dir D --snapshot FILE

`pair` runs two commands one after another, each inside its own temporary git repository with the
same starting tree. Both get the same request text and the same install method; Autopilot must be
the pinned commit. A command receives AISPA_MEASURE_BRIEF, _REQUEST, _REPO, _EVENTS, _FINAL, _INSTALL,
_MODE and _AUTOPILOT_SHA. Its wrapper appends {"event": "question"|"stop"|"intervention"} to the
events file and writes the final message to the _FINAL file. "Delivered" is a run folder without
`--wip` in the side's own work folder plus a final message. Tokens, models and cost come only from
what the environment itself reported (stdout, Codex rollout files, Claude Code logs); anything else
is "unknown", never an estimate.

`compare` takes the judge's verdict per pair ({"aispa": {"unmet", "substitutions", "additions"},
"autopilot": {...}}), groups pairs by environment, mode and brief, and gives each axis one of
"наравне" / "лучше" / "хуже" / "нет данных". Time and tokens are on par up to PARITY times Autopilot.
Standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

SKILLS = ("aispa", "autopilot")
WORK_DIRS = {"aispa": ".aispa-work", "autopilot": ".autopilot"}
PINNED_AUTOPILOT = "25b5fba"
PARITY = 1.2
FORBIDDEN_NAMES = ("spec.md", "manifest.md", "interfaces.md")
HIDDEN_DIRS = (".autopilot", ".aispa-work")
BANNED_EFFORT = ("xhigh", "max", "ultra")
SCRIPTED_MECHANIC = "mechanic-scripted"
TOKEN_FIELDS = (("input_tokens", "input"), ("output_tokens", "output"),
                ("cache_creation_input_tokens", "cacheCreation"), ("cache_read_input_tokens", "cacheRead"))
# Claude Code `modelUsage`: one entry per model, subagents included (top-level `usage` is the main chat only).
MODEL_USAGE_FIELDS = (("inputTokens", "input"), ("outputTokens", "output"),
                      ("cacheCreationInputTokens", "cacheCreation"), ("cacheReadInputTokens", "cacheRead"))
MAX_STDOUT_BYTES = 4 * 1024 * 1024
UNKNOWN = "unknown"
SAME, BETTER, WORSE, NO_DATA = "наравне", "лучше", "хуже", "нет данных"
LIMIT_WORDS = re.compile(r"\b429\b|rate.?limit|usage limit|limit (?:reached|exceeded)|too many requests|quota", re.I)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=measure", "-c", "user.email=measure@localhost",
                           *args], capture_output=True, text=True, check=check)


# --- what the environment reported ------------------------------------------

def _json_items(text):
    """Objects the environment printed: one JSON document (object or list) or one per line."""
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
        items = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            items.extend(value if isinstance(value, list) else [value])
    else:
        items = whole if isinstance(whole, list) else [whole]
    return [item for item in items if isinstance(item, dict)]


def _add(totals, usage, fields):
    for source, name in fields:
        value = usage.get(source)
        if type(value) is int and value >= 0:
            totals[name] = totals.get(name, 0) + value


def _codex_session_usages(items):
    """`usage` of the last `turn.completed` of every Codex session, in order of first appearance.

    A `turn.completed` belongs to the session named by the last `thread.started` (`thread_id`)
    before it. A continued session (`codex exec resume <thread_id>`) prints that same id at the start
    of every turn, and each of its `turn.completed` lines carries the running total of the whole
    session, so the last line of a session is its total. A `turn.completed` with no usable
    `thread_id` before it is its own session, so such lines are summed.
    """
    last_of_session = {}
    thread = None
    for index, item in enumerate(items):
        kind = item.get("type")
        if kind == "thread.started":
            value = item.get("thread_id")
            thread = value if isinstance(value, str) and value else None
        elif kind == "turn.completed" and isinstance(item.get("usage"), dict):
            last_of_session[thread if thread is not None else ("no-thread", index)] = item["usage"]
    return list(last_of_session.values())


def _last_answer_per_session(items, key):
    """Last answer of each Claude Code session that carries `key` (running totals are cumulative)."""
    last = {}
    for index, item in enumerate(items):
        if key in item:
            session = item.get("session_id")
            last[session if isinstance(session, str) and session else ("no-session", index)] = item
    return list(last.values())


def parse_tokens(text):
    """Sum of the environment's own usage numbers, or "unknown". Nothing is estimated.

    Codex: the last `turn.completed` of each session (`thread_id`); sessions are summed.
    Claude Code: `modelUsage` (all models, subagents included) of the last answer of each session
    (`session_id`); sessions are summed. Without `modelUsage` every `result` answer's top-level
    `usage` is summed, otherwise the last top-level `usage`.
    """
    items = _json_items(text)
    totals = {}
    turns = _codex_session_usages(items)
    answers = [item for item in items if isinstance(item.get("modelUsage"), dict)]
    if turns:
        for usage in turns:
            _add(totals, usage, TOKEN_FIELDS)
    elif answers:
        for item in _last_answer_per_session(answers, "modelUsage"):
            for usage in item["modelUsage"].values():
                if isinstance(usage, dict):
                    _add(totals, usage, MODEL_USAGE_FIELDS)
    else:
        with_usage = [item for item in items if isinstance(item.get("usage"), dict)]
        results = [item for item in with_usage if item.get("type") == "result"]
        for item in results or with_usage[-1:]:
            _add(totals, item["usage"], TOKEN_FIELDS)
    return {**totals, "total": sum(totals.values())} if totals else UNKNOWN


def parse_cost(text):
    """`total_cost_usd` of the last answer of each Claude Code session, added up; "unknown" if none."""
    answers = [i for i in _json_items(text) if type(i.get("total_cost_usd")) in (int, float)]
    costs = [item["total_cost_usd"] for item in _last_answer_per_session(answers, "total_cost_usd")]
    return round(sum(costs), 6) if costs else UNKNOWN


def limit_hit(text):
    """True when the environment itself reported a rate limit (429 or a limit message on an error line)."""
    for item in _json_items(text):
        nested = item.get("error") if isinstance(item.get("error"), dict) else {}
        for source in (item, nested):
            if any(str(source.get(key)) == "429" for key in ("api_error_status", "status", "status_code", "code")):
                return True
        failed = (item.get("is_error") is True or item.get("type") in ("error", "turn.failed", "stream_error")
                  or str(item.get("subtype", "")).startswith("error"))
        if failed:
            texts = [item.get(key) for key in ("result", "message", "error", "reason")] + [nested.get("message")]
            if any(isinstance(t, str) and LIMIT_WORDS.search(t) for t in texts):
                return True
    return False


def count_events(path):
    """(questions, stops, interventions) from the events file; "unknown" when the command wrote none."""
    if not path.is_file():
        return UNKNOWN, UNKNOWN, UNKNOWN
    names = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            names.append(json.loads(line).get("event"))
        except (ValueError, AttributeError):
            continue
    return names.count("question"), names.count("stop"), names.count("intervention")


def delivered(repo, skill, final_path):
    """A run folder without `--wip` in the side's own work folder, and a final message on disk."""
    work = Path(repo) / WORK_DIRS[skill]
    folder = work.is_dir() and any(p.is_dir() and p.name[:4].isdigit() and not p.name.endswith("--wip")
                                   for p in work.iterdir())
    message = final_path.is_file() and final_path.read_text(encoding="utf-8", errors="replace").strip() != ""
    return bool(folder and message)


# --- subagents: models and tokens from the environment's own logs -----------

def _rollout_lines(path, kinds):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(kind in line for kind in kinds):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _rollout_summary(path):
    """(session_meta payload, [(model, effort)], last total_token_usage) of one Codex rollout file."""
    meta, contexts, usage = {}, [], {}
    for item in _rollout_lines(path, ("session_meta", "turn_context", "token_count")):
        payload = item.get("payload") or {}
        if item.get("type") == "session_meta":
            meta = payload
        elif item.get("type") == "turn_context":
            pair = (payload.get("model"), payload.get("effort"))
            if pair not in contexts:
                contexts.append(pair)
        elif payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
            usage = payload["info"].get("total_token_usage") or usage
    return meta, contexts, usage


def _parent_of(meta):
    spawn = ((meta.get("source") or {}).get("subagent") or {}).get("thread_spawn") or {}
    return meta.get("parent_thread_id") or spawn.get("parent_thread_id")


def codex_subagents(root, main_threads, since):
    """Rollout files of every thread spawned (directly or not) by the main threads.

    None when no rollout of a main thread is found: then the subagents cannot be told apart from
    "none were started", and nothing is claimed.
    """
    found = {}
    for path in Path(root).rglob("rollout-*.jsonl") if Path(root).is_dir() else ():
        try:
            if path.stat().st_mtime >= since:
                meta, contexts, usage = _rollout_summary(path)
                if meta.get("id"):
                    found[meta["id"]] = (meta, contexts, usage)
        except OSError:
            continue
    if not any(thread in found for thread in main_threads):
        return None
    family, grew = set(main_threads), True
    while grew:
        grew = False
        for thread, (meta, _, _) in found.items():
            if thread not in family and _parent_of(meta) in family:
                family.add(thread)
                grew = True
    totals, launches = {}, []
    for thread in family - set(main_threads):
        meta, contexts, usage = found[thread]
        _add(totals, usage, TOKEN_FIELDS)
        for model, effort in contexts or [(None, None)]:
            launches.append({"agent": meta.get("agent_role"), "path": meta.get("agent_path"),
                             "model": model, "effort": effort or UNKNOWN})
    return {"tokens": totals, "launches": launches}


def claude_subagents(root, sessions):
    """Launches of every subagent of the given Claude Code sessions; None if a session log is not found."""
    launches = []
    for session in sessions:
        if not (list(Path(root).glob("*/%s.jsonl" % session)) or list(Path(root).glob("*/%s" % session))):
            return None
        for meta_path in sorted(Path(root).glob("*/%s/subagents/*.meta.json" % session)):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            models = []
            log = meta_path.with_name(meta_path.name[:-len(".meta.json")] + ".jsonl")
            for item in _rollout_lines(log, ('"model"',)) if log.is_file() else ():
                model = (item.get("message") or {}).get("model")
                if model and model != "<synthetic>" and model not in models:
                    models.append(model)
            for model in models or [meta.get("model")]:
                launches.append({"agent": meta.get("agentType"), "path": meta.get("description"),
                                 "model": model, "effort": meta.get("effort") or UNKNOWN})
    return launches


def violations(launches):
    """Launches against the model rules: no Fable, Astra, xhigh, max, ultra; Haiku or Luna only scripted mechanics."""
    found = []
    for launch in launches:
        model, effort = str(launch.get("model") or "").lower(), str(launch.get("effort") or "").lower()
        role = " ".join(str(launch.get(key) or "") for key in ("agent", "path")).lower()
        reasons = (["model"] if re.search(r"fable|astra", model) else []) + (["effort"] if effort in BANNED_EFFORT else []) + (
            ["cheap-model"] if re.search(r"haiku|luna", model) and SCRIPTED_MECHANIC not in role else [])
        if reasons:
            found.append({**launch, "reasons": reasons})
    return found


def models_record(launches, source):
    if launches is None:
        return {"source": UNKNOWN, "launches": UNKNOWN, "violations": UNKNOWN}
    return {"source": source, "launches": len(launches), "violations": violations(launches)}


def side_logs(text, args, since):
    """(tokens, models) of one side: the stdout numbers plus the subagents the environment logged."""
    tokens, items = parse_tokens(text), _json_items(text)
    threads = [i["thread_id"] for i in items if i.get("type") == "thread.started" and isinstance(i.get("thread_id"), str)]
    sessions = sorted({i["session_id"] for i in items if isinstance(i.get("modelUsage"), dict)
                       and isinstance(i.get("session_id"), str)})
    if threads:
        subs = codex_subagents(args.codex_sessions, threads, since)
        if subs is None or tokens == UNKNOWN:
            return UNKNOWN, models_record(None, "")
        merged = {k: v for k, v in tokens.items() if k != "total"}
        for name, value in subs["tokens"].items():
            merged[name] = merged.get(name, 0) + value
        return {**merged, "total": sum(merged.values())}, models_record(subs["launches"], "codex-rollouts")
    if sessions:
        return tokens, models_record(claude_subagents(args.claude_projects, sessions), "claude-logs")
    return tokens, models_record(None, "")


# --- pair --------------------------------------------------------------------

def make_repo(start_tree, parent, skill):
    repo = Path(tempfile.mkdtemp(prefix="measure-%s-" % skill, dir=parent))
    shutil.copytree(start_tree, repo, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start tree", "--allow-empty")
    return repo, git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def run_command(command, repo, env, limit):
    """(exit code | "not-started", seconds, timed_out, whole stdout bytes).

    Tokens are counted on the whole stdout; only the saved copy is cut to MAX_STDOUT_BYTES.
    """
    began = time.monotonic()
    try:
        process = subprocess.Popen(shlex.split(command), cwd=repo, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except (OSError, ValueError):
        return "not-started", 0.0, False, b""
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
    return process.returncode, round(min(seconds, limit) if timed_out else seconds, 3), timed_out, out or b""


def measure_one(skill, command, args, out, shared):
    work = Path(args.workdir) if args.workdir else out / "work"
    work.mkdir(parents=True, exist_ok=True)
    repo, tree = make_repo(Path(args.start_tree), work, skill)
    events, final = out / ("events-%s.jsonl" % skill), out / ("final-%s.txt" % skill)
    events.unlink(missing_ok=True)  # Leftovers of an earlier run in the same folder must not count.
    final.unlink(missing_ok=True)
    env = {**os.environ, "AISPA_MEASURE_BRIEF": str(shared["brief"]), "AISPA_MEASURE_REQUEST": str(shared["request"]),
           "AISPA_MEASURE_REPO": str(repo), "AISPA_MEASURE_EVENTS": str(events), "AISPA_MEASURE_FINAL": str(final),
           "AISPA_MEASURE_INSTALL": args.install, "AISPA_MEASURE_MODE": args.mode,
           "AISPA_MEASURE_AUTOPILOT_SHA": shared["autopilotSha"]}
    started = time.time()
    code, seconds, timed_out, stdout = run_command(command, repo, env, args.limit_seconds)
    (out / ("%s-stdout.txt" % skill)).write_bytes(stdout[:MAX_STDOUT_BYTES])
    text = stdout.decode("utf-8", errors="replace")
    questions, stops, interventions = count_events(events)
    tokens, models = side_logs(text, args, started - 5)
    return {"skill": skill, "runtime": args.runtime, "repo": str(repo), "startTree": tree, "exitCode": code,
            "startedAt": started, "seconds": seconds, "timedOut": timed_out, "questions": questions, "stops": stops,
            "interventions": interventions, "tokens": tokens, "costUsd": parse_cost(text), "models": models,
            "rateLimited": limit_hit(text), "delivered": delivered(repo, skill, final)}


def refuse_other_autopilot(args):
    """Why this Autopilot is not the pinned commit, or None when it is."""
    sha = args.autopilot_sha.lower()
    if not (re.fullmatch("[0-9a-f]+", sha) and len(sha) in (len(PINNED_AUTOPILOT), 40) and sha.startswith(PINNED_AUTOPILOT)):
        return "Autopilot берётся только с коммита %s, а указан %s" % (PINNED_AUTOPILOT, args.autopilot_sha)
    if args.autopilot_src:
        head = git(args.autopilot_src, "rev-parse", "HEAD", check=False).stdout.strip()
        if not head.startswith(PINNED_AUTOPILOT):
            return "Копия Autopilot стоит не на коммите %s" % PINNED_AUTOPILOT
    return None


def pair(args):
    why = refuse_other_autopilot(args)
    if why:
        print(why)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    brief_copy, request_copy = out / "brief.md", out / "request.txt"
    shutil.copyfile(args.brief, brief_copy)
    shutil.copyfile(args.request or args.brief, request_copy)
    shared = {"brief": brief_copy, "request": request_copy, "autopilotSha": args.autopilot_sha.lower()}
    commands = {"aispa": args.aispa_cmd, "autopilot": args.autopilot_cmd}
    order = [args.first] + [s for s in SKILLS if s != args.first]
    record = {"runtime": args.runtime, "mode": args.mode, "install": args.install, "order": order,
              "autopilotSha": shared["autopilotSha"], "brief": {"sha256": sha256_file(brief_copy)},
              "request": {"sha256": sha256_file(request_copy)}, "limitSeconds": args.limit_seconds}
    for skill in order:
        record[skill] = measure_one(skill, commands[skill], args, out, shared)
    record["sameStartTree"] = record["aispa"]["startTree"] == record["autopilot"]["startTree"]
    reasons = (["start-tree"] if not record["sameStartTree"] else []) + (
        ["limit"] if record["aispa"]["rateLimited"] or record["autopilot"]["rateLimited"] else [])
    record["valid"], record["invalidReasons"] = not reasons, reasons
    (out / "pair.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"valid": record["valid"], **{skill: {k: record[skill][k] for k in (
        "exitCode", "seconds", "timedOut", "questions", "stops", "interventions", "delivered")} for skill in SKILLS}},
        ensure_ascii=False))
    return 0 if record["valid"] else 1


# --- compare -----------------------------------------------------------------

def _number(value):
    return value if type(value) in (int, float) else None


def _both(valid, mine, theirs):
    """[(aispa value, autopilot value)] of the valid pairs where both sides have a number."""
    rows = [(_number(mine(r)), _number(theirs(r))) for r in valid]
    return [(a, b) for a, b in rows if a is not None and b is not None]


def _row(axis, values, kind="count", forced=False):
    """One axis. count: fewer is better; more: more is better; ratio: PARITY times the other side is on par."""
    if not values:
        return {"axis": axis, "aispa": None, "autopilot": None, "verdict": WORSE if forced else NO_DATA}
    mine, theirs = (statistics.median(column) for column in zip(*values))
    row = {"axis": axis, "aispa": mine, "autopilot": theirs}
    if kind == "ratio":
        if theirs <= 0:
            return {**row, "verdict": NO_DATA}
        row["ratio"] = round(mine / theirs, 3)
        row["verdict"] = WORSE if forced or mine / theirs > PARITY else BETTER if mine / theirs < 1.0 else SAME
    else:
        low, high = (BETTER, WORSE) if kind == "count" else (WORSE, BETTER)
        row["verdict"] = WORSE if forced else low if mine < theirs else high if mine > theirs else SAME
    return row


def _violation_count(record, side):
    found = (record[side].get("models") or {}).get("violations")
    return len(found) if isinstance(found, list) else None


def _models_row(valid):
    mine = [_violation_count(r, "aispa") for r in valid]
    theirs = [n for n in (_violation_count(r, "autopilot") for r in valid) if n is not None]
    known = [n for n in mine if n is not None]
    if sum(known) > 0:
        verdict = WORSE
    elif not valid or None in mine:
        verdict = NO_DATA
    else:
        verdict = SAME
    return {"axis": "models", "aispa": sum(known) if known else None, "autopilot": sum(theirs) if theirs else None,
            "verdict": verdict}


def group_axes(items):
    """Axes of one group; items are (pair record, judge's verdict | None) of its valid pairs."""
    valid = [r for r, _ in items]
    blind = {id(r): b for r, b in items}

    def verdict_of(side, key):
        return lambda r: ((blind.get(id(r)) or {}).get(side) or {}).get(key)

    def judged(key):
        return _both(valid, verdict_of("aispa", key), verdict_of("autopilot", key))

    def per_side(key):
        return _both(valid, lambda r: r["aispa"].get(key), lambda r: r["autopilot"].get(key))

    def stops(side):
        return lambda r: sum(r[side].get(k) for k in ("questions", "stops")) if all(
            type(r[side].get(k)) is int for k in ("questions", "stops")) else None

    def tokens(side):
        return lambda r: (r[side].get("tokens") or {}).get("total") if isinstance(r[side].get("tokens"), dict) else None

    def breached(values):
        # Absolute parity conditions for AISpa — no substituted way, no intervention, a result on every run:
        # one breach in the group is «хуже», whatever the medians say
        return any(type(v) in (int, float) and v > 0 for v in values)

    slower = any(r["aispa"].get("timedOut") and not r["autopilot"].get("timedOut") for r in valid)
    substituted = breached(verdict_of("aispa", "substitutions")(r) for r in valid)
    intervened = breached(r["aispa"].get("interventions") for r in valid)
    undelivered = any(not r["aispa"].get("delivered") for r in valid)
    return [_row("unmet", judged("unmet")), _row("substitutions", judged("substitutions"), forced=substituted),
            _row("delivered", [(int(bool(r["aispa"].get("delivered"))), int(bool(r["autopilot"].get("delivered"))))
                               for r in valid], "more", forced=undelivered),
            _row("interventions", per_side("interventions"), forced=intervened),
            _row("questions_stops", _both(valid, stops("aispa"), stops("autopilot"))),
            _row("additions", judged("additions")),
            _row("time", per_side("seconds"), "ratio", forced=slower),
            _row("tokens", _both(valid, tokens("aispa"), tokens("autopilot")), "ratio"),
            _row("cost", per_side("costUsd"), "ratio"), _models_row(valid)]


def overall(verdicts):
    """Worse anywhere is worse; missing data (cost aside) is no data; better needs one better and none worse."""
    if WORSE in verdicts:
        return WORSE
    if NO_DATA in verdicts:
        return NO_DATA
    return BETTER if BETTER in verdicts else SAME


def compare(args):
    if args.blind and len(args.blind) != len(args.pair):
        print("Вердиктов судьи должно быть столько же, сколько пар: %d, а дано %d" % (len(args.pair), len(args.blind)))
        return 2
    records = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.pair]
    blinds = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.blind] if args.blind else [None] * len(records)
    groups, invalid = {}, []
    for index, (record, blind) in enumerate(zip(records, blinds)):
        key = (record.get("runtime"), record.get("mode"), (record.get("brief") or {}).get("sha256"))
        items = groups.setdefault(key, [])
        if record.get("valid", True):
            items.append((record, blind))
        else:
            invalid.append({"pair": index, "reason": ", ".join(record.get("invalidReasons") or ["invalid"])})
    result_groups = []
    for (runtime, mode, brief), items in groups.items():
        axes = group_axes(items)
        verdicts = [row["verdict"] for row in axes if not (row["axis"] == "cost" and row["verdict"] == NO_DATA)]
        result_groups.append({"runtime": runtime, "mode": mode, "brief": brief, "pairs": len(items), "axes": axes,
                              "verdict": overall(verdicts)})
    result = {"groups": result_groups, "invalid": invalid, "verdict": overall([g["verdict"] for g in result_groups])}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["verdict"] in (SAME, BETTER) else 1


# --- blind package -----------------------------------------------------------

class Refused(Exception):
    pass


def leaked(path):
    parts = Path(path).parts
    return any(part in HIDDEN_DIRS for part in parts) or (len(parts) == 1 and parts[0] in FORBIDDEN_NAMES)


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
            p.name in HIDDEN_DIRS or p.name in FORBIDDEN_NAMES and p.parent in (staging, staging / "brief", staging / "repo"))]
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
    p.add_argument("--mode", choices=("run", "continue"), default="run")
    p.add_argument("--install", choices=("project", "global"), default="project")
    p.add_argument("--request", help="request text for both sides; default is the brief itself")
    p.add_argument("--first", choices=SKILLS, default="aispa")
    p.add_argument("--autopilot-sha", default=PINNED_AUTOPILOT)
    p.add_argument("--autopilot-src")
    p.add_argument("--limit-seconds", type=float, default=5400.0)
    p.add_argument("--codex-sessions", default=str(Path.home() / ".codex" / "sessions"))
    p.add_argument("--claude-projects", default=str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"))
    p.add_argument("--workdir")
    p.set_defaults(handler=pair)
    c = sub.add_parser("compare")
    c.add_argument("--pair", action="append", required=True)
    c.add_argument("--blind", action="append")
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
