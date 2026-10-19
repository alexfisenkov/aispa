"""Local append-only history and confined, immutable input artifacts."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from . import VERSION, SCHEMA
from .portable import relative_name, is_link, executable, match_name

COMMON_INPUTS = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md", ".gitignore", "package.json",
                 "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "pyproject.toml",
                 "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "Cargo.toml", "Cargo.lock", "go.mod",
                 "go.sum", "tsconfig.json", "jsconfig.json", "Makefile", "Dockerfile", "compose.yaml"}


def common_input(name):
    names = {match_name(item) for item in COMMON_INPUTS}
    name = match_name(name)
    return "/" not in name and (name in names or name.startswith("requirements") and name.endswith(".txt"))


class Invalid(ValueError):
    pass


def canonical_zone(value):
    """One spelling for relative task paths before matching or fingerprinting."""
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\0" in value:
        raise Invalid("Некорректная зона задачи")
    try:
        normalized = relative_name(value, root=True)
    except ValueError as exc:
        raise Invalid("Некорректная зона задачи") from exc
    path = Path(normalized)
    if path.parts and path.parts[0].casefold() in (".git", ".aispa"):
        raise Invalid("Некорректная зона задачи")
    return match_name(normalized)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".aispa-write-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def confined(base, name):
    try:
        name = relative_name(name)
    except ValueError as exc:
        raise Invalid("Нужен относительный путь внутри прогона") from exc
    candidate = base
    for part in Path(name).parts:
        candidate = candidate / part
        if is_link(candidate):
            raise Invalid("Ссылки и junction в путях свидетельств запрещены")
    result = candidate.resolve()
    if not result.is_relative_to(base.resolve()):
        raise Invalid("Путь выходит за пределы прогона")
    return result


class Journal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / ".aispa"
        if is_link(self.home):
            raise Invalid(".aispa не должна быть символической ссылкой")
        self.run = None

    @contextmanager
    def lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        lock = self.home / ".lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Invalid("AISpa уже занят. Если процесс прерван, проверьте PID в .aispa/.lock перед удалением блокировки")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            yield
        finally:
            lock.unlink(missing_ok=True)

    def create(self, slug, title, initializer=None):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise Invalid("Имя прогона: латинские буквы, цифры и дефис")
        if (self.home / "active.json").exists():
            old = self.load(handoff_only=True)
            if old["status"] not in ("accepted", "delivered_with_remaining", "cancelled"):
                raise Invalid("Предыдущий прогон не завершён; продолжите его или явно отмените")
        ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + slug + "-" + uuid.uuid4().hex[:8]
        self.run = confined(self.home, "runs/" + ident)
        self.run.mkdir(parents=True)
        state = {"schema": SCHEMA, "version": VERSION, "run": ident, "title": title,
                 "status": "running", "stage": "brief", "createdAt": timestamp(),
                 "briefs": [], "requirements": [], "spec": None, "tasks": [],
                 "gates": {}, "verification": None, "waiting": None, "notes": []}
        state.update(startedAt=state["createdAt"], activeSince=state["createdAt"], elapsedSeconds=0.0)
        if initializer is not None:
            initializer(state)  # Failure leaves the previous active pointer intact.
        self.append(state, "init")
        atomic_json(self.home / "active.json", {"run": ident})
        return state

    def load(self, *, handoff_only=False):
        try:
            pointer = json.loads(confined(self.home, "active.json").read_text(encoding="utf-8"))
            self.run = confined(self.home / "runs", pointer["run"])
            lines = confined(self.run, "events.jsonl").read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError, KeyError) as e:
            raise Invalid("Не удалось прочитать журнал; init нужен только для нового проекта") from e
        previous = None
        state = None
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
                saved = row.pop("hash")
                if row["sequence"] != number or row["previous"] != previous or digest(row) != saved:
                    raise Invalid("Нарушена цепочка журнала")
                previous, state = saved, row["state"]
            except (KeyError, ValueError) as e:
                raise Invalid(f"Повреждён журнал, запись {number}; автоматическое исправление запрещено") from e
        if not state or state.get("schema") != SCHEMA:
            raise Invalid("Неподдерживаемая схема журнала")
        if state.get("version") != VERSION:
            supported = {"0.1.0-alpha.1", "0.1.0-alpha.2", "0.1.0-alpha.3", "0.1.0-alpha.4", "0.1.0-alpha.5", "0.1.0-alpha.6"}
            if not (handoff_only and state.get("version") in supported
                    and state.get("status") in ("accepted", "delivered_with_remaining", "cancelled")):
                raise Invalid("Версия движка отличается: активный прогон продолжайте закреплённым runtime; handoff доступен только для завершённых поддерживаемых версий")
        for artifact in state["briefs"] + ([state["spec"]] if state["spec"] else []):
            path = confined(self.run, artifact["file"])
            if not path.is_file() or file_hash(path) != artifact["sha256"]:
                raise Invalid("Изменён сохранённый источник: " + artifact["file"])
        return state

    def append(self, state, event):
        history = confined(self.run, "events.jsonl")
        lines = history.read_text(encoding="utf-8").splitlines() if history.exists() else []
        previous = json.loads(lines[-1])["hash"] if lines else None
        state["updatedAt"] = timestamp()
        row = {"sequence": len(lines) + 1, "previous": previous, "at": state["updatedAt"],
               "event": event, "state": state}
        row["hash"] = digest(row)
        with history.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def artifact(self, group, data, suffix):
        name = group + "/" + uuid.uuid4().hex + suffix
        path = confined(self.run, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        return {"file": name, "sha256": file_hash(path)}

    def text(self, artifact):
        return confined(self.run, artifact["file"]).read_text(encoding="utf-8")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True)
        if result.returncode:
            raise Invalid("Нужен доступный Git-репозиторий с коммитом HEAD")
        return result.stdout

    def code(self, zones=None, include_common=False):
        """Hash project files, respecting Git ignores, excluding only AISpa accounting."""
        if zones is not None:
            zones = [canonical_zone(z) for z in zones]
        top = Path(os.fsdecode(self.git("rev-parse", "--show-toplevel")).strip()).resolve()
        if top != self.root:
            raise Invalid("--root должен указывать на корень Git-репозитория")
        names = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        modes = {}
        if os.name == "nt":
            for record in self.git("ls-files", "--stage", "-z").split(b"\0"):
                if record:
                    metadata, name = record.split(b"\t", 1)
                    modes[os.fsdecode(name)] = metadata.split()[0]
        records = []
        for raw in sorted(set(names)):
            if not raw:
                continue
            name = os.fsdecode(raw)
            if name == ".aispa" or name.startswith(".aispa/"):
                continue
            common = common_input(name)
            if zones is not None and not (include_common and common) and not any(
                z in ("", ".") or match_name(name) == z.rstrip("/") or match_name(name).startswith(z.rstrip("/") + "/") for z in zones
            ):
                continue
            path = self.root / name
            if is_link(path):
                value = {"link": os.readlink(path)}
            elif path.is_file():
                value = {"sha256": file_hash(path), "executable": executable(path, modes.get(name))}
            elif path.is_dir():
                raise Invalid("Вложенные Git-модули пока не поддерживаются: " + name)
            else:
                value = {"missing": True}
            records.append([name, value])
        return digest(records)

    def clean(self):
        # Porcelain -z keeps filenames with spaces/newlines unambiguous.
        data = self.git("status", "--porcelain=v1", "-z").split(b"\0")
        i = 0
        while i < len(data):
            record = data[i]; i += 1
            if not record:
                continue
            status, path = record[:2], os.fsdecode(record[3:])
            paths = [path]
            if b"R" in status or b"C" in status:
                paths.append(os.fsdecode(data[i])); i += 1
            if any(p != ".aispa/" and not p.startswith(".aispa/") for p in paths):
                return False
        return True
