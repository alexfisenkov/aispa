"""Reproducible local Git clones and command evidence; not an execution sandbox.

The caller owns journal/state transitions. Save prepare() as ``cleanroom`` and
execute() as ``cleanrun``. Only valid() is evidence of a current successful run.
"""
import ast
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import time
import uuid

from .store import Invalid, confined, digest, file_hash, timestamp


MAX_OUTPUT = 1024 * 1024
SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                    r"sk-[A-Za-z0-9_-]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
                    r"-----BEGIN [^-]*PRIVATE KEY-----|"
                    r"(?i:https?://)[^\s/@:]+:[^\s/@]+@)")
SENSITIVE_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH|COOKIE", re.I)
SENSITIVE_NAMES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials.json",
                   "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
LIMITATIONS = {"sandbox": False, "networkIsolation": False, "inheritedEnvironment": False,
               "ambientPackageIsolation": False,
               "dependencies": "Install project dependencies from committed lockfiles inside the clone",
               "sourceMonitoring": "tracked and unignored Git files"}


def _require(condition, message):
    if not condition:
        raise Invalid(message)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _no_links(path, base):
    """Reject symlink ancestors without resolving them into somebody else's tree."""
    _require(path.is_absolute() and path.is_relative_to(base), "Путь clean-clone выходит за пределы проекта")
    for part in (base, *reversed(path.parents), path):
        if part == base or part.is_relative_to(base):
            _require(not part.is_symlink(), "Символическая ссылка в служебном пути clean-clone")


def _environment(home):
    # No inherited tokens, SSH agent, user Git/package config, PYTHONPATH or PATH.
    return {"PATH": SAFE_PATH, "HOME": str(home), "USERPROFILE": str(home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "CI": "1",
            "TMPDIR": str(home / "tmp"), "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"), "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "0", "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


def _git(root, *args, home=None, allow_failure=False):
    env = _environment(home or Path(os.devnull))
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
             "-c", "core.attributesFile=" + os.devnull, "-C", str(root), *args],
            capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Invalid("Не удалось выполнить Git для clean-clone") from error
    if result.returncode and not allow_failure:
        # Git stderr can contain a private origin URL. Do not persist or echo it.
        raise Invalid("Git не смог подготовить или проверить clean-clone")
    return result


def _safe_name(name):
    p = PurePosixPath(name)
    _require(name and not p.is_absolute() and p.as_posix() == name and
             not any(part in ("..", ".") or part.casefold() in (".git", ".aispa") for part in p.parts)
             and "\\" not in name, "Небезопасный путь в Git tree")
    return p


def _credential_name(name):
    parts = PurePosixPath(name).parts
    return any(p.lower() in SENSITIVE_NAMES or p.lower() in (".ssh", ".aws", ".gnupg") or
               p.lower() == ".env" or (p.lower().startswith(".env.") and
               p.lower() not in (".env.example", ".env.sample", ".env.template")) or
               p.lower().endswith((".p12", ".pfx", ".key")) for p in parts)


def _records(root, *, scan_secrets=False, verify_head=False):
    entries = _git(root, "ls-tree", "-rz", "--full-tree", "HEAD").stdout.split(b"\0")
    object_format = _git(root, "rev-parse", "--show-object-format").stdout.decode().strip()
    records = []
    for entry in entries:
        if not entry:
            continue
        meta, raw = entry.split(b"\t", 1)
        mode, kind, oid = meta.split(b" ")
        name = os.fsdecode(raw)
        _safe_name(name)
        _require(mode in (b"100644", b"100755") and kind == b"blob",
                 "Clean-clone пока не поддерживает submodules и tracked symlinks")
        _require(name != ".gitmodules", "Clean-clone пока не поддерживает submodules")
        _require(not _credential_name(name), "Git tree содержит файл с возможными credentials")
        p = root / name
        _no_links(p, root)
        _require(p.is_file() and stat.S_ISREG(p.stat().st_mode), "В clean-clone отсутствует обычный tracked-файл")
        content_hash = hashlib.sha256()
        git_hash = hashlib.new(object_format)
        git_hash.update(("blob %d\0" % p.stat().st_size).encode())
        tail = b""
        with p.open("rb") as stream:
            while chunk := stream.read(65536):
                content_hash.update(chunk)
                git_hash.update(chunk)
                if scan_secrets:
                    text = (tail + chunk).decode("utf-8", "replace")
                    _require(not SECRET.search(text), "Git tree содержит возможный секрет")
                    tail = chunk[-4096:]
        if verify_head:
            _require(git_hash.hexdigest().encode() == oid and
                     bool(p.stat().st_mode & 0o111) == (mode == b"100755"),
                     "Рабочий файл отличается от committed HEAD")
        records.append([name, {"sha256": content_hash.hexdigest(),
                               "executable": bool(p.stat().st_mode & 0o111)}])
    return sorted(records)


