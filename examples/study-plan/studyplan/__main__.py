"""Консольный интерфейс локального планировщика."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .domain import DomainError, plan_tasks, read_tasks


def _budget(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("бюджет должен быть целым числом минут")
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("бюджет должен быть целым числом минут") from exc


def _source_is_output(source: Path, output: Path) -> bool:
    # resolve covers spelling aliases; samefile also catches hard links.
    if source.resolve() == output.resolve():
        return True
    try:
        return source.samefile(output)
    except OSError:
        return False


def _save_json(output: Path, payload: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="План учебного дня из CSV.")
    parser.add_argument("csv", type=Path, help="CSV-файл задач в UTF-8")
    parser.add_argument("--budget", type=_budget, required=True, help="доступное время в минутах (0–1440)")
    parser.add_argument("--output", type=Path, help="путь для сохранения JSON вместо stdout")
    args = parser.parse_args(argv)

    try:
        if args.output is not None and _source_is_output(args.csv, args.output):
            raise DomainError("Выходной файл совпадает с исходным CSV")
        result = plan_tasks(read_tasks(args.csv), args.budget)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(payload)
        else:
            _save_json(args.output, payload)
    except (DomainError, OSError, UnicodeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
