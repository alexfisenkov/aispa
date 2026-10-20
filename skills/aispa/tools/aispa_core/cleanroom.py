"""Reproducible local Git clones and command evidence; not an execution sandbox.

The caller owns journal/state transitions. Save prepare() as ``cleanroom`` and
execute() as ``cleanrun``. Only valid() is evidence of a current successful run.
"""
import ast
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid

from .store import Invalid, confined, digest, file_hash, timestamp
from .portable import is_link, relative_name
from . import git_snapshot, process_runner


MAX_OUTPUT = 1024 * 1024


def _windows_root():
    """Read the installed Windows directory, not an arbitrary inherited value."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    api.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Не удалось определить каталог Windows")
    return Path(buffer.value)


def _windows_safe_path():
    windows = _windows_root()
    directories = [Path(sys.executable).resolve().parent, windows / "System32",
                   windows / "System32/WindowsPowerShell/v1.0"]
    # Discover specific installed tool locations once; never copy the host PATH.
    for name in ("git.exe", "node.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            directories.append(Path(found).absolute().parent)
    return os.pathsep.join(dict.fromkeys(str(p) for p in directories if p.is_dir()))


SAFE_PATH = (_windows_safe_path() if os.name == "nt"
             else "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
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
            _require(not is_link(part), "Символическая ссылка в служебном пути clean-clone")


def _environment(home):
    # No inherited tokens, SSH agent, user Git/package config, PYTHONPATH or PATH.
    result = {"PATH": SAFE_PATH, "HOME": str(home), "USERPROFILE": str(home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "CI": "1",
            "TMPDIR": str(home / "tmp"), "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"), "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "0", "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
    if os.name == "nt":
        windows = _windows_root()
        result.update(SystemRoot=str(windows), WINDIR=str(windows),
                      COMSPEC=str(windows / "System32/cmd.exe"), PATHEXT=".COM;.EXE;.BAT;.CMD",
                      APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"))
    return result


def _which(name, environment):
    if os.name != "nt":
        return shutil.which(name, path=environment["PATH"])
    # shutil.which on older Windows Python also consults caller cwd/PATHEXT.
    # Resolve only in the explicit clean PATH with fixed executable extensions.
    suffixes = ("",) if Path(name).suffix else (".exe", ".com", ".cmd", ".bat")
    for directory in environment["PATH"].split(os.pathsep):
        if not directory:
            continue
        for suffix in suffixes:
            path = Path(directory) / (name + suffix)
            if path.is_file():
                return str(path)
    return None


def _git(root, *args, home=None, allow_failure=False, input_data=None):
    env = _environment(home or Path(os.devnull))
    executable = _which("git", env) if os.name == "nt" else "git"
    _require(executable is not None, "Не найден установленный Git для clean-clone")
    if os.name == "nt":
        _guard_path(executable, root, None)
    try:
        result = subprocess.run(
            [executable, "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
             "-c", "core.attributesFile=" + os.devnull, "-c", "core.longpaths=true",
             "-c", "i18n.logOutputEncoding=utf-8", "-c", "core.quotepath=false", "-C", str(root), *args],
            capture_output=True, env=env, timeout=60,
            **({"input": input_data} if input_data is not None else {"stdin": subprocess.DEVNULL}))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Invalid("Не удалось выполнить Git для clean-clone") from error
    if result.returncode and not allow_failure:
        # Git stderr can contain a private origin URL. Do not persist or echo it.
        raise Invalid("Git не смог подготовить или проверить clean-clone")
    return result


def _safe_name(name):
    try:
        canonical = relative_name(name)
    except ValueError as exc:
        raise Invalid("Небезопасный путь в Git tree") from exc
    p = PurePosixPath(canonical)
    _require(canonical == name and not any(part.casefold() in (".git", ".aispa") for part in p.parts),
             "Небезопасный путь в Git tree")
    return p


def _credential_name(name):
    parts = PurePosixPath(name).parts
    return any(p.lower() in SENSITIVE_NAMES or p.lower() in (".ssh", ".aws", ".gnupg") or
               p.lower() == ".env" or (p.lower().startswith(".env.") and
               p.lower() not in (".env.example", ".env.sample", ".env.template")) or
               p.lower().endswith((".p12", ".pfx", ".key")) for p in parts)


def _records(root, *, scan_secrets=False, verify_head=False):
    # Every call verifies canonical content; legacy keyword arguments remain for
    # callers and tests. The shared module owns Git attributes/EOL handling.
    return git_snapshot.records(root, _git, secret_pattern=SECRET,
                                credential_name=_credential_name)


def _identity(root):
    _require(not is_link(root / ".git"), ".git не должна быть символической ссылкой")
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()).resolve()
    _require(top == root, "--root должен указывать на корень Git-репозитория")
    return {"head": _git(root, "rev-parse", "HEAD").stdout.decode().strip(),
            "tree": _git(root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()}


def _source(store):
    identity = _identity(store.root)
    _require(_git(store.root, "check-ignore", "--no-index", ".aispa/", allow_failure=True).returncode == 0,
             "До clean-clone добавьте .aispa/ в Git ignore")
    _require(_git(store.root, "rev-parse", "--show-object-format").stdout.strip() == b"sha1",
             "Clean-clone пока поддерживает только Git object-format sha1")
    # Do not rely only on Git status: assume-unchanged/skip-worktree can hide edits.
    records = _records(store.root, scan_secrets=True, verify_head=True)
    git_snapshot.ensure_clean_index(store.root, _git)
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
    _require(gitdir.is_dir() and not is_link(gitdir), "Подменён .git clean-clone")
    for parent, dirs, files in os.walk(gitdir, followlinks=False):
        for name in dirs + files:
            _require(not is_link(Path(parent) / name), "Ссылка в .git clean-clone")
    _require(not (gitdir / "objects/info/alternates").exists(), "Clean-clone использует внешние Git objects")
    # This configuration is written by prepare, never inherited from source.
    _require((gitdir / "config").read_bytes() == _CONFIG, "Изменена конфигурация clean-clone")
    exclude = gitdir / "info/exclude"
    _require(not exclude.exists() or exclude.read_bytes() == b"", "Изменены исключения clean-clone")
    attrs = gitdir / "info/attributes"
    _require(not attrs.exists() or attrs.read_bytes() == b"", "Изменены Git attributes clean-clone")


_CONFIG = ("[core]\n\trepositoryformatversion = 0\n\tfilemode = "
           + ("false" if os.name == "nt" else "true")
           + "\n\tbare = false\n\tlogallrefupdates = false\n\tautocrlf = false\n\teol = lf\n").encode()


def _remove_owned_clone(owned):
    """Remove only this preparation's new directory, including Git readonly blobs."""
    if is_link(owned):
        if owned.is_symlink():
            owned.unlink()
        else:
            owned.rmdir()
        return

    def retry_generated_file(function, name, error):
        path = Path(name)
        failure = error[1]
        if not isinstance(failure, PermissionError) or function not in (os.unlink, os.remove):
            raise failure
        _no_links(path, owned)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise failure
        # Python 3.11 Windows chmod has no follow_symlinks option. In that
        # runtime the component guard above refuses every link/junction first.
        options = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
        os.chmod(path, info.st_mode | stat.S_IWUSR, **options)
        function(path)

    if owned.exists():
        shutil.rmtree(owned, onerror=retry_generated_file)


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
        git_snapshot.ensure_clean_index(repo, _git)
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
        _remove_owned_clone(owned)
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
    git_snapshot.ensure_clean_index(repo, _git)
    return prepared, repo, home


