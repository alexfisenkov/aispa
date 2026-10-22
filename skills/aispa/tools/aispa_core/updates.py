"""Opt-in, release-only updates for installed AISpa packages.

The fixed GitHub repository is the publisher trust root. HTTPS and checksums
bind assets to the published manifest; this is not an independent signature.
check() never writes. prepare() stages a review without altering the install.
apply()/rollback() require an explicit boolean confirmation and a digest CAS.
"""
from contextlib import contextmanager
import ast
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
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
import zipfile

from .store import Invalid, atomic_json
from .portable import executable, is_link, relative_name

REPOSITORY = "alexfisenkov/aispa"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_FILES = 2048
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_HASH = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _semver(value):
    if not isinstance(value, str) or not (match := _SEMVER.fullmatch(value)):
        raise Invalid("Некорректная версия SemVer")
    major, minor, patch, pre, _build = match.groups()
    # Numeric prerelease identifiers sort below textual identifiers. A stable
    # version sorts above every prerelease with the same major/minor/patch.
    suffix = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return int(major), int(minor), int(patch), not bool(pre), suffix


def _version_text(raw):
    try:
        tree = ast.parse(raw)
        for item in tree.body:
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in item.targets):
                value = ast.literal_eval(item.value)
                _semver(value)
                return value
    except (SyntaxError, ValueError, TypeError, UnicodeError) as exc:
        raise Invalid("Не удалось прочитать версию пакета") from exc
    raise Invalid("В пакете отсутствует VERSION")


def _version(package):
    try:
        return _version_text((package / "tools/aispa_core/__init__.py").read_text(encoding="utf-8"))
    except OSError as exc:
        raise Invalid("Не найден установленный пакет AISpa") from exc


def _canonical(package):
    path = Path(package).absolute()
    target = path.resolve(strict=True)
    if not target.is_dir():
        raise Invalid("Пакет AISpa должен быть каталогом")
    return path, target


def _runtime_path(path):
    parts = tuple(part.casefold() for part in path.parts)
    return any(parts[i:i + 2] == (".aispa", "runs") and "runtime" in parts[i + 3:]
               for i in range(len(parts) - 3))


def _install_policy(alias, target):
    if _runtime_path(alias) or _runtime_path(target):
        return "pinned_runtime"
    if os.name == "nt" and Path.cwd().resolve().is_relative_to(target):
        return "cwd_inside_package"
    # A package accidentally containing a project/run must never be replaced.
    if (target / ".aispa").exists():
        return "contains_runs"
    try:
        result = subprocess.run(["git", "-C", str(target), "ls-files", "-z", "--", "."],
                                capture_output=True, timeout=3)
        if result.returncode == 0 and result.stdout:
            return "git_checkout"
        if result.returncode != 0 and any((p / ".git").exists() or is_link(p / ".git") for p in (target, *target.parents)):
            return "git_check_failed"
    except FileNotFoundError:
        # Protect obvious checkouts even when Git itself is unavailable.
        if any((p / ".git").exists() for p in (target, *target.parents)):
            return "git_unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "git_check_failed"
    return None


def _writable_target(package):
    alias, target = _canonical(package)
    reason = _install_policy(alias, target)
    if reason:
        raise Invalid("Обновление этого каталога запрещено: " + reason)
    _version(target)
    return target


def _safe_path(name):
    try:
        canonical = relative_name(name)
    except ValueError as exc:
        raise Invalid("Небезопасный путь в пакете") from exc
    if len(name) > 512 or canonical != name:
        raise Invalid("Небезопасный путь в пакете")
    path = PurePosixPath(name)
    if any(p.casefold() in (".git", ".aispa", "__pycache__") for p in path.parts):
        raise Invalid("Служебный каталог запрещён в выпуске")
    return name


def _ignored(parts):
    return "__pycache__" in parts or parts[-1] == ".DS_Store" or parts[-1].endswith((".pyc", ".pyo"))


def _walk_error(error):
    raise Invalid("Не удалось прочитать полный состав пакета") from error


