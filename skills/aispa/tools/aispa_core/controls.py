"""CLI controls for source-first planning, usage and recovery."""
import argparse
from . import participants, preparation, briefmap, design_contract, telemetry, task_reports
from .contracts import require

READS = {"preparation", "brief-source", "brief-map-context", "template", "ready", "participants", "journal-context", "execution-obligations"}
WRITES = {"brief-extract", "brief-review", "brief-map", "scenarios", "interfaces",
          "reference", "participant", "usage-import", "wait", "answered",
          "journal-verify", "journal-reconcile", "test-intent", "execution-obligation"}


def parsers(sub):
    for name in sorted(READS - {"template"}):
        sub.add_parser(name)
    t = sub.add_parser("template")
    t.add_argument("kind", choices=("brief-extract", "brief-review", "brief-map", "scenarios", "interfaces",
                                   "failure-checks", *task_reports.ROLES))
    t.add_argument("--actor")
    t.add_argument("--task")
    t.add_argument("--write", action="store_true")
    test = sub.add_parser("test-intent")
    test.add_argument("--task", required=True)
    test.add_argument("--file")
    for name in ("brief-extract", "brief-review", "brief-map", "scenarios", "interfaces", "reference", "journal-reconcile", "execution-obligation"):
        sub.add_parser(name).add_argument("--file", required=True)
    p = sub.add_parser("participant")
    p.add_argument("--role", required=True, choices=sorted(participants.ROLES - participants.TASK_ROLES - {"knowledge-writer"}))
    p.add_argument("--runtime", required=True, choices=("codex", "claude-code", "cursor"))
    p.add_argument("--native-id", required=True); p.add_argument("--session", required=True); p.add_argument("--model")
    u = sub.add_parser("usage-import")
    u.add_argument("--file", required=True); u.add_argument("--format", required=True, choices=("claude", "codex"))
    u.add_argument("--participant")
    u.add_argument("--final", action="store_true")
    u.add_argument("--includes-final-coordinator-reply", action="store_true")
    sub.add_parser("wait").add_argument("--note", required=True)
    sub.add_parser("answered").add_argument("--file", required=True)
    v = sub.add_parser("journal-verify")
    v.add_argument("--timeout", type=int, default=180)
    v.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("journal-inspect")
    a = sub.add_parser("journal-recover")
    a.add_argument("--sha256", required=True); a.add_argument("--confirm", action="store_true")
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("operation", choices=("start", "status", "stop", "archive"))
    dashboard.add_argument("--port", type=int, default=0)


def execute(store, state, flow, args):
    import json
    action = args.action
    actor_check = preparation.actor_check(store)
    if action in ("execution-obligation", "execution-obligations"):
        from . import execution_obligations
        if action == "execution-obligations":
            return execution_obligations.view(store, state)
        flow.running()
        result = execution_obligations.record(store, state, json.loads(flow.read_input(args.file)))
        flow.save("execution-obligation-recorded:" + result["id"])
        return result
    if action == "test-intent":
        from . import test_intent
        if not args.file:
            return test_intent.view(store, state, args.task)
        flow.build_ready()
        result = test_intent.record(store, state, args.task, json.loads(flow.read_input(args.file)))
        flow.save("test-intent-recorded:" + args.task)
        return result
    if action == "brief-source":
        return preparation.source_context(store, state)
    if action == "brief-map-context":
        return preparation.map_context(store, state)
    if action == "preparation":
        return {"brief": briefmap.view(store, state, actor_check), "design": design_contract.view(store, state)}
    if action == "template":
        if args.kind == "failure-checks":
            require(args.task is None and args.actor is None and not args.write,
                    "Шаблон failure-checks выводится без --task, --actor и --write")
            from . import failure_checks
            return failure_checks.template()
        if args.kind in task_reports.ROLES:
            require(args.task, "Для шаблона роли нужен --task")
            require(args.actor is None, "Actor шаблона задачи берётся из назначенной роли; --actor не нужен")
            report = task_reports.template(flow.pipeline, args.task, args.kind)
            return task_reports.write(store, report, args.task, args.kind) if args.write else report
        require(args.task is None and not args.write,
                "--task и --write поддерживаются только для шаблонов worker, critic1, critic2, acceptor")
        return preparation.template(store, state, args.kind, actor=args.actor)
    if action == "ready":
        return flow.ready()
    if action == "participants":
        return participants.list_participants(store, state)
    if action == "participant":
        if args.role != "recovery-reviewer":
            flow.running()
        value = {"actor": participants.register(store, state, args.role, args.runtime, args.native_id,
                                                args.session, model=args.model)}
        flow.save("participant-assigned:" + args.role)
        return value
    if action == "usage-import":
        value = telemetry.import_log(store, state, args.file, format=args.format, participant=args.participant,
                                     final=args.final, includes_final_coordinator_reply=args.includes_final_coordinator_reply)
        flow.save("native-usage-imported")
        return value
    if action in ("wait", "answered"):
        return flow.wait(args.note) if action == "wait" else flow.answered(args.file)
    if action.startswith("journal-"):
        from . import recovery
        if action == "journal-context":
            return recovery.context(store, state)
        if action == "journal-verify":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            receipt = recovery.verify(store, state, command, args.timeout)
            state["journalRecovery"]["verification"] = receipt
            flow.save("recovery-verification-executed")
            return receipt
        result = recovery.reconcile(store, state, json.loads(flow.read_input(args.file)))
        flow.save("recovery-reconciled" if not state["journalRecovery"]["pending"] else "recovery-blocked")
        return result
    flow.running()
    report = json.loads(flow.read_input(args.file))
    result = preparation.owner_reference(store, state, report) if action == "reference" else preparation.record(
        store, state, action, report)
    flow.save(action + "-recorded")
    return result
