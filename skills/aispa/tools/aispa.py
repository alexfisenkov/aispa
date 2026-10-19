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
from aispa_core.store import Invalid, Journal, digest, file_hash, timestamp
from aispa_core.portable import is_link
from aispa_core.workflow import Workflow
from aispa_core import project_context, packets, discovery, carryover, cleanroom, blind, docmeta, ideas, planning, knowledge

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
    for name in ("brief", "requirements", "spec", "coverage", "acceptance"):
        sub.add_parser(name).add_argument("--file", required=True)
    plan = sub.add_parser("plan"); plan.add_argument("--file", required=True); plan.add_argument("--review")
    sub.add_parser("plan-check").add_argument("--file", required=True)
    idea = sub.add_parser("idea"); idea.add_argument("--file"); idea.add_argument("--full", action="store_true")
    know = sub.add_parser("knowledge")
    know.add_argument("--path", action="append"); know.add_argument("--kind", action="append", choices=("command", "trap", "decision", "glossary"))
    know.add_argument("--limit", type=int, default=8)
    know.add_argument("--archive", action="store_true", help="Адресный поиск в архиве знаний")
    know.add_argument("--archive-depth", type=int, default=4)
    sub.add_parser("knowledge-compact")
    writer = sub.add_parser("knowledge-assign")
    writer.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    writer.add_argument("--native-id", required=True); writer.add_argument("--session", required=True)
    writer.add_argument("--model")
    sub.add_parser("knowledge-record").add_argument("--file", required=True)
    for name in ("seal", "finish", "resume", "status"):
        sub.add_parser(name)
    ctx = sub.add_parser("context")
    ctx.add_argument("--kind", choices=("coverage", "task", "wave", "acceptance", "blind", "knowledge"), required=True)
    ctx.add_argument("--task")
    ctx.add_argument("--role", choices=("worker", "critic1", "critic2", "acceptor"))
    ctx.add_argument("--source", action="append", help="Уже известный точный файл: не вкладывать каталог; можно повторять")
    ctx.add_argument("--write", action="store_true", help="Сохранить пакет контекста, вернуть только путь и basis")
    discover = sub.add_parser("discover")
    discover.add_argument("--file")
    discover.add_argument("--include", action="append", help="Дополнительный безопасный исходник; можно повторять")
    sub.add_parser("discovery")
    sub.add_parser("memory")
    documents = sub.add_parser("documents")
    documents.add_argument("--detailed", action="store_true", help="Полные хеши и диагностика вместо краткой навигации")
    documents.add_argument("--refresh", action="store_true", help="Обновить описания изменённых зарегистрированных источников")
    documents.add_argument("--path", action="append", help="Показать выбранный путь; можно повторять")
    document = sub.add_parser("document")
    document.add_argument("--path", required=True)
    document.add_argument("--sha256", help="Ожидаемый SHA-256 прочитанного файла; обязателен для --summary и --write-header")
    document.add_argument("--summary", help="Краткое описание, привязанное к текущему содержимому")
    document.add_argument("--write-header", action="store_true", help="Записать YAML только в совместимый Markdown с проверкой SHA-256")
    carried = sub.add_parser("carry")
    carried.add_argument("id"); carried.add_argument("--action", dest="carry_action", required=True, choices=("keep", "map", "resolve", "drop", "defer"))
    carried.add_argument("--requirement"); carried.add_argument("--brief"); carried.add_argument("--quote")
    sub.add_parser("clean-prepare")
    sub.add_parser("blind").add_argument("--file", required=True)
    clean = sub.add_parser("clean-verify")
    clean.add_argument("--timeout", type=int, default=600)
    clean.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("usage").add_argument("--file")
    adapter = sub.add_parser("project-context")
    adapter.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    sub.add_parser("context-refresh")
    team = sub.add_parser("team"); team.add_argument("--file")
    assignment = sub.add_parser("assign"); assignment.add_argument("id")
    assignment.add_argument("--role", required=True, choices=("worker", "critic1", "critic2", "acceptor"))
    assignment.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    assignment.add_argument("--native-id", required=True); assignment.add_argument("--session", required=True)
    assignment.add_argument("--model", help="Фактический ID выбранной модели из среды, если она его сообщает")
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
            ignore = store.root / ".gitignore"
            if is_link(ignore) or ignore.exists() and not ignore.is_file():
                raise Invalid(".gitignore должен быть обычным файлом; содержимое сохранено")
            if is_link(store.home / "dashboard.html"):
                raise Invalid("Дашборд не должен быть символической ссылкой")
            previous_state = store.load(handoff_only=True) if (store.home / "active.json").exists() else None
            inherited = carryover.capture(store, previous_state) if previous_state else None
            learned = knowledge.capture(store, previous_state) if previous_state else None
            def initialize_run(state):
                state["policy"] = {"discovery": True, "cleanClone": True, "ideaReview": True,
                                   "planReview": True, "projectKnowledge": True}
                state["mode"], state["sealed"], state["core"] = args.mode, False, []
                state["runtime"] = args.runtime
                state["runtimeDigest"] = runtime_digest()
                if inherited is not None:
                    carryover.attach(store, state, inherited)
                knowledge.attach(store, state, learned)
                shutil.copytree(PACKAGE, store.run / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            state = store.create(args.slug, args.title, initializer=initialize_run)
            shutil.copy2(PACKAGE / "assets/dashboard.html", store.home / "dashboard.html")
            previous = ignore.read_bytes() if ignore.exists() else b""
            if b".aispa/" not in previous.splitlines():
                ignore.write_bytes(previous + (b"\n" if previous and not previous.endswith(b"\n") else b"") + b".aispa/\n")
            store.append(state, "runtime-pinned")
            project_context.initialize(store, state, args.runtime)
            store.append(state, "project-context-initialized")
            project_context.refresh(store, state)
            print(json.dumps(publish(store, state), ensure_ascii=False, indent=2))
            return 0
        state = store.load()
        read_only = (args.action in ("status", "context", "memory", "discovery", "plan-check", "knowledge")
                     or (args.action == "idea" and not args.file) or (args.action == "documents" and not args.refresh))
        if not read_only and state.get("runtimeDigest") != runtime_digest():
            raise Invalid("Пакет изменён. Продолжайте сохранённой копией runs/<run>/runtime/tools/aispa.py; обновление требует миграции")
        flow = Workflow(store, state)
        ok = True
        receipt = None
        action = args.action
        if action in ("brief", "requirements", "spec", "coverage", "acceptance"):
            result = getattr(flow, action)(args.file)
            if isinstance(result, bool):
                ok = result
        elif action == "plan":
            flow.plan(args.file, review_path=args.review)
            receipt = state.get("planReview")
        elif action == "plan-check":
            print(json.dumps(planning.analyze(store, state, json.loads(flow.read_input(args.file))), ensure_ascii=False, indent=2))
            return 0
        elif action == "idea":
            if args.file:
                flow.running()
                receipt = ideas.record(store, state, json.loads(flow.read_input(args.file)))
                flow.save("idea-reviewed")
            else:
                print(json.dumps(ideas.view(store, state, compact=not args.full), ensure_ascii=False, indent=2))
                return 0
        elif action == "knowledge":
            print(json.dumps(knowledge.query(store, state, paths=args.path, kinds=args.kind, limit=args.limit,
                                            include_archive=args.archive, archive_depth=args.archive_depth), ensure_ascii=False, indent=2))
            return 0
        elif action == "knowledge-compact":
            flow.running()
            receipt = knowledge.compact(store, state)
            flow.save("project-knowledge-checkpointed")
        elif action == "knowledge-assign":
            flow.running()
            receipt = {"actor": knowledge.assign(store, state, args.runtime, args.native_id, args.session, model=args.model)}
            flow.save("knowledge-writer-assigned")
        elif action == "knowledge-record":
            flow.running()
            receipt = knowledge.record(store, state, json.loads(flow.read_input(args.file)))
            flow.save("project-knowledge-recorded")
        elif action in ("seal", "finish", "resume"):
            getattr(flow, action)()
        elif action == "scope":
            flow.scope(args.id, args.decision, args.brief, args.quote)
        elif action == "context":
            if args.kind == "acceptance" and state.get("policy", {}).get("cleanClone") and not blind.valid(store, state):
                raise Invalid("Сначала сохраните независимые наблюдения: context --kind blind, затем blind --file")
            if args.kind == "blind":
                if not cleanroom.current(store, state):
                    raise Invalid("Сначала подготовьте актуальный чистый клон: clean-prepare")
            packet = packets.build(store, state, flow.pipeline, args.kind, args.task, args.role, source_paths=args.source)
            print(json.dumps(packets.write(store, packet) if args.write else packet, ensure_ascii=False, indent=2))
            return 0
        elif action == "discover":
            flow.running()
            if args.file:
                receipt = discovery.record(store, state, json.loads(flow.read_input(args.file)))
                flow.save("project-discovery-recorded")
            else:
                metadata_receipt = {}
                evidence = discovery.collect(store, state, paths=args.include, metadata_receipt=metadata_receipt)
                receipt = {"collection": state["discoveryCollection"],
                           "absolutePath": str(store.run / state["discoveryCollection"]["file"]),
                           "sourceCount": len(evidence["sources"]), "omitted": evidence["omitted"],
                           "documentRegistration": metadata_receipt}
                flow.save("project-discovery-collected")
        elif action == "discovery":
            print(json.dumps(discovery.view(store, state), ensure_ascii=False, indent=2))
            return 0
        elif action == "memory":
            print(json.dumps(carryover.view(store, state), ensure_ascii=False, indent=2))
            return 0
        elif action == "documents":
            if args.refresh:
                flow.running()
                receipt = docmeta.refresh(store, state, paths=args.path)
                flow.save("document-metadata-refreshed")
                receipt = {"refresh": receipt, "catalog": docmeta.catalog(store, state, paths=args.path, detailed=args.detailed)}
            else:
                print(json.dumps(docmeta.catalog(store, state, paths=args.path, detailed=args.detailed), ensure_ascii=False, indent=2))
                return 0
        elif action == "document":
            flow.running()
            receipt = docmeta.register(store, state, args.path, expected_sha256=args.sha256,
                                       summary=args.summary, write_header=args.write_header)
            flow.save("document-metadata-registered")
        elif action == "carry":
            receipt = carryover.decide(store, state, args.id, args.carry_action, current_requirement=args.requirement,
                                       brief=args.brief, quote=args.quote)
            flow.save("inherited-obligation-" + args.carry_action)
        elif action == "clean-prepare":
            flow.running()
            state["cleanroom"] = cleanroom.prepare(store, state)
            state.pop("cleanrun", None)
            receipt = state["cleanroom"]
            flow.save("clean-clone-prepared")
        elif action == "blind":
            flow.running()
            receipt = blind.record(store, state, json.loads(flow.read_input(args.file)))
            flow.save("blind-observations-recorded")
        elif action == "clean-verify":
            flow.running()
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            state.pop("cleanrun", None)
            state["activity"] = {"kind": "clean-verification", "startedAt": timestamp()}
            flow.save("clean-verification-started")
            try:
                state["cleanrun"] = cleanroom.execute(store, state, command, args.timeout)
                receipt = state["cleanrun"]
            finally:
                state.pop("activity", None)
                flow.save("clean-verification-recorded")
            ok = cleanroom.valid(store, state)
        elif action == "usage":
            if args.file:
                receipt = packets.record_usage(store, state, json.loads(flow.read_input(args.file)))
                flow.save("native-usage-recorded")
            else:
                print(json.dumps(packets.usage_summary(store, state), ensure_ascii=False, indent=2))
                return 0
        elif action == "project-context":
            receipt = project_context.initialize(store, state, args.runtime)
            flow.save("project-context-adapted")
        elif action == "context-refresh":
            receipt = project_context.refresh(store, state)
        elif action == "team":
            receipt = flow.pipeline.team(args.file)
        elif action == "assign":
            receipt = {"actor": flow.pipeline.assign(args.id, args.role, args.runtime, args.native_id, args.session, model=args.model)}
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
    # JSON and diagnostics have the same UTF-8 contract in Windows pipes/console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        return execute(parser().parse_args())
    except (Invalid, OSError, ValueError) as error:
        print("AISpa: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