def _snapshot(package):
    if is_link(package):
        raise Invalid("Ссылка вместо каталога пакета запрещена")
    records = {}
    total = 0
    for root, dirs, files in os.walk(package, followlinks=False, onerror=_walk_error):
        for name in dirs + files:
            path = Path(root) / name
            if is_link(path):
                raise Invalid("Внутри пакета обнаружена символическая ссылка")
        dirs[:] = sorted(d for d in dirs if not _ignored((d,)))
        for name in sorted(files):
            path = Path(root) / name
            rel = path.relative_to(package)
            if _ignored(rel.parts):
                continue
            _safe_path(rel.as_posix())
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise Invalid("Внутри пакета допустимы только обычные файлы")
            if info.st_size > MAX_FILE_BYTES:
                raise Invalid("Превышен размер файла пакета")
            total += info.st_size
            if len(records) >= MAX_FILES or total > MAX_PACKAGE_BYTES:
                raise Invalid("Превышен размер пакета")
            data = path.read_bytes()
            if len(data) != info.st_size:
                raise Invalid("Пакет изменился во время чтения")
            records[rel.as_posix()] = {"sha256": hashlib.sha256(data).hexdigest(),
                                     "size": len(data), "mode": 0o755 if executable(path) else 0o644}
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return records, hashlib.sha256(encoded).hexdigest()


def _disk_records(records):
    """NTFS cannot preserve POSIX execute bits; hashes and sizes remain exact.

    Archive metadata is checked before extraction on every platform. Only the
    comparison to the installed filesystem normalizes that unavailable bit.
    """
    if os.name != "nt":
        return records
    return {name: dict(record, mode=record["mode"] & ~0o111)
            for name, record in records.items()}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _trusted_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _trusted_url(url, *, redirect=False):
    if not isinstance(url, str):
        raise Invalid("Отсутствует URL ресурса GitHub")
    value = urllib.parse.urlsplit(url)
    if value.scheme != "https" or value.username or value.password or value.port not in (None, 443) or value.fragment:
        raise Invalid("Недоверенный URL ресурса")
    host = value.hostname
    if host == "api.github.com" and value.path.startswith(f"/repos/{REPOSITORY}/"):
        return
    if host == "github.com" and value.path.startswith(f"/{REPOSITORY}/releases/"):
        return
    if redirect and host in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"):
        return
    raise Invalid("Ресурс находится вне доверенного GitHub-репозитория")


def _https_context():
    """Keep verified TLS, using an OS bundle only for a Python without CA paths.

    Explicit CA environment settings and Windows' native trust loading are
    authoritative. The fallback belongs to this opener, never the process env.
    """
    context = ssl.create_default_context()
    if sys.platform == "win32" or "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ:
        return context
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile or defaults.capath:
        return context
    for name in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                 "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(name).is_file():
            context.load_verify_locations(cafile=name)
            break
    return context


