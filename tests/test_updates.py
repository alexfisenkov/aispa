import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import updates
from aispa_core.store import Invalid


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-updates-test-")
        self.root = Path(self.temp.name)
        self.package = self.root / "install" / "aispa"
        self.package.mkdir(parents=True)
        self.old = self.package_content("0.1.0-alpha.2")
        self.write_package(self.package, self.old)
        self.assets = {}

    def tearDown(self):
        self.temp.cleanup()

    def package_content(self, version):
        return {"SKILL.md": b"# AISpa\n", "tools/aispa.py": b"print('aispa')\n",
                "tools/aispa_core/__init__.py": ('VERSION = "' + version + '"\n').encode(),
                "LICENSE": b"MIT fixture\n"}

    def write_package(self, path, content):
        for name, data in content.items():
            file = path / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            file.chmod(0o644)

    def release(self, version="0.1.0-alpha.3", *, files=None, zip_names=None, symlink=False):
        files = files or self.package_content(version)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, (name, data) in enumerate(sorted(files.items())):
                info = zipfile.ZipInfo((zip_names or {}).get(name, "aispa/" + name))
                info.create_system = 3
                info.external_attr = ((stat.S_IFLNK if symlink and index == 0 else stat.S_IFREG) | 0o644) << 16
                archive.writestr(info, data)
        archive = stream.getvalue()
        manifest = {"schema": 1, "repository": updates.REPOSITORY, "version": version, "commit": "a" * 40,
                    "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(archive).hexdigest()},
                    "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644}
                              for name, data in sorted(files.items())]}
        tag = "v" + version
        ref_url = f"https://api.github.com/repos/{updates.REPOSITORY}/git/ref/tags/{tag}"
        self.assets[ref_url] = json.dumps({"ref": "refs/tags/" + tag, "object": {"type": "commit", "sha": "a" * 40}}).encode()
        prefix = f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/"
        self.assets[prefix + "aispa-update.json"] = json.dumps(manifest).encode()
        self.assets[prefix + "aispa.zip"] = archive
        return {"draft": False, "prerelease": "-" in version, "published_at": "2026-10-19T00:00:00Z", "tag_name": tag,
                "html_url": f"https://github.com/{updates.REPOSITORY}/releases/tag/{tag}",
                "assets": [{"name": name, "state": "uploaded", "browser_download_url": prefix + name}
                           for name in ("aispa.zip", "aispa-update.json")]}

    def network(self, releases):
        self.assets[updates.RELEASES_URL] = json.dumps(releases).encode()
        return patch.object(updates, "_download", side_effect=lambda url, limit, deadline: self.assets[url])

    def prepare(self):
        with self.network([self.release()]):
            return updates.prepare(self.package)

    def test_no_release_and_offline_are_read_only(self):
        before = updates._snapshot(self.package)[1]
        with self.network([]):
            self.assertEqual(updates.check(self.package)["status"], "no_release")
        with patch.object(updates, "_download", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(updates.check(self.package)["status"], "offline")
        with patch.object(updates, "_download", side_effect=http.client.IncompleteRead(b"partial")):
            self.assertEqual(updates.check(self.package)["status"], "offline")
        self.assertEqual(updates._snapshot(self.package)[1], before)
        self.assertEqual(list(self.package.parent.iterdir()), [self.package])

    def test_selects_newest_semver_not_api_order(self):
        with self.network([self.release("0.1.0-alpha.3"), self.release("0.1.0-alpha.12"), self.release("0.1.0-alpha.2")]):
            result = updates.check(self.package)
        self.assertEqual(result["release"]["version"], "0.1.0-alpha.12")
        self.assertEqual(result["status"], "available")

    def test_stable_ignores_prereleases_and_drafts(self):
        self.write_package(self.package, self.package_content("0.1.0"))
        draft = self.release("3.0.0")
        draft["draft"] = True
        unpublished = self.release("2.0.0")
        unpublished["published_at"] = None
        with self.network([self.release("0.2.0-alpha.1"), self.release("0.1.0"), draft, unpublished]):
            result = updates.check(self.package)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["channel"], "stable")
        self.assertEqual(result["release"]["version"], "0.1.0")

    def test_stable_release_is_found_after_full_prerelease_page(self):
        self.write_package(self.package, self.package_content("0.1.0"))
        prerelease = self.release("0.3.0-alpha.1")
        stable = self.release("0.2.0")
        self.assets[updates.RELEASES_URL + "&page=2"] = json.dumps([stable]).encode()
        with self.network([prerelease] * 100):
            result = updates.check(self.package)
        self.assertEqual(result["release"]["version"], "0.2.0")
        self.assertEqual(result["status"], "available")

    def test_semver_rejects_leading_zero_and_compares_build_as_equal(self):
        for version in ("01.2.3", "1.2", "1.2.3-01", "1.2.3-", "v1.2.3", "1.2.3.4"):
            with self.subTest(version=version), self.assertRaises(Invalid):
                updates._semver(version)
        self.assertEqual(updates._semver("1.2.3+a"), updates._semver("1.2.3+b"))
        self.assertLess(updates._semver("1.2.3-alpha.9"), updates._semver("1.2.3-alpha.10"))
        self.assertLess(updates._semver("1.2.3-rc.9"), updates._semver("1.2.3"))

    def test_untrusted_asset_and_missing_asset_rejected(self):
        release = self.release()
        release["assets"][0]["browser_download_url"] = "https://evil.invalid/aispa.zip"
        with self.network([release]):
            self.assertEqual(updates.check(self.package)["status"], "invalid_release")
            with self.assertRaises(Invalid):
                updates.prepare(self.package)
        release["assets"] = []
        with self.network([release]):
            self.assertEqual(updates.check(self.package)["status"], "invalid_release")

    def test_redirect_origins_are_allowlisted(self):
        for url in ("http://github.com/alexfisenkov/aispa/releases/x", "https://evil.invalid/x",
                    "https://github.com/other/aispa/releases/x", "https://user@github.com/alexfisenkov/aispa/releases/x",
                    "https://github.com:444/alexfisenkov/aispa/releases/x"):
            with self.subTest(url=url), self.assertRaises(Invalid):
                updates._trusted_url(url, redirect=True)
        updates._trusted_url("https://release-assets.githubusercontent.com/x?signature=example", redirect=True)

    def test_manifest_commit_must_match_published_tag(self):
        release = self.release()
        url = release["assets"][1]["browser_download_url"]
        manifest = json.loads(self.assets[url])
        manifest["commit"] = "b" * 40
        self.assets[url] = json.dumps(manifest).encode()
        with self.network([release]), self.assertRaisesRegex(Invalid, "Commit"):
            updates.prepare(self.package)

    def test_annotated_release_tag_resolves_to_commit(self):
        release = self.release()
        url = f"https://api.github.com/repos/{updates.REPOSITORY}/git/ref/tags/{release['tag_name']}"
        self.assets[url] = json.dumps({"ref": "refs/tags/" + release["tag_name"], "object": {"type": "tag", "sha": "b" * 40}}).encode()
        self.assets[f"https://api.github.com/repos/{updates.REPOSITORY}/git/tags/{'b' * 40}"] = json.dumps({"object": {"type": "commit", "sha": "a" * 40}}).encode()
        with self.network([release]):
            self.assertEqual(updates.prepare(self.package)["commit"], "a" * 40)

    def test_tampered_archive_rejected_before_staging(self):
        release = self.release()
        self.assets[release["assets"][0]["browser_download_url"]] += b"tampered"
        with self.network([release]), self.assertRaises(Invalid):
            updates.prepare(self.package)
        self.assertEqual(list(self.package.parent.iterdir()), [self.package])

    def test_archive_traversal_symlink_and_extra_file_rejected(self):
        for name in ("aispa/../outside", "/outside", "aispa/C:\\outside"):
            with self.subTest(name=name):
                release = self.release(zip_names={"SKILL.md": name})
                with self.network([release]), self.assertRaises(Invalid):
                    updates.prepare(self.package)
        release = self.release(symlink=True)
        with self.network([release]), self.assertRaises(Invalid):
            updates.prepare(self.package)
        release = self.release()
        archive_url, manifest_url = [a["browser_download_url"] for a in release["assets"]]
        buffer = io.BytesIO(self.assets[archive_url])
        with zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("aispa/extra.py", "unexpected")
        self.assets[archive_url] = buffer.getvalue()
        manifest = json.loads(self.assets[manifest_url])
        manifest["archive"]["sha256"] = hashlib.sha256(buffer.getvalue()).hexdigest()
        self.assets[manifest_url] = json.dumps(manifest).encode()
        with self.network([release]), self.assertRaises(Invalid):
            updates.prepare(self.package)
        self.assertEqual(list(self.package.parent.iterdir()), [self.package])

    def test_zip_compression_is_allowlisted_and_output_is_bounded(self):
        destination = self.root / "extract"
        destination.mkdir()
        record = {"payload": {"sha256": hashlib.sha256(b"A").hexdigest(), "size": 1, "mode": 0o644}}
        for compression in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                info = zipfile.ZipInfo("aispa/payload")
                info.compress_type = compression
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, b"A")
            with self.assertRaisesRegex(Invalid, "ZIP_STORED"):
                updates._extract(buffer.getvalue(), destination, record)
        # Forge a small uncompressed size while retaining a much larger DEFLATE
        # body. ZipExtFile trusts that header; bounded streaming must still keep
        # the decompressor allocation bounded, even with a matching one-byte CRC.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("aispa/payload")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, b"A" * (2 * 1024 * 1024))
        forged = bytearray(buffer.getvalue())
        central = forged.index(b"PK\x01\x02")
        for offset in (14, central + 16):
            struct.pack_into("<I", forged, offset, zlib.crc32(b"A"))
        for offset in (22, central + 24):
            struct.pack_into("<I", forged, offset, 1)
        output_sizes = []
        factory = zipfile._get_decompressor

        class BoundedSpy:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, key):
                return getattr(self.inner, key)

            def decompress(self, *args):
                data = self.inner.decompress(*args)
                output_sizes.append(len(data))
                return data

        with patch.object(zipfile, "_get_decompressor", side_effect=lambda *args: BoundedSpy(factory(*args))):
            updates._extract(bytes(forged), destination, record)
        self.assertTrue(output_sizes)
        self.assertLessEqual(max(output_sizes), 4096)
        self.assertEqual((destination / "payload").read_bytes(), b"A")

    def test_manifest_hash_size_and_case_collisions_rejected(self):
        for change in ("hash", "size", "case", "version", "oversize"):
            with self.subTest(change=change):
                release = self.release()
                url = release["assets"][1]["browser_download_url"]
                manifest = json.loads(self.assets[url])
                if change == "hash":
                    manifest["files"][0]["sha256"] = "0" * 64
                elif change == "size":
                    manifest["files"][0]["size"] += 1
                elif change == "case":
                    entry = dict(manifest["files"][0])
                    entry["path"] = entry["path"].lower()
                    manifest["files"].append(entry)
                elif change == "version":
                    manifest["version"] = "0.1.0-alpha.9"
                else:
                    manifest["files"][0]["size"] = updates.MAX_FILE_BYTES + 1
                self.assets[url] = json.dumps(manifest).encode()
                with self.network([release]), self.assertRaises(Invalid):
                    updates.prepare(self.package)

    def test_prepare_review_is_not_install_and_needs_explicit_confirmation(self):
        result = self.prepare()
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")
        self.assertEqual(result["diff"]["changed"], ["tools/aispa_core/__init__.py"])
        self.assertTrue(result["requiresConfirmation"])
        for value in (False, None, "yes", 1):
            with self.subTest(value=value), self.assertRaises(Invalid):
                updates.apply(self.package, result["candidateId"], confirmed=value)

    def test_apply_and_rollback_preserve_symlink_and_retain_backup(self):
        alias = self.root / "alias"
        alias.symlink_to(self.package, target_is_directory=True)
        result = self.prepare()
        receipt = updates.apply(alias, result["candidateId"], confirmed=True)
        backup = self.package.parent / receipt["backupId"]
        self.assertTrue(alias.is_symlink())
        self.assertEqual(updates._version(alias), "0.1.0-alpha.3")
        self.assertEqual(updates._version(backup / "package"), "0.1.0-alpha.2")
        with self.assertRaises(Invalid):
            updates.rollback(alias, receipt["backupId"])
        result = updates.rollback(alias, receipt["backupId"], confirmed=True)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(updates._version(alias), "0.1.0-alpha.2")
        self.assertEqual(updates._version(backup / "replaced"), "0.1.0-alpha.3")
        self.assertTrue(alias.is_symlink())

    def test_concurrent_modification_and_candidate_tamper_refused(self):
        result = self.prepare()
        (self.package / "SKILL.md").write_text("local edit", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(Invalid, "изменён"):
            updates.apply(self.package, result["candidateId"], confirmed=True)
        self.write_package(self.package, self.old)
        staged = self.package.parent / result["candidateId"] / "package"
        (staged / "SKILL.md").write_text("tamper", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            updates.apply(self.package, result["candidateId"], confirmed=True)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")

    def test_failed_swap_restores_original(self):
        result = self.prepare()
        original_replace = updates.os.replace

        def replace(source, destination):
            if Path(source).name == "package" and Path(source).parent.name == result["candidateId"]:
                raise OSError("simulated disk failure")
            return original_replace(source, destination)

        with patch.object(updates.os, "replace", side_effect=replace), self.assertRaises(OSError):
            updates.apply(self.package, result["candidateId"], confirmed=True)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")
        self.assertEqual(updates._snapshot(self.package)[1], result["expectedCurrentDigest"])

    def test_rollback_refuses_modified_current_or_backup(self):
        result = self.prepare()
        receipt = updates.apply(self.package, result["candidateId"], confirmed=True)
        (self.package / "local.txt").write_text("local", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            updates.rollback(self.package, receipt["backupId"], confirmed=True)
        (self.package / "local.txt").unlink()
        (self.package.parent / receipt["backupId"] / "package/SKILL.md").write_text("changed", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            updates.rollback(self.package, receipt["backupId"], confirmed=True)

    def test_pinned_runtime_git_checkout_and_contained_runs_refused(self):
        runtime = self.root / "project/.aispa/runs/example/runtime"
        self.write_package(runtime, self.old)
        with self.network([self.release()]):
            self.assertEqual(updates.check(runtime)["installReason"], "pinned_runtime")
            with self.assertRaises(Invalid):
                updates.prepare(runtime)
        subprocess.run(["git", "init", "-q", str(self.package)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.package), "add", "."], check=True, capture_output=True)
        with self.network([self.release()]):
            self.assertEqual(updates.check(self.package)["installReason"], "git_checkout")
            with self.assertRaises(Invalid):
                updates.prepare(self.package)
        shutil.rmtree(self.package / ".git")
        (self.package / ".aispa").mkdir()
        with self.assertRaises(Invalid):
            updates.prepare(self.package)

    def test_git_probe_failure_in_checkout_fails_closed(self):
        repo = self.root / "broken-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        package = repo / "skills/aispa"
        self.write_package(package, self.old)
        failed = subprocess.CompletedProcess([], 128, b"", b"fatal: broken index")
        with patch.object(updates.subprocess, "run", return_value=failed):
            self.assertEqual(updates._install_policy(package, package), "git_check_failed")
            with self.assertRaises(Invalid):
                updates.prepare(package)

    def test_runtime_copy_not_changed_by_installed_update(self):
        runtime = self.root / "project/.aispa/runs/example/runtime"
        self.write_package(runtime, self.old)
        before = updates._snapshot(runtime)[1]
        result = self.prepare()
        updates.apply(self.package, result["candidateId"], confirmed=True)
        self.assertEqual(updates._snapshot(runtime)[1], before)

    def test_candidate_for_other_target_and_path_ids_refused(self):
        result = self.prepare()
        other = self.package.parent / "different"
        self.write_package(other, self.old)
        with self.assertRaises(Invalid):
            updates.apply(other, result["candidateId"], confirmed=True)
        for ident in ("../other", "/tmp/anything", ".aispa-update-../x"):
            with self.subTest(ident=ident), self.assertRaises(Invalid):
                updates.apply(self.package, ident, confirmed=True)

    def test_bytecode_cache_does_not_break_cas(self):
        result = self.prepare()
        cache = self.package / "tools/aispa_core/__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-311.pyc").write_bytes(b"transient")
        receipt = updates.apply(self.package, result["candidateId"], confirmed=True)
        self.assertEqual(receipt["status"], "installed")

    def test_producer_is_reproducible_and_commit_bound(self):
        spec = importlib.util.spec_from_file_location("package_update", ROOT / "tools/package_update.py")
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        repo = self.root / "repo"
        repo.mkdir()
        self.write_package(repo / "skills/aispa", self.package_content("0.1.0-alpha.3"))
        (repo / "LICENSE").write_bytes(b"MIT fixture\n")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "release")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "skills/aispa/SKILL.md").write_text("uncommitted edit", encoding="utf-8", newline="\n")
        first = producer.build_release(repo, self.root / "artifact-1")
        second = producer.build_release(repo, self.root / "artifact-2")
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(Path(first["manifest"]).read_bytes(), Path(second["manifest"]).read_bytes())
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertEqual(archive.read("aispa/SKILL.md"), b"# AISpa\n")
            self.assertIn("aispa/LICENSE", archive.namelist())
        self.assertFalse(first["published"])
        with self.assertRaises(Invalid):
            producer.build_release(repo, self.root / "artifact-1")
        (repo / "skills/aispa/LICENSE").write_text("different notice", encoding="utf-8", newline="\n")
        for args in (("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "mismatched license")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        with self.assertRaises(Invalid):
            producer.build_release(repo, self.root / "artifact-bad")


if __name__ == "__main__":
    unittest.main()