def current(store, state):
    """True only if immutable preparation, source and actual clone still agree."""
    try:
        _check_prepared(store, state)
        return True
    except (Invalid, KeyError, OSError, ValueError, TypeError):
        return False


def _run(command, cwd, env, timeout, secrets):
    return process_runner.run(command, cwd, env, timeout, max_output=MAX_OUTPUT,
                              secret_pattern=SECRET, secrets=secrets)


def _path_argument(value):
    return "/" in value or "\\" in value or value.startswith(".") or bool(PureWindowsPath(value).drive)


def _argument_path(value, clone):
    if os.name == "nt":
        value = value.replace("/", "\\")
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        _require(not value.startswith("\\\\.\\"), "Device-пути запрещены в clean-run")
        windows = PureWindowsPath(value)
        _require(not windows.drive or windows.is_absolute(), "Drive-relative путь запрещён в clean-run")
    return Path(os.path.abspath(clone / value))


def _guard_path(value, source, clone):
    """Inspect intermediate symlinks too: source venv/bin/python may link out."""
    if not value or not _path_argument(value):
        return
    if os.name == "nt":
        # Whole inline programs are also candidates; invalid Windows filenames
        # cannot be filesystem references. Their quoted literals are checked
        # separately below, including Python raw strings and shell strings.
        probe = value[4:] if value.startswith("\\\\?\\") else value
        if any(c in probe for c in '<>"|?*\n\r\t') or ":" in probe[2:]:
            _require(not PureWindowsPath(probe).drive and not probe.startswith("\\"),
                     "Некорректный абсолютный путь или ADS запрещён в clean-run")
            return
    path = _argument_path(value, clone or source)
    seen = set()
    for _ in range(40):
        _require(not path.is_relative_to(source) or clone is not None and path.is_relative_to(clone),
                 "Clean-run не может использовать пути исходного проекта вне клона; установите зависимости внутри клона")
        _require(path not in seen, "Циклическая ссылка в пути clean-run")
        seen.add(path)
        link = next((part for part in [*reversed(path.parents), path] if is_link(part)), None)
        if link is None:
            return
        target = _argument_path(os.readlink(link), link.parent)
        path = _argument_path(str(target / path.relative_to(link)), clone or source)
    raise Invalid("Слишком длинная цепочка ссылок в пути clean-run")


