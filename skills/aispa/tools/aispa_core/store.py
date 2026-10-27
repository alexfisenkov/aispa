"""Local append-only history and confined, immutable input artifacts."""
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from . import VERSION, SCHEMA
from .portable import relative_name, is_link, match_name

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


def _regular_identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _journal_generation_error(error):
    return str(error) in {"AISpa read: source identity changed at open",
                          "AISpa read: source changed while reading",
                          "AISpa read: source grew while hashing",
                          "AISpa read: source shrank while hashing",
                          "Журнал вырос во время чтения", "Журнал сократился во время чтения"}


@contextmanager
def regular_file(path, *, nofollow=False, budget=None):
    """Open one finite regular-file generation without changing generic link policy."""
    path = Path(path)
    if budget is not None:
        budget.check(0, path=str(path))
    before = path.lstat() if nofollow else path.stat()
    if (nofollow and is_link(path)) or not stat.S_ISREG(before.st_mode):
        raise Invalid("AISpa read: source is not an allowed regular file")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
             | (getattr(os, "O_NOFOLLOW", 0) if nofollow else 0))
    fd = os.open(path, flags)
    try:
        stream = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _regular_identity(opened) != _regular_identity(before):
            raise Invalid("AISpa read: source identity changed at open")
        if budget is not None:
            budget.check(0, path=str(path))
        yield stream, before, opened
        after = path.lstat() if nofollow else path.stat()
        if (nofollow and is_link(path)) or not stat.S_ISREG(after.st_mode) or (
                _regular_identity(after) != _regular_identity(opened)):
            raise Invalid("AISpa read: source changed while reading")
    if budget is not None:
        budget.check(0, path=str(path))


