#!/usr/bin/env python3
"""AISpa local CLI. Run this file directly; no installation or network required."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from aispa_core import VERSION
from aispa_core.contracts import basis, active, task_closure
from aispa_core.display import publish, project
from aispa_core.store import Invalid, Journal, digest, file_hash, timestamp
from aispa_core.portable import is_link
from aispa_core.workflow import Workflow
from aispa_core import check_modes, project_context, packets, discovery, carryover, cleanroom, blind, docmeta, ideas, planning, knowledge, executions, repairs, handoff, controls, dashboard_server, integration
from aispa_core.cli_output import emit
from aispa_core import phase_receipts

PACKAGE = Path(__file__).resolve().parents[1]


def runtime_digest():
    return digest({p.relative_to(PACKAGE).as_posix(): file_hash(p)
                   for p in sorted(PACKAGE.rglob("*")) if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix != ".pyc"})


def parser():
    p = argparse.ArgumentParser(description="AISpa " + VERSION)
    p.add_argument("--root", default=".", help="Корень Git-проекта")
    p.add_argument("--compact", action="store_true",
                   help="Краткий ответ изменений/status/init со ссылкой и SHA-256 полного JSON; перед подкомандой")
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
    init.add_argument("--depth", choices=("strict", "balanced", "deep"), default="balanced")
    init.add_argument("--check", required=True, choices=check_modes.MODES,
                      help="Сколько проверять: fast (быстро), balanced (сбалансированно), max (максимум)")
    controls.parsers(sub)
    for name in ("brief", "requirements", "spec", "coverage", "acceptance"):
        action = sub.add_parser(name)
        action.add_argument("--file", required=True)
        if name == "coverage":
            action.add_argument("--actor")
    plan = sub.add_parser("plan"); plan.add_argument("--file", required=True); plan.add_argument("--review")
    plan.add_argument("--actor")
    sub.add_parser("plan-check").add_argument("--file", required=True)
    idea = sub.add_parser("idea"); idea.add_argument("--file"); idea.add_argument("--full", action="store_true")
    idea.add_argument("--actor")
    commands = sub.add_parser("commands")
    commands.add_argument("--purpose", choices=executions.PURPOSES)
    commands.add_argument("--kind", choices=executions.KINDS)
    commands.add_argument("--limit", type=int, default=8)
    commands.add_argument("--receipt", help="SHA-256 одной записи: прочитать точное исполнение")
    sub.add_parser("handoff").add_argument("--file", required=True)
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
    ctx.add_argument("--with-template", action="store_true",
                     help="Для task --role --write также подготовить пустую форму назначенного участника")
    discover = sub.add_parser("discover")
    discover.add_argument("--file")
    discover.add_argument("--actor")
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
    clean.add_argument("--purpose", choices=executions.PURPOSES, default="check")
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
    repair_plan = sub.add_parser("repair-plan")
    repair_plan.add_argument("id"); repair_plan.add_argument("--file")
    critic = sub.add_parser("critic"); critic.add_argument("id"); critic.add_argument("--round", type=int, required=True, choices=(1, 2))
    critic.add_argument("--file", required=True)
    attempt = sub.add_parser("new-attempt"); attempt.add_argument("id")
    attempt.add_argument("--brief", required=True); attempt.add_argument("--quote", required=True)
    switch = sub.add_parser("check-mode"); switch.add_argument("mode", choices=check_modes.MODES)
    switch.add_argument("--file", required=True, help="Файл с дословными словами владельца о смене режима")
    sub.add_parser("extra-wave").add_argument("id")
    promotion = sub.add_parser("promote-task"); promotion.add_argument("id")
    promotion.add_argument("--reason", required=True)
    global_review = sub.add_parser("integration-review")
    global_review.add_argument("--file"); global_review.add_argument("--actor")
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
    verify.add_argument("--purpose", choices=executions.PURPOSES, default="check")
    verify.add_argument("command", nargs=argparse.REMAINDER)
    close = sub.add_parser("close")
    close.add_argument("status", choices=("paused", "blocked", "cancelled")); close.add_argument("--note", required=True)
    sub.add_parser("decision").add_argument("text")
    serve = sub.add_parser("serve"); serve.add_argument("--port", type=int, default=0)
    sub.add_parser("batch").add_argument("--file", required=True)
    return p


BATCH_LIMIT = 25
# Each step is an ordinary command with its own lock and journal event; these
# change the package, run a server, repair the journal or create a run.
BATCH_DENIED = frozenset({"update-check", "update-prepare", "update-apply", "update-rollback", "serve",
                          "dashboard", "init", "batch"})
BATCH_JOURNAL_READS = frozenset({"journal-inspect", "journal-context"})


def batch(args):
    """Run up to 25 sequential steps in one host call; stop at the first failure."""
    import contextlib
    import io
    try:
        steps = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Invalid("batch: нужен JSON-файл со списком шагов") from exc
    if not (isinstance(steps, list) and 0 < len(steps) <= BATCH_LIMIT and all(
            isinstance(step, list) and step and all(isinstance(x, str) for x in step) for step in steps)):
        raise Invalid("batch: нужен список от 1 до %d шагов, каждый — непустой список строк argv" % BATCH_LIMIT)
    for step in steps:
        journal_write = step[0].startswith("journal-") and step[0] not in BATCH_JOURNAL_READS
        if (step[0] in BATCH_DENIED or journal_write or step[0].startswith("-")
                or {"--root", "--compact", "-h", "--help"} & set(step)):
            raise Invalid("batch: шаг недопустим в пакете: " + " ".join(step[:2]))
    import signal
    results, code, interrupted = [], 0, None

    def terminate(signum, _frame):
        # Unwind through the step's lock context instead of dying holding it.
        raise KeyboardInterrupt("signal %d" % signum)
    previous = signal.signal(signal.SIGTERM, terminate) if hasattr(signal, "SIGTERM") else None
    for index, step in enumerate(steps):
        if interrupted:
            break
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = execute(parser().parse_args(["--root", args.root, "--compact", *step]))
            except (Invalid, OSError, ValueError) as error:
                print("AISpa: " + str(error), file=sys.stderr)
                code = 2
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 2
            except KeyboardInterrupt as stop:
                print("AISpa: пакет прерван: " + str(stop), file=sys.stderr)
                code, interrupted = 130, str(stop) or "interrupt"
        text = out.getvalue()
        try:
            value = json.loads(text) if text.strip() else None
        except ValueError:
            value = text
        results.append({"step": index + 1, "argv": step, "exit": code, "output": value,
                        **({"error": err.getvalue().strip()} if err.getvalue().strip() else {})})
        if code:
            break
    if previous is not None:
        signal.signal(signal.SIGTERM, previous)
    # A non-zero step may still have written its journal event (critic WARN,
    # failed verify): "completed" counts zero exits only, never "nothing happened".
    print(json.dumps({"batch": True, "ok": code == 0, "completed": sum(r["exit"] == 0 for r in results),
                      "total": len(steps), "stoppedAt": results[-1]["step"] if code else None,
                      **({"interrupted": interrupted} if interrupted else {}),
                      "note": "A failed step may have recorded its own journal event; check status before retrying.",
                      "steps": results}, ensure_ascii=False, indent=2))
    return 0 if code == 0 else 2


def execute(args):
    if args.action == "batch":
        return batch(args)
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
    if args.action in ("journal-inspect", "journal-recover"):
        value = store.inspect_recovery() if args.action == "journal-inspect" else store.apply_recovery(
            args.sha256, confirmed=args.confirm)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.action == "dashboard" and args.operation != "archive":
        if args.operation == "start":
            value = dashboard_server.start(store, port=args.port)
        elif args.operation == "stop":
            value = dashboard_server.stop(store)
        else:
            value = dashboard_server.inspect(store)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.action == "serve":
        # The lock is never held while a long-lived read-only server runs.
        with store.lock():
            state = store.load()
            publish(store, state)
        dashboard_server.serve(store, port=args.port)
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
                                   "planReview": True, "projectKnowledge": True, "productChallenge": True,
                                   "deliveryPlan": True, "repairDiagnosis": True, "deliveryDocs": True,
                                   "sourceAccounting": True, "designContract": True, "allRoleUsage": True,
                                   "integrationReview": True, "testIntent": True,
                                   "adaptiveCritique": True, "phaseReceipts": True,
                                   "executionObligations": True, "finalWaveReuse": True,
                                   "errorBoundaryChecks": True, "taskTiers": True,
                                   "leanControl": True}
                state["mode"], state["sealed"], state["core"] = args.mode, False, []
                state["depth"] = args.depth
                check_modes.initialize(state, args.check)
                state["runtime"] = args.runtime
                state["runtimeDigest"] = runtime_digest()
                integration.capture(store, state)
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
            emit(store, publish(store, state), compact_mode=args.compact)
            return 0
        state = store.load()
        preparation_write = (args.action in ("template", "brief-source", "brief-map-context") and args.write
                             or args.action == "context" and args.with_template)
        read_only = not preparation_write and (
                     args.action in ("status", "context", "memory", "discovery", "plan-check", "knowledge", "commands")
                     or args.action in controls.READS
                     or args.action == "test-intent" and not args.file
                     or (args.action in ("idea", "repair-plan", "integration-review") and not args.file)
                     or (args.action == "documents" and not args.refresh))
        if not read_only and state.get("runtimeDigest") != runtime_digest():
            raise Invalid("Пакет изменён. Продолжайте сохранённой копией runs/<run>/runtime/tools/aispa.py; обновление требует миграции")
        flow = Workflow(store, state)
        if state.get("journalRecovery", {}).get("pending") and not read_only and args.action not in (
                "journal-verify", "journal-reconcile", "context-refresh") and not (
                args.action == "participant" and args.role == "recovery-reviewer"):
            raise Invalid("Восстановление ещё не сверено: journal-context, journal-verify, journal-reconcile")
        ok = True
        receipt = None
        action = args.action
        if action in controls.READS | controls.WRITES:
            receipt = controls.execute(store, state, flow, args)
            if action in controls.READS or action == "test-intent" and not args.file:
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
                return 0
            if action == "brief-map":
                from aispa_core import briefmap, preparation
                ok = briefmap.valid(store, state, preparation.actor_check(store))
        elif action == "dashboard":
            from aispa_core.dashboard_export import archive
            receipt = archive(store, state)
        elif action in ("brief", "requirements", "spec", "coverage", "acceptance"):
            result = (flow.coverage(args.file, actor=args.actor) if action == "coverage"
                      else getattr(flow, action)(args.file))
            if isinstance(result, bool):
                ok = result
        elif action == "plan":
            flow.plan(args.file, review_path=args.review, actor=args.actor)
            receipt = state.get("planReview")
        elif action == "plan-check":
            print(json.dumps(planning.analyze(store, state, json.loads(flow.read_input(args.file))), ensure_ascii=False, indent=2))
            return 0
        elif action == "idea":
            if args.file:
                flow.running()
                author = phase_receipts.authorize(store, state, "idea", args.actor)
                receipt = ideas.record(store, state, json.loads(flow.read_input(args.file)))
                phase_receipts.record(store, state, "idea", receipt, actor=author)
                flow.save("idea-reviewed")
            else:
                print(json.dumps(ideas.view(store, state, compact=not args.full), ensure_ascii=False, indent=2))
                return 0
        elif action == "commands":
            value = executions.resolve(store, state, {"sha256": args.receipt}) if args.receipt else executions.view(
                store, state, kind=args.kind, purpose=args.purpose, limit=args.limit)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        elif action == "handoff":
            flow.build_ready()
            receipt = handoff.record(store, state, json.loads(flow.read_input(args.file)),
                                     command_receipt=lambda ref: executions.resolve(store, state, ref))
            flow.save("project-handoff-recorded")
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
            if args.with_template:
                if args.kind != "task" or not args.write or not args.task or not args.role:
                    raise Invalid("--with-template требует --kind task, --task, --role и --write")
                result = packets.write_task_bundle(store, state, flow.pipeline, args.task, args.role,
                                                   source_paths=args.source)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
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
                author = phase_receipts.authorize(store, state, "discovery", args.actor)
                receipt = discovery.record(store, state, json.loads(flow.read_input(args.file)))
                phase_receipts.record(store, state, "discovery", receipt, actor=author)
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
            flow.handoff_ready()
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
            executions.validate_command(command)
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
            receipt = {**receipt, "execution": executions.record(store, state, kind="cleanrun", purpose=args.purpose)}
            flow.save("execution-recorded")
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
            receipt = {"report": result.get("report"), "phase": flow.task(args.id).get("phase"),
                       **({"completionKind": result["completionKind"]} if result.get("completionKind") else {})}
        elif action == "repair":
            result = flow.pipeline.repair(args.id, args.file)
            receipt = {"report": result.get("report"), "phase": flow.task(args.id).get("phase"),
                       **({"completionKind": result["completionKind"]} if result.get("completionKind") else {})}
        elif action == "repair-plan":
            if args.file:
                flow.build_ready()
                receipt = repairs.record(store, state, args.id, json.loads(flow.read_input(args.file)))
                flow.save("repair-diagnosed:" + args.id)
            else:
                print(json.dumps(repairs.view(store, state, args.id), ensure_ascii=False, indent=2))
                return 0
        elif action == "critic":
            ok = flow.pipeline.review(args.id, args.round, args.file)
        elif action == "new-attempt":
            receipt = {"attempt": flow.pipeline.new_attempt(args.id, args.brief, args.quote)}
        elif action == "check-mode":
            receipt = {"mode": args.mode, "tasks": flow.check_mode(args.mode, args.file)}
        elif action == "extra-wave":
            receipt = {"attempt": flow.pipeline.extra_wave(args.id)}
        elif action == "promote-task":
            receipt = {"attempt": flow.pipeline.promote(args.id, args.reason)}
        elif action == "integration-review":
            if args.file:
                flow.build_ready()
                receipt = integration.project_review(store, state, json.loads(flow.read_input(args.file)), actor=args.actor)
                flow.save("project-integration-reviewed")
                ok = receipt["passed"]
            else:
                print(json.dumps(integration.project_context(store, state, actor=args.actor), ensure_ascii=False, indent=2))
                return 0
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
            receipt = executions.record(store, state, kind="task" if args.task else "project",
                                        purpose=args.purpose, task_id=args.task)
            flow.save("execution-recorded")
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
        emit(store, output, compact_mode=args.compact)
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