def _identity(root):
    _require(not (root / ".git").is_symlink(), ".git не должна быть символической ссылкой")
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").stdout).strip()).resolve()
    _require(top == root, "--root должен указывать на корень Git-репозитория")
    return {"head": _git(root, "rev-parse", "HEAD").stdout.decode().strip(),
            "tree": _git(root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()}


def _source(store):
    identity = _identity(store.root)
    _require(_git(store.root, "check-ignore", "--no-index", ".aispa/", allow_failure=True).returncode == 0,
             "До clean-clone добавьте .aispa/ в Git ignore")
    _require(not _git(store.root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                      "--ignore-submodules=none").stdout,
             "Clean-clone требует чистый committed HEAD: сохраните tracked и unignored изменения")
    # Do not rely only on Git status: assume-unchanged/skip-worktree can hide edits.
    records = _records(store.root, scan_secrets=True, verify_head=True)
    commit = _git(store.root, "cat-file", "commit", "HEAD").stdout.decode("utf-8", "replace")
    _require(not SECRET.search(commit), "Commit содержит возможный секрет")
    return dict(identity, sourceCode=digest(records)), records


def _paths(store, prepared):
    ident = prepared.get("id", "")
    _require(isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{32}", ident), "Некорректный clean-clone ID")
    owned = store.home / "cleanrooms" / ident
    repo, home = owned / "repo", owned / "home"
    _no_links(repo, store.root)
    _no_links(home, store.root)
    _require(prepared.get("path") == str(repo), "Подменён путь clean-clone")
    return owned, repo, home


def _artifact_value(store, artifact):
    _no_links(store.run, store.root)
    p = confined(store.run, artifact["file"])
    _no_links(p, store.root)
    _require(p.is_file() and file_hash(p) == artifact["sha256"], "Изменено свидетельство clean-clone")
    return json.loads(p.read_text(encoding="utf-8"))


def _receipt_valid(store, receipt):
    return _artifact_value(store, receipt["artifact"]) == {k: v for k, v in receipt.items() if k != "artifact"}


def _artifact(store, group, data, suffix):
    _no_links(store.run / group, store.root)
    return store.artifact(group, data, suffix)


def _git_metadata(repo):
    gitdir = repo / ".git"
    _require(gitdir.is_dir() and not gitdir.is_symlink(), "Подменён .git clean-clone")
    for parent, dirs, files in os.walk(gitdir, followlinks=False):
        for name in dirs + files:
            _require(not (Path(parent) / name).is_symlink(), "Ссылка в .git clean-clone")
    _require(not (gitdir / "objects/info/alternates").exists(), "Clean-clone использует внешние Git objects")
    # This configuration is written by prepare, never inherited from source.
    _require((gitdir / "config").read_bytes() == _CONFIG, "Изменена конфигурация clean-clone")
    exclude = gitdir / "info/exclude"
    _require(not exclude.exists() or exclude.read_bytes() == b"", "Изменены исключения clean-clone")
    attrs = gitdir / "info/attributes"
    _require(not attrs.exists() or attrs.read_bytes() == b"", "Изменены Git attributes clean-clone")


_CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = false\n"


def prepare(store, state):
    """Clone exact current HEAD without local objects, credentials or working files."""
    _require(store.run is not None and state.get("run") == store.run.name, "Нет активного прогона")
    source, records = _source(store)
    ident = uuid.uuid4().hex
    owned = store.home / "cleanrooms" / ident
    repo, home = owned / "repo", owned / "home"
    _no_links(owned, store.root)
    owned.mkdir(parents=True, exist_ok=False)
    try:
        home.mkdir()
        (home / "tmp").mkdir()
        _git(store.root, "-c", "protocol.file.allow=always", "clone", "--no-local", "--no-hardlinks",
             "--no-checkout", "--depth=1", "--no-tags", "--no-recurse-submodules",
             "--template=", "--", str(store.root), str(repo), home=home)
        # Replace clone's origin (which reveals the source path) before checkout.
        (repo / ".git/config").write_bytes(_CONFIG)
        _git(repo, "checkout", "--detach", source["head"], home=home)
        _git_metadata(repo)
        _require(_identity(repo) == {k: source[k] for k in ("head", "tree")}, "Клон получил другой commit")
        _require(_records(repo, scan_secrets=True) == records, "Содержимое клона отличается от исходного commit")
        _require(not _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout,
                 "Созданный clean-clone не чист")
        for name, _record in records:
            a, b = (store.root / name).stat(), (repo / name).stat()
            _require((a.st_dev, a.st_ino) != (b.st_dev, b.st_ino), "Clean-clone разделяет файл с исходником")
        after, _ = _source(store)
        _require(after == source, "Исходник изменился во время создания clean-clone")
        snapshot = _artifact(store, "cleanroom", _json(dict(source, files=records)), ".snapshot.json")
        receipt = dict(source, id=ident, run=state["run"], path=str(repo), snapshot=snapshot,
                       createdAt=timestamp(), limitations=LIMITATIONS.copy())
        receipt["artifact"] = _artifact(store, "cleanroom", _json(receipt), ".prepared.json")
        return receipt
    except Exception:
        # owned is newly made by this call. Never remove any caller-provided path.
        if owned.is_symlink():
            owned.unlink()
        elif owned.exists():
            shutil.rmtree(owned)
        raise


def _check_prepared(store, state):
    prepared = state.get("cleanroom")
    _require(isinstance(prepared, dict) and prepared.get("run") == state.get("run"), "Нет подготовленного clean-clone")
    _require(_receipt_valid(store, prepared), "Изменена квитанция clean-clone")
    _owned, repo, home = _paths(store, prepared)
    _require(repo.is_dir() and home.is_dir(), "Clean-clone отсутствует")
    snapshot = _artifact_value(store, prepared["snapshot"])
    source, records = _source(store)
    _require(all(prepared.get(k) == v and snapshot.get(k) == v for k, v in source.items()),
             "Clean-clone устарел относительно текущего HEAD")
    _require(snapshot.get("files") == records, "Изменён снимок исходных файлов")
    _git_metadata(repo)
    _require(_identity(repo) == {k: source[k] for k in ("head", "tree")}, "Изменён HEAD clean-clone")
    _require(_records(repo, scan_secrets=True) == records, "Изменены файлы clean-clone")
    _require(not _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                      "--ignore-submodules=none").stdout, "Clean-clone содержит tracked или unignored изменения")
    return prepared, repo, home


def current(store, state):
    """True only if immutable preparation, source and actual clone still agree."""
    try:
        _check_prepared(store, state)
        return True
    except (Invalid, KeyError, OSError, ValueError, TypeError):
        return False


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can return EPERM for a group whose only member is a zombie.
        # Reap that parent; a running process with EPERM is a real failure.
        if proc.poll() is None:
            raise Invalid("Не удалось остановить process group clean-run")


def _run(command, cwd, env, timeout, secrets):
    _require(os.name == "posix", "Clean-run с остановкой дочерних процессов поддерживается только на POSIX")
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return b"AISpa: command could not start.\n", 127, False, False, False
    captured = bytearray()
    truncated, timed_out, redacted = False, False, False
    tail = b""
    tail_size = max([4096] + [len(s.encode()) for s in secrets])
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    stopped, drain_deadline = False, None
    try:
        while selector.get_map() or proc.poll() is None:
            if not stopped and time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc, signal.SIGKILL)
                stopped, drain_deadline = True, time.monotonic() + 1
            elif not stopped and proc.poll() is not None:
                # An exited parent must not leave background children running.
                _kill_group(proc, signal.SIGKILL)
                stopped, drain_deadline = True, time.monotonic() + 1
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                break
            for key, _mask in selector.select(timeout=0.05):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                text = (tail + chunk).decode("utf-8", "replace")
                redacted = redacted or bool(SECRET.search(text)) or any(v in text for v in secrets)
                tail = (tail + chunk)[-tail_size:]
                keep = min(len(chunk), MAX_OUTPUT - len(captured))
                captured.extend(chunk[:keep])
                truncated = truncated or keep < len(chunk)
        rc = proc.wait(timeout=5)
        if timed_out:
            captured.extend(b"\nAISpa: timeout; process group terminated.\n")
            rc = 124
        if truncated:
            captured.extend(b"\nAISpa: output truncated at 1048576 bytes.\n")
        return bytes(captured), rc, timed_out, truncated, redacted
    finally:
        if not stopped:
            _kill_group(proc, signal.SIGKILL)
        proc.wait(timeout=5)
        selector.close()
        proc.stdout.close()