def file_hash(path, *, budget=None, nofollow=False, metadata=None):
    """Hash a regular file by its physical starting size, with no entity cap."""
    hasher = hashlib.sha256()
    with regular_file(path, nofollow=nofollow, budget=budget) as (stream, before, opened):
        remaining, completed = before.st_size + 1, 0
        while remaining:
            amount = min(65536, remaining)
            if budget is not None:
                # A stream may be larger than the memory profile; only its
                # current block must fit alongside already retained bytes.
                amount = min(amount, max(1, budget.memory_bytes - budget.base_memory))
                budget.check(0, path=str(path), progress=completed)
                budget.check_memory(amount, path=str(path), progress=completed)
            block = stream.read(amount)
            if not block:
                break
            completed += len(block)
            remaining -= len(block)
            if completed > before.st_size:
                raise Invalid("AISpa read: source grew while hashing")
            hasher.update(block)
        if completed != before.st_size:
            raise Invalid("AISpa read: source shrank while hashing")
        if budget is not None:
            budget.check(0, path=str(path), progress=completed)
        if metadata is not None:
            metadata.update(generation=_regular_identity(opened), mode=opened.st_mode)
    return hasher.hexdigest()


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
            from . import scale
            budget = scale.Budget()
            pointer = self._active_pointer(budget)
            self.run = confined(self.home, "runs/" + pointer["run"])
            journal = self._scan_journal(budget=budget)
        except Invalid:
            raise
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Invalid("Не удалось прочитать журнал; init нужен только для нового проекта") from e
        state = journal["last"]["state"] if journal["last"] else None
        if not state or state.get("schema") != SCHEMA:
            raise Invalid("Неподдерживаемая схема журнала")
        self._require_version(state, handoff_only=handoff_only)
        self._verify_sources(state, budget=budget)
        self._verified = journal
        value = copy.deepcopy(state)
        budget.check(0, path="journal load")
        return value

    def _active_pointer(self, budget=None):
        """Read one complete, small active pointer from a checked fresh fd."""
        from . import scale
        from .verified_reads import physical_read
        budget = budget or scale.Budget()
        raw = physical_read(confined(self.home, "active.json"), budget, limit=min(65536, budget.max_bytes))
        budget.check(0, path="active.json")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise Invalid("Некорректный указатель активного прогона") from error
        if not isinstance(value, dict) or not isinstance(value.get("run"), str):
            raise Invalid("Некорректный указатель активного прогона")
        budget.check(0, path="active.json")
        return value

    def _require_version(self, state, *, handoff_only=False):
        """Normal writers stay version-pinned; a read-only viewer may specialize this guard."""
        if state.get("version") != VERSION:
            supported = {"0.1.0-alpha.1", "0.1.0-alpha.2", "0.1.0-alpha.3", "0.1.0-alpha.4", "0.1.0-alpha.5", "0.1.0-alpha.6", "0.1.0-alpha.7", "0.1.0-alpha.8", "0.1.0-alpha.9", "0.1.0-alpha.10", "0.1.0", "0.2.0", "0.3.0", "0.3.1", "0.3.2", "0.3.3", "0.3.4", "0.3.5", "0.3.6", "0.4.0"}
            if not (handoff_only and state.get("version") in supported
                    and state.get("status") in ("accepted", "delivered_with_remaining", "cancelled")):
                raise Invalid("Версия движка отличается: активный прогон продолжайте закреплённым runtime; handoff доступен только для завершённых поддерживаемых версий")

    def _require_recovery_version(self, state):
        """Only the run's own pinned runtime recovers it; a read-only repair reader may specialize this."""
        if state.get("version") != VERSION:
            raise Invalid("Журнал другой версии восстанавливает только его закреплённый runtime")

    def _verify_sources(self, state, *, budget=None):
        if budget is None:
            from . import scale
            budget = scale.Budget()
        try:
            artifacts = state["briefs"] + ([state["spec"]] if state["spec"] else [])
            for artifact in artifacts:
                path = confined(self.run, artifact["file"])
                if file_hash(path, budget=budget, nofollow=True) != artifact["sha256"]:
                    raise Invalid("Изменён сохранённый источник: " + artifact["file"])
            budget.check(0, path="saved sources")
        except (KeyError, TypeError, OSError) as exc:
            raise Invalid("Некорректные ссылки на сохранённые источники") from exc

    @staticmethod
    def _hash_file(path, *, budget=None):
        from . import scale
        try:
            return file_hash(path, budget=budget or scale.Budget(), nofollow=True)
        except Invalid as error:
            if _journal_generation_error(error):
                raise Invalid("Журнал изменился после проверки") from error
            raise
        except OSError as error:
            raise Invalid("Не удалось прочитать журнал") from error

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

    @staticmethod
    def _journal_lines(path, budget, hasher):
        """Yield finite lines from one regular generation, including a torn tail."""
        with regular_file(path, nofollow=True, budget=budget) as (stream, before, _opened):
            remaining, completed = before.st_size + 1, 0
            pending = bytearray()
            while remaining:
                budget.check(0, path=str(path), progress=completed)
                amount = min(65536, remaining)
                budget.check_memory((len(pending) + amount) * 4, path=str(path), progress=completed)
                block = stream.read(amount)
                if not block:
                    break
                completed += len(block)
                remaining -= len(block)
                if completed > before.st_size:
                    raise Invalid("Журнал вырос во время чтения")
                hasher.update(block)
                search_from = len(pending)
                pending.extend(block)
                budget.check_memory(len(pending) * 4, path=str(path), progress=completed)
                while True:
                    end = pending.find(b"\n", search_from)
                    if end < 0:
                        break
                    raw = bytes(pending[:end + 1])
                    del pending[:end + 1]
                    search_from = 0
                    yield raw
                    budget.check(0, path=str(path), progress=completed)
            if completed != before.st_size:
                raise Invalid("Журнал сократился во время чтения")
            if pending:
                yield bytes(pending)
            budget.check(0, path=str(path), progress=completed)

    def _scan_journal(self, *, include_events=False, allow_incomplete=False, budget=None):
        path = confined(self.run, "events.jsonl")
        if budget is None:
            from . import scale
            budget = scale.Budget()
        previous_hash, last, prior, count, valid_bytes = None, None, None, 0, 0
        hasher = hashlib.sha256()
        events = [] if include_events else None
        event_tail = []
        receipt_times = {}
        tail = None
        try:
            for raw in self._journal_lines(path, budget, hasher):
                if not raw.endswith(b"\n"):
                    tail = raw
                    continue
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
                self._index_receipt_times(row, receipt_times)
                if include_events:
                    events.append(row)
                budget.check(0, path=str(path), progress=valid_bytes)
        except Invalid as exc:
            if _journal_generation_error(exc):
                raise Invalid("Журнал изменился после проверки") from exc
            raise
        except OSError as exc:
            raise Invalid("Не удалось прочитать журнал") from exc
        if not count:
            raise Invalid("Пустой журнал не восстанавливается автоматически")
        if tail is not None and (not allow_incomplete or not self._incomplete_json_tail(
                tail, count + 1, previous_hash, last["state"])):
            raise Invalid("Последняя строка журнала неоднозначна или повреждена; автоматическое исправление запрещено")
        budget.check(0, path=str(path), progress=valid_bytes)
        result = {"path": path, "count": count, "last": last, "previous": prior,
                  "sha256": hasher.hexdigest(), "size": valid_bytes + (len(tail) if tail else 0),
                  "eventTail": event_tail,
                  "validBytes": valid_bytes, "_hasher": hasher, "_tail": tail,
                  "_receipt_times": receipt_times}
        if include_events:
            result["events"] = events
        budget.check(0, path=str(path), progress=valid_bytes)
        return result

    @staticmethod
    def _receipt_identity(record):
        ref = record.get("record") if isinstance(record, dict) else None
        if isinstance(ref, dict) and isinstance(ref.get("file"), str) and isinstance(ref.get("sha256"), str):
            return ref["file"], ref["sha256"]  # exact sealed receipt, not its report body
        return None

    @classmethod
    def _index_receipt_times(cls, row, index):
        if not isinstance(row.get("at"), str):
            return
        histories = row["state"].get("taskHistory", {})
        if not isinstance(histories, dict):
            return
        for history in histories.values():
            if not isinstance(history, list):
                continue
            for attempt in history:
                if not isinstance(attempt, dict):
                    continue
                records = [attempt.get(key) for key in ("submitted", "repair", "acceptance", "verifiedCompletion")]
                for key in ("rounds", "submissionHistory", "repairHistory"):
                    if isinstance(attempt.get(key), list):
                        records += attempt[key]
                for record in records:
                    identity = cls._receipt_identity(record)
                    if identity is not None:
                        index.setdefault(identity, row["at"])

    def receipt_time(self, record):
        """Historical first appearance in an already verified journal cursor."""
        budget = self._read_budget()
        scope = getattr(self, "_reads", None)
        if scope is not None:
            from .verified_reads import current_scope
            if current_scope() is scope:
                scope.deadline_check("receipt cache")
        cursor = self._verified if self._verified is not None else self._journal_cursor(budget=budget)
        value = cursor.get("_receipt_times", {}).get(self._receipt_identity(record))
        budget.check(0, path="receipt time")
        return value

    def _read_budget(self, budget=None):
        if budget is not None:
            return budget
        scope = getattr(self, "_reads", None)
        if scope is not None:
            from .verified_reads import current_scope
            if current_scope() is scope:
                return scope._stream_budget()
        from . import scale
        return scale.Budget()

    def _journal_cursor(self, *, include_events=False, budget=None):
        path = confined(self.run, "events.jsonl")
        cached = self._verified
        if cached is not None and cached["path"] == path and (not include_events or "events" in cached):
            actual = self._hash_file(path) if budget is None else self._hash_file(path, budget=budget)
            if actual != cached["sha256"]:
                raise Invalid("Журнал изменился после проверки")
            result = cached
        else:
            result = self._scan_journal(include_events=include_events, budget=budget)
        self._verified = result
        return result

    def read_journal(self, *, include_events=False, budget=None):
        """Verify all bytes; return detached data, never the mutable cache."""
        budget = self._read_budget(budget)
        result = self._journal_cursor(include_events=include_events, budget=budget)
        value = copy.deepcopy({key: value for key, value in result.items()
                               if key != "path" and not key.startswith("_") and (include_events or key != "events")})
        scope = getattr(self, "_reads", None)
        if scope is not None:
            from .verified_reads import current_scope
            if current_scope() is scope:
                scope.deadline_check("journal copy")
        budget.check(0, path="journal copy")
        return value

    def event_pair(self, sequence):
        """Copy only an addressed event and its predecessor after chain validation."""
        if type(sequence) is not int or sequence < 1:
            raise Invalid("Нужен положительный номер события")
        budget = self._read_budget()
        events = self._journal_cursor(include_events=True, budget=budget)["events"]
        if sequence > len(events):
            budget.check(0, path="event pair")
            return None, None
        value = copy.deepcopy((events[sequence - 1], events[sequence - 2] if sequence > 1 else None))
        scope = getattr(self, "_reads", None)
        if scope is not None:
            from .verified_reads import current_scope
            if current_scope() is scope:
                scope.deadline_check("event copy")
        budget.check(0, path="event pair")
        return value

    def task_timeline(self, task_id):
        """Project only the evidence-order fields needed by a single task."""
        if not isinstance(task_id, str):
            raise Invalid("Нужен ID задачи")
        budget = self._read_budget()
        rows = []
        for row in self._journal_cursor(include_events=True, budget=budget)["events"]:
            budget.check(0, path="task timeline")
            scope = getattr(self, "_reads", None)
            if scope is not None:
                from .verified_reads import current_scope
                if current_scope() is scope:
                    scope.deadline_check("timeline projection")
            history = row["state"].get("taskHistory", {}).get(task_id, [])
            attempt = history[-1] if history else {}
            rows.append({"sequence": row["sequence"], "event": row.get("event"),
                         "state": {"taskHistory": {task_id: [{
                             "id": attempt.get("id"),
                             "testIntentHistory": copy.deepcopy(attempt.get("testIntentHistory", []))
                         }] if history else []}}})
        scope = getattr(self, "_reads", None)
        if scope is not None:
            from .verified_reads import current_scope
            if current_scope() is scope:
                scope.deadline_check("timeline projection")
        budget.check(0, path="task timeline")
        return rows

    def verified_events(self):
        return self.read_journal(include_events=True)["events"]

    def _active_run_for_recovery(self, budget=None):
        try:
            pointer = self._active_pointer(budget)
            self.run = confined(self.home, "runs/" + pointer["run"])
        except Invalid:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Invalid("Нет доступного активного прогона для проверки восстановления") from exc

    def _recovery_candidate(self, *, budget=None):
        from . import scale
        budget = budget or scale.Budget()
        self._active_run_for_recovery(budget)
        result = self._scan_journal(allow_incomplete=True, budget=budget)
        tail = result["_tail"]
        if tail is None:
            raise Invalid("Журнал цел; восстановление не требуется")
        state = result["last"]["state"]
        if state.get("schema") != SCHEMA:
            raise Invalid("Неподдерживаемая схема журнала")
        self._require_recovery_version(state)
        if not isinstance(state.get("gates", {}), dict):
            raise Invalid("Некорректные контрольные поля прогона")
        self._verify_sources(state, budget=budget)
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

    @staticmethod
    def _copy_journal_generation(path, target, expected_sha256, copy_bytes, budget):
        """Hash the entire checked source generation while copying an exact prefix."""
        try:
            return Journal._copy_journal_opened(path, target, expected_sha256, copy_bytes, budget)
        except OSError as error:
            raise Invalid("Не удалось прочитать журнал при восстановлении") from error

    @staticmethod
    def _copy_journal_opened(path, target, expected_sha256, copy_bytes, budget):
        with regular_file(path, nofollow=True, budget=budget) as (source, before, opened):
            if copy_bytes > before.st_size:
                raise Invalid("Журнал сократился во время подготовки восстановления")
            hasher = hashlib.sha256()
            completed = 0
            while completed < before.st_size:
                budget.check(0, path=str(path), progress=completed)
                amount = min(65536, before.st_size - completed + 1)
                budget.check_memory(amount, path=str(path), progress=completed)
                block = source.read(amount)
                if not block:
                    raise Invalid("Журнал сократился во время подготовки восстановления")
                completed += len(block)
                if completed > before.st_size:
                    raise Invalid("Журнал вырос во время подготовки восстановления")
                hasher.update(block)
                offset = completed - len(block)
                if offset < copy_bytes:
                    target.write(block[:copy_bytes - offset])
            if hasher.hexdigest() != expected_sha256:
                raise Invalid("Журнал изменился во время подготовки восстановления")
            budget.check(0, path=str(path), progress=completed)
            return opened.st_mode

    def apply_recovery(self, expected_sha256, *, confirmed=False):
        if not confirmed or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256)):
            raise Invalid("Восстановление требует подтверждения и SHA исходного журнала")
        with self.lock():
            from . import scale
            budget = scale.Budget()
            candidate, scanned = self._recovery_candidate(budget=budget)
            if candidate["journalSha256"] != expected_sha256:
                raise Invalid("Журнал изменился после осмотра; восстановление отменено")
            history = confined(self.run, "events.jsonl")
            backup = confined(self.run, candidate["backupFile"])
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = confined(self.run, candidate["backupFile"])
            if self._hash_file(history, budget=budget) != expected_sha256:
                raise Invalid("Журнал изменился до резервного копирования")
            if backup.exists():
                backup_meta = {}
                if (file_hash(backup, budget=budget, nofollow=True, metadata=backup_meta) != expected_sha256
                        or backup_meta["generation"][2] != scanned["size"]):
                    raise Invalid("Путь резервной копии занят другим содержимым")
                if os.name != "nt" and stat.S_IMODE(backup_meta["mode"]) & 0o077:
                    raise Invalid("Существующая резервная копия имеет слишком широкие права")
            else:
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(fd, "wb") as target:
                        self._copy_journal_generation(history, target, expected_sha256, scanned["size"], budget)
                        target.flush()
                        os.fsync(target.fileno())
                    if self._hash_file(backup, budget=budget) != expected_sha256:
                        raise Invalid("Резервная копия не совпала с исходным журналом")
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
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
                with os.fdopen(fd, "wb") as target:
                    mode = self._copy_journal_generation(history, target, expected_sha256,
                                                         scanned["validBytes"], budget)
                    target.write(event)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temp, stat.S_IMODE(mode))
                if is_link(history) or self._hash_file(history, budget=budget) != expected_sha256:
                    raise Invalid("Журнал изменился перед применением восстановления")
                budget.check(0, path="recovery apply")
                os.replace(temp, history)
                self._sync_directory(history.parent)
            finally:
                temp.unlink(missing_ok=True)
            self._verified = None
            # Post-commit verification is a new read operation; an expired
            # pre-commit budget must not report failure after a durable replace.
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
        receipt_times = dict(cursor.get("_receipt_times", {})) if sequence > 1 else {}
        self._index_receipt_times(row, receipt_times)
        self._verified = {"path": history, "count": sequence, "last": row,
                          "previous": cursor["last"] if sequence > 1 else None,
                          "eventTail": ((cursor.get("eventTail", []) if sequence > 1 else []) +
                                        [{"event": event, "at": row["at"]}])[-64:],
                          "sha256": hasher.hexdigest(), "size": (cursor["size"] if sequence > 1 else 0) + len(raw),
                          "_hasher": hasher, "_receipt_times": receipt_times}

    def artifact(self, group, data, suffix):
        from . import scale
        budget = scale.Budget()
        name = group + "/" + uuid.uuid4().hex + suffix
        path = confined(self.run, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only domain inputs/reports use this transport. Execution receipts,
        # binary attachments and private history retain their native schemas.
        groups = {"briefs", "specs", "reviews", "plans", "ideas", "feature-scenarios",
                  "interface-contracts", "brief-extractions", "brief-reviews", "brief-maps",
                  "plan-reviews", "test-intent", "test-baselines", "integration-baselines",
                  "project-integration-reviews", "blind-reviews", "blind-candidates", "discovery"}
        if getattr(self, "scale_enabled", False) and group in groups and len(data) > budget.chunk_bytes:
            scale.write_manifest(path, data, kind="json" if suffix == ".json" else "text",
                                 basis=hashlib.sha256(data).hexdigest())
        else:
            with path.open("xb") as stream:
                stream.write(data)
        try:
            return {"file": name, "sha256": file_hash(path, budget=budget, nofollow=True)}
        except OSError as error:
            raise Invalid("Артефакт изменился после записи") from error

    def text(self, artifact):
        budget = self._read_budget()
        value = self.artifact_bytes(artifact, budget=budget).decode("utf-8")
        budget.check(0, path="artifact text")
        return value

    def artifact_bytes(self, artifact, *, budget=None):
        from . import scale
        budget = self._read_budget(budget)
        path = confined(self.run, artifact["file"])
        scope = getattr(self, "_reads", None)
        if scope is not None:
            value = scope.artifact(path, artifact["sha256"])
        else:
            value = scale.read_file(path, expected_sha256=artifact["sha256"], budget=budget)
        budget.check(0, path=str(path))
        return value

    def json(self, artifact):
        budget = self._read_budget()
        raw = self.artifact_bytes(artifact, budget=budget)
        scope = getattr(self, "_reads", None)
        try:
            value = scope.json(raw) if scope is not None else json.loads(raw.decode("utf-8"))
        except Invalid:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise Invalid("Некорректный JSON артефакта") from error
        budget.check(0, path="artifact JSON")
        return value

    def artifact_hash_valid(self, artifact, *, budget=None):
        path = confined(self.run, artifact["file"])
        scope = getattr(self, "_reads", None)
        try:
            if scope is None and budget is None:
                from . import scale
                budget = scale.Budget()
            return (scope.hash_valid(path, artifact["sha256"]) if scope is not None
                    else file_hash(path, budget=budget, nofollow=True) == artifact["sha256"])
        except OSError as error:
            raise Invalid("Артефакт недоступен для проверки") from error

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True)
        if result.returncode:
            raise Invalid("Нужен доступный Git-репозиторий с коммитом HEAD")
        return result.stdout

    def code_records(self, zones=None, include_common=False, *, budget=None):
        """The exact path/value rows used by code() for its existing digest."""
        if budget is None:
            from . import scale
            budget = scale.Budget()
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
        retained = 0
        for raw in sorted(set(names)):
            budget.check(0, path="code records")
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
                before = path.lstat()
                try:
                    link = os.readlink(path)
                except OSError as error:
                    raise Invalid("Исходный код изменился во время чтения: " + name) from error
                after = path.lstat()
                if (not is_link(path) or _regular_identity(before) != _regular_identity(after)
                        or before.st_mode != after.st_mode):
                    raise Invalid("Исходный код изменился во время чтения: " + name)
                value = {"link": link}
            elif path.is_file():
                metadata = {}
                try:
                    sha = file_hash(path, nofollow=True, budget=budget, metadata=metadata)
                    after = path.lstat()
                except OSError as error:
                    raise Invalid("Исходный код изменился во время чтения: " + name) from error
                if (is_link(path) or _regular_identity(after) != metadata["generation"]
                        or after.st_mode != metadata["mode"]):
                    raise Invalid("Исходный код изменился во время чтения: " + name)
                mode = modes.get(name) if os.name == "nt" else metadata["mode"]
                value = {"sha256": sha, "executable": (mode in ("100755", b"100755") if os.name == "nt"
                                                       else bool(mode & 0o111))}
            elif path.is_dir():
                raise Invalid("Вложенные Git-модули пока не поддерживаются: " + name)
            else:
                value = {"missing": True}
            retained += 256 + len(name) * 4
            budget.check_memory(retained, path=name)
            records.append([name, value])
        budget.check(0, path="code records")
        return records

    def code(self, zones=None, include_common=False, *, budget=None):
        """Hash project files, respecting Git ignores, excluding only AISpa accounting."""
        if budget is None:
            from . import scale
            budget = scale.Budget()
        result = digest(self.code_records(zones, include_common, budget=budget))
        budget.check(0, path="code digest")
        return result

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
