#!/usr/bin/env python3
"""AISpa local CLI. Run this file directly; no installation or network required."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import functools
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

from aispa_core import VERSION
from aispa_core.contracts import basis, active
from aispa_core.display import publish, project
from aispa_core.store import Invalid, Journal, digest, file_hash
from aispa_core.workflow import Workflow

PACKAGE = Path(__file__).resolve().parents[1]


def runtime_digest():
    return digest({p.relative_to(PACKAGE).as_posix(): file_hash(p)
                   for p in sorted(PACKAGE.rglob("*")) if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix != ".pyc"})


def parser():
    p = argparse.ArgumentParser(description="AISpa " + VERSION)
    p.add_argument("--root", default=".", help="Корень Git-проекта")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--slug", required=True)
    init.add_argument("--title", required=True)
    init.add_argument("--mode", required=True, choices=("auto", "important", "interview"))
    for name in ("brief", "requirements", "spec", "coverage", "plan", "acceptance"):
        sub.add_parser(name).add_argument("--file", required=True)
    for name in ("seal", "finish", "resume", "status"):
        sub.add_parser(name)
    ctx = sub.add_parser("context")
    ctx.add_argument("--kind", choices=("coverage", "task", "wave", "acceptance"), required=True)
    ctx.add_argument("--task")
    scope = sub.add_parser("scope")
    scope.add_argument("id"); scope.add_argument("decision", choices=("drop", "defer", "restore"))
    scope.add_argument("--brief", required=True); scope.add_argument("--quote", required=True)
    start = sub.add_parser("start"); start.add_argument("id"); start.add_argument("--context", required=True)
    complete = sub.add_parser("complete"); complete.add_argument("id"); complete.add_argument("--file", required=True)
    reset = sub.add_parser("reset"); reset.add_argument("id")
    block = sub.add_parser("block"); block.add_argument("id"); block.add_argument("--note", required=True)
    wave = sub.add_parser("wave"); wave.add_argument("number", type=int); wave.add_argument("--file", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--timeout", type=int, default=180)
    verify.add_argument("command", nargs=argparse.REMAINDER)
    close = sub.add_parser("close")
    close.add_argument("status", choices=("paused", "blocked", "cancelled")); close.add_argument("--note", required=True)
    sub.add_parser("decision").add_argument("text")
    serve = sub.add_parser("serve"); serve.add_argument("--port", type=int, default=0)
    return p


def execute(args):
    store = Journal(args.root)
    if args.action == "serve":
        # The lock is never held while a long-lived read-only server runs.
        with store.lock():
            state = store.load()
            publish(store, state)
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                if urlsplit(self.path).path == "/status.json":
                    try:
                        # Each reader has its own run pointer. Never wait for a long
                        # verification command's writer lock, and never edit the journal.
                        reader = Journal(args.root)
                        payload = json.dumps(project(reader, reader.load()), ensure_ascii=False).encode()
                    except (Invalid, OSError, ValueError):
                        self.send_error(409, "Current state could not be verified")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                super().do_GET()

        handler = functools.partial(Handler, directory=str(store.home))
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"http://127.0.0.1:{server.server_port}/dashboard.html", flush=True)
            server.serve_forever()
        return 0
    with store.lock():
        if args.action == "init":
            store.code()  # Require an actual Git root before creating a run.
            state = store.create(args.slug, args.title)
            state["mode"], state["sealed"], state["core"] = args.mode, False, []
            state["runtimeDigest"] = runtime_digest()
            shutil.copytree(PACKAGE, store.run / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(PACKAGE / "assets/dashboard.html", store.home / "dashboard.html")
            ignore = store.root / ".gitignore"
            previous = ignore.read_text() if ignore.exists() else ""
            if ".aispa/" not in previous.splitlines():
                ignore.write_text(previous.rstrip("\n") + "\n.aispa/\n")
            store.append(state, "runtime-pinned")
            print(json.dumps(publish(store, state), ensure_ascii=False, indent=2))
            return 0
        state = store.load()
        if args.action not in ("status", "context") and state.get("runtimeDigest") != runtime_digest():
            raise Invalid("Пакет изменён. Продолжайте сохранённой копией runs/<run>/runtime/tools/aispa.py; обновление требует миграции")
        flow = Workflow(store, state)
        ok = True
        action = args.action
        if action in ("brief", "requirements", "spec", "coverage", "plan", "acceptance"):
            result = getattr(flow, action)(args.file)
            if isinstance(result, bool):
                ok = result
        elif action in ("seal", "finish", "resume"):
            getattr(flow, action)()
        elif action == "scope":
            flow.scope(args.id, args.decision, args.brief, args.quote)
        elif action == "context":
            print(json.dumps({"basis": basis(store, state, args.kind, args.task),
                              "runDirectory": str(store.run), "briefs": state["briefs"],
                              "requirements": active(state), "spec": state["spec"],
                              "tasks": state["tasks"], "verification": state["verification"]},
                             ensure_ascii=False, indent=2))
            return 0
        elif action == "start":
            flow.start(args.id, args.context)
        elif action == "complete":
            flow.complete_task(args.id, args.file)
        elif action == "block":
            flow.block(args.id, args.note)
        elif action == "reset":
            flow.reset(args.id)
        elif action == "wave":
            ok = flow.wave(args.number, args.file)
        elif action == "verify":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if args.timeout < 1:
                raise Invalid("Таймаут должен быть положительным")
            ok = flow.verify(command, args.timeout)
        elif action == "close":
            flow.close(args.status, args.note)
        elif action == "decision":
            flow.decision(args.text)
        print(json.dumps(publish(store, state), ensure_ascii=False, indent=2))
        return 0 if ok else 2


def main():
    try:
        return execute(parser().parse_args())
    except (Invalid, OSError, ValueError) as error:
        print("AISpa: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
