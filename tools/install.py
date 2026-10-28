#!/usr/bin/env python3
"""First AISpa install from a published GitHub Release (Python 3.11+ and Git).

Preview stages a verified candidate; apply requires its ID and --confirm. The
fixed publisher is the trust root. HTTPS, the published tag, manifest and file
hashes establish provenance and integrity, not an independent signature.
"""
import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "alexfisenkov/aispa"
API = f"https://api.github.com/repos/{REPOSITORY}"
GIT_URL = f"https://github.com/{REPOSITORY}.git"
RELEASES_URL = API + "/releases?per_page=100"
LATEST_RELEASE_URL = API + "/releases/latest"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_FILES = 2048
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE = re.compile(r"aispa-install-[A-Za-z0-9_-]{8,64}\Z")
_RESERVED = re.compile(r"^(?:con|conin\$|conout\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
# Standalone installer: keep keys in sync with project_context.RUNTIMES.
_RUNTIME_DIRS = {"codex": ".codex", "claude-code": ".claude", "cursor": ".cursor",
                 "openclaw": ".openclaw", "gemini": ".gemini",
                 "antigravity": ".gemini/config"}
_PROJECT_DIRS = {"codex": ".agents", "claude-code": ".claude", "cursor": ".cursor",
                 "openclaw": ".agents", "gemini": ".gemini", "antigravity": ".agents"}


def _require_runtime(package, runtime):
    # Legacy releases supported the original three hosts before this metadata
    # was introduced. New destinations must not install a host-incompatible ZIP.
    if runtime is None or runtime in ("codex", "claude-code", "cursor"):
        return
    _need(isinstance(runtime, str) and runtime in _RUNTIME_DIRS, "Неизвестный runtime установки")
    supported = ()
    try:
        tree = ast.parse((package / "tools/aispa_core/project_context.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RUNTIMES" for t in node.targets):
                supported = ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    _need(isinstance(supported, (tuple, list)) and runtime in supported,
          "Опубликованный пакет не поддерживает runtime " + runtime + "; нужен выпуск с этим адаптером")


class InstallError(ValueError):
    pass


def _need(ok, message):
    if not ok:
        raise InstallError(message)


def _json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _need(key not in value, "Повтор ключа в JSON выпуска")
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise InstallError("Некорректный JSON выпуска") from exc


def _trusted_url(url, *, redirect=False):
    _need(isinstance(url, str), "Нужен URL опубликованного выпуска")
    value = urllib.parse.urlsplit(url)
    _need(value.scheme == "https" and value.username is None and value.password is None
          and value.port in (None, 443) and not value.fragment, "Недоверенный URL выпуска")
    host = value.hostname
    if host == "api.github.com" and value.path.startswith(f"/repos/{REPOSITORY}/"):
        return
    if host == "github.com" and value.path.startswith(f"/{REPOSITORY}/releases/"):
        return
    if redirect and host in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"):
        return
    raise InstallError("Ресурс находится вне фиксированного GitHub publisher")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _trusted_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url, limit, timeout):
    _trusted_url(url)
    _need(isinstance(timeout, (int, float)) and 0 < timeout <= 120, "Некорректный тайм-аут")
    headers = {"User-Agent": "AISpa-first-installer", "Accept": "application/vnd.github+json"}
    if urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Cache-Control"] = "no-cache"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_SafeRedirect())
    deadline = time.monotonic() + timeout
    with opener.open(request, timeout=min(timeout, 20)) as response:
        _trusted_url(response.geturl(), redirect=True)
        length = response.headers.get("Content-Length")
        if length is not None:
            _need(length.isdigit() and int(length) <= limit, "Ресурс выпуска превышает предел")
        data = bytearray()
        while True:
            _need(time.monotonic() < deadline, "Истекло время загрузки выпуска")
            reader = getattr(response, "read1", response.read)
            chunk = reader(min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            _need(len(data) <= limit, "Ресурс выпуска превышает предел")


def _system_ca_bundle():
    """Use an existing OS CA bundle if this Python build has none configured."""
    if os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile:
        return
    for name in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                 "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(name).is_file():
            os.environ["SSL_CERT_FILE"] = name
            return


def _semver(version):
    match = _SEMVER.fullmatch(version) if isinstance(version, str) else None
    _need(match is not None, "Некорректная версия опубликованного выпуска")
    major, minor, patch, pre, _build = match.groups()
    suffix = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return (int(major), int(minor), int(patch), bool(not pre), suffix)


def _releases(timeout):
    _need(isinstance(timeout, (int, float)) and 0 < timeout <= 120, "Некорректный тайм-аут")
    deadline = time.monotonic() + timeout
    rows = []

    def metadata(url):
        remaining = deadline - time.monotonic()
        _need(remaining > 0, "Истекло время обнаружения выпусков")
        return _json(_fetch(url, MAX_METADATA_BYTES, remaining))

    def page_rows(url):
        batch = metadata(url)
        _need(isinstance(batch, list) and len(batch) <= 100, "Неверный список GitHub Releases")
        return batch

    for page in range(1, 6):
        url = RELEASES_URL if page == 1 else RELEASES_URL + f"&page={page}"
        batch = page_rows(url)
        if page == 1 and not batch:
            batch = page_rows(RELEASES_URL + "&page=1")
        rows.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise InstallError("Слишком много выпусков для безопасного выбора")

    # A separate published-release endpoint corroborates empty or stale lists;
    # its record replaces the same tag, while selection still uses SemVer.
    try:
        latest = metadata(LATEST_RELEASE_URL)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code != 404:
            raise
        return rows
    _need(isinstance(latest, dict) and latest.get("draft") is False
          and latest.get("prerelease") is False and latest.get("published_at"),
          "GitHub вернул некорректный последний выпуск")
    tag = latest.get("tag_name")
    _need(isinstance(tag, str) and _semver(tag.removeprefix("v"))[3],
          "Последний стабильный выпуск не имеет стабильной версии")
    expected = f"https://github.com/{REPOSITORY}/releases/tag/{urllib.parse.quote(tag, safe='')}"
    _need(latest.get("html_url") == expected, "Некорректный адрес последнего выпуска")
    return [row for row in rows if not isinstance(row, dict) or row.get("tag_name") != tag] + [latest]


def _validate_tag(tag):
    _need(isinstance(tag, str) and len(tag) <= 128
          and re.fullmatch(r"v?[0-9A-Za-z.+-]+", tag), "Некорректный tag")
    _semver(tag.removeprefix("v"))


def _tag_release(tag, timeout):
    # Explicit versions must be published Releases, not just Git refs. Validate
    # before network access and never depend on the global list for this route.
    _validate_tag(tag)
    url = API + "/releases/tags/" + urllib.parse.quote(tag, safe="")
    row = _json(_fetch(url, MAX_METADATA_BYTES, timeout))
    _need(isinstance(row, dict) and row.get("tag_name") == tag,
          "Опубликованный Release не соответствует запрошенному tag")
    return _select_release([row], tag)


def _select_release(rows, tag=None):
    if tag is not None:
        _validate_tag(tag)
    eligible = []
    for row in rows:
        if not isinstance(row, dict) or row.get("draft") is not False or not row.get("published_at"):
            continue
        release_tag = row.get("tag_name")
        if not isinstance(release_tag, str) or (tag is not None and release_tag != tag):
            continue
        version = release_tag.removeprefix("v")
        try:
            order = _semver(version)
        except InstallError:
            continue
        if tag is None and (not order[3] or row.get("prerelease") is not False):
            continue
        _need(type(row.get("prerelease")) is bool, "Некорректный признак prerelease у Release")
        expected_url = f"https://github.com/{REPOSITORY}/releases/tag/{urllib.parse.quote(release_tag, safe='')}"
        _need(row.get("html_url") == expected_url, "Некорректный адрес Release")
        assets = row.get("assets")
        _need(isinstance(assets, list), "Некорректные ресурсы Release")
        counts = [sum(1 for item in assets if isinstance(item, dict) and item.get("name") == name)
                  for name in ("aispa-update.json", "aispa.zip")]
        if tag is None and counts != [1, 1]:
            # A Release without the package files (v1.0.0, for one) is not an
            # install source; the newest one that has them is chosen instead.
            continue
        urls = {}
        for name in ("aispa-update.json", "aispa.zip"):
            matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
            _need(len(matches) == 1, "Выпуску нужен ровно один ресурс " + name)
            expected = f"https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(release_tag, safe='')}/{name}"
            _need(matches[0].get("browser_download_url") == expected
                  and matches[0].get("state") == "uploaded", "Некорректный ресурс Release")
            urls[name] = expected
        eligible.append((order, {"tag": release_tag, "version": version,
                                 "url": expected_url, "assets": urls,
                                 "prerelease": row["prerelease"],
                                 "publishedAt": row["published_at"]}))
    _need(eligible, "Опубликованный стабильный Release с проверяемым пакетом не найден"
          if tag is None else "Опубликованный Release с проверяемым пакетом не найден")
    return max(eligible, key=lambda item: item[0])[1]


def _source_commit(tag, timeout):
    quoted = urllib.parse.quote(tag, safe="")
    ref = _json(_fetch(f"{API}/git/ref/tags/{quoted}", MAX_METADATA_BYTES, timeout))
    _need(isinstance(ref, dict) and ref.get("ref") == "refs/tags/" + tag,
          "Git tag выпуска не подтверждён")
    obj = ref.get("object")
    for _ in range(5):
        _need(isinstance(obj, dict) and isinstance(obj.get("sha"), str)
              and _COMMIT.fullmatch(obj["sha"]), "Некорректный объект Git tag")
        if obj.get("type") == "commit":
            return obj["sha"]
        _need(obj.get("type") == "tag", "Tag не указывает на commit")
        tag_object = _json(_fetch(f"{API}/git/tags/{obj['sha']}", MAX_METADATA_BYTES, timeout))
        _need(isinstance(tag_object, dict), "Некорректный annotated tag")
        obj = tag_object.get("object")
    raise InstallError("Слишком длинная цепочка annotated tag")


def _git(args, *, timeout, cwd=None):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    try:
        result = subprocess.run(["git", "-c", "protocol.file.allow=never",
                                 "-c", "submodule.recurse=false", "-c", "core.autocrlf=false", *args],
                                cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("Git недоступен или не завершился в установленное время") from exc
    _need(result.returncode == 0, "Не удалось получить точный source commit Release")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _clone_verified(tag, commit, destination, timeout):
    _need(_COMMIT.fullmatch(commit) is not None, "Некорректный source commit")
    _git(["clone", "--depth", "1", "--single-branch", "--branch", tag,
          "--", GIT_URL, str(destination)], timeout=timeout)
    head = _git(["-C", str(destination), "rev-parse", "HEAD"], timeout=10)
    _need(head == commit, "Clone HEAD не совпал с опубликованным Git tag")
    _need((destination / "skills/aispa/tools/aispa_core/updates.py").is_file(),
          "В commit выпуска отсутствует штатный verifier")


def _load_pinned_verifier(repo):
    package = repo / "skills/aispa/tools/aispa_core"
    _need(package.is_dir(), "В commit выпуска нет пакета AISpa")
    name = "_aispa_verified_" + hashlib.sha256(str(repo).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                   submodule_search_locations=[str(package)])
    _need(spec is not None and spec.loader is not None, "Не удалось загрузить verifier выпуска")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    verifier = importlib.import_module(name + ".updates")
    _need(verifier.REPOSITORY == REPOSITORY, "Verifier выпуска указывает на другой publisher")
    return verifier


def _is_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & 0x400:
        tag = getattr(info, "st_reparse_tag", 0)
        return not tag or bool(tag & 0x20000000)
    return False


def _safe_destination(destination):
    _need(isinstance(destination, (str, os.PathLike)), "Нужен абсолютный путь установки")
    raw = os.fspath(destination)
    _need(isinstance(raw, str) and raw and "\0" not in raw, "Нужен абсолютный путь установки")
    alias = Path(raw).expanduser()
    _need(alias.is_absolute() and ".." not in alias.parts and alias.name not in ("", ".", ".."),
          "Путь установки должен быть абсолютным каталогом без ..")
    _need(not _is_link(alias), "Ссылка или junction вместо каталога установки запрещены")
    # A physical path makes a legitimate ancestor alias (macOS /var, for
    # example) visible in the preview and pins apply to its actual target.
    path = alias.resolve(strict=False)
    for part in path.parts[1:]:
        _need(part not in ("", ".", "..") and not part.endswith((".", " "))
              and not _RESERVED.match(part)
              and not any(ord(char) < 32 or char in '<>:"|?*' for char in part),
              "Путь содержит запрещённый компонент Windows")
    folded = tuple(part.casefold() for part in path.parts)
    _need(".git" not in folded and not any(folded[i:i + 2] == (".aispa", "runs")
                                       for i in range(len(folded) - 1)),
          "Нельзя установить навык внутрь Git metadata или активного прогона")
    for ancestor in (path, *path.parents):
        _need(not _is_link(ancestor), "Ссылка или junction в физическом пути установки запрещены")
    _need(not path.exists() and not _is_link(path), "Каталог установки уже существует")
    return path


def choose_destination(*, runtime=None, scope=None, project_root=None,
                       destination=None, home=None):
    if destination is not None:
        _need(runtime is None and scope is None and project_root is None,
              "--destination нельзя совмещать с runtime/scope/project-root")
        return _safe_destination(destination)
    _need(runtime in _RUNTIME_DIRS and scope in ("project", "global"),
          "Выберите --runtime и --scope либо --destination")
    if scope == "project":
        _need(project_root is not None, "Для проектной установки нужен --project-root")
        root = Path(project_root).expanduser()
        _need(root.is_absolute() and root.is_dir() and not _is_link(root),
              "Корень проекта должен быть существующим абсолютным каталогом")
        target = root / _PROJECT_DIRS[runtime] / "skills" / "aispa"
    else:
        _need(project_root is None, "Глобальная установка не принимает --project-root")
        root = Path(home) if home is not None else Path.home()
        _need(root.is_absolute() and root.is_dir() and not _is_link(root),
              "Домашний каталог должен существовать")
        target = root / _RUNTIME_DIRS[runtime] / "skills" / "aispa"
    return _safe_destination(target)


def _cache(cache_dir):
    alias = Path(cache_dir).expanduser() if cache_dir is not None else Path.home() / ".aispa-installs"
    _need(alias.is_absolute() and ".." not in alias.parts and not _is_link(alias),
          "Каталог кандидатов должен быть абсолютным обычным каталогом")
    root = alias.resolve(strict=False)
    for ancestor in (root, *root.parents):
        _need(not _is_link(ancestor), "Ссылка или junction в каталоге кандидатов запрещены")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _need(root.is_dir(), "Каталог кандидатов недоступен")
    return root


def _write_json(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    _need(len(data) <= MAX_METADATA_BYTES, "Квитанция превышает предел")
    with path.open("xb") as stream:
        stream.write(data)


def _scan_package(package):
    _need(package.is_dir() and not _is_link(package), "Пакет отсутствует или является ссылкой")
    records = {}
    total = 0
    for base, dirs, files in os.walk(package, followlinks=False):
        for name in dirs + files:
            _need(not _is_link(Path(base) / name), "Ссылка в пакете запрещена")
        for name in files:
            path = Path(base) / name
            rel = path.relative_to(package).as_posix()
            info = path.stat()
            _need(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES,
                  "Неподдерживаемый файл в пакете")
            data = path.read_bytes()
            _need(len(data) == info.st_size, "Пакет изменился при чтении")
            total += len(data)
            _need(len(records) < MAX_FILES and total <= MAX_PACKAGE_BYTES, "Пакет превышает предел")
            records[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                            "mode": 0o644 if os.name == "nt" else (0o755 if info.st_mode & 0o111 else 0o644)}
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return records, hashlib.sha256(encoded).hexdigest()


def _disk_records(records):
    if os.name != "nt":
        return records
    return {name: dict(record, mode=record["mode"] & ~0o111) for name, record in records.items()}


def _safe_member(name):
    _need(isinstance(name, str) and 0 < len(name) <= 512 and "\\" not in name,
          "Небезопасный путь файла в manifest")
    posix, windows = PurePosixPath(name), PureWindowsPath(name)
    _need(not posix.is_absolute() and not windows.drive and not windows.root
          and posix.as_posix() == name and posix.parts
          and all(part not in ("", ".", "..") and not part.endswith((".", " "))
                  and not _RESERVED.match(part) and part.casefold() not in (".git", ".aispa", "__pycache__")
                  and not any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                  for part in posix.parts), "Небезопасный путь файла в manifest")
    return name


def preview(*, runtime=None, scope=None, project_root=None, destination=None,
            tag=None, cache_dir=None, home=None, timeout=30):
    """Stage exactly one published release without touching the destination."""
    target = choose_destination(runtime=runtime, scope=scope, project_root=project_root,
                                destination=destination, home=home)
    _need(isinstance(timeout, (int, float)) and 0 < timeout <= 120, "Тайм-аут должен быть 0–120 секунд")
    _system_ca_bundle()
    release = _tag_release(tag, timeout) if tag is not None else _select_release(_releases(timeout))
    commit = _source_commit(release["tag"], timeout)
    cache = _cache(cache_dir)
    candidate = Path(tempfile.mkdtemp(prefix="aispa-install-", dir=cache))
    try:
        clone = candidate / "source"
        _clone_verified(release["tag"], commit, clone, timeout)
        verifier = _load_pinned_verifier(clone)
        # The already pinned publisher verifier checks exact release asset URLs,
        # manifest schema, ZIP entries, modes, sizes and per-file hashes.
        raw = {"draft": False, "published_at": release["publishedAt"],
               "prerelease": release["prerelease"], "tag_name": release["tag"],
               "html_url": release["url"], "assets": [
                   {"name": name, "state": "uploaded", "browser_download_url": url}
                   for name, url in release["assets"].items()]}
        validated = verifier._release([raw], verifier._semver("0.0.0-alpha.0"))
        _need(validated is not None and validated["tag"] == release["tag"],
              "Verifier не подтвердил опубликованный выпуск")
        deadline = verifier._deadline(timeout)
        manifest_bytes = verifier._download(validated["assets"]["aispa-update.json"],
                                            verifier.MAX_METADATA_BYTES, deadline)
        manifest = verifier._json(manifest_bytes)
        records = verifier._manifest(manifest, validated)
        _need(manifest["commit"] == commit
              and verifier._source_commit(release["tag"], deadline) == commit,
              "Manifest не связан с проверенным Git tag")
        archive = verifier._download(validated["assets"]["aispa.zip"],
                                     verifier.MAX_ARCHIVE_BYTES, deadline)
        archive_hash = hashlib.sha256(archive).hexdigest()
        _need(archive_hash == manifest["archive"]["sha256"], "SHA-256 ZIP не совпал с manifest")
        staged = candidate / "package"
        staged.mkdir()
        verifier._extract(archive, staged, records)
        _need(verifier._version(staged) == validated["version"], "Версия файлов не совпала с Release")
        actual, candidate_digest = _scan_package(staged)
        _need(actual == _disk_records(records), "Файлы не совпали с manifest после распаковки")
        _require_runtime(staged, runtime)
        _need(_safe_destination(target) == target, "Назначение изменилось во время подготовки")
        receipt = {"schema": 1, "status": "prepared", "candidateId": candidate.name,
                   "repository": REPOSITORY, "destination": str(target), "runtime": runtime,
                   "version": validated["version"], "tag": validated["tag"],
                   "prerelease": release["prerelease"],
                   "commit": commit, "releaseUrl": validated["url"],
                   "archiveSha256": archive_hash,
                   "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
                   "candidateDigest": candidate_digest, "fileCount": len(records),
                   "requiresConfirmation": True}
        with (candidate / "manifest.json").open("xb") as stream:
            stream.write(manifest_bytes)
        _write_json(candidate / "preview.json", receipt)
        return receipt
    except BaseException:
        shutil.rmtree(candidate)
        raise


def _candidate(cache_dir, ident):
    _need(isinstance(ident, str) and _CANDIDATE.fullmatch(ident), "Некорректный candidate ID")
    root = _cache(cache_dir)
    directory = root / ident
    _need(directory.is_dir() and not _is_link(directory) and directory.resolve().parent == root.resolve(),
          "Подготовленный кандидат недоступен")
    path = directory / "preview.json"
    _need(path.is_file() and not _is_link(path) and path.stat().st_size <= MAX_METADATA_BYTES,
          "Квитанция кандидата недоступна")
    receipt = _json(path.read_bytes())
    _need(isinstance(receipt, dict) and receipt.get("schema") == 1
          and receipt.get("status") == "prepared" and receipt.get("candidateId") == ident
          and receipt.get("repository") == REPOSITORY, "Кандидат уже использован или относится к другому publisher")
    return directory, receipt


def _mkdir_parents(target):
    # Missing parents are created one by one; an existing link or file aborts.
    missing = []
    current = target.parent
    while not current.exists():
        _need(not _is_link(current), "Ссылка в пути назначения запрещена")
        missing.append(current)
        current = current.parent
    _need(current.is_dir() and not _is_link(current), "Родитель назначения не является каталогом")
    for path in reversed(missing):
        path.mkdir(mode=0o755)
        _need(not _is_link(path), "Родитель установки изменился при создании")


def apply(candidate_id, *, confirmed=False, cache_dir=None):
    """Install into a newly reserved directory; never replace an existing one."""
    _need(confirmed is True, "После просмотра кандидата нужен явный --confirm")
    candidate, receipt = _candidate(cache_dir, candidate_id)
    target = _safe_destination(receipt.get("destination"))
    _need(str(target) == receipt["destination"], "Физическое назначение изменилось после просмотра")
    _need(receipt.get("requiresConfirmation") is True
          and isinstance(receipt.get("commit"), str) and _COMMIT.fullmatch(receipt["commit"])
          and isinstance(receipt.get("archiveSha256"), str) and _HASH.fullmatch(receipt["archiveSha256"])
          and isinstance(receipt.get("manifestSha256"), str) and _HASH.fullmatch(receipt["manifestSha256"]),
          "Квитанция кандидата повреждена")
    manifest_path = candidate / "manifest.json"
    _need(manifest_path.is_file() and not _is_link(manifest_path)
          and manifest_path.stat().st_size <= MAX_METADATA_BYTES
          and hashlib.sha256(manifest_path.read_bytes()).hexdigest() == receipt["manifestSha256"],
          "Manifest кандидата изменён")
    manifest = _json(manifest_path.read_bytes())
    _need(receipt.get("tag") in (receipt.get("version"), "v" + str(receipt.get("version")))
          and receipt.get("releaseUrl") == f"https://github.com/{REPOSITORY}/releases/tag/{urllib.parse.quote(receipt['tag'], safe='')}"
          and isinstance(receipt.get("candidateDigest"), str) and _HASH.fullmatch(receipt["candidateDigest"])
          and manifest.get("schema") == 1 and manifest.get("repository") == REPOSITORY
          and manifest.get("commit") == receipt["commit"]
          and manifest.get("version") == receipt["version"]
          and isinstance(manifest.get("archive"), dict)
          and manifest["archive"].get("name") == "aispa.zip"
          and manifest["archive"].get("sha256") == receipt["archiveSha256"],
          "Квитанция и manifest расходятся")
    files = manifest.get("files")
    _need(isinstance(files, list) and 0 < len(files) == receipt.get("fileCount") <= MAX_FILES,
          "Состав кандидата повреждён")
    records = {}
    for row in files:
        _need(isinstance(row, dict) and isinstance(row.get("path"), str)
              and isinstance(row.get("sha256"), str) and _HASH.fullmatch(row["sha256"])
              and type(row.get("size")) is int and 0 <= row["size"] <= MAX_FILE_BYTES
              and row.get("mode") in (0o644, 0o755), "Некорректная запись manifest")
        name = _safe_member(row["path"])
        _need(name not in records, "Повтор пути файла в manifest")
        records[name] = {key: row[key] for key in ("sha256", "size", "mode")}
    staged = candidate / "package"
    before, digest = _scan_package(staged)
    _require_runtime(staged, receipt.get("runtime"))
    _need(before == _disk_records(records) and digest == receipt.get("candidateDigest"),
          "Подготовленные файлы изменились после просмотра")
    _mkdir_parents(target)
    _need(_safe_destination(str(target)) == target, "Назначение изменилось при создании родителей")
    # mkdir is exclusive on both POSIX and Windows, unlike rename over an empty
    # directory. A competing installer or user file causes FileExistsError.
    try:
        target.mkdir(mode=0o755)
    except FileExistsError as exc:
        raise InstallError("Каталог установки появился после просмотра; перезапись запрещена") from exc
    try:
        identity = target.stat()
        for name in sorted(records):
            source = staged / name
            path = target / name
            _need(not any(_is_link(parent) for parent in (target, *target.parents))
                  and target.resolve() == target
                  and (target.stat().st_dev, target.stat().st_ino) == (identity.st_dev, identity.st_ino),
                  "Физический каталог установки изменился; частичная копия сохранена")
            path.parent.mkdir(parents=True, exist_ok=True)
            _need(not any(_is_link(parent) for parent in (path, *path.parents))
                  and path.resolve().is_relative_to(target),
                  "Ссылка или смена каталога во время установки")
            with source.open("rb") as reader, path.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=65536)
            path.chmod(records[name]["mode"])
        installed, installed_digest = _scan_package(target)
        _need(installed == before and installed_digest == digest,
              "Установленный пакет не прошёл повторную проверку")
        done = dict(receipt, status="installed", installedDigest=installed_digest,
                    requiresConfirmation=False)
        # Retain an audit receipt beside the candidate, never in the package.
        _write_json(candidate / "installed.json", done)
        return done
    except BaseException:
        # Preserve a partial exclusive destination for inspection: deleting it
        # could erase a concurrent user's files. A fresh install can choose a
        # different destination after resolving this explicit failure.
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    first = commands.add_parser("preview", help="stage and show a published release")
    first.add_argument("--runtime", choices=tuple(_RUNTIME_DIRS))
    first.add_argument("--scope", choices=("project", "global"))
    first.add_argument("--project-root")
    first.add_argument("--destination")
    first.add_argument("--tag", help="explicit published tag; otherwise choose the newest stable release")
    first.add_argument("--cache-dir")
    first.add_argument("--timeout", type=float, default=30)
    second = commands.add_parser("apply", help="install the reviewed candidate")
    second.add_argument("--candidate", required=True)
    second.add_argument("--cache-dir")
    second.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            value = preview(runtime=args.runtime, scope=args.scope, project_root=args.project_root,
                            destination=args.destination, tag=args.tag, cache_dir=args.cache_dir,
                            timeout=args.timeout)
        else:
            value = apply(args.candidate, confirmed=args.confirm, cache_dir=args.cache_dir)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
