#!/usr/bin/env python3
"""Explicit offline repair of official active AISpa 0.3.5 source-quote runs."""
import argparse
import json
import sys

from aispa_core.runtime_repair import apply, inspect
from aispa_core.store import Invalid


def main(argv=None):
    # Match the regular CLI's UTF-8 JSON/diagnostic contract in Windows pipes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--apply", action="store_true", help="Stage repair and append one journal event")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-journal-sha256")
    parser.add_argument("--approval-file", help="UTF-8 file containing the real owner's repair approval")
    args = parser.parse_args(argv)
    try:
        if not args.apply and any((args.confirm, args.expected_journal_sha256, args.approval_file)):
            raise Invalid("Runtime repair: write options require --apply")
        result = (apply(args.root, confirm=args.confirm, expected_journal_sha256=args.expected_journal_sha256,
                        approval_file=args.approval_file) if args.apply else inspect(args.root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print("AISpa: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
