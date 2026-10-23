"""Local append-only history and confined, immutable input artifacts."""
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

from . import VERSION, SCHEMA
from .portable import relative_name, is_link, executable, match_name

COMMON_INPUTS = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md", ".gitignore", "package.json",
                 "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "pyproject.toml",
                 "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "Cargo.toml", "Cargo.lock", "go.mod",
                 "go.sum", "tsconfig.json", "jsconfig.json", "Makefile", "Dockerfile", "compose.yaml"}
NATIVE_RULE_INPUTS = {".agents/rules/aispa.md", ".agents/rules.json", ".cursor/rules/aispa.mdc"}
NATIVE_RULE_DIRS = {".agents/rules", ".cursor/rules"}
COMMON_INPUTS |= NATIVE_RULE_INPUTS | NATIVE_RULE_DIRS


def native_rule_input(name):
    """Only project rule Markdown, never arbitrary hidden settings or credentials."""
    name = match_name(name)
    return any(name.startswith(match_name(folder) + "/") and name.endswith(extension)
               for folder, extension in ((".agents/rules", ".md"), (".cursor/rules", ".mdc")))


def common_input(name):
    names = {match_name(item) for item in COMMON_INPUTS}
    name = match_name(name)
    return (name in names or native_rule_input(name)
            or ("/" not in name and name.startswith("requirements") and name.endswith(".txt")))


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
        self._verified = None

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

    def preflight_create(self, slug):
        """Read-only validation before init modifies any ordinary project file."""
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise Invalid("Имя прогона: латинские буквы, цифры и дефис")
        old = None
        if (self.home / "active.json").exists():
            old = self.load(handoff_only=True)
            if old["status"] not in ("accepted", "delivered_with_remaining", "cancelled"):
                raise Invalid("Предыдущий прогон не завершён; продолжите его или явно отмените")
        return old

    def private_history_ignored(self):
        try:
            result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--no-index", "-q", "--", ".aispa/"],
                                    capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            raise Invalid("Не удалось проверить исключение приватного каталога .aispa") from error
        if result.returncode not in (0, 1):
            raise Invalid("Не удалось проверить исключение приватного каталога .aispa")
        return result.returncode == 0

    def create(self, slug, title, initializer=None):
        self.preflight_create(slug)
        ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + slug + "-" + uuid.uuid4().hex[:8]
        self.run = confined(self.home, "runs/" + ident)
        self._verified = None
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
        self._verified = None
        try:
            pointer = json.loads(confined(self.home, "active.json").read_text(encoding="utf-8"))
            self.run = confined(self.home, "runs/" + pointer["run"])
            journal = self._scan_journal()
        except Invalid:
            raise
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Invalid("Не удалось прочитать журнал; init нужен только для нового проекта") from e
        state = journal["last"]["state"] if journal["last"] else None
        if not state or state.get("schema") != SCHEMA:
            raise Invalid("Неподдерживаемая схема журнала")
        self._require_version(state, handoff_only=handoff_only)
        self._verify_sources(state)
        self._verified = journal
        return copy.deepcopy(state)

    def _require_version(self, state, *, handoff_only=False):
        """Normal writers stay version-pinned; a read-only viewer may specialize this guard."""
        if state.get("version") != VERSION:
            supported = {"0.1.0-alpha.1", "0.1.0-alpha.2", "0.1.0-alpha.3", "0.1.0-alpha.4", "0.1.0-alpha.5", "0.1.0-alpha.6", "0.1.0-alpha.7", "0.1.0-alpha.8", "0.1.0-alpha.9", "0.1.0-alpha.10", "0.1.0", "0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.3.4"}
            if not (handoff_only and state.get("version") in supported
                    and state.get("status") in ("accepted", "delivered_with_remaining", "cancelled")):
                raise Invalid("Версия движка отличается: активный прогон продолжайте закреплённым runtime; handoff доступен только для завершённых поддерживаемых версий")

    def _verify_sources(self, state):
        try:
            artifacts = state["briefs"] + ([state["spec"]] if state["spec"] else [])
            for artifact in artifacts:
                path = confined(self.run, artifact["file"])
                if not path.is_file() or file_hash(path) != artifact["sha256"]:
                    raise Invalid("Изменён сохранённый источник: " + artifact["file"])
        except (KeyError, TypeError, OSError) as exc:
            raise Invalid("Некорректные ссылки на сохранённые источники") from exc

    @staticmethod
    def _hash_file(path):
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    @staticmethod
    def _sync_directory(path):
        if os.name == "nt":
            return
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Some filesystems do not support directory fsync; file fsync still applies.
            pass

    @staticmethod
    def _state_field_prefix(fragment):
        """Check the stable create() fields before any dynamic phase fields."""
        fields = ("schema", "version", "run", "title", "status", "stage", "createdAt",
                  "briefs", "requirements", "spec", "tasks", "gates", "verification", "waiting", "notes")
        decoder, position = json.JSONDecoder(), 1
        for index, name in enumerate(fields):
            literal = ("," if index else "") + json.dumps(name) + ":"
            remaining = fragment[position:]
            if literal.startswith(remaining):
                return True
            if not remaining.startswith(literal):
                return False
            position += len(literal)
            if position == len(fragment):
                return True
            try:
                _value, position = decoder.raw_decode(fragment, position)
            except json.JSONDecodeError:
                # The outer decoder separately checks that this value is a
                # syntactically valid incomplete JSON prefix, not corruption.
                return True
        return True

    @staticmethod
    def _incomplete_json_tail(raw, expected_sequence, previous_hash, previous_state):
        """Accept only an interrupted canonical next-row prefix stopped at EOF."""
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # An interrupted UTF-8 character at EOF is a normal torn write.
            # Invalid bytes or broken encoding in the middle remain corruption.
            if exc.reason != "unexpected end of data" or exc.end != len(raw):
                return False
            try:
                value = raw[:exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return False
        if not value or "\n" in value or "\r" in value:
            return False
        prefix = json.dumps({"sequence": expected_sequence, "previous": previous_hash},
                            separators=(",", ":"))[:-1]
        state_prefix = json.dumps({k: previous_state[k] for k in ("schema", "version", "run")},
                                  ensure_ascii=False, separators=(",", ":"))[:-1]
        decoder, position = json.JSONDecoder(), 0
        known = {"sequence": expected_sequence, "previous": previous_hash}
        for literal, field in ((prefix + ',"at":', "at"), (',"event":', "event"),
                               (',"state":', "state"), (',"hash":', "hash")):
            remaining = value[position:]
            if literal.startswith(remaining):
                return True  # Interrupted within a known field name/prefix.
            if not remaining.startswith(literal):
                return False
            position += len(literal)
            fragment = value[position:]
            if not fragment:
                return True
            if field == "state":
                if state_prefix.startswith(fragment):
                    return True
                if not fragment.startswith(state_prefix):
                    return False
                if not Journal._state_field_prefix(fragment):
                    return False
            elif not fragment.startswith('"'):
                return False
            if field == "hash":
                expected_hash = json.dumps(digest(known))
                if expected_hash.startswith(fragment):
                    return True
                if not fragment.startswith(expected_hash):
                    return False
            try:
                parsed, end = decoder.raw_decode(value, position)
            except json.JSONDecodeError as exc:
                if field == "hash":
                    return bool(re.fullmatch(r'"[0-9a-f]{0,64}', fragment))
                if field == "at":
                    body = fragment[1:]
                    formats = ("dddd-dd-ddTdd:dd:dd.dddddd+00:00", "dddd-dd-ddTdd:dd:dd+00:00")
                    return any(len(body) <= len(pattern) and all(
                        ch.isascii() and ch.isdigit() if expected == "d" else ch == expected
                        for ch, expected in zip(body, pattern)) for pattern in formats)
                if exc.msg == "Unterminated string starting at":
                    return True
                return exc.pos == len(value) and exc.msg in {
                    "Expecting value", "Expecting ',' delimiter", "Expecting ':' delimiter",
                    "Expecting property name enclosed in double quotes",
                    "Expecting '}' delimiter", "Expecting ']' delimiter"}
            if field == "at":
                try:
                    at = datetime.fromisoformat(parsed)
                    if at.isoformat() != parsed or at.utcoffset() != timezone.utc.utcoffset(at):
                        return False
                except (TypeError, ValueError):
                    return False
            elif field == "event" and (not isinstance(parsed, str) or not parsed):
                return False
            elif field == "state" and (not isinstance(parsed, dict) or any(
                    parsed.get(k) != previous_state[k] for k in ("schema", "version", "run"))):
                return False
            elif field == "hash" and not (isinstance(parsed, str) and re.fullmatch(r"[0-9a-f]{64}", parsed)):
                return False
            position = end
            known[field] = parsed
        # A complete JSON row without its newline is deliberately ambiguous.
        return position == len(value)

    def _scan_journal(self, *, include_events=False, allow_incomplete=False):
        path = confined(self.run, "events.jsonl")
        if not path.is_file():
            raise Invalid("Журнал отсутствует")
        previous_hash, last, prior, count, valid_bytes = None, None, None, 0, 0
        hasher = hashlib.sha256()
        events = [] if include_events else None
        event_tail = []
        tail = None
        try:
            with path.open("rb") as stream:
                for raw in stream:
                    hasher.update(raw)
                    if not raw.endswith(b"\n"):
                        tail = raw
                        break
                    number = count + 1
                    try:
                        row = json.loads(raw)
                        if (not isinstance(row, dict) or type(row.get("sequence")) is not int
                                or row["sequence"] != number or row["previous"] != previous_hash
                                or not isinstance(row.get("state"), dict)
                                or row["state"].get("run") != self.run.name
                                or digest({k: v for k, v in row.items() if k != "hash"}) != row["hash"]):
                            raise ValueError("chain mismatch")
                    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                        raise Invalid(f"Повреждён журнал, запись {number}; автоматическое исправление запрещено") from exc
                    previous_hash, prior, last = row["hash"], last, row
                    count, valid_bytes = number, valid_bytes + len(raw)
                    event_tail = (event_tail + [{"event": row.get("event", ""), "at": row.get("at")}])[-64:]
                    if include_events:
                        events.append(row)
        except OSError as exc:
            raise Invalid("Не удалось прочитать журнал") from exc
        if not count:
            raise Invalid("Пустой журнал не восстанавливается автоматически")
        if tail is not None and (not allow_incomplete or not self._incomplete_json_tail(
                tail, count + 1, previous_hash, last["state"])):
            raise Invalid("Последняя строка журнала неоднозначна или повреждена; автоматическое исправление запрещено")
        result = {"path": path, "count": count, "last": last, "previous": prior,
                  "sha256": hasher.hexdigest(), "size": valid_bytes + (len(tail) if tail else 0),
                  "eventTail": event_tail,
                  "validBytes": valid_bytes, "_hasher": hasher, "_tail": tail}
        if include_events:
            result["events"] = events
        return result

    def _journal_cursor(self, *, include_events=False):
        path = confined(self.run, "events.jsonl")
        cached = self._verified
        if cached is not None and cached["path"] == path and (not include_events or "events" in cached):
            if self._hash_file(path) != cached["sha256"]:
                raise Invalid("Журнал изменился после проверки")
            result = cached
        else:
            result = self._scan_journal(include_events=include_events)
        self._verified = result
        return result

    def read_journal(self, *, include_events=False):
        """Verify all bytes; return detached data, never the mutable cache."""
        result = self._journal_cursor(include_events=include_events)
        return copy.deepcopy({key: value for key, value in result.items()
                              if key != "path" and not key.startswith("_") and (include_events or key != "events")})

    def event_pair(self, sequence):
        """Copy only an addressed event and its predecessor after chain validation."""
        if type(sequence) is not int or sequence < 1:
            raise Invalid("Нужен положительный номер события")
        events = self._journal_cursor(include_events=True)["events"]
        if sequence > len(events):
            return None, None
        return copy.deepcopy((events[sequence - 1], events[sequence - 2] if sequence > 1 else None))

    def task_timeline(self, task_id):
        """Project only the evidence-order fields needed by a single task."""
        if not isinstance(task_id, str):
            raise Invalid("Нужен ID задачи")
        rows = []
        for row in self._journal_cursor(include_events=True)["events"]:
            history = row["state"].get("taskHistory", {}).get(task_id, [])
            attempt = history[-1] if history else {}
            rows.append({"sequence": row["sequence"], "event": row.get("event"),
                         "state": {"taskHistory": {task_id: [{
                             "id": attempt.get("id"),
                             "testIntentHistory": copy.deepcopy(attempt.get("testIntentHistory", []))
                         }] if history else []}}})
        return rows

    def verified_events(self):
        return self.read_journal(include_events=True)["events"]

    def _active_run_for_recovery(self):
        try:
            pointer = json.loads(confined(self.home, "active.json").read_text(encoding="utf-8"))
            self.run = confined(self.home, "runs/" + pointer["run"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Invalid("Нет доступного активного прогона для проверки восстановления") from exc

    def _recovery_candidate(self):
        self._active_run_for_recovery()
        result = self._scan_journal(allow_incomplete=True)
        tail = result["_tail"]
        if tail is None:
            raise Invalid("Журнал цел; восстановление не требуется")
        state = result["last"]["state"]
        if state.get("schema") != SCHEMA:
            raise Invalid("Неподдерживаемая схема журнала")
        if state.get("version") != VERSION:
            raise Invalid("Журнал другой версии восстанавливает только его закреплённый runtime")
        if not isinstance(state.get("gates", {}), dict):
            raise Invalid("Некорректные контрольные поля прогона")
        self._verify_sources(state)
        backup = "recovery/original-" + result["sha256"] + ".jsonl"
        candidate = {"recoverable": True, "run": self.run.name,
                     "journalSha256": result["sha256"], "journalBytes": result["size"],
                     "validSequence": result["count"], "validHash": result["last"]["hash"],
                     "tailBytes": len(tail), "tailSha256": hashlib.sha256(tail).hexdigest(),
                     "backupFile": backup}
        return candidate, result

    def inspect_recovery(self):
        """Read-only recovery plan; a complete line without LF is ambiguous."""
        candidate, _ = self._recovery_candidate()
        return candidate

    def apply_recovery(self, expected_sha256, *, confirmed=False):
        if not confirmed or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256)):
            raise Invalid("Восстановление требует подтверждения и SHA исходного журнала")
        with self.lock():
            candidate, scanned = self._recovery_candidate()
            if candidate["journalSha256"] != expected_sha256:
                raise Invalid("Журнал изменился после осмотра; восстановление отменено")
            history = confined(self.run, "events.jsonl")
            backup = confined(self.run, candidate["backupFile"])
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = confined(self.run, candidate["backupFile"])
            if self._hash_file(history) != expected_sha256:
                raise Invalid("Журнал изменился до резервного копирования")
            if backup.exists():
                if self._hash_file(backup) != expected_sha256 or backup.stat().st_size != scanned["size"]:
                    raise Invalid("Путь резервной копии занят другим содержимым")
                if os.name != "nt" and stat.S_IMODE(backup.stat().st_mode) & 0o077:
                    raise Invalid("Существующая резервная копия имеет слишком широкие права")
            else:
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with history.open("rb") as source, os.fdopen(fd, "wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if self._hash_file(backup) != expected_sha256:
                    raise Invalid("Резервная копия не совпала с исходным журналом")
                self._sync_directory(backup.parent)
            state = copy.deepcopy(scanned["last"]["state"])
            state["status"] = "paused"
            state["waiting"] = "Восстановлен оборванный конец журнала; нужна сверка незавершённых действий и свежая проверка"
            state["verification"] = None
            state.setdefault("gates", {}).pop("acceptance", None)
            state.pop("activity", None)
            state["journalRecovery"] = {"pending": True, "sourceSha256": expected_sha256,
                                        "backupFile": candidate["backupFile"],
                                        "tailSha256": candidate["tailSha256"],
                                        "validSequence": candidate["validSequence"]}
            state["updatedAt"] = timestamp()
            row = {"sequence": scanned["count"] + 1, "previous": scanned["last"]["hash"],
                   "at": state["updatedAt"], "event": "journal-recovered", "state": state}
            row["hash"] = digest(row)
            event = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            fd, name = tempfile.mkstemp(prefix=".aispa-recover-", dir=self.run)
            temp = Path(name)
            try:
                with os.fdopen(fd, "wb") as target, history.open("rb") as source:
                    remaining = scanned["validBytes"]
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise Invalid("Журнал изменился во время подготовки восстановления")
                        target.write(block)
                        remaining -= len(block)
                    target.write(event)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temp, stat.S_IMODE(history.stat().st_mode))
                if is_link(history) or self._hash_file(history) != expected_sha256:
                    raise Invalid("Журнал изменился перед применением восстановления")
                os.replace(temp, history)
                self._sync_directory(history.parent)
            finally:
                temp.unlink(missing_ok=True)
            self._verified = None
            self.read_journal()
            return {**candidate, "recoverySequence": row["sequence"], "recoveryHash": row["hash"]}

    def append(self, state, event):
        history = confined(self.run, "events.jsonl")
        if history.exists():
            cursor = self._verified if self._verified and self._verified["path"] == history else None
            if cursor is None:
                cursor = self._scan_journal()
                self._verified = cursor
            if self._hash_file(history) != cursor["sha256"]:
                raise Invalid("Журнал изменился после проверки; запись запрещена")
            sequence = cursor["count"] + 1
            previous = cursor["last"]["hash"]
        else:
            if self._verified is not None:
                raise Invalid("Журнал исчез после проверки; запись запрещена")
            sequence, previous = 1, None
        state["updatedAt"] = timestamp()
        row = {"sequence": sequence, "previous": previous, "at": state["updatedAt"],
               "event": event, "state": state}
        row["hash"] = digest(row)
        raw = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        row = json.loads(raw)  # The verified cache must never alias mutable caller state.
        with history.open("ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        hasher = cursor["_hasher"].copy() if history.exists() and sequence > 1 else hashlib.sha256()
        hasher.update(raw)
        self._verified = {"path": history, "count": sequence, "last": row,
                          "previous": cursor["last"] if sequence > 1 else None,
                          "eventTail": ((cursor.get("eventTail", []) if sequence > 1 else []) +
                                        [{"event": event, "at": row["at"]}])[-64:],
                          "sha256": hasher.hexdigest(), "size": (cursor["size"] if sequence > 1 else 0) + len(raw),
                          "_hasher": hasher}

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

    def code_records(self, zones=None, include_common=False):
        """The exact path/value rows used by code() for its existing digest."""
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
        return records

    def code(self, zones=None, include_common=False):
        """Hash project files, respecting Git ignores, excluding only AISpa accounting."""
        return digest(self.code_records(zones, include_common))

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
