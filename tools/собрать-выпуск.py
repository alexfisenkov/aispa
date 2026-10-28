#!/usr/bin/env python3
"""Собирает файлы выпуска AISpa из коммита Git — ничего не публикует.

    python3 tools/собрать-выпуск.py              # из HEAD в dist/
    python3 tools/собрать-выпуск.py --commit v1.0.1 --output /tmp/выпуск

В dist/ появляются три файла, их прикладывают к GitHub Release:
  aispa.zip          — пакет skills/aispa/ (+ LICENSE из корня) с корнем aispa/;
  aispa-update.json  — манифест: версия, commit, sha256 архива и каждого файла;
  install.py         — первый установщик (tools/install.py) для ссылки
                       releases/latest/download/install.py.
По ним обновляются копии, поставленные из выпуска, — все AISpa 0.x и всё, что
поставил их обновитель. Берутся только файлы коммита, не рабочей папки: один
коммит — одни и те же байты. После записи сборка проверяет себя тем же
кодом, которым проверяет обновитель: распаковывает архив по манифесту и сверяет.
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
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import updates  # noqa: E402
from aispa_core.store import Invalid  # noqa: E402

PATHS = ("skills/aispa/", "LICENSE", "tools/install.py")
ASSETS = ("aispa.zip", "aispa-update.json", "install.py")
_SKILL_VERSION = re.compile(r"^\s*version:\s*[\"']?([0-9A-Za-z.+-]+)", re.M)


def git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=30)
    if r.returncode:
        raise Invalid("git %s: %s" % (" ".join(args[:2]), r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout


def committed(commit):
    """{имя в пакете: (байты, права)} и байты установщика — из объектов Git."""
    content, installer = {}, None
    for line in git("ls-tree", "-rz", commit, "--", *PATHS).split(b"\0"):
        if not line:
            continue
        meta, raw = line.split(b"\t", 1)
        mode, kind, oid = meta.split()
        path = raw.decode("utf-8")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise Invalid("в выпуске не бывает ссылок и подмодулей: " + path)
        data = git("cat-file", "blob", oid.decode())
        if path == "tools/install.py":
            installer = data
            continue
        name = "LICENSE" if path == "LICENSE" else path[len("skills/aispa/"):]
        if name in content:
            raise Invalid("файл дважды: " + name)
        content[name] = (data, 0o755 if mode == b"100755" else 0o644)
    if installer is None:
        raise Invalid("в коммите нет tools/install.py")
    return content, installer


def skill_version(text):
    head = text.split("\n---", 1)[0] if text.startswith("---") else ""
    m = _SKILL_VERSION.search(head)
    return m.group(1) if m else None


def build(commit):
    sha = git("rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    content, installer = committed(sha)
    for required in ("SKILL.md", "tools/aispa.py", "tools/aispa_core/__init__.py", "LICENSE"):
        if required not in content:
            raise Invalid("в пакете нет обязательного файла " + required)
    version = updates._version_text(content["tools/aispa_core/__init__.py"][0])
    in_skill = skill_version(content["SKILL.md"][0].decode("utf-8"))
    if in_skill != version:
        raise Invalid("версии разошлись: SKILL.md %s, aispa_core %s" % (in_skill, version))
    buffer, files = io.BytesIO(), []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (data, mode) in sorted(content.items()):
            updates._safe_path(name)
            info = zipfile.ZipInfo("aispa/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
            files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                          "size": len(data), "mode": mode})
    data = buffer.getvalue()
    if len(data) > updates.MAX_ARCHIVE_BYTES:
        raise Invalid("архив больше допустимого")
    manifest = {"schema": 1, "repository": updates.REPOSITORY, "version": version, "commit": sha,
                "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(data).hexdigest()},
                "files": files}
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return {"aispa.zip": data, "aispa-update.json": raw, "install.py": installer}, manifest


def verify(output, version, sha):
    """Те же проверки, что у обновителя: манифест, архив, распаковка, версия, состав."""
    manifest = updates._json((output / "aispa-update.json").read_bytes())
    release = {"version": version}
    records = updates._manifest(manifest, release)
    data = (output / "aispa.zip").read_bytes()
    if manifest["commit"] != sha or hashlib.sha256(data).hexdigest() != manifest["archive"]["sha256"]:
        raise Invalid("манифест не совпал с архивом или коммитом")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if any(item.is_dir() for item in archive.infolist()):
            raise Invalid("в архиве есть записи-каталоги")
    with tempfile.TemporaryDirectory(prefix="aispa-verify-") as tmp:
        staged = Path(tmp) / "package"
        staged.mkdir()
        updates._extract(data, staged, records)
        if updates._version(staged) != version:
            raise Invalid("версия распакованного пакета не та")
        if updates._snapshot(staged)[0] != updates._disk_records(records):
            raise Invalid("распакованный пакет не совпал с манифестом")
    return len(records)


def dirty():
    return git("status", "--porcelain", "--", *PATHS).decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        assets, manifest = build(args.commit)
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        for name in ASSETS:
            tmp = output / ("." + name + ".tmp")
            tmp.write_bytes(assets[name])
            tmp.replace(output / name)
        count = verify(output, manifest["version"], manifest["commit"])
    except (Invalid, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, "сборка выпуска: %s\n" % exc)
    tag = "v" + manifest["version"]
    report = {"version": manifest["version"], "tag": tag, "commit": manifest["commit"],
              "files": count, "output": str(output), "published": False,
              "sha256": {name: hashlib.sha256(assets[name]).hexdigest() for name in ASSETS}}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if dirty():
        print("! в рабочей папке есть незакоммиченные правки — в выпуск они не попали", file=sys.stderr)


if __name__ == "__main__":
    main()
