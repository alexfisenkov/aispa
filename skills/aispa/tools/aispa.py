#!/usr/bin/env python3
"""AISpa release updater — the one command left from AISpa 0.x.

    python3 tools/aispa.py update-check                       # read-only, JSON
    python3 tools/aispa.py update-prepare                     # stage the newer Release, JSON review
    python3 tools/aispa.py update-apply --candidate ID --confirm
    python3 tools/aispa.py update-backups                     # read-only: copies an update kept, newest first
    python3 tools/aispa.py update-rollback [--backup ID] --confirm   # no ID: the newest one
    python3 tools/aispa.py legacy-run [--root DIR]            # read-only: the project's run of AISpa 0.x
    python3 tools/aispa.py --version

An install made from a GitHub Release (the 0.x updater or tools/install.py)
keeps updating through this file. An install made by `npx skills` is updated by
`npx skills update aispa`; here it is only reported. Every other 0.x command
(init, status, dashboard, resume, …) answers with one plain message: the run
tooling of 1.0 is .aispa-work/ap.py, started by SKILL.md.
"""
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "tools"))

from aispa_core import VERSION  # noqa: E402
from aispa_core.legacy import old_run  # noqa: E402
from aispa_core.store import Invalid  # noqa: E402

UPDATE = ("update-check", "update-prepare", "update-apply", "update-rollback", "update-backups")
SKILLS_CLI = "npx skills update aispa"
SOURCE = "alexfisenkov/aispa"


def _lock_entry(lock):
    try:
        skills = json.loads(lock.read_text(encoding="utf-8")).get("skills")
    except (OSError, ValueError, AttributeError):
        return None
    entry = skills.get("aispa") if isinstance(skills, dict) else None
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return {}
    source = entry.get("source")
    return entry if source is None or (isinstance(source, str) and SOURCE in source) else None