def _download(url, limit, deadline):
    _trusted_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Истекло время проверки обновления")
    headers = {"User-Agent": "AISpa-updater", "Accept": "application/vnd.github+json"}
    if urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Cache-Control"] = "no-cache"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_SafeRedirect(), urllib.request.HTTPSHandler(context=_https_context()))
    with opener.open(request, timeout=min(remaining, 5.0)) as response:
        _trusted_url(response.geturl(), redirect=True)
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            raise Invalid("Ресурс превышает допустимый размер")
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Истекло время загрузки обновления")
            # read1 avoids waiting for an attacker to fill a large buffered read.
            reader = getattr(response, "read1", response.read)
            block = reader(min(65536, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > limit:
                raise Invalid("Ресурс превышает допустимый размер")
        return bytes(data)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Invalid("Повтор ключа в JSON")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise Invalid("Некорректные метаданные обновления") from exc


def _deadline(timeout):
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise Invalid("Тайм-аут обновления должен быть от 0 до 60 секунд")
    return time.monotonic() + timeout


def _release(releases, current):
    if not isinstance(releases, list):
        raise Invalid("GitHub вернул неожиданный список выпусков")
    eligible = []
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") is not False or not item.get("published_at"):
            continue
        tag = item.get("tag_name", "")
        if not isinstance(tag, str):
            continue
        version = tag[1:] if tag.startswith("v") else tag
        try:
            order = _semver(version)
        except Invalid:
            continue
        if current[3] and (not order[3] or item.get("prerelease") is not False):
            continue
        eligible.append((order, version, tag, item))
    if not eligible:
        return None
    _order, version, tag, item = max(eligible, key=lambda row: row[0])
    url = item.get("html_url")
    if url != f"https://github.com/{REPOSITORY}/releases/tag/{urllib.parse.quote(tag, safe='')}":
        raise Invalid("Некорректный адрес опубликованного выпуска")
    assets = item.get("assets", [])
    if not isinstance(assets, list):
        raise Invalid("Некорректный список ресурсов выпуска")
    selected = {}
    for name in ("aispa.zip", "aispa-update.json"):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matches) != 1:
            raise Invalid("В опубликованном выпуске нужен один ресурс " + name)
        asset = matches[0]
        expected = f"https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(tag, safe='')}/{name}"
        if asset.get("browser_download_url") != expected or asset.get("state") != "uploaded":
            raise Invalid("Некорректный ресурс опубликованного выпуска")
        selected[name] = expected
    return {"version": version, "tag": tag, "url": url, "publishedAt": item["published_at"],
            "assets": selected}


def _published_releases(deadline):
    releases = []

    def page_rows(url):
        batch = _json(_download(url, MAX_METADATA_BYTES, deadline))
        if not isinstance(batch, list) or len(batch) > 100:
            raise Invalid("GitHub вернул неожиданный список выпусков")
        return batch

    # Keep the bounded SemVer scan, including prereleases. An empty first page
    # is retried once through explicit pagination; neither response alone proves
    # that a published stable release is absent.
    for page in range(1, 6):
        url = RELEASES_URL if page == 1 else RELEASES_URL + f"&page={page}"
        batch = page_rows(url)
        if page == 1 and not batch:
            batch = page_rows(RELEASES_URL + "&page=1")
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise Invalid("Список выпусков превышает предел безопасной проверки")

    # GitHub's latest is a separate published-release endpoint, not a Git tag
    # or a branch. Reconcile even nonempty lists, which can be stale as well.
    try:
        latest = _json(_download(LATEST_RELEASE_URL, MAX_METADATA_BYTES, deadline))
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code != 404:
            raise
        return releases  # A repository containing only prereleases has no latest.
    if (not isinstance(latest, dict) or latest.get("draft") is not False
            or latest.get("prerelease") is not False or not latest.get("published_at")):
        raise Invalid("GitHub вернул некорректный последний выпуск")
    tag = latest.get("tag_name")
    if not isinstance(tag, str) or not _semver(tag.removeprefix("v"))[3]:
        raise Invalid("Последний стабильный выпуск не имеет стабильной версии")
    expected = f"https://github.com/{REPOSITORY}/releases/tag/{urllib.parse.quote(tag, safe='')}"
    if latest.get("html_url") != expected:
        raise Invalid("Некорректный адрес последнего выпуска")
    # Prefer the direct record for this tag (assets may have just been uploaded).
    # _release still validates the selected assets and chooses maximum SemVer.
    return [row for row in releases if not isinstance(row, dict) or row.get("tag_name") != tag] + [latest]


def check(package, *, timeout=5):
    """Read-only check. Offline/invalid/no-release results never block startup."""
    alias, target = _canonical(package)
    version = _version(target)
    reason = _install_policy(alias, target)
    result = {"status": "no_release", "repository": REPOSITORY, "installedVersion": version,
              "channel": "stable" if _semver(version)[3] else "prerelease",
              "installAllowed": reason is None, "installReason": reason}
    try:
        release = _release(_published_releases(_deadline(timeout)), _semver(version))
        if release:
            result.update(status="available" if _semver(release["version"]) > _semver(version) else "current", release=release)
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        result.update(status="offline", message="Проверка недоступна; продолжайте с установленной версией.")
    except (Invalid, ValueError, TypeError, KeyError, RecursionError):
        result.update(status="invalid_release", message="Метаданные выпуска не прошли проверку; текущая версия сохранена.")
    return result


def _manifest(value, release):
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1 or value.get("repository") != REPOSITORY:
        raise Invalid("Неподдерживаемый манифест обновления")
    if value.get("version") != release["version"] or not isinstance(value.get("commit"), str) or not _COMMIT.fullmatch(value["commit"]):
        raise Invalid("Версия или commit манифеста не соответствует выпуску")
    _semver(value["version"])
    archive = value.get("archive")
    if not isinstance(archive, dict) or archive.get("name") != "aispa.zip" or not isinstance(archive.get("sha256"), str) or not _HASH.fullmatch(archive["sha256"]):
        raise Invalid("Некорректная контрольная сумма архива")
    files = value.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise Invalid("Некорректный список файлов пакета")
    records, folded = {}, set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise Invalid("Некорректная запись файла")
        path = _safe_path(entry.get("path"))
        key = path.casefold()
        if key in folded or any(key.startswith(p + "/") or p.startswith(key + "/") for p in folded):
            raise Invalid("Повторяющиеся или конфликтующие пути файлов")
        folded.add(key)
        if type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES:
            raise Invalid("Некорректный размер файла")
        if not isinstance(entry.get("sha256"), str) or not _HASH.fullmatch(entry["sha256"]) or entry.get("mode") not in (0o644, 0o755):
            raise Invalid("Некорректная контрольная сумма или права файла")
        total += entry["size"]
        if total > MAX_PACKAGE_BYTES:
            raise Invalid("Превышен размер распакованного пакета")
        records[path] = {key: entry[key] for key in ("sha256", "size", "mode")}
    for required in ("SKILL.md", "tools/aispa.py", "tools/aispa_core/__init__.py", "LICENSE"):
        if required not in records:
            raise Invalid("В выпуске отсутствует обязательный файл " + required)
    return records


def _extract(data, destination, records):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != len(records):
                raise Invalid("Состав архива не соответствует манифесту")
            seen = set()
            for item in entries:
                if item.orig_filename != item.filename or not item.filename.startswith("aispa/"):
                    raise Invalid("Неверный корень архива")
                name = _safe_path(item.filename[len("aispa/"):])
                if name in seen or name not in records or item.is_dir() or item.flag_bits & 1:
                    raise Invalid("Лишний, повторный или зашифрованный файл в архиве")
                seen.add(name)
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise Invalid("Допустимы только ZIP_STORED и ZIP_DEFLATED")
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise Invalid("Ссылки и специальные файлы запрещены в архиве")
                expected = records[name]
                if item.file_size != expected["size"]:
                    raise Invalid("Размер файла не соответствует манифесту")
                if mode & 0o7777 != expected["mode"]:
                    raise Invalid("Права файла не соответствуют манифесту")
                # Bounded streaming read also limits memory when a hostile ZIP
                # header understates the true DEFLATE output size.
                with archive.open(item) as source:
                    content = source.read(expected["size"] + 1)
                if len(content) != expected["size"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                    raise Invalid("Контрольная сумма файла не совпала")
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(content)
                path.chmod(expected["mode"])
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise Invalid("Повреждённый или неподдерживаемый архив") from exc


def _diff(before, after):
    return {"added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "changed": sorted(p for p in before.keys() & after.keys() if before[p] != after[p]),
            "unchangedCount": sum(before[p] == after[p] for p in before.keys() & after.keys())}


def _source_commit(tag, deadline):
    """Resolve the published tag, including annotated tags, to its Git commit."""
    url = f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    ref = _json(_download(url, MAX_METADATA_BYTES, deadline))
    if not isinstance(ref, dict) or ref.get("ref") != "refs/tags/" + tag:
        raise Invalid("Не удалось подтвердить Git tag выпуска")
    obj = ref.get("object")
    for _ in range(5):
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str) or not _COMMIT.fullmatch(obj["sha"]):
            raise Invalid("Некорректный объект Git tag")
        if obj.get("type") == "commit":
            return obj["sha"]
        if obj.get("type") != "tag":
            raise Invalid("Git tag выпуска не указывает на commit")
        tag_object = _json(_download(f"https://api.github.com/repos/{REPOSITORY}/git/tags/{obj['sha']}", MAX_METADATA_BYTES, deadline))
        if not isinstance(tag_object, dict):
            raise Invalid("Некорректный annotated Git tag")
        obj = tag_object.get("object")
    raise Invalid("Слишком длинная цепочка annotated Git tags")


def prepare(package, *, timeout=20):
    """Stage a validated release beside the install; return a review + local ID."""
    target = _writable_target(package)
    deadline = _deadline(timeout)
    before, old_digest = _snapshot(target)
    version = _version(target)
    release = _release(_published_releases(deadline), _semver(version))
    if release is None or _semver(release["version"]) <= _semver(version):
        raise Invalid("Нового опубликованного выпуска нет")
    manifest = _json(_download(release["assets"]["aispa-update.json"], MAX_METADATA_BYTES, deadline))
    records = _manifest(manifest, release)
    if _source_commit(release["tag"], deadline) != manifest["commit"]:
        raise Invalid("Commit манифеста не соответствует опубликованному Git tag")
    data = _download(release["assets"]["aispa.zip"], MAX_ARCHIVE_BYTES, deadline)
    if hashlib.sha256(data).hexdigest() != manifest["archive"]["sha256"]:
        raise Invalid("Контрольная сумма архива не совпала")
    candidate = Path(tempfile.mkdtemp(prefix=".aispa-update-", dir=target.parent))
    try:
        staged = candidate / "package"
        staged.mkdir()
        _extract(data, staged, records)
        if _version(staged) != release["version"]:
            raise Invalid("Версия кода не соответствует манифесту")
        actual, staged_digest = _snapshot(staged)
        if actual != _disk_records(records) or _snapshot(target)[1] != old_digest:
            raise Invalid("Пакет изменился во время подготовки")
        result = {"schema": 1, "status": "prepared", "candidateId": candidate.name,
                  "target": str(target), "installedVersion": version, "version": release["version"],
                  "commit": manifest["commit"], "releaseUrl": release["url"],
                  "archiveSha256": manifest["archive"]["sha256"], "expectedCurrentDigest": old_digest,
                  "candidateDigest": staged_digest, "diff": _diff(before, actual),
                  "requiresConfirmation": True}
        atomic_json(candidate / "manifest.json", manifest)
        atomic_json(candidate / "candidate.json", result)
        return result
    except BaseException:
        shutil.rmtree(candidate)
        raise


def _local_record(target, ident, prefix, filename):
    if not isinstance(ident, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{8,64}", ident):
        raise Invalid("Некорректный идентификатор локального обновления")
    directory = target.parent / ident
    if is_link(directory) or not directory.is_dir() or directory.resolve().parent != target.parent:
        raise Invalid("Недоступен локальный каталог обновления")
    path = directory / filename
    if is_link(path) or not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        raise Invalid("Некорректная локальная запись обновления")
    value = _json(path.read_bytes())
    if not isinstance(value, dict) or value.get("target") != str(target) or value.get("schema") != 1:
        raise Invalid("Обновление подготовлено для другого пакета")
    return directory, value


@contextmanager
def _lock(target):
    path = target.parent / (".aispa-update-lock-" + hashlib.sha256(str(target).encode()).hexdigest()[:20])
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Invalid("Пакет уже обновляется; проверьте процесс перед удалением блокировки") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def _confirmed(confirmed):
    if confirmed is not True:
        raise Invalid("Нужно явное подтверждение пользователя; можно продолжить с текущей версией")


def _check_stage(path):
    if is_link(path) or not path.is_dir():
        raise Invalid("Некорректный локальный пакет обновления")


def apply(package, candidate_id, *, confirmed=False):
    """Install a previously reviewed candidate with CAS, retained backup + undo."""
    _confirmed(confirmed)
    target = _writable_target(package)
    with _lock(target):
        candidate, review = _local_record(target, candidate_id, ".aispa-update-", "candidate.json")
        if review.get("status") != "prepared":
            raise Invalid("Этот кандидат уже применён или недоступен")
        staged = candidate / "package"
        _check_stage(staged)
        manifest_path = candidate / "manifest.json"
        if is_link(manifest_path) or manifest_path.stat().st_size > MAX_METADATA_BYTES:
            raise Invalid("Некорректный локальный манифест")
        manifest = _json(manifest_path.read_bytes())
        expected = _manifest(manifest, review)
        actual, digest = _snapshot(staged)
        if actual != _disk_records(expected) or digest != review.get("candidateDigest") or _version(staged) != review["version"]:
            raise Invalid("Подготовленный пакет изменён после проверки")
        if _snapshot(target)[1] != review.get("expectedCurrentDigest"):
            raise Invalid("Установленный пакет изменён; подготовьте обновление заново")
        if _semver(review["version"]) <= _semver(_version(target)):
            raise Invalid("Кандидат не новее установленной версии")
        backup = Path(tempfile.mkdtemp(prefix=".aispa-backup-", dir=target.parent))
        receipt = {"schema": 1, "status": "installing", "backupId": backup.name,
                   "target": str(target), "version": review["version"],
                   "previousVersion": review["installedVersion"], "commit": review["commit"],
                   "expectedCurrentDigest": digest, "backupDigest": review["expectedCurrentDigest"]}
        atomic_json(backup / "receipt.json", receipt)
        old_moved = new_moved = False
        try:
            os.replace(target, backup / "package")
            old_moved = True
            # Catch a final external edit between the first CAS and the rename.
            if _snapshot(backup / "package")[1] != receipt["backupDigest"]:
                raise Invalid("Установленный пакет изменён при применении")
            os.replace(staged, target)
            new_moved = True
            if _snapshot(target)[1] != digest:
                raise Invalid("Установленный пакет не прошёл проверку")
            receipt["status"] = "installed"
            atomic_json(backup / "receipt.json", receipt)
        except BaseException:
            if new_moved:
                os.replace(target, staged)
            if old_moved:
                os.replace(backup / "package", target)
            raise
        review["status"] = "applied"
        # The receipt is authoritative; stale review status cannot replay apply
        # because the staged package no longer exists and CAS would fail.
        try:
            atomic_json(candidate / "candidate.json", review)
        except OSError:
            pass
        return receipt


def rollback(package, backup_id, *, confirmed=False):
    """Restore an unchanged prior install; retain the displaced newer package."""
    _confirmed(confirmed)
    target = _writable_target(package)
    with _lock(target):
        backup, receipt = _local_record(target, backup_id, ".aispa-backup-", "receipt.json")
        if receipt.get("status") != "installed":
            raise Invalid("Эта резервная копия уже восстановлена или не завершена")
        prior = backup / "package"
        _check_stage(prior)
        if _snapshot(prior)[1] != receipt.get("backupDigest"):
            raise Invalid("Резервная копия изменена")
        if _snapshot(target)[1] != receipt.get("expectedCurrentDigest"):
            raise Invalid("Текущий пакет изменён после обновления; автоматический откат запрещён")
        displaced = backup / "replaced"
        if displaced.exists() or is_link(displaced):
            raise Invalid("Каталог сохранения текущей версии уже занят")
        old_moved = new_moved = False
        try:
            os.replace(target, displaced)
            old_moved = True
            if _snapshot(displaced)[1] != receipt["expectedCurrentDigest"]:
                raise Invalid("Текущий пакет изменён при откате")
            os.replace(prior, target)
            new_moved = True
            if _snapshot(target)[1] != receipt["backupDigest"]:
                raise Invalid("Восстановленный пакет не прошёл проверку")
            receipt["status"] = "rolled_back"
            atomic_json(backup / "receipt.json", receipt)
        except BaseException:
            if new_moved:
                os.replace(target, prior)
            if old_moved:
                os.replace(displaced, target)
            raise
        return receipt
