import copy
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
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

    def release(self, version="0.1.0-alpha.3", *, files=None, zip_names=None, symlink=False, modes=None):
        files = files or self.package_content(version)
        modes = modes or {}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, (name, data) in enumerate(sorted(files.items())):
                info = zipfile.ZipInfo((zip_names or {}).get(name, "aispa/" + name))
                info.create_system = 3
                info.external_attr = ((stat.S_IFLNK if symlink and index == 0 else stat.S_IFREG)
                                      | modes.get(name, 0o644)) << 16
                archive.writestr(info, data)
        archive = stream.getvalue()
        manifest = {"schema": 1, "repository": updates.REPOSITORY, "version": version, "commit": "a" * 40,
                    "archive": {"name": "aispa.zip", "sha256": hashlib.sha256(archive).hexdigest()},
                    "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                               "mode": modes.get(name, 0o644)}
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
        self.assets.setdefault(updates.RELEASES_URL + "&page=1", json.dumps(releases).encode())
        def download(url, limit, deadline):
            if url == updates.LATEST_RELEASE_URL and url not in self.assets:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            value = self.assets[url]
            if isinstance(value, Exception):
                raise value
            return value
        return patch.object(updates, "_download", side_effect=download)

    def prepare(self):
        with self.network([self.release()]):
            return updates.prepare(self.package)

    def test_timeout_contract_accepts_60_and_120_for_check_and_prepare(self):
        for timeout in (60, 120):
            with self.subTest(timeout=timeout), self.network([self.release()]):
                self.assertEqual(updates.check(self.package, timeout=timeout)["status"], "available")
                prepared = updates.prepare(self.package, timeout=timeout)
                self.assertEqual(prepared["status"], "prepared")
                self.assertEqual((self.package / "SKILL.md").read_bytes(), self.old["SKILL.md"])

    def test_invalid_timeout_never_reads_update_network_or_changes_install(self):
        for timeout in (True, False, float("nan"), float("inf"), -float("inf"), 0, -1, 120.01, "30", None):
            with self.subTest(timeout=timeout), patch.object(updates, "_download") as download:
                with self.assertRaises(Invalid):
                    updates._deadline(timeout)
                self.assertEqual(updates.check(self.package, timeout=timeout)["status"], "invalid_release")
                with self.assertRaises(Invalid):
                    updates.prepare(self.package, timeout=timeout)
                download.assert_not_called()
                self.assertEqual((self.package / "SKILL.md").read_bytes(), self.old["SKILL.md"])
                self.assertEqual(list(self.package.parent.glob(".aispa-update-*")), [])

    def test_https_context_preserves_valid_default_ca_and_windows_native_trust(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        for platform, cafile, capath in (("darwin", "/configured/ca.pem", None),
                                        ("linux", None, "/configured/certs"), ("win32", None, None)):
            with self.subTest(platform=platform, cafile=cafile, capath=capath):
                secure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                with patch.dict(os.environ, clean_env, clear=True), patch.object(updates.sys, "platform", platform), \
                     patch.object(ssl, "create_default_context", return_value=secure), \
                     patch.object(ssl, "get_default_verify_paths", return_value=SimpleNamespace(cafile=cafile, capath=capath)), \
                     patch.object(Path, "is_file", side_effect=AssertionError("No fallback with native/default trust")):
                    self.assertIs(updates._https_context(), secure)
                    self.assertTrue(secure.check_hostname)
                    self.assertEqual(secure.verify_mode, ssl.CERT_REQUIRED)
                    self.assertEqual(dict(os.environ), clean_env)

    def test_https_context_respects_explicit_ca_environment_even_when_empty(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        for key, value in (("SSL_CERT_FILE", "/custom/ca.pem"), ("SSL_CERT_DIR", "/custom/certs"),
                           ("SSL_CERT_FILE", ""), ("SSL_CERT_DIR", "")):
            with self.subTest(key=key, value=value):
                secure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                configured = dict(clean_env, **{key: value})
                with patch.dict(os.environ, configured, clear=True), \
                     patch.object(ssl, "create_default_context", return_value=secure), \
                     patch.object(ssl, "get_default_verify_paths", side_effect=AssertionError("Explicit configuration wins")), \
                     patch.object(Path, "is_file", side_effect=AssertionError("No system fallback for explicit CA")):
                    self.assertIs(updates._https_context(), secure)
                    self.assertTrue(secure.check_hostname)
                    self.assertEqual(secure.verify_mode, ssl.CERT_REQUIRED)
                    self.assertEqual(dict(os.environ), configured)

    def test_https_context_uses_existing_system_bundle_only_when_defaults_absent(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            with self.subTest(bundle=bundle):
                secure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                with patch.dict(os.environ, clean_env, clear=True), patch.object(updates.sys, "platform", "darwin"), \
                     patch.object(ssl, "create_default_context", return_value=secure), \
                     patch.object(ssl, "get_default_verify_paths", return_value=SimpleNamespace(cafile=None, capath=None)), \
                     patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path.as_posix() == bundle), \
                     patch.object(ssl.SSLContext, "load_verify_locations") as load:
                    self.assertIs(updates._https_context(), secure)
                    load.assert_called_once_with(cafile=bundle)
                    self.assertTrue(secure.check_hostname)
                    self.assertEqual(secure.verify_mode, ssl.CERT_REQUIRED)
                    self.assertEqual(dict(os.environ), clean_env)

    def test_missing_all_ca_stays_verified_and_offline_without_env_mutation(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        secure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("untrusted certificate"))
        before = updates._snapshot(self.package)
        with patch.dict(os.environ, clean_env, clear=True), patch.object(updates.sys, "platform", "darwin"), \
             patch.object(ssl, "create_default_context", return_value=secure), \
             patch.object(ssl, "get_default_verify_paths", return_value=SimpleNamespace(cafile=None, capath=None)), \
             patch.object(Path, "is_file", return_value=False), \
             patch.object(ssl.SSLContext, "load_verify_locations") as load, \
             patch.object(updates.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(updates.check(self.package)["status"], "offline")
            load.assert_not_called()
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(handler, updates._SafeRedirect) for handler in handlers))
            https = next(handler for handler in handlers if isinstance(handler, updates.urllib.request.HTTPSHandler))
            self.assertIs(https._context, secure)
            self.assertTrue(secure.check_hostname)
            self.assertEqual(secure.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(dict(os.environ), clean_env)
        self.assertEqual(updates._snapshot(self.package), before)

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

    def test_empty_list_finds_latest_and_prepare_uses_same_discovery(self):
        release = self.release("0.2.0")
        self.assets[updates.LATEST_RELEASE_URL] = json.dumps(release).encode()
        before = updates._snapshot(self.package)
        with self.network([]) as fetch:
            result = updates.check(self.package)
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["release"]["version"], "0.2.0")
            self.assertEqual([call.args[0] for call in fetch.call_args_list],
                             [updates.RELEASES_URL, updates.RELEASES_URL + "&page=1", updates.LATEST_RELEASE_URL])
            self.assertEqual(updates._snapshot(self.package), before)
            candidate = updates.prepare(self.package)
        self.assertEqual(candidate["version"], "0.2.0")
        self.assertEqual(updates._snapshot(self.package), before)
        receipt = updates.apply(self.package, candidate["candidateId"], confirmed=True)
        self.assertEqual(updates._version(self.package), "0.2.0")
        updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(updates._snapshot(self.package), before)

    def test_empty_first_page_retry_can_recover_prerelease(self):
        release = self.release()
        self.assets[updates.RELEASES_URL + "&page=1"] = json.dumps([release]).encode()
        with self.network([]):
            result = updates.check(self.package)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["release"]["version"], "0.1.0-alpha.3")

    def test_stale_nonempty_list_is_reconciled_with_latest(self):
        self.write_package(self.package, self.package_content("0.1.0"))
        old, latest = self.release("0.1.0"), self.release("0.2.0")
        self.assets[updates.LATEST_RELEASE_URL] = json.dumps(latest).encode()
        with self.network([old]):
            result = updates.check(self.package)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["release"]["version"], "0.2.0")

    def test_latest_does_not_override_semver_order_or_prerelease_channel(self):
        latest = self.release("0.2.0")
        newer_stable, preview = self.release("0.3.0"), self.release("0.4.0-alpha.1")
        self.assets[updates.LATEST_RELEASE_URL] = json.dumps(latest).encode()
        with self.network([newer_stable, preview]):
            self.assertEqual(updates.check(self.package)["release"]["version"], "0.4.0-alpha.1")
            self.write_package(self.package, self.package_content("0.1.0"))
            self.assertEqual(updates.check(self.package)["release"]["version"], "0.3.0")

    def test_direct_latest_refreshes_same_tag_assets_without_duplicate_tie(self):
        latest = self.release("0.2.0")
        stale = copy.deepcopy(latest)
        stale["assets"] = []
        self.assets[updates.LATEST_RELEASE_URL] = json.dumps(latest).encode()
        with self.network([stale, stale]):
            self.assertEqual(updates.check(self.package)["status"], "available")
        latest["assets"] = []
        self.assets[updates.LATEST_RELEASE_URL] = json.dumps(latest).encode()
        with self.network([stale]):
            self.assertEqual(updates.check(self.package)["status"], "invalid_release")

    def test_latest_metadata_is_not_silently_ignored(self):
        good = self.release("0.2.0")
        bad_records = [[], None, {},
                       dict(good, draft=True), dict(good, published_at=None),
                       dict(good, prerelease=True), dict(good, tag_name="main"),
                       dict(good, tag_name="v0.2.0-alpha.1"),
                       dict(good, html_url="https://github.com/other/aispa/releases/tag/v0.2.0")]
        for bad in bad_records:
            with self.subTest(value=bad):
                self.assets[updates.LATEST_RELEASE_URL] = json.dumps(bad).encode()
                with self.network([good]):
                    self.assertEqual(updates.check(self.package)["status"], "invalid_release")
        self.assets[updates.LATEST_RELEASE_URL] = b'{"draft":false,"draft":true}'
        with self.network([]):
            self.assertEqual(updates.check(self.package)["status"], "invalid_release")

    def test_latest_errors_never_report_no_release_or_current(self):
        for error in [urllib.error.HTTPError(updates.LATEST_RELEASE_URL, code, "error", {}, None)
                      for code in (403, 429, 500)] + [urllib.error.URLError("offline"), TimeoutError()]:
            with self.subTest(error=error):
                self.assets[updates.LATEST_RELEASE_URL] = error
                with self.network([]):
                    self.assertEqual(updates.check(self.package)["status"], "offline")
                with self.network([self.release("0.1.0-alpha.1")]):
                    self.assertEqual(updates.check(self.package)["status"], "offline")

    RATE_LIMIT_TEXT = ("GitHub временно ограничил запросы без входа. "
                       "Аккаунт и токен не нужны — попробуйте через час")

    def test_github_rate_limit_is_named_without_account_or_token(self):
        for url in (updates.RELEASES_URL, updates.LATEST_RELEASE_URL):
            for code in (403, 429):
                with self.subTest(url=url, code=code):
                    self.assets.clear()
                    error = urllib.error.HTTPError(url, code, "rate limited", {}, None)
                    self.addCleanup(error.close)
                    self.assets[url] = error
                    with self.network([self.release()]) if url != updates.RELEASES_URL else \
                            patch.object(updates, "_download", side_effect=error):
                        result = updates.check(self.package)
                    self.assertEqual(result["status"], "offline")
                    self.assertEqual(result["reason"], "github_rate_limit")
                    self.assertEqual(result["message"], self.RATE_LIMIT_TEXT)
                    self.assertNotIn("HTTP Error", json.dumps(result, ensure_ascii=False))
        # Other network failures keep the previous offline result without that reason.
        for error in (urllib.error.HTTPError(updates.LATEST_RELEASE_URL, 500, "error", {}, None),
                      urllib.error.HTTPError(updates.LATEST_RELEASE_URL, 401, "error", {}, None),
                      urllib.error.URLError("offline"), TimeoutError()):
            with self.subTest(error=error), patch.object(updates, "_download", side_effect=error):
                result = updates.check(self.package)
                self.assertEqual(result["status"], "offline")
                self.assertNotIn("reason", result)
                self.assertNotEqual(result["message"], self.RATE_LIMIT_TEXT)

    def test_prepare_names_github_rate_limit_instead_of_raw_http_error(self):
        error = urllib.error.HTTPError(updates.RELEASES_URL, 429, "Too Many Requests", {}, None)
        self.addCleanup(error.close)
        with patch.object(updates, "_download", side_effect=error), \
             self.assertRaisesRegex(Invalid, "Аккаунт и токен не нужны"):
            updates.prepare(self.package)
        self.assertEqual(list(self.package.parent.glob(".aispa-update-*")), [])

    def test_empty_terminal_page_is_not_retried_and_requests_share_deadline(self):
        row = self.release()
        self.assets[updates.RELEASES_URL + "&page=2"] = b'[]'
        with self.network([row] * 100) as fetch:
            self.assertEqual(updates.check(self.package)["status"], "available")
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [updates.RELEASES_URL, updates.RELEASES_URL + "&page=2", updates.LATEST_RELEASE_URL])
        self.assertEqual(len({call.args[2] for call in fetch.call_args_list}), 1)

    def test_page_bound_and_malformed_list_remain_errors(self):
        row = self.release()
        for page in range(2, 6):
            self.assets[updates.RELEASES_URL + f"&page={page}"] = json.dumps([row] * 100).encode()
        with self.network([row] * 100) as fetch:
            self.assertEqual(updates.check(self.package)["status"], "invalid_release")
            self.assertEqual(fetch.call_count, 5)
        for bad in ({}, [row] * 101):
            with self.network(bad):
                self.assertEqual(updates.check(self.package)["status"], "invalid_release")

    def test_api_requests_revalidate_cache_and_keep_deadline(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = updates.RELEASES_URL
        response.headers = {}
        response.read1.side_effect = [b'[]', b'']
        opener = Mock()
        opener.open.return_value = response
        with patch.object(updates.urllib.request, "build_opener", return_value=opener), \
             patch.object(updates.time, "monotonic", return_value=10):
            self.assertEqual(updates._download(updates.RELEASES_URL, 100, 12), b'[]')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Cache-control"), "no-cache")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 2)
        with patch.object(updates.urllib.request, "build_opener") as build, \
             patch.object(updates.time, "monotonic", return_value=12):
            with self.assertRaises(TimeoutError):
                updates._download(updates.LATEST_RELEASE_URL, 100, 12)
            build.assert_not_called()

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
        containing_runs = self.package.parent / "contains-runs"
        self.write_package(containing_runs, self.old)
        (containing_runs / ".aispa").mkdir()
        with self.assertRaises(Invalid):
            updates.prepare(containing_runs)

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

    def test_alpha10_to_stable_and_rollback_preserve_bytes_modes_and_pinned_runtime(self):
        old = dict(self.package_content("0.1.0-alpha.10"), **{"assets/original.bin": b"\x00\xffalpha.10\r\n"})
        self.write_package(self.package, old)
        (self.package / "tools/aispa.py").chmod(0o755)
        runtime = self.root / "project/.aispa/runs/active-alpha10/runtime"
        shutil.copytree(self.package, runtime)
        journal = runtime.parent / "events.jsonl"
        journal.write_bytes(b'{"version":"0.1.0-alpha.10","status":"running"}\n')

        def file_state(directory):
            return {p.relative_to(directory): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                    for p in directory.rglob("*") if p.is_file()}

        before = file_state(self.package)
        pinned_before = file_state(runtime.parent)
        stable = dict(self.package_content("0.1.0"), **{"assets/stable.bin": b"\x00\xffstable\n"})
        release = self.release("0.1.0", files=stable, modes={"tools/aispa.py": 0o755})
        with self.network([release]):
            self.assertEqual(updates.check(self.package)["status"], "available")
            self.assertEqual(updates.check(runtime)["installReason"], "pinned_runtime")
            with self.assertRaisesRegex(Invalid, "pinned_runtime"):
                updates.prepare(runtime)
            review = updates.prepare(self.package)
        self.assertEqual((review["installedVersion"], review["version"]), ("0.1.0-alpha.10", "0.1.0"))
        self.assertEqual(file_state(self.package), before)
        self.assertEqual(file_state(runtime.parent), pinned_before)
        staged = self.package.parent / review["candidateId"] / "package"
        stable_before = file_state(staged)
        self.assertEqual({name.as_posix(): value[0] for name, value in stable_before.items()}, stable)
        with self.assertRaisesRegex(Invalid, "pinned_runtime"):
            updates.apply(runtime, review["candidateId"], confirmed=True)
        installed = updates.apply(self.package, review["candidateId"], confirmed=True)
        backup = self.package.parent / installed["backupId"]
        self.assertEqual((installed["previousVersion"], installed["version"]), ("0.1.0-alpha.10", "0.1.0"))
        self.assertEqual(file_state(self.package), stable_before)
        self.assertEqual(file_state(backup / "package"), before)
        self.assertEqual(file_state(runtime.parent), pinned_before)
        self.assertEqual(updates._version(runtime), "0.1.0-alpha.10")
        with self.network([release, self.release("0.2.0-alpha.1")]):
            checked = updates.check(self.package)
        self.assertEqual((checked["status"], checked["channel"]), ("current", "stable"))
        restored = updates.rollback(self.package, installed["backupId"], confirmed=True)
        self.assertEqual(restored["status"], "rolled_back")
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.10")
        self.assertEqual(file_state(self.package), before)
        self.assertEqual(file_state(backup / "replaced"), stable_before)
        self.assertEqual(file_state(runtime.parent), pinned_before)

    def test_stable_010_to_020_and_rollback_preserve_bytes_and_pinned_runtime(self):
        old = dict(self.package_content("0.1.0"), **{"assets/original.bin": b"\x00\xff010\r\n"})
        self.write_package(self.package, old)
        (self.package / "tools/aispa.py").chmod(0o755)
        runtime = self.root / "project/.aispa/runs/active-010/runtime"
        shutil.copytree(self.package, runtime)

        def file_state(directory):
            return {p.relative_to(directory): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                    for p in directory.rglob("*") if p.is_file()}

        before = file_state(self.package)
        pinned_before = file_state(runtime.parent)
        new = dict(self.package_content("0.2.0"), **{"assets/new.bin": b"\x00\xffnew\n"})
        release = self.release("0.2.0", files=new, modes={"tools/aispa.py": 0o755})
        with self.network([release]):
            self.assertEqual(updates.check(self.package)["status"], "available")
            self.assertEqual(updates.check(runtime)["installReason"], "pinned_runtime")
            review = updates.prepare(self.package)
        self.assertEqual((review["installedVersion"], review["version"]), ("0.1.0", "0.2.0"))
        installed = updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual((installed["previousVersion"], installed["version"]), ("0.1.0", "0.2.0"))
        self.assertEqual(file_state(runtime.parent), pinned_before)
        self.assertEqual(updates._version(runtime), "0.1.0")
        restored = updates.rollback(self.package, installed["backupId"], confirmed=True)
        self.assertEqual(restored["status"], "rolled_back")
        self.assertEqual(file_state(self.package), before)
        self.assertEqual(file_state(runtime.parent), pinned_before)

    def agent_bytes(self, name, version):
        return ("---\nname: %s\nmodel: sonnet\neffort: low\n---\n%s\n" % (name[:-3], version)).encode()

    def claude_install(self, old_agents):
        """A Claude Code skill install with its sibling agents directory."""
        self.package = self.root / "проект" / ".claude" / "skills" / "aispa"
        self.package.mkdir(parents=True)
        old = dict(self.old, **{"agents/claude-code/" + n: self.agent_bytes(n, "old") for n in old_agents})
        self.write_package(self.package, old)
        agents = self.package.parent.parent / "agents"
        agents.mkdir()
        (agents / "other.md").write_bytes(b"unrelated agent\n")
        return agents

    def agents_state(self, agents):
        return {p.name: p.read_bytes() for p in agents.iterdir()}

    def agent_release(self, names=("aispa-judge.md", "aispa-worker.md")):
        files = dict(self.package_content("0.1.0-alpha.3"),
                     **{"agents/claude-code/" + n: self.agent_bytes(n, "new") for n in names})
        return self.release("0.1.0-alpha.3", files=files)

    def test_apply_updates_own_agents_with_backup_and_rollback_restores_bytes(self):
        agents = self.claude_install(["aispa-judge.md", "aispa-worker.md"])
        for name in ("aispa-judge.md", "aispa-worker.md"):
            (agents / name).write_bytes(self.agent_bytes(name, "old"))
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        self.assertEqual(self.agents_state(agents), before)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        backup = self.package.parent / receipt["backupId"]
        self.assertEqual(receipt["agents"]["directory"], str(agents))
        self.assertEqual(receipt["agents"]["installed"], ["aispa-judge.md", "aispa-worker.md"])
        self.assertEqual(receipt["agents"]["skipped"], [])
        for name in ("aispa-judge.md", "aispa-worker.md"):
            self.assertEqual((agents / name).read_bytes(), self.agent_bytes(name, "new"))
            self.assertEqual((backup / "agents" / name).read_bytes(), before[name])
        self.assertEqual((agents / "other.md").read_bytes(), b"unrelated agent\n")
        restored = updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(restored["status"], "rolled_back")
        self.assertEqual(self.agents_state(agents), before)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")

    def test_apply_adds_missing_agents_and_rollback_moves_them_aside(self):
        agents = self.claude_install([])
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(receipt["agents"]["installed"], ["aispa-judge.md", "aispa-worker.md"])
        self.assertEqual((agents / "aispa-judge.md").read_bytes(), self.agent_bytes("aispa-judge.md", "new"))
        updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(self.agents_state(agents), before)
        moved = self.package.parent / receipt["backupId"] / "replaced-agents"
        self.assertEqual((moved / "aispa-judge.md").read_bytes(), self.agent_bytes("aispa-judge.md", "new"))

    def test_foreign_or_edited_agent_is_not_overwritten_or_rolled_back(self):
        agents = self.claude_install(["aispa-judge.md", "aispa-worker.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        edited = b"---\nname: aispa-worker\nmodel: opus\n---\nlocal edit\n"
        (agents / "aispa-worker.md").write_bytes(edited)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(receipt["agents"]["installed"], ["aispa-judge.md"])
        self.assertEqual(receipt["agents"]["skipped"], [{"file": "aispa-worker.md", "reason": "different"}])
        self.assertIn("aispa-worker.md", receipt["agents"]["reason"])
        self.assertEqual((agents / "aispa-worker.md").read_bytes(), edited)
        updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual((agents / "aispa-worker.md").read_bytes(), edited)
        self.assertEqual((agents / "aispa-judge.md").read_bytes(), self.agent_bytes("aispa-judge.md", "old"))

    def test_agent_edited_after_update_is_kept_by_rollback_and_named(self):
        agents = self.claude_install([])
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        (agents / "aispa-worker.md").write_bytes(b"edited after update\n")
        restored = updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual((agents / "aispa-worker.md").read_bytes(), b"edited after update\n")
        self.assertFalse((agents / "aispa-judge.md").exists())
        self.assertEqual(restored["agentsRollback"]["skipped"], [{"file": "aispa-worker.md", "reason": "changed"}])
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")

    def test_failed_agent_write_restores_package_and_agents(self):
        agents = self.claude_install(["aispa-judge.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        original_replace = updates.os.replace

        def replace(source, destination):
            if Path(destination).name == "aispa-worker.md" or Path(source).name.endswith("aispa-worker.md"):
                raise OSError("simulated agent write failure")
            return original_replace(source, destination)

        original_open = Path.open
        def open_(path, mode="r", *args, **kwargs):
            if path.name == "aispa-worker.md" and "x" in mode:
                raise OSError("simulated agent write failure")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(updates.os, "replace", side_effect=replace), \
             patch.object(Path, "open", open_), self.assertRaises(OSError):
            updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")
        self.assertEqual(updates._snapshot(self.package)[1], review["expectedCurrentDigest"])
        self.assertEqual(self.agents_state(agents), before)

    SIX_AGENTS = ("aispa-judge.md", "aispa-mechanic-scripted.md", "aispa-mechanic.md",
                  "aispa-researcher.md", "aispa-sensitive-worker.md", "aispa-worker.md")

    def interrupted_open(self, name):
        """Path.open whose exclusive write of `name` stores half the bytes and then fails."""
        original_open = Path.open

        class Interrupted:
            def __init__(self, stream):
                self.stream = stream

            def write(self, data):
                self.stream.write(data[:len(data) // 2])
                self.stream.flush()
                raise OSError("simulated interrupted agent write")

            def __getattr__(self, key):
                return getattr(self.stream, key)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()
                return False

        def open_(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return Interrupted(stream) if path.name == name and "x" in mode else stream
        return patch.object(Path, "open", open_)

    def test_check_names_missing_agents_and_confirmed_command_installs_them(self):
        # An install updated by the 0.4.0 apply has the new package but no agent files.
        agents = self.claude_install(list(self.SIX_AGENTS))
        edited = b"---\nname: aispa-worker\nmodel: opus\n---\nmy own worker\n"
        (agents / "aispa-worker.md").write_bytes(edited)
        before_package = updates._snapshot(self.package)[1]
        with self.network([]):
            result = updates.check(self.package)
        view = result["agents"]
        self.assertEqual(view["status"], "missing")
        self.assertEqual(view["directory"], str(agents))
        missing = [n for n in self.SIX_AGENTS if n != "aispa-worker.md"]
        self.assertEqual(view["missing"], missing)
        self.assertEqual(view["different"], ["aispa-worker.md"])
        for name in missing + ["aispa-worker.md"]:
            self.assertIn(name, view["message"])
        self.assertIn("update-agents --confirm", view["message"])
        self.assertEqual(sorted(p.name for p in agents.iterdir()), ["aispa-worker.md", "other.md"])
        with self.assertRaises(Invalid):
            updates.install_agents(self.package)
        self.assertEqual(sorted(p.name for p in agents.iterdir()), ["aispa-worker.md", "other.md"])
        receipt = updates.install_agents(self.package, confirmed=True)
        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(receipt["installed"], missing)
        self.assertEqual(receipt["skipped"], [{"file": "aispa-worker.md", "reason": "different"}])
        self.assertIn("aispa-worker.md", receipt["reason"])
        for name in self.SIX_AGENTS:
            self.assertTrue((agents / name).is_file())
        for name in missing:
            self.assertEqual((agents / name).read_bytes(), self.agent_bytes(name, "old"))
        self.assertEqual((agents / "aispa-worker.md").read_bytes(), edited)
        self.assertEqual((agents / "other.md").read_bytes(), b"unrelated agent\n")
        self.assertEqual(updates._snapshot(self.package)[1], before_package)
        with self.network([]):
            again = updates.check(self.package)["agents"]
        self.assertEqual((again["status"], again["missing"], again["different"]),
                         ("conflict", [], ["aispa-worker.md"]))
        (agents / "aispa-worker.md").write_bytes(self.agent_bytes("aispa-worker.md", "old"))
        with self.network([]):
            self.assertEqual(updates.check(self.package)["agents"]["status"], "complete")
        self.assertEqual(updates.install_agents(self.package, confirmed=True)["installed"], [])

    def test_receipt_write_failure_after_agents_restores_agents_and_package(self):
        agents = self.claude_install(["aispa-judge.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        original = updates.atomic_json
        calls = []

        def atomic_json(path, value, *args, **kwargs):
            if Path(path).name == "receipt.json":
                calls.append(value.get("status"))
                if len(calls) == 2:
                    raise OSError("simulated receipt write failure")
            return original(path, value, *args, **kwargs)

        with patch.object(updates, "atomic_json", side_effect=atomic_json), self.assertRaises(OSError):
            updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.agents_state(agents), before)
        self.assertEqual(updates._snapshot(self.package)[1], review["expectedCurrentDigest"])
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")

    def test_interrupted_new_agent_write_leaves_no_partial_file_and_retry_installs_it(self):
        agents = self.claude_install([])
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        with self.interrupted_open("aispa-worker.md"), self.assertRaises(OSError):
            updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(self.agents_state(agents), before)
        self.assertEqual(updates._snapshot(self.package)[1], review["expectedCurrentDigest"])
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(receipt["agents"]["installed"], ["aispa-judge.md", "aispa-worker.md"])
        self.assertEqual(receipt["agents"]["skipped"], [])
        self.assertEqual((agents / "aispa-worker.md").read_bytes(), self.agent_bytes("aispa-worker.md", "new"))

    def test_empty_current_agent_has_hex_previous_sha_and_rolls_back(self):
        agents = self.claude_install(["aispa-judge.md"])
        (self.package / "agents/claude-code/aispa-judge.md").write_bytes(b"")
        (agents / "aispa-judge.md").write_bytes(b"")
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        change = {item["file"]: item for item in receipt["agents"]["changes"]}["aispa-judge.md"]
        self.assertEqual(change["previousSha256"], hashlib.sha256(b"").hexdigest())
        saved = json.loads((self.package.parent / receipt["backupId"] / "receipt.json").read_text())
        self.assertEqual(saved["status"], "installed")
        self.assertEqual((agents / "aispa-judge.md").read_bytes(), self.agent_bytes("aispa-judge.md", "new"))
        updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual((agents / "aispa-judge.md").read_bytes(), b"")
        self.assertFalse((agents / "aispa-worker.md").exists())

    def failing_receipt(self, status):
        """atomic_json that fails when a receipt with `status` is written; `state["failed"]` marks it."""
        original = updates.atomic_json
        state = {"failed": False}

        def atomic_json(path, value, *args, **kwargs):
            if Path(path).name == "receipt.json" and value.get("status") == status and not state["failed"]:
                state["failed"] = True
                raise OSError("simulated receipt write failure")
            return original(path, value, *args, **kwargs)
        return patch.object(updates, "atomic_json", side_effect=atomic_json), state

    def test_failed_agent_undo_still_restores_package_and_names_unrestored_files(self):
        agents = self.claude_install(["aispa-judge.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt_patch, state = self.failing_receipt("installed")
        original_replace = updates._replace_file

        def replace_file(path, data):
            if state["failed"]:
                raise OSError("simulated disk full during undo")
            return original_replace(path, data)

        with receipt_patch, patch.object(updates, "_replace_file", side_effect=replace_file), \
             self.assertRaises(OSError) as caught:
            updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(str(caught.exception), "simulated receipt write failure")
        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("aispa-judge.md", notes)
        self.assertNotIn("aispa-worker.md", notes)
        self.assertEqual(updates._snapshot(self.package)[1], review["expectedCurrentDigest"])
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")
        self.assertFalse((agents / "aispa-worker.md").exists())
        backup = next(self.package.parent.glob(".aispa-backup-*"))
        self.assertIn(str(backup / "agents"), notes)
        self.assertEqual((backup / "agents" / "aispa-judge.md").read_bytes(), self.agent_bytes("aispa-judge.md", "old"))

    def test_rollback_receipt_failure_returns_agents_to_updated_state(self):
        agents = self.claude_install(["aispa-judge.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        before = self.agents_state(agents)
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        updated, digest = self.agents_state(agents), updates._snapshot(self.package)[1]
        receipt_patch, _ = self.failing_receipt("rolled_back")
        with receipt_patch, self.assertRaises(OSError) as caught:
            updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(str(caught.exception), "simulated receipt write failure")
        self.assertEqual(self.agents_state(agents), updated)
        self.assertEqual(updates._snapshot(self.package)[1], digest)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.3")
        saved = json.loads((self.package.parent / receipt["backupId"] / "receipt.json").read_text())
        self.assertEqual(saved["status"], "installed")
        restored = updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(restored["status"], "rolled_back")
        self.assertEqual(self.agents_state(agents), before)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.2")

    def test_failed_rollback_undo_still_restores_package_and_names_unrestored_files(self):
        agents = self.claude_install(["aispa-judge.md"])
        (agents / "aispa-judge.md").write_bytes(self.agent_bytes("aispa-judge.md", "old"))
        with self.network([self.agent_release()]):
            review = updates.prepare(self.package)
        receipt = updates.apply(self.package, review["candidateId"], confirmed=True)
        digest = updates._snapshot(self.package)[1]
        receipt_patch, state = self.failing_receipt("rolled_back")
        original_replace = updates.os.replace

        def replace(source, destination):
            if state["failed"] and Path(source).parent.name == "replaced-agents":
                raise OSError("simulated disk full during undo")
            return original_replace(source, destination)

        with receipt_patch, patch.object(updates.os, "replace", side_effect=replace), \
             self.assertRaises(OSError) as caught:
            updates.rollback(self.package, receipt["backupId"], confirmed=True)
        self.assertEqual(str(caught.exception), "simulated receipt write failure")
        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("aispa-judge.md", notes)
        self.assertIn("aispa-worker.md", notes)
        self.assertIn("replaced-agents", notes)
        self.assertEqual(updates._snapshot(self.package)[1], digest)
        self.assertEqual(updates._version(self.package), "0.1.0-alpha.3")

    def test_agents_install_and_check_say_a_new_session_is_needed(self):
        agents = self.claude_install(list(self.SIX_AGENTS))
        with self.network([]):
            view = updates.check(self.package)["agents"]
        self.assertEqual(view["status"], "missing")
        self.assertIn("новом сеансе Claude Code", view["message"])
        receipt = updates.install_agents(self.package, confirmed=True)
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(receipt["installed"], list(self.SIX_AGENTS))
        self.assertIn("новом сеансе Claude Code", receipt["message"])
        again = updates.install_agents(self.package, confirmed=True)
        self.assertEqual((again["status"], again["installed"]), ("installed", []))
        self.assertNotIn("новом сеансе", again["message"])
        self.assertTrue((agents / "aispa-worker.md").is_file())

    def test_install_agents_refuses_a_replacement_plan_without_touching_files(self):
        agents = self.claude_install(["aispa-judge.md"])
        stale = self.agent_bytes("aispa-judge.md", "stale")
        (agents / "aispa-judge.md").write_bytes(stale)
        before = self.agents_state(agents)
        original = updates._agent_sources
        calls = []

        def sources(package):
            # The package changes between the two reads of one plan.
            result = original(package)
            calls.append(package)
            return dict(result, **{"aispa-judge.md": stale}) if len(calls) == 2 else result

        with patch.object(updates, "_agent_sources", side_effect=sources), \
             self.assertRaisesRegex(Invalid, "только недостающие"):
            updates.install_agents(self.package, confirmed=True)
        self.assertEqual(self.agents_state(agents), before)

    def test_unknown_agents_status_in_check_names_directory(self):
        agents = self.claude_install(["aispa-judge.md"])
        with self.network([]), patch.object(updates, "agents_status", side_effect=OSError("boom")):
            view = updates.check(self.package)["agents"]
        self.assertEqual(view["status"], "unknown")
        self.assertIn("directory", view)
        self.assertEqual(view["directory"], str(agents))
        self.assertIn("boom", view["message"])

    def test_package_outside_claude_skills_does_not_touch_agents(self):
        sibling = self.package.parent.parent / "agents"
        sibling.mkdir()
        result = self.prepare()
        receipt = updates.apply(self.package, result["candidateId"], confirmed=True)
        self.assertNotIn("agents", receipt)
        self.assertEqual(list(sibling.iterdir()), [])

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
        skill = b'---\nname: aispa\nmetadata:\n  version: "0.1.0-alpha.3"\n---\n# AISpa\n'
        content = dict(self.package_content("0.1.0-alpha.3"), **{"SKILL.md": skill})
        content.update({name: b"fixture\n" for name in producer.REQUIRED if name not in content})
        self.write_package(repo / "skills/aispa", content)
        (repo / "LICENSE").write_bytes(b"MIT fixture\n")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "release")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "skills/aispa/SKILL.md").write_text("uncommitted edit", encoding="utf-8", newline="\n")
        first = producer.build_release(repo, self.root / "artifact-1")
        second = producer.build_release(repo, self.root / "artifact-2")
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(Path(first["manifest"]).read_bytes(), Path(second["manifest"]).read_bytes())
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertEqual(archive.read("aispa/SKILL.md"), skill)
            self.assertIn("aispa/LICENSE", archive.namelist())
        self.assertFalse(first["published"])
        with self.assertRaises(Invalid):
            producer.build_release(repo, self.root / "artifact-1")
        (repo / "skills/aispa/LICENSE").write_text("different notice", encoding="utf-8", newline="\n")
        for args in (("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "mismatched license")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        with self.assertRaises(Invalid):
            producer.build_release(repo, self.root / "artifact-bad")

    def test_stable_producer_package_prepares_applies_and_rolls_back(self):
        spec = importlib.util.spec_from_file_location("package_update_stable", ROOT / "tools/package_update.py")
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        repo = self.root / "stable-repo"
        repo.mkdir()
        content = dict(self.package_content("0.1.0"),
                       **{"SKILL.md": b'---\nname: aispa\nmetadata:\n  version: "0.1.0"\n---\n# AISpa\n'})
        content.update({name: b"fixture\n" for name in producer.REQUIRED if name not in content})
        self.write_package(repo / "skills/aispa", content)
        (repo / "LICENSE").write_bytes(b"MIT fixture\n")
        (repo / "tools").mkdir()
        installer = (ROOT / "tools/install.py").read_bytes()
        (repo / "tools/install.py").write_bytes(installer)
        for args in (("init", "-q"), ("add", "."), ("update-index", "--chmod=+x", "skills/aispa/tools/aispa.py"),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "stable fixture")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        first = producer.build_release(repo, self.root / "stable-artifact-1")
        second = producer.build_release(repo, self.root / "stable-artifact-2")
        self.assertEqual(first["version"], "0.1.0")
        self.assertFalse(first["published"])
        for key in ("archive", "manifest", "installer"):
            self.assertEqual(Path(first[key]).read_bytes(), Path(second[key]).read_bytes())
        self.assertEqual(Path(first["installer"]).read_bytes(), installer)
        self.assertEqual(first["installerSha256"], hashlib.sha256(installer).hexdigest())
        manifest = json.loads(Path(first["manifest"]).read_bytes())
        self.assertEqual(manifest["commit"], first["commit"])
        self.assertEqual(next(row["mode"] for row in manifest["files"] if row["path"] == "tools/aispa.py"), 0o755)
        release = self.release("0.1.0")
        for asset in release["assets"]:
            key = "archive" if asset["name"] == "aispa.zip" else "manifest"
            self.assets[asset["browser_download_url"]] = Path(first[key]).read_bytes()
        ref = f"https://api.github.com/repos/{updates.REPOSITORY}/git/ref/tags/v0.1.0"
        self.assets[ref] = json.dumps({"ref": "refs/tags/v0.1.0",
                                      "object": {"type": "commit", "sha": first["commit"]}}).encode()
        self.write_package(self.package, self.package_content("0.1.0-alpha.10"))
        before = updates._snapshot(self.package)
        with self.network([release]):
            review = updates.prepare(self.package)
        self.assertEqual(review["commit"], first["commit"])
        self.assertEqual(updates._snapshot(self.package), before)
        installed = updates.apply(self.package, review["candidateId"], confirmed=True)
        self.assertEqual(updates._version(self.package), "0.1.0")
        self.assertEqual(updates._snapshot(self.package)[0], updates._disk_records(updates._manifest(manifest, review)))
        updates.rollback(self.package, installed["backupId"], confirmed=True)
        self.assertEqual(updates._snapshot(self.package), before)


if __name__ == "__main__":
    unittest.main()
