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
from aispa_core.contracts import basis, active, task_closure
from aispa_core.display import publish, project
from aispa_core.store import Invalid, Journal, digest, file_hash
from aispa_core.workflow import Workflow
from aispa_core import project_context

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
    sub.add_parser("update-check")
    sub.add_parser("update-prepare")
    apply = sub.add_parser("update-apply")
    apply.add_argument("--candidate", required=True); apply.add_argument("--confirm", action="store_true")
    rollback = sub.add_parser("update-rollback")
    rollback.add_argument("--backup", required=True); rollback.add_argument("--confirm", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("--slug", required=True)
    init.add_argument("--title", required=True)
    init.add_argument("--mode", required=True, choices=("auto", "important", "interview"))
    init.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    for name in ("brief", "requirements", "spec", "coverage", "plan", "acceptance"):
        sub.add_parser(name).add_argument("--file", required=True)
    for name in ("seal", "finish", "resume", "status"):
        sub.add_parser(name)
    ctx = sub.add_parser("context")
    ctx.add_argument("--kind", choices=("coverage", "task", "wave", "acceptance"), required=True)
    ctx.add_argument("--task")
    ctx.add_argument("--role", choices=("worker", "critic1", "critic2", "acceptor"))
    ctx.add_argument("--write", action="store_true", help="Сохранить пакет контекста, вернуть только путь и basis")
    adapter = sub.add_parser("project-context")
    adapter.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    sub.add_parser("context-refresh")
    team = sub.add_parser("team"); team.add_argument("--file")
    assignment = sub.add_parser("assign"); assignment.add_argument("id")
    assignment.add_argument("--role", required=True, choices=("worker", "critic1", "critic2", "acceptor"))
    assignment.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    assignment.add_argument("--native-id", required=True); assignment.add_argument("--session", required=True)
    for name in ("submit", "repair"):
        action = sub.add_parser(name); action.add_argument("id"); action.add_argument("--file", required=True)
    critic = sub.add_parser("critic"); critic.add_argument("id"); critic.add_argument("--round", type=int, required=True, choices=(1, 2))
    critic.add_argument("--file", required=True)
    attempt = sub.add_parser("new-attempt"); attempt.add_argument("id")
    attempt.add_argument("--brief", required=True); attempt.add_argument("--quote", required=True)
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
    verify.add_argument("--task")
    verify.add_argument("command", nargs=argparse.REMAINDER)
    close = sub.add_parser("close")
    close.add_argument("status", choices=("paused", "blocked", "cancelled")); close.add_argument("--note", required=True)
    sub.add_parser("decision").add_argument("text")
    serve = sub.add_parser("serve"); serve.add_argument("--port", type=int, default=0)
    return p


def execute(args):
    if args.action.startswith("update-"):
        from aispa_core import updates
        if args.action == "update-check":
            result = updates.check(PACKAGE)
        elif args.action == "update-prepare":
            result = updates.prepare(PACKAGE)
        elif args.action == "update-apply":
            result = updates.apply(PACKAGE, args.candidate, confirmed=args.confirm)
        else:
            result = updates.rollback(PACKAGE, args.backup, confirmed=args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
            if (store.root / ".gitignore").is_symlink():
                raise Invalid(".gitignore не должен быть символической ссылкой; содержимое сохранено")
            state = store.create(args.slug, args.title)
            state["mode"], state["sealed"], state["core"] = args.mode, False, []
            state["runtime"] = args.runtime
            state["runtimeDigest"] = runtime_digest()
            shutil.copytree(PACKAGE, store.run / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(PACKAGE / "assets/dashboard.html", store.home / "dashboard.html")
            ignore = store.root / ".gitignore"
            previous = ignore.read_text() if ignore.exists() else ""
            if ".aispa/" not in previous.splitlines():
                ignore.write_text(previous.rstrip("\n") + "\n.aispa/\n")
            store.append(state, "runtime-pinned")
            project_context.initialize(store, state, args.runtime)
            store.append(state, "project-context-initialized")
            project_context.refresh(store, state)
            print(json.dumps(publish(store, state), ensure_ascii=False, indent=2))
            return 0
        state = store.load()
        if args.action not in ("status", "context") and state.get("runtimeDigest") != runtime_digest():
            raise Invalid("Пакет изменён. Продолжайте сохранённой копией runs/<run>/runtime/tools/aispa.py; обновление требует миграции")
        flow = Workflow(store, state)
        ok = True
        receipt = None
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
            selected = task_closure(state, args.task) if args.kind == "task" else state["tasks"]
            req_ids = {rid for task in selected for rid in task["requirements"]}
            reqs = [r for r in active(state) if args.kind != "task" or r["id"] in req_ids or r["kind"] == "constraint"]
            packet = {"basis": basis(store, state, args.kind, args.task), "runDirectory": str(store.run),
                      "briefs": state["briefs"], "requirements": reqs, "spec": state["spec"], "tasks": selected,
                      "verification": state["verification"], "projectContext": state.get("projectContext")}
            if args.kind == "task":
                current = state["taskHistory"][args.task][-1]
                packet.update(verification=current.get("verification"), attempt=current["id"], roles=current["roles"],
                              actors=current["actors"], rounds=current["rounds"], repair=current.get("repair"),
                              traceRequirements=flow.pipeline.trace_requirements(args.task),
                              inheritedProofs=flow.pipeline.inherited_proofs(args.task),
                              neighbors=[{"id": t["id"], "zones": t["zones"], "status": t["status"]}
                                         for t in state["tasks"] if t["id"] != args.task])
                if args.role:
                    packet["expectedChecks"] = [{"requirement": rid, "criterion": criterion}
                                                for rid, criterion in sorted(flow.pipeline.expected_checks(args.task, args.role))]
            if args.write:
                artifact = store.artifact("contexts", (json.dumps(packet, ensure_ascii=False, indent=2) + "\n").encode(), ".json")
                print(json.dumps(dict(artifact, basis=packet["basis"], absolutePath=str(store.run / artifact["file"])), ensure_ascii=False))
            else:
                print(json.dumps(packet, ensure_ascii=False, indent=2))
            return 0
        elif action == "project-context":
            receipt = project_context.initialize(store, state, args.runtime)
            flow.save("project-context-adapted")
        elif action == "context-refresh":
            receipt = project_context.refresh(store, state)
        elif action == "team":
            receipt = flow.pipeline.team(args.file)
        elif action == "assign":
            receipt = {"actor": flow.pipeline.assign(args.id, args.role, args.runtime, args.native_id, args.session)}
        elif action == "submit":
            result = flow.pipeline.submit(args.id, args.file)
            receipt = {"report": result.get("report"), "phase": flow.task(args.id).get("phase")}
        elif action == "repair":
            result = flow.pipeline.repair(args.id, args.file)
            receipt = {"report": result.get("report"), "phase": flow.task(args.id).get("phase")}
        elif action == "critic":
            ok = flow.pipeline.review(args.id, args.round, args.file)
        elif action == "new-attempt":
            receipt = {"attempt": flow.pipeline.new_attempt(args.id, args.brief, args.quote)}
        elif action == "start":
            flow.start(args.id, args.context)
        elif action == "complete":
            ok = flow.complete_task(args.id, args.file)
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
            ok = flow.verify(command, args.timeout, args.task)
        elif action == "close":
            flow.close(args.status, args.note)
        elif action == "decision":
            flow.decision(args.text)
        view = publish(store, state)
        output = view if action == "status" else {
            "ok": ok, "run": state["run"], "runDirectory": view["runDirectory"], "status": view["status"],
            "stage": view["displayStage"], "gates": view["gates"], "receipt": receipt,
            "task": next(({"id": t["id"], "status": t["status"], "phase": t.get("phase"), "note": t.get("note")}
                          for t in state["tasks"] if t["id"] == getattr(args, "id", None)), None),
            "counts": {name: sum(t["status"] == name for t in state["tasks"])
                       for name in ("pending", "running", "blocked", "completed")},
            "contextIndex": str(store.home / "context/INDEX.md")}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if ok else 2


def main():
    try:
        return execute(parser().parse_args())
    except (Invalid, OSError, ValueError) as error:
        print("AISpa: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
