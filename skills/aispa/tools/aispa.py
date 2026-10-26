#!/usr/bin/env python3
"""AISpa local CLI. Run this file directly; no installation or network required."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True

from aispa_core import VERSION
from aispa_core.contracts import basis, active, task_closure
from aispa_core.display import publish, project
from aispa_core.store import Invalid, Journal, digest, file_hash, timestamp
from aispa_core.portable import is_link
from aispa_core.workflow import Workflow
from aispa_core import check_modes, project_context, packets, discovery, carryover, cleanroom, blind, docmeta, ideas, planning, knowledge, executions, repairs, handoff, controls, dashboard_server, integration
from aispa_core.cli_output import emit, saved_without_view
from aispa_core import phase_receipts, dashboard_host, work_progress
from aispa_core import autonomy, project_documents, preflight, brief_diagnostics, participants, timing
from aispa_core import task_zone_extension

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
    audit = sub.add_parser("assess", help="audit-only: bounded filesystem reading without a run, writes or commands")
    for name, default in (("max-entries", 1000), ("max-depth", 4), ("max-file-bytes", 131072),
                          ("max-total-bytes", 1048576), ("max-excerpt-chars", 2000)):
        audit.add_argument("--" + name, type=int, default=default)
    audit.add_argument("--timeout", type=float, default=2)
    faq = sub.add_parser("faq", help="Read source-bound support facts from this exact package, without a run")
    faq.add_argument("--version", dest="faq_version", required=True, help="Detected installed/pinned package version")
    faq.add_argument("--id", action="append")
    faq.add_argument("--sha256", help="Expected canonical input SHA-256")
    pack = sub.add_parser("pack", help="Prepare a verified chunked input without changing a run")
    pack.add_argument("--file", required=True)
    pack.add_argument("--output", required=True)
    pack.add_argument("--kind", required=True, choices=("text", "json"))
    pack.add_argument("--basis", help="Source-package SHA-256 binding; defaults to payload SHA-256")
    pack.add_argument("--chunk-bytes", type=int)
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
    init.add_argument("--runtime", required=True, choices=project_context.RUNTIMES)
    init.add_argument("--depth", choices=("strict", "balanced", "deep"), default="balanced")
    init.add_argument("--interview-scope", choices=("compact", "standard", "deep"), default="standard")
    init.add_argument("--interview-minutes", type=int, default=15)
    init.add_argument("--check", required=True, choices=check_modes.MODES,
                      help="Сколько проверять: fast (быстро), balanced (сбалансированно), max (максимум)")
    controls.parsers(sub)
    progress = sub.add_parser("progress")
    progress_steps = progress.add_subparsers(dest="progress_action", required=True)
    progress_steps.add_parser("begin").add_argument("phase", choices=work_progress.PHASES)
    progress_steps.add_parser("end")
    for name in ("brief", "requirements", "spec", "coverage", "acceptance"):
        action = sub.add_parser(name)
        action.add_argument("--file", required=True)
        if name == "coverage":
            action.add_argument("--actor")
    plan = sub.add_parser("plan"); plan.add_argument("--file", required=True); plan.add_argument("--review")
    plan.add_argument("--actor")
    sub.add_parser("plan-check").add_argument("--file", required=True)
    page = sub.add_parser("page", help="Read a digest-bound presentation page; domain validation still uses full input")
    page.add_argument("--file", required=True)
    page.add_argument("--cursor")
    page.add_argument("--limit", type=int, default=48)
    page.add_argument("--field", help="Address a top-level list field of a report")
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
    writer.add_argument("--runtime", required=True, choices=project_context.RUNTIMES)
    writer.add_argument("--native-id", required=True); writer.add_argument("--session", required=True)
    writer.add_argument("--model")
    writer.add_argument("--reasoning"); writer.add_argument("--model-evidence")
    sub.add_parser("knowledge-record").add_argument("--file", required=True)
    for name in ("seal", "finish", "resume", "status"):
        command = sub.add_parser(name)
        if name == "resume":
            command.add_argument("--host"); command.add_argument("--expected-basis")
    for name in ("preflight", "host-context"):
        command = sub.add_parser(name)
        command.add_argument("--file", required=name == "host-context")
        command.add_argument("--purpose", choices=preflight.PURPOSES, default="continue")
        command.add_argument("--expected-basis")
        if name == "preflight":
            command.add_argument("--write", action="store_true")
    sub.add_parser("brief-diagnostics")
    observation = sub.add_parser("model-observe")
    observation.add_argument("--actor", required=True); observation.add_argument("--file", required=True)
    resource = sub.add_parser("resources")
    resource.add_argument("operation", choices=("profile", "reserve", "bind", "finish", "heartbeat", "reconcile", "view", "wait-end"))
    resource.add_argument("--file"); resource.add_argument("--kind", choices=("agents", "browsers", "testProcesses", "services"))
    resource.add_argument("--lease"); resource.add_argument("--owner", default="coordinator"); resource.add_argument("--session")
    resource.add_argument("--task"); resource.add_argument("--role"); resource.add_argument("--model"); resource.add_argument("--reasoning")
    resource.add_argument("--model-evidence"); resource.add_argument("--confirmed", action="store_true")
    resource.add_argument("--outcome", choices=("completed", "launch_failed"), default="completed")
    resource.add_argument("--complete", action="store_true")
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
    discover.add_argument("--cursor", help="Continue a digest-bound discovery page")
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
    adapter.add_argument("--runtime", required=True, choices=project_context.RUNTIMES)
    sub.add_parser("context-refresh")
    project_docs = sub.add_parser("project-documents")
    project_docs.add_argument("--role", choices=project_documents.ROLES)
    project_docs.add_argument("--path")
    project_docs.add_argument("--sha256")
    project_docs.add_argument("--reason")
    team = sub.add_parser("team"); team.add_argument("--file")
    assignment = sub.add_parser("assign"); assignment.add_argument("id")
    assignment.add_argument("--role", required=True, choices=("worker", "critic1", "critic2", "acceptor"))
    assignment.add_argument("--runtime", required=True, choices=project_context.RUNTIMES)
    assignment.add_argument("--native-id", required=True); assignment.add_argument("--session", required=True)
    assignment.add_argument("--model", help="Запрошенный ID; actual требует отдельного host/provider metadata")
    assignment.add_argument("--reasoning"); assignment.add_argument("--model-evidence")
    for name in ("submit", "repair"):
        action = sub.add_parser(name); action.add_argument("id"); action.add_argument("--file", required=True)
    repair_plan = sub.add_parser("repair-plan")
    repair_plan.add_argument("id"); repair_plan.add_argument("--file")
    zones = sub.add_parser("task-zone-extension")
    zones.add_argument("id"); zones.add_argument("--file"); zones.add_argument("--actor")
    zones.add_argument("--apply", action="store_true")
    zones.add_argument("--expected-journal-sha256")
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
# Resume and seal return control for mandatory dashboard/briefing host actions.
BATCH_DENIED = frozenset({"update-check", "update-prepare", "update-apply", "update-rollback", "serve",
                          "dashboard", "init", "resume", "seal", "batch"})
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


def resources_command(store, state, flow, args):
    from aispa_core import resources, task_tiers
    from aispa_core.contracts import require
    require(resources.enabled(state), "Resources: нужен новый managedExecution init")
    value = json.loads(flow.read_input(args.file)) if args.file else None
    if args.operation == "profile":
        flow.running()
        receipt = resources.configure(store, state, value)
        flow.save("resource-profile-configured")
    elif args.operation == "wait-end":
        require(timing.waiting(state) and state["waiting"]["kind"] == "resource_wait", "Resources: нет resource wait")
        timing.end_wait(state, timestamp())
        flow.save("resource-wait-ended")
        receipt = timing.summary(state, timestamp())
    else:
        require(args.session is None or args.session == state.get("hostSession"), "Resources: session не принята через host-context")
        ctx = resources.context(store, state, owner=args.owner, session=args.session, allow_paused=True)
        launch = None
        if args.operation == "reserve":
            require(args.kind is not None, "Resources: reserve требует --kind")
            if args.kind in ("agents", "browsers"):
                preflight.require_adopted(store, state, purpose="browser" if args.kind == "browsers" else "continue")
            if args.kind == "agents":
                task = flow.task(args.task) if args.task else None
                evidence = json.loads(flow.read_input(args.model_evidence)) if args.model_evidence else None
                launch = participants.launch_plan(store, state, runtime=state["runtime"], session=ctx["session"],
                    model=args.model, reasoning=args.reasoning, evidence=evidence, sensitive=task_tiers.sensitive_task(state, task))
        try:
            if args.operation == "reserve":
                receipt = resources.reserve(ctx, args.kind)
                try:
                    flow.resource_admitted(args.kind)
                except BaseException:
                    # No native launch is authorized until its admission event
                    # is saved. Release only this still-unbound reservation.
                    resources.finish(ctx, receipt["id"], outcome="launch_failed", confirmed=True)
                    raise
                if launch is not None:
                    receipt = {"lease": receipt, "launchPlan": launch}
            elif args.operation == "view":
                receipt = resources.view(ctx)
            elif args.operation == "reconcile":
                require(isinstance(value, list), "Resources: reconcile требует observations array")
                receipt = resources.reconcile(ctx, value, complete=args.complete)
            else:
                require(args.lease is not None, "Resources: нужен --lease")
                if args.operation == "bind":
                    receipt = resources.bind(ctx, args.lease, value)
                elif args.operation == "finish":
                    receipt = resources.finish(ctx, args.lease, handle=value, outcome=args.outcome, confirmed=args.confirmed)
                else:
                    receipt = resources.heartbeat(ctx, args.lease, handle=value)
        except resources.CapacityRefused as error:
            timing.begin_wait(state, "resource_wait", timestamp())
            state["waiting"].update(note=str(error), resourceKind=error.kind)
            flow.save("resource-capacity-awaited")
            emit(store, {"ok": False, "run": state["run"], "receipt": {"code": error.code,
                "kind": error.kind, "cap": error.cap, "occupied": error.occupied}, "timing": timing.summary(state, timestamp())},
                compact_mode=args.compact)
            return 2
    emit(store, {"ok": True, "run": state["run"], "receipt": receipt}, compact_mode=args.compact)
    return 0


def execute(args):
    if args.action == "assess":
        from aispa_core import assessment
        result = assessment.assess(args.root, max_entries=args.max_entries, max_depth=args.max_depth,
            max_file_bytes=args.max_file_bytes, max_total_bytes=args.max_total_bytes,
            max_excerpt_chars=args.max_excerpt_chars, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.action == "faq":
        from aispa_core import support_faq
        print(json.dumps(support_faq.select(PACKAGE, version=args.faq_version, ids=args.id,
                         expected_sha256=args.sha256), ensure_ascii=False, indent=2))
        return 0
    if args.action == "init":
        autonomy.interview_setting(args.interview_scope, args.interview_minutes)
    if args.action == "pack":
        from aispa_core import scale
        from aispa_core.workflow import read_input
        import hashlib
        raw = read_input(args.file, scalable=True).encode("utf-8")
        output = Path(args.output).absolute()
        value = scale.write_manifest(output, raw, kind=args.kind,
                                     basis=args.basis or hashlib.sha256(raw).hexdigest(),
                                     chunk_bytes=args.chunk_bytes)
        if scale.read_file(output) != raw:
            raise Invalid("Packed input read-back mismatch")
        print(json.dumps({"file": str(output), "sha256": file_hash(output),
                          "digest": value["digest"], "totalBytes": value["totalBytes"],
                          "chunks": len(value["chunks"]), "verified": True}, ensure_ascii=False, indent=2))
        return 0
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
        if args.operation in ("start", "refresh-view"):
            managed = {}
            if args.operation == "start":
                expected_run = store.load()["run"]

                def admitted_service(kind):
                    # Dashboard dispatch precedes the normal CLI journal lock.
                    # Verification/clean/recovery callbacks already hold theirs.
                    with store.lock():
                        current = store.load()
                        if current["run"] != expected_run:
                            raise Invalid("Dashboard admission: active run изменился")
                        return Workflow(store, current).resource_admitted(kind)

                managed["on_admitted"] = admitted_service
            value = dashboard_server.start(store, port=args.port, refresh_view=args.operation == "refresh-view", **managed)
            dashboard_state = store.load()
            value = {**value, "requiredHostActions": dashboard_host.open_request(value, managed=preflight.enabled(dashboard_state))}
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
            previous_state = store.preflight_create(args.slug)
            if store.git("ls-files", "-z", "--", ".aispa"):
                raise Invalid("Приватный каталог .aispa уже отслеживается Git; init не будет добавлять туда снимки")
            inherited = carryover.capture(store, previous_state) if previous_state else None
            learned = knowledge.capture(store, previous_state) if previous_state else None
            def initialize_run(state):
                state["policy"] = {"discovery": True, "cleanClone": True, "ideaReview": True,
                                   "planReview": True, "projectKnowledge": True, "productChallenge": True,
                                   "deliveryPlan": True, "repairDiagnosis": True, "deliveryDocs": True,
                                   "sourceAccounting": True, "designContract": True, "allRoleUsage": True,
                                   "integrationReview": True, "testIntent": True, "mixedEvidence": True,
                                   "incrementalTestIntent": True,
                                   "adaptiveCritique": True, "phaseReceipts": True,
                                   "executionObligations": True, "finalWaveReuse": True,
                                   "errorBoundaryChecks": True, "taskTiers": True,
                                   "leanControl": True, "projectDocuments": True,
                                   "firstBriefingClosure": True, "scalableInputs": True,
                                   "criticalProductFlows": True, "managedExecution": True}
                store.scale_enabled = True
                state["mode"], state["sealed"], state["core"] = args.mode, False, []
                state["depth"] = args.depth
                state["interview"] = autonomy.interview_setting(args.interview_scope, args.interview_minutes)
                state["projectRoot"] = str(store.root)
                check_modes.initialize(state, args.check)
                state["runtime"] = args.runtime
                state["runtimeDigest"] = runtime_digest()
                integration.capture(store, state)
                if inherited is not None:
                    carryover.attach(store, state, inherited)
                knowledge.attach(store, state, learned)
                project_documents.initialize(store, state, previous=previous_state)
                shutil.copytree(PACKAGE, store.run / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            # Keep private source snapshots ignored even if initialization is
            # interrupted after one document has been captured.
            previous = ignore.read_bytes() if ignore.exists() else b""
            if b".aispa/" in previous.splitlines() and not store.private_history_ignored():
                raise Invalid("Исключение .aispa/ переопределено правилами Git; исправьте .gitignore до init")
            if b".aispa/" not in previous.splitlines():
                ignore.write_bytes(previous + (b"\n" if previous and not previous.endswith(b"\n") else b"") + b".aispa/\n")
            if not store.private_history_ignored():
                raise Invalid("Git не исключает .aispa; приватные снимки не созданы")
            state = store.create(args.slug, args.title, initializer=initialize_run)
            shutil.copy2(PACKAGE / "assets/dashboard.html", store.home / "dashboard.html")
            store.append(state, "runtime-pinned")
            project_context.initialize(store, state, args.runtime)
            store.append(state, "project-context-initialized")
            project_context.refresh(store, state)
            try:
                output = {**publish(store, state), "autonomy": autonomy.view(state),
                          "requiredHostActions": dashboard_host.start_request(managed=True)}
            except (Invalid, OSError, ValueError, KeyError, TypeError):
                output = saved_without_view(store, state, {"run": state["run"]}, ok=True)
                if output is None:
                    raise
            emit(store, output, compact_mode=args.compact)
            return 0
        state = store.load()
        previous_sequence = store.read_journal()["count"]
        preparation_write = (args.action in ("template", "brief-source", "brief-map-context") and args.write
                             or args.action == "context" and args.with_template)
        read_only = not preparation_write and (
                     args.action in ("status", "context", "memory", "discovery", "plan-check", "knowledge", "commands", "page", "preflight", "brief-diagnostics")
                     or args.action == "resources" and args.operation == "view"
                     or args.action in controls.READS
                     or args.action == "test-intent" and not args.file
                     or args.action == "task-zone-extension" and not args.apply
                     or (args.action in ("idea", "repair-plan", "integration-review") and not args.file)
                     or (args.action == "documents" and not args.refresh)
                     or (args.action == "project-documents" and not any((args.role, args.path, args.sha256, args.reason))))
        if not read_only and state.get("runtimeDigest") != runtime_digest():
            raise Invalid("Пакет изменён. Продолжайте сохранённой копией runs/<run>/runtime/tools/aispa.py; обновление требует миграции")
        flow = Workflow(store, state)
        if state.get("journalRecovery", {}).get("pending") and not read_only and args.action not in (
                "journal-verify", "journal-reconcile", "context-refresh") and not (
                args.action == "participant" and args.role == "recovery-reviewer"):
            raise Invalid("Восстановление ещё не сверено: journal-context, journal-verify, journal-reconcile")
        ok = True
        receipt = None
        first_briefing_closure = False
        action = args.action
        if action == "preflight":
            host = json.loads(flow.read_input(args.file)) if args.file else None
            result = preflight.check(store, state, host, purpose=args.purpose)
            if args.write:
                result = {"ok": result["ok"], "preflight": packets.write(store, result)}
            emit(store, result, compact_mode=args.compact)
            return 0 if result["ok"] else 2
        elif action == "host-context":
            receipt = preflight.adopt(flow, json.loads(flow.read_input(args.file)), purpose=args.purpose,
                                       expected_basis=args.expected_basis)
        elif action == "brief-diagnostics":
            emit(store, brief_diagnostics.analyze(store, state), compact_mode=args.compact)
            return 0
        elif action == "model-observe":
            receipt = participants.observe_model(store, state, args.actor, json.loads(flow.read_input(args.file)))
            flow.save("model-observation-recorded")
        elif action == "resources":
            return resources_command(store, state, flow, args)
        elif action == "progress":
            receipt = flow.progress_begin(args.phase) if args.progress_action == "begin" else flow.progress_end()
        elif action in controls.READS | controls.WRITES:
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
        elif action == "page":
            from aispa_core import scale
            value = json.loads(flow.read_input(args.file))
            if args.field:
                if not isinstance(value, dict) or args.field not in value:
                    raise Invalid("Page field is absent")
                value = value[args.field]
            print(json.dumps(scale.page(value, cursor=args.cursor, limit=args.limit), ensure_ascii=False, indent=2))
            return 0
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
            receipt = {"actor": knowledge.assign(store, state, args.runtime, args.native_id, args.session, model=args.model,
                reasoning=args.reasoning, model_evidence=json.loads(flow.read_input(args.model_evidence)) if args.model_evidence else None)}
            flow.save("knowledge-writer-assigned")
        elif action == "knowledge-record":
            flow.running()
            receipt = knowledge.record(store, state, json.loads(flow.read_input(args.file)))
            flow.save("project-knowledge-recorded")
        elif action == "seal":
            first_briefing_closure = flow.seal()
        elif action == "resume":
            receipt = flow.resume(host=json.loads(flow.read_input(args.host)) if args.host else None, expected_basis=args.expected_basis)
        elif action == "finish":
            flow.finish()
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
                evidence = discovery.collect(store, state, paths=args.include, metadata_receipt=metadata_receipt, cursor=args.cursor)
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
            start_event = "clean-verification-started"
            flow.save(start_event)

            def admitted_cleanrun(kind):
                if flow.resource_admitted(kind):
                    flow.save(start_event)

            try:
                state["cleanrun"] = cleanroom.execute(store, state, command, args.timeout,
                                                     on_admitted=admitted_cleanrun)
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
        elif action == "project-documents":
            if not any((args.role, args.path, args.sha256, args.reason)):
                print(json.dumps(project_documents.view(store, state), ensure_ascii=False, indent=2))
                return 0
            flow.running()
            if not all((args.role, args.path, args.sha256, args.reason)):
                raise Invalid("Для привязки нужны --role, --path, --sha256 и --reason")
            receipt = project_documents.bind(store, state, args.role, args.path, args.sha256, args.reason)
            flow.save("project-document-bound:" + args.role)
        elif action == "project-context":
            receipt = project_context.initialize(store, state, args.runtime)
            flow.save("project-context-adapted")
        elif action == "context-refresh":
            receipt = project_context.refresh(store, state)
        elif action == "team":
            receipt = flow.pipeline.team(args.file)
        elif action == "assign":
            receipt = {"actor": flow.pipeline.assign(args.id, args.role, args.runtime, args.native_id, args.session, model=args.model,
                reasoning=args.reasoning, model_evidence=json.loads(flow.read_input(args.model_evidence)) if args.model_evidence else None)}
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
        elif action == "task-zone-extension":
            if not args.file:
                if args.apply or args.actor or args.expected_journal_sha256:
                    raise Invalid("task-zone-extension: запись требует --file, --actor и свежий CAS")
                print(json.dumps(task_zone_extension.inspect(store, state, args.id), ensure_ascii=False, indent=2))
                return 0
            if not args.actor:
                raise Invalid("task-zone-extension: нужен реальный --actor planner/coordinator")
            report = json.loads(flow.read_input(args.file))
            if not args.apply:
                if args.expected_journal_sha256:
                    raise Invalid("task-zone-extension: CAS применяется только с --apply")
                print(json.dumps(task_zone_extension.prepare(flow, args.id, report, actor=args.actor), ensure_ascii=False, indent=2))
                return 0
            receipt = task_zone_extension.apply(flow, args.id, report, actor=args.actor,
                                                expected_journal_sha256=args.expected_journal_sha256)
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
        try:
            view = publish(store, state)
        except (Invalid, OSError, ValueError, KeyError, TypeError):
            output = saved_without_view(store, state, receipt, ok=ok, previous_sequence=previous_sequence)
            if output is None:
                raise
            emit(store, output, compact_mode=args.compact)
            return 0 if ok else 2
        output = view if action == "status" else {
            "ok": ok, "run": state["run"], "runDirectory": view["runDirectory"], "status": view["status"],
            "stage": view["displayStage"], "gates": view["gates"], "receipt": receipt,
            "task": next(({"id": t["id"], "status": t["status"], "phase": t.get("phase"), "note": t.get("note")}
                          for t in state["tasks"] if t["id"] == getattr(args, "id", None)), None),
            "counts": {name: sum(t["status"] == name for t in state["tasks"])
                       for name in ("pending", "running", "blocked", "completed")},
            "contextIndex": str(store.home / "context/INDEX.md")}
        autonomy_contract = autonomy.view(state)
        if autonomy_contract is not None:
            output["autonomy"] = autonomy_contract
        if first_briefing_closure:
            output["requiredHostActions"] = autonomy.start_request(state)
        if action == "resume" and ok:
            output["requiredHostActions"] = dashboard_host.start_request(managed=preflight.enabled(state))
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
