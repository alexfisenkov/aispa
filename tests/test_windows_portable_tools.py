import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import carryover, discovery, portable, project_context, updates
from aispa_core.store import Invalid


class PortableToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa portable space-🙂-")
        self.root = Path(self.temp.name).resolve()
        self.junctions = []

    def tearDown(self):
        for link in reversed(self.junctions):
            if portable.is_link(link):
                link.rmdir()
        self.temp.cleanup()

    def test_source_paths_reject_windows_aliases_on_every_platform(self):
        guards = (project_context._path, discovery._safe_path, carryover._safe)
        invalid = ("C:source.md", "C:/source.md", "/source.md", "\\source.md",
                   "//server/share/source.md", "docs/source.md:stream", "NUL", "AUX.md",
                   "docs/COM1.txt", "docs/LPT¹.md", "docs/README.md.", "docs /README.md",
                   "docs/a?.md", "docs/a|b.md", "docs/a\x01.md", "../outside", "", None)
        for guard in guards:
            for name in invalid:
                with self.subTest(guard=guard.__module__, name=name), self.assertRaises(Invalid):
                    guard(self.root, name)
            self.assertEqual(guard(self.root, "docs/Русский 🙂.md"), self.root / "docs/Русский 🙂.md")

    def test_context_accepts_native_relative_path_objects(self):
        self.assertEqual(project_context._path(self.root, Path(".aispa") / "runs" / "example"),
                         self.root / ".aispa/runs/example")

    def test_release_paths_require_portable_canonical_spelling(self):
        for name in ("C:source", "docs/NUL.md", "docs/name.", "docs/name ", "docs/a:b",
                     "CON", "a//b", "a/./b", "./a", "a/", "", None):
            with self.subTest(name=name), self.assertRaises(Invalid):
                updates._safe_path(name)
        self.assertEqual(updates._safe_path("docs/Русский 🙂.md"), "docs/Русский 🙂.md")

    def test_disk_record_normalization_preserves_content_and_does_not_mutate(self):
        records = {"script": {"sha256": "a" * 64, "size": 42, "mode": 0o755}}
        actual = updates._disk_records(records)
        self.assertEqual(records["script"]["mode"], 0o755)
        self.assertEqual(actual["script"]["sha256"], "a" * 64)
        self.assertEqual(actual["script"]["size"], 42)
        self.assertEqual(actual["script"]["mode"], 0o644 if os.name == "nt" else 0o755)

    def archive(self, mode=0o755):
        data = b"print('verified')\n"
        record = {"script.py": {"sha256": hashlib.sha256(data).hexdigest(),
                                "size": len(data), "mode": 0o755}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            entry = zipfile.ZipInfo("aispa/script.py")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(entry, data)
        return buffer.getvalue(), record

    def test_executable_archive_matches_native_snapshot_and_detects_content_change(self):
        data, records = self.archive()
        target = self.root / "stage"
        target.mkdir()
        updates._extract(data, target, records)
        actual, digest = updates._snapshot(target)
        self.assertEqual(actual, updates._disk_records(records))
        (target / "script.py").write_bytes(b"print('tampered')\n")
        self.assertNotEqual(updates._snapshot(target)[1], digest)

    def test_archive_permissions_remain_exact_even_on_windows(self):
        for mode in (0o644, 0o777, 0o4755):
            data, records = self.archive(mode)
            target = self.root / str(mode)
            target.mkdir()
            with self.subTest(mode=mode), self.assertRaisesRegex(Invalid, "Права"):
                updates._extract(data, target, records)

    def install(self, version):
        target = self.root / "install" / "aispa"
        content = {"SKILL.md": b"# AISpa\n", "tools/aispa.py": b"print('aispa')\n",
                   "tools/aispa_core/__init__.py": f'VERSION = "{version}"\n'.encode(),
                   "LICENSE": b"MIT fixture\n"}
        for name, data in content.items():
            file = target / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            file.chmod(0o644)
        return target, content

    def prepared_executable_release(self):
        target, content = self.install("0.1.0-alpha.2")
        version = "0.1.0-alpha.3"
        content["tools/aispa_core/__init__.py"] = f'VERSION = "{version}"\n'.encode()
        buffer, files = io.BytesIO(), []
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in content.items():
                mode = 0o755 if name == "tools/aispa.py" else 0o644
                entry = zipfile.ZipInfo("aispa/" + name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(entry, data)
                files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                              "size": len(data), "mode": mode})
        data = buffer.getvalue()
        manifest = {"schema": 1, "repository": updates.REPOSITORY, "version": version,
                    "commit": "a" * 40, "files": files,
                    "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(data).hexdigest()}}
        tag = "v" + version
        prefix = f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/"
        release = {"draft": False, "prerelease": True, "published_at": "2026-10-19T00:00:00Z",
                   "tag_name": tag, "html_url": f"https://github.com/{updates.REPOSITORY}/releases/tag/{tag}",
                   "assets": [{"name": name, "state": "uploaded", "browser_download_url": prefix + name}
                              for name in ("aispa.zip", "aispa-update.json")]}
        resources = {prefix + "aispa.zip": data,
                     prefix + "aispa-update.json": json.dumps(manifest).encode()}
        with patch.object(updates, "_published_releases", return_value=[release]), \
             patch.object(updates, "_source_commit", return_value="a" * 40), \
             patch.object(updates, "_download", side_effect=lambda url, *_: resources[url]):
            review = updates.prepare(target)
        return target, review

    def test_executable_release_prepares_applies_and_rolls_back(self):
        target, review = self.prepared_executable_release()
        receipt = updates.apply(target, review["candidateId"], confirmed=True)
        self.assertEqual(updates._version(target), "0.1.0-alpha.3")
        self.assertEqual(updates._snapshot(target)[0]["tools/aispa.py"]["mode"],
                         0o644 if os.name == "nt" else 0o755)
        updates.rollback(target, receipt["backupId"], confirmed=True)
        self.assertEqual(updates._version(target), "0.1.0-alpha.2")

    def junction(self, link, target):
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, "mklink /J must work without Developer Mode")
        self.junctions.append(link)
        self.assertTrue(portable.is_link(link))
        return link

    @unittest.skipUnless(os.name == "nt", "native Windows junction test")
    def test_junction_cannot_be_source_or_inventory_tree(self):
        source = self.root / "actual"
        source.mkdir()
        (source / "README.md").write_bytes(b"must not traverse via junction\n")
        link = self.junction(self.root / "junction", source)
        for guard in (project_context._path, discovery._safe_path, carryover._safe):
            with self.subTest(guard=guard.__module__), self.assertRaises(Invalid):
                guard(self.root, "junction/README.md")
        found = project_context.inventory(self.root)
        self.assertNotIn("junction", found["zones"])
        self.assertNotIn("junction/README.md", found["documents"])
        self.assertTrue((link / "README.md").exists(), "fixture junction must be live")

    @unittest.skipUnless(os.name == "nt", "native Windows junction test")
    def test_snapshot_and_stage_reject_junctions_but_install_alias_is_preserved(self):
        target, _ = self.install("0.1.0-alpha.2")
        alias = self.junction(self.root / "install-alias", target)
        self.assertEqual(updates._canonical(alias), (alias, target))
        self.assertEqual(updates._writable_target(alias), target)
        with self.assertRaises(Invalid):
            updates._snapshot(alias)
        with self.assertRaises(Invalid):
            updates._check_stage(alias)
        outside = self.root / "external"
        outside.mkdir()
        (outside / "README.md").write_bytes(b"external\n")
        self.junction(target / "linked", outside)
        with self.assertRaises(Invalid):
            updates._snapshot(target)

    @unittest.skipUnless(os.name == "nt", "native Windows junction test")
    def test_updater_applies_and_rolls_back_through_top_level_junction_alias(self):
        target, review = self.prepared_executable_release()
        alias = self.junction(self.root / "alias", target)
        receipt = updates.apply(alias, review["candidateId"], confirmed=True)
        self.assertTrue(portable.is_link(alias))
        self.assertEqual(updates._version(alias), "0.1.0-alpha.3")
        updates.rollback(alias, receipt["backupId"], confirmed=True)
        self.assertTrue(portable.is_link(alias))
        self.assertEqual(updates._version(alias), "0.1.0-alpha.2")


if __name__ == "__main__":
    unittest.main()
