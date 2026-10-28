#!/usr/bin/env python3
"""Wrapper for `tools/measure.py pair`: one headless run of a skill to the end.

    measure_wrapper.py --runtime claude|codex --skill aispa|autopilot [--skill-src DIR] [--max-turns 6]
                       [--model M] [--effort E]

The environment comes from measure.py: AISPA_MEASURE_REQUEST (the request text, the same for both
skills), _REPO, _EVENTS, _FINAL, _INSTALL (project|global), _MODE (run|continue), _AUTOPILOT_SHA;
the working directory is the repository. Optional: AISPA_MEASURE_CLAUDE and AISPA_MEASURE_CODEX name
the command-line programs; a name ending in `.py` is started with the current Python (on Windows a
script cannot be started by itself).

Install: "project" copies the skill from --skill-src into the project (both skills the same way; the
AISpa agents for Claude Code too); "global" copies nothing. A skill source that is a git checkout of
Autopilot must stand on the pinned commit, otherwise the wrapper refuses before any turn.

A turn that did not reach the end is a question (the answer ends with "?") or a stop; the wrapper
writes that event, then one "intervention" event for the "continue" it sends in the same chat, as
long as turns remain. The end is a run folder without `--wip` in the skill's own work folder; the
last message is then written to the _FINAL file. stdout holds only the numbers and errors the
environment returned. Nothing is estimated.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

WORK_DIRS = {"aispa": ".aispa-work", "autopilot": ".autopilot"}
PINNED_AUTOPILOT = "25b5fba"
NUDGE = "Продолжай"
MODELS = {"claude": ("claude-opus-5-5", None), "codex": ("gpt-6.1-sol", "high")}
KEPT_CODEX = ("thread.started", "turn.completed", "turn.failed", "error")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=measure", "-c", "user.email=measure@localhost", *args],
                   check=True, capture_output=True)


def skill_homes(runtime, skill):
    return ".claude/skills/" + skill if runtime == "claude" else ".agents/skills/" + skill


def refuse_unpinned(source):
    """Why an Autopilot checkout is not the pinned commit, or None (also when it is not a git checkout)."""
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode == 0 and not head.stdout.strip().startswith(PINNED_AUTOPILOT):
        return "Копия Autopilot стоит не на коммите %s" % PINNED_AUTOPILOT
    return None


def install(repo, runtime, skill, source):
    """The skill from `source` into the project; the AISpa agents for Claude Code beside it."""
    shutil.copytree(source, repo / skill_homes(runtime, skill), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    if runtime == "claude" and skill == "aispa":
        agents = repo / ".claude/agents"
        agents.mkdir(parents=True, exist_ok=True)
        for card in sorted((Path(source) / "agents/claude-code").glob("aispa-*.md")):
            shutil.copy2(card, agents / card.name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "инструменты замера")


def first_prompt(runtime, skill, request, mode):
    """The same text for both skills; only the call name differs. `continue` opens a new chat without the brief."""
    call = ("/" if runtime == "claude" else "$") + skill
    if mode == "continue":
        return (call + " " + request.strip()).strip()
    return call + " full\n\n" + request


def finished(repo, skill):
    work = Path(repo) / WORK_DIRS[skill]
    return work.is_dir() and any(p.is_dir() and p.name[:4].isdigit() and not p.name.endswith("--wip")
                                 for p in work.iterdir())


def keep_line(item):
    return item.get("type") in KEPT_CODEX


def program(variable, default):
    name = os.environ.get(variable, default)
    return [sys.executable, name] if name.lower().endswith(".py") else [name]


def run_turn(runtime, prompt, session, log, model, effort):
    if runtime == "claude":
        cmd = [*program("AISPA_MEASURE_CLAUDE", "claude"), "-p", prompt, "--output-format", "json",
               "--dangerously-skip-permissions", "--model", model]
        cmd += ["--resume", session] if session else []
        out = subprocess.run(cmd, capture_output=True, text=True)
        log.write_text(out.stdout + "\n--- stderr\n" + out.stderr, encoding="utf-8")
        try:
            reply = json.loads(out.stdout)
        except ValueError:
            return session, "", []
        return reply.get("session_id", session), str(reply.get("result") or ""), [json.dumps(reply, ensure_ascii=False)]
    last = log.with_suffix(".last.txt")
    flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-o", str(last),
             "-m", model, "-c", 'model_reasoning_effort="%s"' % effort]
    codex = program("AISPA_MEASURE_CODEX", "codex")
    cmd = [*codex, "exec", "resume", session, prompt, *flags] if session else [*codex, "exec", *flags, prompt]
    out = subprocess.run(cmd, capture_output=True, text=True)
    log.write_text(out.stdout + "\n--- stderr\n" + out.stderr, encoding="utf-8")
    keep = []
    for line in out.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and keep_line(item):
            keep.append(line)
            session = item.get("thread_id", session) if item.get("type") == "thread.started" else session
    text = last.read_text(encoding="utf-8", errors="replace") if last.is_file() else ""
    return session, text, keep


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", choices=("claude", "codex"), required=True)
    parser.add_argument("--skill", choices=("aispa", "autopilot"), required=True)
    parser.add_argument("--skill-src")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    args = parser.parse_args()
    repo = Path(os.environ["AISPA_MEASURE_REPO"])
    events, final = Path(os.environ["AISPA_MEASURE_EVENTS"]), Path(os.environ["AISPA_MEASURE_FINAL"])
    request = Path(os.environ["AISPA_MEASURE_REQUEST"]).read_text(encoding="utf-8")
    mode = os.environ.get("AISPA_MEASURE_MODE", "run")
    model, effort = args.model or MODELS[args.runtime][0], args.effort or MODELS[args.runtime][1]
    if args.skill_src and args.skill == "autopilot":
        why = refuse_unpinned(args.skill_src)
        if why:
            print(why, file=sys.stderr)
            return 2
    events.touch()  # an empty file means "no questions, no stops", not "unknown"
    if args.skill_src and os.environ.get("AISPA_MEASURE_INSTALL", "project") == "project":
        install(repo, args.runtime, args.skill, args.skill_src)
    session, prompt = None, first_prompt(args.runtime, args.skill, request, mode)
    for turn in range(1, args.max_turns + 1):
        log = events.parent / ("%s-%s-turn-%d.txt" % (args.runtime, args.skill, turn))
        session, text, usage_lines = run_turn(args.runtime, prompt, session, log, model, effort)
        for line in usage_lines:
            print(line, flush=True)
        if finished(repo, args.skill):
            final.write_text(text, encoding="utf-8")
            return 0
        kind = "question" if "?" in text[-500:] else "stop"
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": kind, "turn": turn}) + "\n")
            if turn < args.max_turns and session is not None:
                stream.write(json.dumps({"event": "intervention", "turn": turn}) + "\n")
        if session is None:
            return 2
        prompt = NUDGE
    return 1


if __name__ == "__main__":
    sys.exit(main())
