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
from unittest.mock import Mock, patch
import urllib.error
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

    def configure_release(self, version, *, modes=None, runtimes=None):
        self.tag = "v" + version
        modes = modes or {}
        self.files = {
            "SKILL.md": b"# Synthetic AISpa skill\n",
            "tools/aispa.py": b"print('synthetic')\n",
            "tools/aispa_core/__init__.py": ('VERSION = "' + version + '"\n').encode(),
            "LICENSE": b"Synthetic license for test fixture.\n",
        }
        if runtimes is not None:
            self.files["tools/aispa_core/project_context.py"] = ("RUNTIMES = " + repr(tuple(runtimes)) + "\n").encode()
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
        rows = [self.release] if releases is None else releases
        def clone(tag, commit, destination, timeout):
            self.assertEqual((tag, commit), (self.tag, self.commit))
            (destination / "skills/aispa/tools/aispa_core").mkdir(parents=True)
            (destination / "skills/aispa/tools/aispa_core/updates.py").write_text("# test source\n")
        def fetch_tag(url, limit, timeout):
            self.assertEqual(url, install.API + "/releases/tags/" + urllib.parse.quote(self.tag, safe=""))
            matches = [row for row in rows if row["tag_name"] == self.tag]
            self.assertEqual(len(matches), 1)
            return json.dumps(matches[0]).encode()
        with patch.object(install, "_releases", return_value=rows), \
             patch.object(install, "_fetch", side_effect=fetch_tag), \
             patch.object(install, "_source_commit", return_value=self.commit), \
             patch.object(install, "_clone_verified", side_effect=clone), \
             patch.object(install, "_load_pinned_verifier", return_value=updates), \
             patch.object(updates, "_download", side_effect=lambda url, limit, deadline: self.assets[url]), \
             patch.object(updates, "_source_commit", return_value=self.commit):
            yield

    def preview(self):
        with self.network():
            return install.preview(destination=self.destination, cache_dir=self.cache, tag=self.tag)

    def metadata_network(self, responses):
        def fetch(url, limit, timeout):
            self.assertEqual(limit, install.MAX_METADATA_BYTES)
            response = responses[url]
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, bytes) else json.dumps(response).encode()
        return patch.object(install, "_fetch", side_effect=fetch)

    def latest_not_found(self):
        error = urllib.error.HTTPError(install.LATEST_RELEASE_URL, 404, "Not Found", {}, None)
        self.addCleanup(error.close)
        return error

    def test_empty_list_retries_then_finds_published_latest(self):
        self.configure_release("0.3.4")
        responses = {install.RELEASES_URL: [], install.RELEASES_URL + "&page=1": [],
                     install.LATEST_RELEASE_URL: self.release}
        with self.metadata_network(responses) as fetch:
            chosen = install._select_release(install._releases(5))
        self.assertEqual(chosen["version"], "0.3.4")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list(responses))
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.cache.exists())

    def test_empty_list_retry_preserves_prereleases_when_latest_is_404(self):
        responses = {install.RELEASES_URL: [], install.RELEASES_URL + "&page=1": [self.release],
                     install.LATEST_RELEASE_URL: self.latest_not_found()}
        with self.metadata_network(responses):
            self.assertEqual(install._releases(5), [self.release])
        responses[install.RELEASES_URL + "&page=1"] = []
        with self.metadata_network(responses):
            self.assertEqual(install._releases(5), [])

    def test_stale_list_is_reconciled_without_losing_semver_order(self):
        self.configure_release("0.3.3")
        older = self.release
        self.configure_release("0.3.4")
        newer = self.release
        for listed, latest in ((older, newer), (newer, older)):
            with self.subTest(listed=listed["tag_name"], latest=latest["tag_name"]), \
                 self.metadata_network({install.RELEASES_URL: [listed], install.LATEST_RELEASE_URL: latest}):
                chosen = install._select_release(install._releases(5))
                self.assertEqual(chosen["version"], "0.3.4")

    def test_latest_replaces_same_tag_with_fresh_asset_metadata(self):
        self.configure_release("0.3.4")
        responses = {install.RELEASES_URL: [dict(self.release, assets=[])],
                     install.LATEST_RELEASE_URL: self.release}
        with self.metadata_network(responses):
            rows = install._releases(5)
        self.assertEqual(rows, [self.release])
        self.assertEqual(install._select_release(rows)["version"], "0.3.4")

    def test_pagination_retries_only_first_empty_page_and_keeps_page_bound(self):
        prerelease = self.release
        self.configure_release("0.3.4")
        responses = {install.RELEASES_URL: [prerelease] * 100,
                     install.RELEASES_URL + "&page=2": [], install.LATEST_RELEASE_URL: self.release}
        with self.metadata_network(responses) as fetch:
            self.assertEqual(install._select_release(install._releases(5))["version"], "0.3.4")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list(responses))
        responses = {install.RELEASES_URL if page == 1 else install.RELEASES_URL + f"&page={page}":
                     [prerelease] * 100 for page in range(1, 6)}
        with self.metadata_network(responses) as fetch, self.assertRaisesRegex(install.InstallError, "Слишком много"):
            install._releases(5)
        self.assertEqual(fetch.call_count, 5)

    def test_malformed_list_and_latest_fail_closed(self):
        self.configure_release("0.3.4")
        for malformed in ({}, [self.release] * 101, b'{"same":1,"same":2}', b"not json"):
            with self.subTest(list=malformed if not isinstance(malformed, list) else "oversized"), \
                 self.metadata_network({install.RELEASES_URL: malformed}), self.assertRaises(install.InstallError):
                install._releases(5)
        invalid_latest = [[], {}, dict(self.release, draft=True), dict(self.release, published_at=None),
                          dict(self.release, prerelease=True), dict(self.release, tag_name="v0.4.0-alpha.1"),
                          dict(self.release, tag_name="main"), dict(self.release, tag_name=42),
                          dict(self.release, html_url="https://github.com/other/aispa/releases/tag/v0.3.4"),
                          b'{"same":1,"same":2}']
        for latest in invalid_latest:
            with self.subTest(latest=latest), self.metadata_network({install.RELEASES_URL: [self.release],
                 install.LATEST_RELEASE_URL: latest}), self.assertRaises(install.InstallError):
                install._releases(5)

    def test_only_latest_404_is_tolerated_other_http_and_network_errors_propagate(self):
        self.configure_release("0.3.4")
        for error in (urllib.error.HTTPError(install.LATEST_RELEASE_URL, code, "failure", {}, None)
                      for code in (401, 403, 429, 500)):
            with self.subTest(code=error.code), self.metadata_network({install.RELEASES_URL: [self.release],
                 install.LATEST_RELEASE_URL: error}), self.assertRaises(urllib.error.HTTPError):
                install._releases(5)
        with self.metadata_network({install.RELEASES_URL: self.latest_not_found()}), \
             self.assertRaises(urllib.error.HTTPError):
            install._releases(5)
        with self.metadata_network({install.RELEASES_URL: [self.release],
             install.LATEST_RELEASE_URL: urllib.error.URLError("offline")}), self.assertRaises(urllib.error.URLError):
            install._releases(5)

    def test_all_discovery_requests_share_one_deadline(self):
        responses = {install.RELEASES_URL: [], install.RELEASES_URL + "&page=1": [],
                     install.LATEST_RELEASE_URL: self.latest_not_found()}
        with self.metadata_network(responses) as fetch, \
             patch.object(install.time, "monotonic", side_effect=[100, 100, 101, 102]):
            self.assertEqual(install._releases(5), [])
        self.assertEqual([call.args[2] for call in fetch.call_args_list], [5, 4, 3])
        with self.metadata_network(responses) as fetch, \
             patch.object(install.time, "monotonic", side_effect=[100, 100, 103]), \
             self.assertRaisesRegex(install.InstallError, "Истекло время"):
            install._releases(2)
        self.assertEqual(fetch.call_count, 1)

    def test_api_fetch_revalidates_metadata_without_changing_asset_headers(self):
        asset_url = f"https://github.com/{install.REPOSITORY}/releases/download/{self.tag}/aispa.zip"
        for url in (install.RELEASES_URL, asset_url):
            with self.subTest(url=url):
                response = Mock(headers={})
                response.geturl.return_value = url
                response.read1.return_value = b""
                opener = Mock()
                opener.open.return_value = contextlib.nullcontext(response)
                with patch.object(install.urllib.request, "build_opener", return_value=opener):
                    self.assertEqual(install._fetch(url, install.MAX_METADATA_BYTES, 5), b"")
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Cache-control"), "no-cache" if url == install.RELEASES_URL else None)

    def test_explicit_tag_fetches_exact_published_release_without_listing(self):
        self.configure_release("0.3.4-rc.1+build.2")
        tag_url = install.API + "/releases/tags/" + urllib.parse.quote(self.tag, safe="")
        with self.metadata_network({tag_url: self.release}) as fetch, \
             patch.object(install, "_releases", side_effect=AssertionError("list queried")):
            chosen = install._tag_release(self.tag, 5)
        self.assertEqual(chosen["tag"], self.tag)
        fetch.assert_called_once_with(tag_url, install.MAX_METADATA_BYTES, 5)
        with self.network(), patch.object(install, "_releases", side_effect=AssertionError("list queried")):
            receipt = install.preview(destination=self.destination, cache_dir=self.cache, tag=self.tag)
        self.assertEqual(receipt["tag"], self.tag)

    def test_explicit_tag_validation_runs_before_network_and_rejects_wrong_release(self):
        for tag in ("", "main", "v01.2.3", "v0.3.4/other", "v0.3.4?query", "v" + "1" * 128, 42):
            with self.subTest(tag=tag), patch.object(install, "_fetch") as fetch, self.assertRaises(install.InstallError):
                install._tag_release(tag, 5)
            fetch.assert_not_called()
        tag_url = install.API + "/releases/tags/" + urllib.parse.quote(self.tag, safe="")
        for row in ([], dict(self.release, tag_name="v0.3.4"), dict(self.release, draft=True),
                    dict(self.release, published_at=None), dict(self.release, assets=[])):
            with self.subTest(row=row), self.metadata_network({tag_url: row}), self.assertRaises(install.InstallError):
                install._tag_release(self.tag, 5)
        with self.metadata_network({tag_url: self.latest_not_found()}), self.assertRaises(urllib.error.HTTPError):
            install._tag_release(self.tag, 5)

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
                                                  ("cursor", ".cursor", ".cursor"),
                                                  ("openclaw", ".agents", ".openclaw"),
                                                  ("gemini", ".gemini", ".gemini"),
                                                  ("antigravity", ".agents", ".gemini/config")):
            self.assertEqual(install.choose_destination(runtime=runtime, scope="project", project_root=project),
                             (project / project_dir / "skills/aispa").resolve())
            self.assertEqual(install.choose_destination(runtime=runtime, scope="global", home=home),
                             (home / global_dir / "skills/aispa").resolve())
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/install.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preview", result.stdout)

    def test_new_runtime_preview_uses_native_unicode_destinations_without_installing(self):
        from aispa_core.project_context import RUNTIMES
        self.configure_release("0.3.0", runtimes=RUNTIMES)
        self.assertEqual(set(install._RUNTIME_DIRS), set(RUNTIMES))
        self.assertEqual(set(install._PROJECT_DIRS), set(RUNTIMES))
        project = self.root / "проект с пробелами 🙂"
        home = self.root / "дом пользователя 🙂"
        project.mkdir(); home.mkdir()
        for runtime in ("openclaw", "gemini", "antigravity"):
            for scope in ("project", "global"):
                with self.subTest(runtime=runtime, scope=scope), self.network(), patch.object(Path, "home", return_value=home):
                    output = io.StringIO()
                    args = ["preview", "--runtime", runtime, "--scope", scope,
                            "--tag", self.tag, "--cache-dir", str(self.cache)]
                    if scope == "project":
                        args += ["--project-root", str(project)]
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(install.main(args), 0)
                    receipt = json.loads(output.getvalue())
                    expected = ((project / install._PROJECT_DIRS[runtime]) if scope == "project"
                                else (home / install._RUNTIME_DIRS[runtime])) / "skills/aispa"
                    self.assertEqual(receipt["destination"], str(expected.resolve()))
                    self.assertFalse(expected.exists())
                    self.assertEqual(receipt["commit"], self.commit)
                    self.assertEqual(receipt["runtime"], runtime)
        with self.assertRaises(install.InstallError):
            install.choose_destination(runtime="unknown-agent", scope="global", home=home)

    def test_new_host_rejects_published_package_without_its_runtime_before_install(self):
        for runtime in ("openclaw", "gemini", "antigravity"):
            for version, supported in (("0.2.0", None), ("0.3.0", ("codex", "claude-code", "cursor"))):
                with self.subTest(runtime=runtime, version=version):
                    self.configure_release(version, runtimes=supported)
                    home = self.root / "unused home"
                    home.mkdir(exist_ok=True)
                    with self.network(), self.assertRaisesRegex(install.InstallError, "не поддерживает runtime"):
                        install.preview(runtime=runtime, scope="global", home=home,
                                        cache_dir=self.cache, tag=self.tag)
                    self.assertFalse((home / install._RUNTIME_DIRS[runtime] / "skills/aispa").exists())
                    self.assertEqual(list(self.cache.glob("aispa-install-*")), [])

    def test_new_host_apply_checks_runtime_and_installs_verified_capable_package(self):
        from aispa_core.project_context import RUNTIMES
        self.configure_release("0.3.0", runtimes=RUNTIMES)
        home = self.root / "agent home"
        home.mkdir()
        for runtime in ("openclaw", "gemini", "antigravity"):
            with self.subTest(runtime=runtime), self.network():
                receipt = install.preview(runtime=runtime, scope="global", home=home,
                                          cache_dir=self.cache, tag=self.tag)
                applied = install.apply(receipt["candidateId"], confirmed=True, cache_dir=self.cache)
                self.assertEqual(applied["runtime"], runtime)
                self.assertEqual(applied["status"], "installed")
                self.assertEqual((Path(receipt["destination"]) / "SKILL.md").read_bytes(), self.files["SKILL.md"])

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
