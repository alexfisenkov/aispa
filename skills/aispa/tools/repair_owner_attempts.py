#!/usr/bin/env python3
"""Inspect, prepare, or select one exact 0.4.0 owner-attempt runtime bridge."""
import argparse
import json
import sys

from aispa_core import runtime_owner_attempt_repair as repair
from aispa_core.store import Invalid


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect")
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--candidate-package", required=True)
    prepare.add_argument("--expected-journal-sha256", required=True)
    prepare.add_argument("--expected-approval-sha256", required=True)
    prepare.add_argument("--owner-quote", required=True)
    apply = actions.add_parser("apply")
    apply.add_argument("--manifest-file", required=True)
    apply.add_argument("--expected-manifest-sha256", required=True)
    apply.add_argument("--expected-journal-sha256", required=True)
    apply.add_argument("--approval-file", required=True)
    apply.add_argument("--expected-approval-sha256", required=True)
    apply.add_argument("--owner-quote", required=True)
    apply.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = vars(args).copy()
        action, root = values.pop("action"), values.pop("root")
        print(json.dumps(getattr(repair, action)(root, **values), ensure_ascii=False, indent=2))
        return 0
    except (Invalid, OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print("AISpa: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