def _stamp(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _release_installed_at(real):
    """Newest time a GitHub Release was put into this folder: the 0.x/1.x updater keeps
    a receipt beside it, tools/install.py one in ~/.aispa-installs. None — never."""
    times = []
    for backup in real.parent.glob(".aispa-backup-*"):
        receipt = _receipt(backup)
        if receipt and receipt.get("target") == str(real) and receipt.get("status") == "installed":
            times.append((backup / "receipt.json").stat().st_mtime)
    for done in (Path.home() / ".aispa-installs").glob("aispa-install-*/installed.json"):
        try:
            value = json.loads(done.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("destination") == str(real):
                times.append(done.stat().st_mtime)
        except (OSError, ValueError):
            continue
    return max(times) if times else None


def skills_cli_managed(package):
    """`npx skills` keeps its copy in <X>/.agents/skills/aispa, or — copy mode, no
    symlinks — straight in an agent's folder (~/.claude/skills/aispa, <project>/.cursor/
    skills/aispa), and names it in a lock: ~/.agents/.skill-lock.json (global) or
    <project>/skills-lock.json. A folder a Release was installed into after the lock
    entry was written stays a release copy."""
    real = package.resolve()
    if real.name != "aispa" or real.parent.name != "skills":
        return False
    agent = real.parent.parent
    if agent.name == ".agents":
        return any(_lock_entry(lock) is not None
                   for lock in (agent / ".skill-lock.json", agent.parent / "skills-lock.json"))
    home = Path.home().resolve()
    try:
        depth = len(real.parent.relative_to(home).parts)
    except ValueError:
        depth = None
    candidates = []
    if depth is not None and depth <= 3 and real.parent.relative_to(home).parts[0].startswith("."):
        candidates.append(home / ".agents" / ".skill-lock.json")   # ~/.claude/skills, ~/.gemini/config/skills
    if agent.name.startswith("."):
        candidates.append(agent.parent / "skills-lock.json")      # <project>/.claude/skills
    for lock in candidates:
        entry = _lock_entry(lock)
        if entry is None:
            continue
        written = _stamp(entry.get("updatedAt") or entry.get("installedAt") or "")
        if written is None:
            try:
                written = lock.stat().st_mtime
            except OSError:
                continue
        released = _release_installed_at(real)
        if released is None or written >= released:
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


def option(args, name, required=True):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    if not required:
        return None
    raise Invalid("нужен %s <id> из ответа предыдущего шага" % name)


def _receipt(backup):
    path = backup / "receipt.json"
    try:
        if backup.is_symlink() or path.is_symlink() or path.stat().st_size > 1 << 20:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == 1 else None


def backups(package):
    """Copies this folder's updates kept beside it, newest first; only those that can still undo."""
    real = package.resolve()
    found = []
    for backup in real.parent.glob(".aispa-backup-*"):
        receipt = _receipt(backup)
        if not receipt or receipt.get("target") != str(real) or receipt.get("status") != "installed":
            continue
        if not (backup / "package").is_dir():
            continue
        at = (backup / "receipt.json").stat().st_mtime
        found.append((at, {"backupId": backup.name, "version": receipt.get("version"),
                           "previousVersion": receipt.get("previousVersion"),
                           "at": datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "package": str(backup / "package")}))
    return [b for _, b in sorted(found, key=lambda pair: pair[0], reverse=True)]


GLOBAL_ROOTS = (".claude/skills", ".agents/skills", ".codex/skills", ".cursor/skills", ".gemini/skills",
                ".gemini/config/skills", ".openclaw/skills")
PROJECT_ROOTS = (".claude/skills", ".agents/skills", ".cursor/skills", ".gemini/skills")


def _package_version(folder):
    try:
        text = (folder / "tools" / "aispa_core" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VERSION = "):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def other_copies(package, root):
    """Other AISpa folders an agent would load beside this one: any copy of 0.x, and a
    ~/.codex/skills/aispa next to ~/.agents/skills/aispa (Codex reads both). Read-only."""
    home, me = Path.home(), package.resolve()
    places = [home / r / "aispa" for r in GLOBAL_ROOTS] + [Path(root) / r / "aispa" for r in PROJECT_ROOTS]
    seen, found = {me}, []
    for place in places:
        try:
            real = place.resolve()
            if real in seen or not (real / "SKILL.md").is_file():
                continue
        except OSError:
            continue
        seen.add(real)
        version = _package_version(real)
        codex_twice = (place == home / ".codex/skills/aispa" and (home / ".agents/skills/aispa").exists()
                       and (home / ".agents/skills/aispa").resolve() != real)
        if (version or "").startswith("0.") or codex_twice:
            found.append({"path": str(place), "version": version})
    return found


def update(action, args, root=Path(".")):
    from aispa_core import updates
    if action == "update-backups":
        return {"status": "backups", "installedVersion": VERSION, "backups": backups(PACKAGE)}
    managed = skills_cli_managed(PACKAGE)
    if action == "update-check":
        copies = other_copies(PACKAGE, root)
        if managed:
            return {"status": "skills_cli", "repository": updates.REPOSITORY,
                    "installedVersion": VERSION, "manager": "skills-cli",
                    "installAllowed": False, "installReason": "skills_cli", "otherCopies": copies,
                    "message": "Эту копию ставил npx skills — обновление: %s" % SKILLS_CLI}
        return dict(updates.check(PACKAGE), manager="release", otherCopies=copies)
    if action == "update-rollback":
        ident = option(args, "--backup", required=False)
        if ident is None:
            kept = backups(PACKAGE)
            if not kept:
                raise Invalid("рядом с навыком нет копии, к которой можно откатиться")
            ident = kept[0]["backupId"]
        return updates.rollback(PACKAGE, ident, confirmed="--confirm" in args)
    if managed:
        raise Invalid("эту копию ставил npx skills — обнови её командой %s" % SKILLS_CLI)
    if action == "update-prepare":
        return updates.prepare(PACKAGE)
    return updates.apply(PACKAGE, option(args, "--candidate"), confirmed="--confirm" in args)


def retired(command, root):
    """Ответ на команду прогона 0.x: сказать как есть и ничего не трогать."""
    skill = PACKAGE / "SKILL.md"
    text = ("AISpa обновлена до %s, команды прежней версии (%s) здесь больше не работают. "
            "Перечитай %s и работай по нему (новая сборка — /aispa, в Codex — $aispa)."
            % (VERSION, command, skill))
    old = old_run(root)
    if old and old["finished"] is not True and old["cli"]:
        text += (" %s AISpa 0.x в этом проекте (%s%s) не потеряна: её доделывает "
                 "её же версия — прочитай %s и все её команды давай через python3 %s. Спроси "
                 "пользователя: доделать её или начать заново в новой версии; на «продолжи» — доделать."
                 % ("Незаконченная сборка" if old["finished"] is False else "Сборка",
                    old["title"] or old["run"], ", этап %s" % old["stage"] if old["stage"] else "",
                    old["skill"], old["cli"]))
    elif old:
        text += " Папка прежних сборок %s остаётся как есть, её не трогать." % old["path"]
    kept = backups(PACKAGE)
    if kept:
        text += (" Прежняя копия навыка (и любые правки, сделанные в ней руками) лежит в %s; "
                 "откат — update-rollback --confirm, только по слову пользователя." % kept[0]["package"])
    print(json.dumps({"status": "upgraded", "version": VERSION, "command": command,
                      "skill": str(skill), "oldRuns": old["path"] if old else None, "oldRun": old,
                      "backup": kept[0] if kept else None, "message": text},
                     ensure_ascii=False, indent=2))
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
    if args[0] == "legacy-run":
        print(json.dumps({"status": "legacy_run", "oldRun": old_run(root)}, ensure_ascii=False, indent=2))
        return 0
    if args[0] not in UPDATE:
        return retired(args[0], root)
    try:
        result = update(args[0], args[1:], root)
    except (Invalid, OSError, ValueError, http.client.HTTPException) as error:
        print("AISpa: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