def _argument_words(argument, *, windows):
    """Extract guard candidates without interpreting Windows backslashes as escapes."""
    try:
        words = shlex.split(argument, posix=not windows)
    except ValueError:
        return set()
    return {word.strip('"\'') if windows else word for word in words}


def _literal_paths(argument, *, windows):
    """Keep shell literals verbatim and also recognize valid Python path escapes."""
    candidates = set()
    for match in re.finditer(r'''(?i:(?:br|rb|r|u|b)?)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', argument):
        literal = match.group()
        quote_at = min(i for i, char in enumerate(literal) if char in "\"'")
        candidates.add(literal[quote_at + 1:-1])
        try:
            value = ast.literal_eval(literal)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and (not windows or not any(ord(char) < 32 for char in value)):
            candidates.add(value)
        # cmd/PowerShell do not decode Python's \t, \f, etc. Do not invent
        # invalid Windows filenames from their ordinary backslash paths. The
        # original literal is still checked, and valid decoded paths remain.
    return candidates


def _guard_command(command, source, clone, environment):
    """Reject detectable source paths, not arbitrary programmatic file access."""
    executable = command[0]
    if not _path_argument(executable):
        executable = _which(executable, environment)
    if executable:
        _guard_path(executable, source, clone)
    for argument in command:
        candidates = {argument}
        normalized = argument.replace("\\", "/") if os.name == "nt" else argument
        source_name = source.as_posix()
        searchable = normalized.casefold() if os.name == "nt" else normalized
        needle = source_name.casefold() if os.name == "nt" else source_name
        start = searchable.find(needle)
        while start >= 0:
            # Also catches attached flags such as -I/source/include.
            candidates.add(normalized[start:])
            start = searchable.find(needle, start + len(needle))
        if "=" in argument:
            candidates.add(argument.split("=", 1)[1])
        candidates.update(_argument_words(argument, windows=os.name == "nt"))
        # Python -c, shell -c and JavaScript -e commonly contain quoted paths.
        candidates.update(_literal_paths(argument, windows=os.name == "nt"))
        for value in candidates:
            if value.startswith("file://"):
                from urllib.parse import unquote, urlsplit
                parsed = urlsplit(value)
                value = unquote(parsed.path)
                if os.name == "nt" and parsed.netloc:
                    value = "//" + parsed.netloc + value
                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
                    value = value[1:]
            _guard_path(value.removeprefix("@"), source, clone)


def _execution_command(command, source, clone, environment):
    argv = list(command)
    if os.name != "nt":
        return argv
    _require(Path(argv[0]).suffix.casefold() not in (".cmd", ".bat"),
             "Для .cmd/.bat укажите явный cmd.exe /d /c или PowerShell")
    # Drop host-installed tool directories that point into the source checkout.
    directories = []
    for directory in environment["PATH"].split(os.pathsep):
        try:
            _guard_path(directory, source, clone)
        except Invalid:
            continue
        directories.append(directory)
    environment["PATH"] = os.pathsep.join(directories)
    executable = (_argument_path(argv[0], clone) if _path_argument(argv[0])
                  else _which(argv[0], environment))
    if executable is not None:
        _require(Path(executable).suffix.casefold() not in (".cmd", ".bat"),
                 "Для .cmd/.bat укажите явный cmd.exe /d /c или PowerShell")
        argv[0] = str(executable)
    else:
        # Do not let CreateProcess fall back to the controller's cwd or PATH.
        return None
    return argv


def execute(store, state, command, timeout=600):
    """Run explicit argv; install lockfile dependencies inside this clone first.

    System interpreters may still have ambient packages. This is not a sandbox
    or a guarantee of full package isolation; detectable source paths are refused.
    """
    from .executions import validate_command
    validate_command(command)
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
    execution_command = _execution_command(command, store.root, repo, environment)
    _guard_command(command, store.root, repo, environment)
    started = time.monotonic()
    if execution_command is None:
        raw, rc, timed_out, truncated, redacted = b"AISpa: command could not start.\n", 127, False, False, False
    else:
        raw, rc, timed_out, truncated, redacted = _run(execution_command, repo, environment, timeout, secrets)
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