def _guard_path(value, source, clone):
    """Inspect intermediate symlinks too: source venv/bin/python may link out."""
    if not value or "/" not in value and not value.startswith("."):
        return
    path = Path(os.path.abspath(clone / value))
    seen = set()
    for _ in range(40):
        _require(not path.is_relative_to(source) or path.is_relative_to(clone),
                 "Clean-run не может использовать пути исходного проекта вне клона; установите зависимости внутри клона")
        _require(path not in seen, "Циклическая ссылка в пути clean-run")
        seen.add(path)
        link = next((part for part in [*reversed(path.parents), path] if part.is_symlink()), None)
        if link is None:
            return
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = link.parent / target
        path = Path(os.path.abspath(target / path.relative_to(link)))
    raise Invalid("Слишком длинная цепочка ссылок в пути clean-run")


def _guard_command(command, source, clone, environment):
    """Reject detectable source paths, not arbitrary programmatic file access."""
    executable = command[0]
    if "/" not in executable:
        executable = shutil.which(executable, path=environment["PATH"])
    if executable:
        _guard_path(executable, source, clone)
    for argument in command:
        candidates = {argument}
        start = argument.find(str(source))
        while start >= 0:
            # Also catches attached flags such as -I/source/include.
            candidates.add(argument[start:])
            start = argument.find(str(source), start + len(str(source)))
        if "=" in argument:
            candidates.add(argument.split("=", 1)[1])
        try:
            candidates.update(shlex.split(argument))
        except ValueError:
            pass
        # Python -c, shell -c and JavaScript -e commonly contain quoted paths.
        for match in re.finditer(r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', argument):
            literal = match.group()
            try:
                value = ast.literal_eval(literal)
            except (ValueError, SyntaxError):
                value = literal[1:-1]
            if isinstance(value, str):
                candidates.add(value)
        for value in candidates:
            if value.startswith("file://"):
                from urllib.parse import unquote, urlsplit
                value = unquote(urlsplit(value).path)
            _guard_path(value.removeprefix("@"), source, clone)


def execute(store, state, command, timeout=600):
    """Run explicit argv; install lockfile dependencies inside this clone first.

    System interpreters may still have ambient packages. This is not a sandbox
    or a guarantee of full package isolation; detectable source paths are refused.
    """
    _require(isinstance(command, list) and command and all(isinstance(v, str) and "\0" not in v for v in command)
             and command[0].strip(), "Нужна явная команда clean-run после --")
    _require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout)
             and 0 < timeout <= 86400, "Timeout clean-run должен быть от 0 до 86400 секунд")
    secrets = [v for k, v in os.environ.items() if SENSITIVE_ENV.search(k) and len(v) >= 8]
    joined = " ".join(command)
    _require(not SECRET.search(joined) and not any(v in joined for v in secrets),
             "Секреты нельзя передавать аргументами clean-run")
    prepared, repo, home = _check_prepared(store, state)
    environment = _environment(home)
    _guard_command(command, store.root, repo, environment)
    started = time.monotonic()
    raw, rc, timed_out, truncated, redacted = _run(command, repo, environment, timeout, secrets)
    output = raw.decode("utf-8", "replace")
    redacted = redacted or bool(SECRET.search(output) or any(v in output for v in secrets))
    if redacted:
        output = "AISpa withheld the entire output because it may contain a secret.\n"
    artifact = _artifact(store, "evidence", output.encode() or b"(no output)\n", ".cleanrun.log")
    try:
        source, _ = _source(store)
        source_changed = any(prepared.get(k) != v for k, v in source.items())
    except (Invalid, OSError, ValueError):
        source_changed = True
    clone_current = current(store, state)
    receipt = {"id": uuid.uuid4().hex, "run": state["run"], "prepared": prepared["id"],
               "head": prepared["head"], "tree": prepared["tree"], "sourceCode": prepared["sourceCode"],
               "code": prepared["sourceCode"], "path": str(repo), "command": list(command), "timeout": timeout,
               "exitCode": rc, "timedOut": timed_out, "truncated": truncated, "redacted": redacted,
               "changedSource": source_changed, "changedCode": not clone_current,
               "passed": rc == 0 and not redacted and clone_current and not source_changed,
               "output": artifact, "durationSeconds": round(time.monotonic() - started, 3),
               "at": timestamp(), "limitations": LIMITATIONS.copy()}
    receipt["artifact"] = _artifact(store, "cleanroom", _json(receipt), ".run.json")
    return receipt


def valid(store, state):
    """Latest cleanrun must pass and remain bound to immutable evidence and files."""
    receipt = state.get("cleanrun")
    try:
        if not (isinstance(receipt, dict) and receipt.get("passed") is True and receipt.get("exitCode") == 0
                and not any(receipt.get(k) for k in ("timedOut", "redacted", "changedSource", "changedCode"))
                and receipt.get("run") == state.get("run") and _receipt_valid(store, receipt)):
            return False
        prepared, _repo, _home = _check_prepared(store, state)
        if not all(receipt.get(k) == prepared[k] for k in ("head", "tree", "sourceCode", "path")):
            return False
        if receipt.get("prepared") != prepared["id"]:
            return False
        p = confined(store.run, receipt["output"]["file"])
        _no_links(p, store.root)
        return p.is_file() and file_hash(p) == receipt["output"]["sha256"]
    except (Invalid, KeyError, OSError, ValueError, TypeError):
        return False


verify = current
