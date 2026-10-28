#!/usr/bin/env python3
"""AISpa package tool: version, update, rollback, Claude Code agents and the FAQ. No run, no network but GitHub Releases."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from aispa_core import VERSION
from aispa_core.store import Invalid

PACKAGE = Path(__file__).resolve().parents[1]
COMMANDS = ("update-check", "update-prepare", "update-apply", "update-rollback", "update-agents", "faq")
# Global options of the 0.4/0.5 tool, accepted before an old command so it is still recognised.
OLD_GLOBALS = {"--root": 1, "--compact": 0, "--full": 0}


def old_run_command(argv):
    """The words of a 0.4/0.5 run command (dashboard, status, init…), or None for this tool's own."""
    index = 0
    while index < len(argv) and argv[index].split("=", 1)[0] in OLD_GLOBALS:
        index += 1 + (OLD_GLOBALS[argv[index]] if "=" not in argv[index] else 0)
    rest = argv[index:]
    if not rest or rest[0].startswith("-") or rest[0] in COMMANDS:
        return None
    return rest


def parser():
    p = argparse.ArgumentParser(description="AISpa " + VERSION)
    p.add_argument("--version", action="version", version=VERSION)
    for name, values in OLD_GLOBALS.items():
        if values:
            p.add_argument(name, help=argparse.SUPPRESS)
        else:
            p.add_argument(name, action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action", required=True)
    faq = sub.add_parser("faq", help="Read source-bound support facts from this exact package, without a run")
    faq.add_argument("--version", dest="faq_version", required=True, help="Detected installed/pinned package version")
    faq.add_argument("--id", action="append")
    faq.add_argument("--sha256", help="Expected canonical input SHA-256")
    sub.add_parser("update-check")
    sub.add_parser("update-prepare")
    apply = sub.add_parser("update-apply")
    apply.add_argument("--candidate", required=True); apply.add_argument("--confirm", action="store_true")
    rollback = sub.add_parser("update-rollback")
    rollback.add_argument("--backup", required=True); rollback.add_argument("--confirm", action="store_true")
    agents_cmd = sub.add_parser("update-agents", help="Install missing Claude Code agent files beside the skill")
    agents_cmd.add_argument("--confirm", action="store_true")
    return p


def execute(args):
    if args.action == "faq":
        from aispa_core import support_faq
        print(json.dumps(support_faq.select(PACKAGE, version=args.faq_version, ids=args.id,
                         expected_sha256=args.sha256), ensure_ascii=False, indent=2))
        return 0
    from aispa_core import updates
    if args.action == "update-check":
        result = updates.check(PACKAGE)
    elif args.action == "update-prepare":
        result = updates.prepare(PACKAGE)
    elif args.action == "update-apply":
        result = updates.apply(PACKAGE, args.candidate, confirmed=args.confirm)
    elif args.action == "update-agents":
        result = updates.install_agents(PACKAGE, confirmed=args.confirm)
    else:
        result = updates.rollback(PACKAGE, args.backup, confirmed=args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    # JSON and diagnostics have the same UTF-8 contract in Windows pipes/console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    argv = sys.argv[1:] if argv is None else argv
    old = old_run_command(argv)
    if old is not None:
        print("AISpa " + VERSION + ": это команда прогона прежней версии — запусти её закреплённой копией: "
              "python3 .aispa/runs/<run>/runtime/tools/aispa.py " + " ".join(argv), file=sys.stderr)
        return 2
    try:
        return execute(parser().parse_args(argv))
    except (Invalid, OSError, ValueError) as error:
        # Notes carry what a failed undo could not restore (update-apply/rollback/agents).
        print("\n".join(["AISpa: " + str(error), *getattr(error, "__notes__", ())]), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
