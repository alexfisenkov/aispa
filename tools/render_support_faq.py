#!/usr/bin/env python3
"""Authoring tool. The installed CLI uses its packaged helper, not this file."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import support_faq
from aispa_core.store import Invalid


def rebind(package):
    """Recompute sha256 of the files already listed in source.files; write the canon only if a hash changed."""
    canon = Path(package).absolute() / "references/support-faq.json"
    raw = support_faq._file(Path(package).absolute(), "references/support-faq.json")
    try:
        value = json.loads(raw.decode("utf-8"))
        rows = value["source"]["files"]
        paths = [row["path"] for row in rows]
    except (UnicodeError, ValueError, KeyError, TypeError) as error:
        raise Invalid("AISpa FAQ: canon has no readable source.files") from error
    # A missing or linked path raises Invalid naming it; nothing has been written yet.
    fresh = []
    for name in paths:
        try:
            fresh.append(hashlib.sha256(support_faq._file(Path(package).absolute(), name)).hexdigest())
        except Invalid as error:
            raise Invalid("%s (%s)" % (error, name)) from error
    if all(row.get("sha256") == new for row, new in zip(rows, fresh)):
        return
    for row, new in zip(rows, fresh):
        row["sha256"] = new
    canon.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render source-bound AISpa support Q/A")
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--id", action="append")
    parser.add_argument("--format", choices=("markdown", "html"), default="markdown")
    parser.add_argument("--output", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--check", action="store_true", help="Verify existing outputs without writing")
    parser.add_argument("--rebind", action="store_true",
                        help="Re-hash the already listed source files; run it only after the answers are re-read against the changed files")
    args = parser.parse_args(argv)
    try:
        if args.rebind and args.check:
            raise Invalid("--rebind writes the canon; it cannot be combined with --check")
        if args.rebind:
            rebind(args.package)
        selected = support_faq.select(args.package, version=args.version, ids=args.id)
        rendered = support_faq.render(selected, format=args.format)
        output, receipt = Path(args.output), Path(args.receipt)
        encoded = rendered["text"].encode("utf-8")
        metadata = (json.dumps(rendered["receipt"], ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if output.absolute() == receipt.absolute() or output.is_symlink() or receipt.is_symlink():
            raise Invalid("FAQ outputs must be distinct ordinary paths")
        if args.check:
            if not output.is_file() or not receipt.is_file() or output.read_bytes() != encoded or receipt.read_bytes() != metadata:
                raise Invalid("Generated FAQ or receipt differs from canonical source")
        else:
            output.write_bytes(encoded)
            receipt.write_bytes(metadata)
        print(json.dumps(rendered["receipt"], ensure_ascii=False))
        return 0
    except (Invalid, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
