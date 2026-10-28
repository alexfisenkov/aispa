#!/usr/bin/env python3
"""AISpa release updater — the one command left from AISpa 0.x.

    python3 tools/aispa.py update-check                       # read-only, JSON
    python3 tools/aispa.py update-prepare                     # stage the newer Release, JSON review
    python3 tools/aispa.py update-apply --candidate ID --confirm
    python3 tools/aispa.py update-rollback --backup ID --confirm
    python3 tools/aispa.py --version

An install made from a GitHub Release (the 0.x updater or tools/install.py)
keeps updating through this file. An install made by `npx skills` is updated by
`npx skills update aispa`; here it is only reported. Every other 0.x command
(init, status, dashboard, resume, …) answers with one plain message: the run
tooling of 1.0 is .aispa-work/ap.py, started by SKILL.md.
"""
import http.client
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "tools"))

from aispa_core import VERSION  # noqa: E402
from aispa_core.store import Invalid  # noqa: E402

UPDATE = ("update-check", "update-prepare", "update-apply", "update-rollback")
SKILLS_CLI = "npx skills update aispa"


def skills_cli_managed(package):
    """`npx skills` keeps its copy in <X>/.agents/skills/aispa and names it in a lock:
    <X>/.agents/.skill-lock.json (global) or <X>/skills-lock.json (project)."""
    real = package.resolve()
    if (real.name, real.parent.name, real.parent.parent.name) != ("aispa", "skills", ".agents"):
        return False
    agents = real.parent.parent
    for lock in (agents / ".skill-lock.json", agents.parent / "skills-lock.json"):
        try:
            skills = json.loads(lock.read_text(encoding="utf-8")).get("skills")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(skills, dict) and "aispa" in skills:
            return True
    return False


def split_args(argv):
    """0.x put --root DIR and --compact before the subcommand; both are accepted anywhere."""
    rest, root, i = [], ".", 0
    while i < len(argv):
        a = argv[i]
        if a == "--root" and i + 1 < len(argv):
            root, i = argv[i + 1], i + 2
            continue
        if a.startswith("--root="):
            root = a.split("=", 1)[1]
        elif a != "--compact":
            rest.append(a)
        i += 1
    return rest, Path(root)


def option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    raise Invalid("нужен %s <id> из ответа предыдущего шага" % name)


def update(action, args):
    from aispa_core import updates
    managed = skills_cli_managed(PACKAGE)
    if action == "update-check":
        if managed:
            return {"status": "skills_cli", "repository": updates.REPOSITORY,
                    "installedVersion": VERSION, "manager": "skills-cli",
                    "installAllowed": False, "installReason": "skills_cli",
                    "message": "Эту копию ставил npx skills — обновление: %s" % SKILLS_CLI}
        return dict(updates.check(PACKAGE), manager="release")
    if action == "update-rollback":
        return updates.rollback(PACKAGE, option(args, "--backup"), confirmed="--confirm" in args)
    if managed:
        raise Invalid("эту копию ставил npx skills — обнови её командой %s" % SKILLS_CLI)
    if action == "update-prepare":
        return updates.prepare(PACKAGE)
    return updates.apply(PACKAGE, option(args, "--candidate"), confirmed="--confirm" in args)


def retired(command, root):
    """Ответ на команду прогона 0.x: сказать как есть и ничего не трогать."""
    skill = PACKAGE / "SKILL.md"
    text = ("AISpa обновлена до %s, команды прежней версии (%s) больше не работают. "
            "Перечитай %s и работай по нему: новый прогон начинается с /aispa (в Codex — $aispa)."
            % (VERSION, command, skill))
    old = root / ".aispa"
    if old.is_dir():
        text += (" В проекте есть папка прогона AISpa 0.x (%s): новая версия его не продолжает, "
                 "папка не тронута — сборку можно начать заново." % old)
    print(json.dumps({"status": "upgraded", "version": VERSION, "command": command,
                      "skill": str(skill), "oldRuns": str(old) if old.is_dir() else None,
                      "message": text}, ensure_ascii=False, indent=2))
    print("AISpa: " + text, file=sys.stderr)
    return 2


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args, root = split_args(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] not in UPDATE:
        return retired(args[0], root)
    try:
        result = update(args[0], args[1:])
    except (Invalid, OSError, ValueError, http.client.HTTPException) as error:
        print("AISpa: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
