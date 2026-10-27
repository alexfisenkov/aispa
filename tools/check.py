#!/usr/bin/env python3
"""One project check: all unittest modules and the dashboard Node tests.

    python3 tools/check.py                         # whole suite: unittest + node --test tests/dashboard.test.cjs
    python3 tools/check.py --file tests/test_x.py  # one file (a .cjs file runs under node --test)

Exits non-zero when anything fails or cannot run. Standard library only.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = "tests/dashboard.test.cjs"


def unittest_command(root, pattern="test*.py", start="tests", verbose=False):
    # Same form as the documented command: tests import each other by module name.
    command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", start, "-p", pattern]
    return command + (["-v"] if verbose else [])


def node_command(path):
    node = shutil.which("node")
    return None if node is None else [node, "--test", str(path)]


def private_env(scratch):
    """Environment of the test processes: calibration goes to a throw-away file, never to ~/.aispa."""
    return {**os.environ, "AISPA_CALIBRATION_FILE": str(Path(scratch) / "calibration.json")}


def run(name, command, root, env=None):
    if command is None:
        print("check: %s не запущен — не найден node" % name, file=sys.stderr)
        return 1
    print("check: %s" % name, flush=True)
    return 1 if subprocess.run(command, cwd=root, env=env).returncode else 0


def file_steps(root, name, verbose):
    path = (root / name).resolve()
    if not path.is_file():
        print("check: файл не найден: %s" % name, file=sys.stderr)
        return None
    if path.suffix == ".cjs":
        return [(path.name, node_command(path))]
    if path.suffix != ".py":
        print("check: поддерживаются файлы .py и .cjs: %s" % name, file=sys.stderr)
        return None
    start = path.parent.relative_to(root).as_posix() if path.parent.is_relative_to(root) else str(path.parent)
    return [(path.name, unittest_command(root, pattern=path.name, start=start, verbose=verbose))]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка проекта AISpa: unittest и тесты панели")
    parser.add_argument("--file", help="Один файл тестов, например tests/test_cli.py или tests/dashboard.test.cjs")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный вывод unittest")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.file:
        steps = file_steps(root, args.file, args.verbose)
        if steps is None:
            return 2
    else:
        steps = [("unittest", unittest_command(root, verbose=args.verbose)),
                 ("node --test " + DASHBOARD, node_command(root / DASHBOARD))]
    with tempfile.TemporaryDirectory(prefix="aispa-check-") as scratch:
        env = private_env(scratch)
        failed = [name for name, command in steps if run(name, command, root, env)]
    if failed:
        print("check: FAILED — " + ", ".join(failed), file=sys.stderr)
        return 1
    print("check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
