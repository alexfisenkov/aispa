#!/usr/bin/env python3
"""Build aispa.zip and aispa-update.json from a Git commit, without publishing.

Usage: python3 tools/package_update.py --output /tmp/aispa-release [--commit HEAD]
Only committed skills/aispa files and the root LICENSE are included. Fixed ZIP
metadata and canonical JSON make the output reproducible for the same commit.
The build refuses a commit without a file of REQUIRED or whose SKILL.md
metadata.version differs from VERSION.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core.store import Invalid
from aispa_core.portable import is_link
from aispa_core.updates import (MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_FILES,
                                MAX_PACKAGE_BYTES, REPOSITORY, _manifest,
                                _safe_path, _version_text)

# Without these the old updater (SKILL.md, LICENSE, tools/aispa.py, tools/aispa_core/__init__.py)
# or the skill itself does not work.
REQUIRED = (
    "SKILL.md", "LICENSE", "tools/aispa.py", "tools/aispa_core/__init__.py",
    "tools/ap.py", "phases/dashboard-template.html", "prompts/executor.md", "prompts/review.md",
    "agents/claude-code/aispa-judge.md", "agents/claude-code/aispa-mechanic-scripted.md",
    "agents/claude-code/aispa-mechanic.md", "agents/claude-code/aispa-researcher.md",
    "agents/claude-code/aispa-sensitive-worker.md", "agents/claude-code/aispa-worker.md",
    "agents/openai.yaml",
)
_SKILL_VERSION = re.compile(r'^metadata:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+version:[ \t]*["\']?([^"\'\s]+)["\']?[ \t]*$', re.M)


def _skill_version(raw):
    """metadata.version from the SKILL.md front matter, or None."""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    match = re.match(r"---\n(.*?\n)---\n", text, re.S)
    found = _SKILL_VERSION.search(match.group(1)) if match else None
    return found.group(1) if found else None


def build_release(repository, output, *, commit="HEAD"):
    repository, output = Path(repository).resolve(), Path(output).resolve()

    def git(*args):
        result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, timeout=30)
        if result.returncode:
            raise Invalid("Не удалось прочитать commit Git для сборки выпуска")
        return result.stdout

    source_commit = git("rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    tree = git("ls-tree", "-rz", source_commit, "--", "skills/aispa/", "LICENSE", "tools/install.py")
    listed = {path.decode("utf-8", errors="replace").removeprefix("skills/aispa/")
              for path in (line.split(b"\t", 1)[-1] for line in tree.split(b"\0") if line)}
    missing = [name for name in REQUIRED if name not in listed]
    if missing:
        raise Invalid("В выбранном commit нет обязательных файлов пакета AISpa: " + ", ".join(missing))
    content = {}
    installer = None
    total = 0
    for line in tree.split(b"\0"):
        if not line:
            continue
        metadata, raw_path = line.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = raw_path.decode("utf-8")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise Invalid("Выпуск не может содержать Git-модули или ссылки")
        name = "LICENSE" if path == "LICENSE" else path.removeprefix("skills/aispa/")
        _safe_path(name)
        data = git("cat-file", "blob", oid.decode())
        if path == "tools/install.py":
            installer = data
            continue
        if name in content:
            if name == "LICENSE" and content[name] == (data, 0o644) and mode == b"100644":
                continue  # The installable package carries the same root notice.
            raise Invalid("Повтор или несовпадающая копия файла в выпуске: " + name)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES or len(content) >= MAX_FILES:
            raise Invalid("Превышен допустимый размер выпуска")
        content[name] = (data, 0o755 if mode == b"100755" else 0o644)
    version = _version_text(content["tools/aispa_core/__init__.py"][0])
    skill_version = _skill_version(content["SKILL.md"][0])
    if skill_version != version:
        raise Invalid("Версия в SKILL.md (metadata.version: %s) не равна VERSION пакета (%s)"
                      % (skill_version or "нет", version))
    buffer = io.BytesIO()
    files = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (data, mode) in sorted(content.items()):
            info = zipfile.ZipInfo("aispa/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
            files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode})
    data = buffer.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise Invalid("Архив превышает допустимый размер")
    manifest = {"schema": 1, "repository": REPOSITORY, "version": version, "commit": source_commit,
                "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(data).hexdigest()}, "files": files}
    _manifest(manifest, {"version": version})
    output.mkdir(parents=True, exist_ok=True)
    archive_path, manifest_path = output / "aispa.zip", output / "aispa-update.json"
    installer_path = output / "install.py"
    if (archive_path.exists() or is_link(archive_path) or manifest_path.exists() or is_link(manifest_path)
            or installer is not None and (installer_path.exists() or is_link(installer_path))):
        raise Invalid("Выходные файлы уже существуют; выберите свободный каталог")
    with archive_path.open("xb") as stream:
        stream.write(data)
    try:
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        if installer is not None:
            with installer_path.open("xb") as stream:
                stream.write(installer)
    except BaseException:
        archive_path.unlink()
        raise
    return {"version": version, "commit": source_commit, "archive": str(archive_path),
            "manifest": str(manifest_path), "archiveSha256": manifest["archive"]["sha256"],
            "installer": str(installer_path) if installer is not None else None,
            "installerSha256": hashlib.sha256(installer).hexdigest() if installer is not None else None,
            "fileCount": len(files), "published": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", default="HEAD")
    args = parser.parse_args()
    try:
        print(json.dumps(build_release(ROOT, args.output, commit=args.commit), ensure_ascii=False, indent=2))
    except (Invalid, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
