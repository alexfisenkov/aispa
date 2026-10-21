import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import updates
from aispa_core.store import Invalid

SPEC = importlib.util.spec_from_file_location("aispa_first_install", ROOT / "tools/install.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-install-test-")
        self.root = Path(self.temp.name)
        self.cache = self.root / "private cache"
        self.destination = self.root / "my project" / ".agents" / "skills" / "aispa"
        self.commit = "a" * 40
        self.configure_release("0.1.0-alpha.10")

    def configure_release(self, version, *, modes=None):
        self.tag = "v" + version
        modes = modes or {}
        self.files = {
            "SKILL.md": b"# Synthetic AISpa skill\n",
            "tools/aispa.py": b"print('synthetic')\n",
            "tools/aispa_core/__init__.py": ('VERSION = "' + version + '"\n').encode(),
            "LICENSE": b"Synthetic license for test fixture.\n",
        }
        buffer = io.BytesIO()
        entries = []
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(self.files.items()):
                info = zipfile.ZipInfo("aispa/" + name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | modes.get(name, 0o644)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
                entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                                "size": len(data), "mode": modes.get(name, 0o644)})
        self.archive = buffer.getvalue()
        self.manifest = {"schema": 1, "repository": install.REPOSITORY,
                         "version": version, "commit": self.commit,
                         "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(self.archive).hexdigest()},
                         "files": entries}
        quoted_tag = urllib.parse.quote(self.tag, safe="")
        prefix = f"https://github.com/{install.REPOSITORY}/releases/download/{quoted_tag}/"
        self.assets = {prefix + "aispa.zip": self.archive,
                       prefix + "aispa-update.json": json.dumps(self.manifest).encode()}
        self.release = {"draft": False, "prerelease": not install._semver(version)[3],
                        "published_at": "2026-10-20T00:00:00Z",
                        "tag_name": self.tag,
                        "html_url": f"https://github.com/{install.REPOSITORY}/releases/tag/{quoted_tag}",
                        "assets": [{"name": name, "state": "uploaded", "browser_download_url": prefix + name}
                                   for name in ("aispa.zip", "aispa-update.json")]}

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def network(self, releases=None):
        def clone(tag, commit, destination, timeout):
            self.assertEqual((tag, commit), (self.tag, self.commit))
            (destination / "skills/aispa/tools/aispa_core").mkdir(parents=True)
            (destination / "skills/aispa/tools/aispa_core/updates.py").write_text("# test source\n")
        with patch.object(install, "_releases", return_value=[self.release] if releases is None else releases), \
             patch.object(install, "_source_commit", return_value=self.commit), \
             patch.object(install, "_clone_verified", side_effect=clone), \
             patch.object(install, "_load_pinned_verifier", return_value=updates), \
             patch.object(updates, "_download", side_effect=lambda url, limit, deadline: self.assets[url]), \
             patch.object(updates, "_source_commit", return_value=self.commit):
            yield

    def preview(self):
        with self.network():
            return install.preview(destination=self.destination, cache_dir=self.cache, tag=self.tag)

    def test_real_cli_argv_preview_then_confirmed_install_to_unicode_path(self):
        self.destination = self.root / "проект с пробелом" / ".agents" / "skills" / "aispa"
        output = io.StringIO()
        with self.network(), contextlib.redirect_stdout(output):
            self.assertEqual(install.main(["preview", "--destination", str(self.destination),
                                           "--cache-dir", str(self.cache), "--tag", self.tag]), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["destination"], str(self.destination.resolve()))
        self.assertEqual(receipt["commit"], self.commit)
        self.assertEqual(receipt["archiveSha256"], hashlib.sha256(self.archive).hexdigest())
        self.assertFalse(self.destination.exists())
        with self.assertRaisesRegex(install.InstallError, "confirm"):
            install.apply(receipt["candidateId"], cache_dir=self.cache)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(install.main(["apply", "--candidate", receipt["candidateId"],
                                           "--cache-dir", str(self.cache), "--confirm"]), 0)
        installed = json.loads(output.getvalue())
        self.assertEqual(installed["status"], "installed")
        self.assertEqual(installed["destination"], str(self.destination.resolve()))
        self.assertEqual((self.destination / "SKILL.md").read_bytes(), self.files["SKILL.md"])
        self.assertEqual({p.relative_to(self.destination).as_posix() for p in self.destination.rglob("*") if p.is_file()},
                         set(self.files))
        self.assertTrue((self.cache / receipt["candidateId"] / "installed.json").is_file())
        with self.assertRaises(install.InstallError):
            install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)

    def test_existing_empty_directory_and_symlink_refused_before_network(self):
        self.destination.mkdir(parents=True)
        with patch.object(install, "_releases", side_effect=AssertionError("network called")):
            with self.assertRaises(install.InstallError):
                install.preview(destination=self.destination, cache_dir=self.cache)
        self.destination.rmdir()
        marker = self.root / "real"
        marker.mkdir()
        try:
            self.destination.symlink_to(marker, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(install.InstallError):
            install.choose_destination(destination=self.destination)

    def test_only_published_release_or_explicit_published_tag(self):
        draft = dict(self.release, draft=True)
        unpublished = dict(self.release, published_at=None)
        with self.assertRaises(install.InstallError):
            install._select_release([draft, unpublished], self.tag)
        with self.assertRaises(install.InstallError):
            install._select_release([self.release], "v0.1.0-alpha.11")
        self.assertEqual(install._select_release([draft, self.release], self.tag)["tag"], self.tag)
        self.assertEqual(install._select_release([self.release], self.tag)["version"], "0.1.0-alpha.10")

    def test_default_selects_stable_despite_higher_prereleases_and_metadata_flags(self):
        self.configure_release("4.0.0-alpha.1")
        higher_prerelease = self.release
        self.configure_release("3.0.0")
        stable_tag_flagged_prerelease = dict(self.release, prerelease=True)
        self.configure_release("2.0.0-alpha.1")
        prerelease_tag_flagged_stable = dict(self.release, prerelease=False)
        self.configure_release("1.0.0")
        invalid_flags = [dict(self.release, prerelease=flag) for flag in (None, 0, "false")]
        self.configure_release("0.1.0+build-with-hyphen")
        stable = self.release
        for releases in ([higher_prerelease, stable], [stable, higher_prerelease],
                         [stable_tag_flagged_prerelease, prerelease_tag_flagged_stable, *invalid_flags, stable]):
            with self.subTest(tags=[row["tag_name"] for row in releases]):
                chosen = install._select_release(releases)
                self.assertEqual(chosen["version"], "0.1.0+build-with-hyphen")
                self.assertIs(chosen["prerelease"], False)

    def test_default_without_published_stable_fails_clearly_before_staging(self):
        draft = dict(self.release, draft=True)
        unpublished = dict(self.release, published_at=None)
        with self.network([self.release, draft, unpublished]), \
             patch.object(install, "_source_commit", side_effect=AssertionError("source accessed")):
            with self.assertRaisesRegex(install.InstallError, "стабильный Release.*не найден"):
                install.preview(destination=self.destination, cache_dir=self.cache)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.cache.exists())

    def test_explicit_tag_preserves_boolean_github_prerelease_metadata(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                release = dict(self.release, prerelease=flag)
                self.assertIs(install._select_release([release], self.tag)["prerelease"], flag)
                with self.network([release]), patch.object(updates, "_release", wraps=updates._release) as verify:
                    receipt = install.preview(destination=self.destination, cache_dir=self.cache, tag=self.tag)
                self.assertIs(verify.call_args.args[0][0]["prerelease"], flag)
                self.assertIs(receipt["prerelease"], flag)
        for flag in (None, 0, 1, "false"):
            with self.subTest(flag=flag), self.assertRaisesRegex(install.InstallError, "prerelease"):
                install._select_release([dict(self.release, prerelease=flag)], self.tag)

    def test_stable_default_preview_and_apply_preserve_verified_bytes_and_modes(self):
        self.configure_release("0.1.0", modes={"tools/aispa.py": 0o755})
        output = io.StringIO()
        with self.network(), contextlib.redirect_stdout(output):
            self.assertEqual(install.main(["preview", "--destination", str(self.destination),
                                           "--cache-dir", str(self.cache)]), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["version"], "0.1.0")
        self.assertIs(receipt["prerelease"], False)
        self.assertTrue(receipt["requiresConfirmation"])
        self.assertFalse(self.destination.exists())
        staged = self.cache / receipt["candidateId"] / "package"
        before = {p.relative_to(staged): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                  for p in staged.rglob("*") if p.is_file()}
        self.assertEqual({name.as_posix(): value[0] for name, value in before.items()}, self.files)
        installed = install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)
        self.assertEqual(installed["status"], "installed")
        self.assertEqual(updates._version(self.destination), "0.1.0")
        self.assertEqual({p.relative_to(self.destination): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                          for p in self.destination.rglob("*") if p.is_file()}, before)
        self.assertEqual(install._scan_package(self.destination)[1], receipt["candidateDigest"])

    def test_source_commit_and_manifest_hash_mismatch_fail_without_install(self):
        self.manifest["commit"] = "b" * 40
        url = f"https://github.com/{install.REPOSITORY}/releases/download/{self.tag}/aispa-update.json"
        self.assets[url] = json.dumps(self.manifest).encode()
        with self.network(), self.assertRaises(install.InstallError):
            install.preview(destination=self.destination, cache_dir=self.cache, tag=self.tag)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.cache.iterdir()), [])
        # A mismatched checked-out source is rejected before importing it.
        with patch.object(install, "_git", side_effect=["", "b" * 40]), self.assertRaises(install.InstallError):
            install._clone_verified(self.tag, self.commit, self.root / "source", 10)

    def test_new_parent_link_after_mkdir_is_rejected_before_copy(self):
        receipt = self.preview()
        outside = self.root / "outside"
        outside.mkdir()
        probe = self.root / "link-probe"
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlink unavailable")
        original = install._mkdir_parents
        def interleave(target):
            original(target)
            parent = target.parent
            parent.rename(parent.with_name("old-skills"))
            parent.symlink_to(outside, target_is_directory=True)
        with patch.object(install, "_mkdir_parents", side_effect=interleave):
            with self.assertRaises(install.InstallError):
                install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)
        self.assertFalse((outside / "aispa").exists())

    def test_stage_and_manifest_cas_detect_tampering(self):
        receipt = self.preview()
        candidate = self.cache / receipt["candidateId"]
        (candidate / "package/SKILL.md").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)
        self.assertFalse(self.destination.exists())
        (candidate / "package/SKILL.md").write_bytes(self.files["SKILL.md"])
        (candidate / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)
        self.assertFalse(self.destination.exists())

    def test_runtime_paths_and_standalone_help_without_package_import(self):
        project = self.root / "project"
        project.mkdir()
        home = self.root / "fake home"
        home.mkdir()
        for runtime, project_dir, global_dir in (("codex", ".agents", ".codex"),
                                                  ("claude-code", ".claude", ".claude"),
                                                  ("cursor", ".cursor", ".cursor")):
            self.assertEqual(install.choose_destination(runtime=runtime, scope="project", project_root=project),
                             (project / project_dir / "skills/aispa").resolve())
            self.assertEqual(install.choose_destination(runtime=runtime, scope="global", home=home),
                             (home / global_dir / "skills/aispa").resolve())
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/install.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preview", result.stdout)

    def test_pinned_verifier_has_isolated_import_name(self):
        source = self.root / "source"
        package = source / "skills/aispa/tools/aispa_core"
        shutil.copytree(ROOT / "skills/aispa/tools/aispa_core", package)
        verifier = install._load_pinned_verifier(source)
        self.assertNotEqual(verifier, updates)
        self.assertTrue(verifier.__name__.startswith("_aispa_verified_"))
        self.assertEqual(verifier.REPOSITORY, install.REPOSITORY)


if __name__ == "__main__":
    unittest.main()
