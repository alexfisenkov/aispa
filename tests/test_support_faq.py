"""Canonical support facts, source binding and actual packaged CLI consumption."""
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))
from aispa_core import VERSION, updates
from aispa_core.store import Invalid


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SupportFaqTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("aispa_core.support_faq"),
            "T05 requires a packaged source-bound FAQ consumer shared with the renderer")
        module = importlib.import_module("aispa_core.support_faq")
        self.assertTrue(callable(getattr(module, "select", None)))
        self.assertTrue(callable(getattr(module, "render", None)))
        return module

    def fixture(self, root):
        (root / "references").mkdir(parents=True)
        source = root / "tools/aispa_core/__init__.py"
        source.parent.mkdir(parents=True)
        source.write_text('VERSION = "0.4.0"\n', encoding="utf-8")
        value = {"schema": 1, "packageVersion": "0.4.0",
            "source": {"repository": "alexfisenkov/aispa", "files": [{"path": "tools/aispa_core/__init__.py", "sha256": sha(source.read_bytes())}]},
            "axes": {"questions": ["auto", "important", "interview"], "checks": ["fast", "balanced", "max"],
                     "interviewScopes": ["compact", "standard", "deep"], "independent": True},
            "entries": [{"id": "question-check-modes", "question": "Режимы связаны?", "answer": "Режим вопросов и проверок выбираются независимо."},
                        {"id": "ordinary-autonomy", "question": "Нужно разрешение между волнами?", "answer": "Обычные переходы продолжаются самостоятельно; <repair> требует исходного поручения."}]}
        path = root / "references/support-faq.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return value, path

    def test_selection_is_exact_version_source_bound_and_render_receipt_keeps_ids(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, path = self.fixture(root)
            selected = module.select(root, version="0.4.0", ids=["ordinary-autonomy"], expected_sha256=sha(path.read_bytes()))
            self.assertEqual([row["id"] for row in selected["entries"]], ["ordinary-autonomy"])
            self.assertEqual(selected["canonicalSha256"], sha(path.read_bytes()))
            rendered = module.render(selected, format="html")
            self.assertIn("&lt;repair&gt;", rendered["text"])
            self.assertNotIn("0.4.0", rendered["text"], "Visible learner text has no version label")
            self.assertEqual(rendered["receipt"]["entryIds"], ["ordinary-autonomy"])
            self.assertEqual(rendered["receipt"]["canonicalSha256"], sha(path.read_bytes()))
            self.assertEqual(rendered["receipt"]["outputSha256"], sha(rendered["text"].encode("utf-8")))
            self.assertEqual(rendered["receipt"]["packageVersion"], "0.4.0")
            self.assertEqual(len(rendered["receipt"]["sourceSha256"]), 64)
            self.assertEqual(module.render(selected, format="html"), rendered)
            self.assertNotIn("canonicalSha256", value)

    def test_schema_selection_tamper_and_private_metadata_fail_closed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, path = self.fixture(root)
            for kwargs in ({"version": "0.3.5"}, {"version": "0.4.0", "expected_sha256": "0" * 64},
                           {"version": "0.4.0", "ids": ["missing"]}, {"version": "0.4.0", "ids": ["ordinary-autonomy", "ordinary-autonomy"]}):
                with self.subTest(kwargs=kwargs), self.assertRaises(Invalid):
                    module.select(root, **kwargs)
            variants = []
            duplicate = copy.deepcopy(value); duplicate["entries"].append(copy.deepcopy(value["entries"][0])); variants.append(duplicate)
            schema = copy.deepcopy(value); schema["schema"] = 2; variants.append(schema)
            private = copy.deepcopy(value); private["studentMessages"] = []; variants.append(private)
            mixed = copy.deepcopy(value); mixed["entries"][0]["packageVersion"] = "0.3.5"; variants.append(mixed)
            escaped = copy.deepcopy(value); escaped["source"]["files"][0]["path"] = "../outside"; variants.append(escaped)
            for variant in variants:
                path.write_text(json.dumps(variant), encoding="utf-8")
                with self.subTest(variant=variant), self.assertRaises(Invalid):
                    module.select(root, version="0.4.0")
            path.write_text(json.dumps(value), encoding="utf-8")
            (root / "tools/aispa_core/__init__.py").write_text('VERSION = "0.4.0"\n# source changed\n')
            with self.assertRaises(Invalid):
                module.select(root, version="0.4.0")

    def test_actual_canonical_data_has_independent_axes_autonomy_and_pinned_recovery(self):
        module = self.module()
        path = PACKAGE / "references/support-faq.json"
        self.assertTrue(path.is_file(), "FAQ must ship inside the skill, not only repo docs")
        canonical = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(canonical["packageVersion"], VERSION)
        self.assertEqual(canonical["axes"]["questions"], ["auto", "important", "interview"])
        self.assertEqual(canonical["axes"]["checks"], ["fast", "balanced", "max"])
        self.assertEqual(canonical["axes"]["interviewScopes"], ["compact", "standard", "deep"])
        self.assertTrue(canonical["axes"]["independent"])
        selected = module.select(PACKAGE, version=VERSION)
        entries = {row["id"]: row for row in selected["entries"]}
        self.assertTrue({"question-check-modes", "ordinary-autonomy", "pinned-runtime", "audit-repair"} <= set(entries))
        self.assertIn("независим", entries["question-check-modes"]["answer"].lower())
        self.assertIn("самостоятель", entries["ordinary-autonomy"]["answer"].lower())
        self.assertIn("закрепл", entries["pinned-runtime"]["answer"].lower())
        self.assertIn("поручен", entries["audit-repair"]["answer"].lower())

    def test_root_renderer_uses_same_canon_and_checks_generated_drift(self):
        self.module()
        renderer = ROOT / "tools/render_support_faq.py"
        self.assertTrue(renderer.is_file(), "A deterministic renderer must use the installed canonical helper")
        with tempfile.TemporaryDirectory() as tmp:
            output, receipt = Path(tmp) / "faq.html", Path(tmp) / "receipt.json"
            argv = [sys.executable, "-B", str(renderer), "--package", str(PACKAGE), "--version", VERSION,
                    "--format", "html", "--output", str(output), "--receipt", str(receipt)]
            result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(receipt.read_bytes())
            self.assertEqual(evidence["canonicalSha256"], sha((PACKAGE / "references/support-faq.json").read_bytes()))
            self.assertEqual(evidence["outputSha256"], sha(output.read_bytes()))
            self.assertNotIn(VERSION, output.read_text(encoding="utf-8"))
            output.write_text("edited by hand", encoding="utf-8")
            checked = subprocess.run([*argv, "--check"], capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertNotEqual(checked.returncode, 0, "Generated drift must fail rather than silently rewrite")
            self.assertEqual(output.read_text(encoding="utf-8"), "edited by hand")

    def test_producer_install_and_real_cli_consume_exact_packaged_faq_without_repo_docs(self):
        self.module()
        canonical = PACKAGE / "references/support-faq.json"
        self.assertTrue(canonical.is_file(), "Packaged FAQ must exist before claiming a support consumer")
        producer_spec = importlib.util.spec_from_file_location("faq_release_producer", ROOT / "tools/package_update.py")
        producer = importlib.util.module_from_spec(producer_spec); producer_spec.loader.exec_module(producer)
        installer_spec = importlib.util.spec_from_file_location("faq_installer", ROOT / "tools/install.py")
        installer = importlib.util.module_from_spec(installer_spec); installer_spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory(prefix="FAQ package 🙂 ") as tmp:
            root = Path(tmp); repo = root / "source"
            shutil.copytree(PACKAGE, repo / "skills/aispa", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copyfile(ROOT / "LICENSE", repo / "LICENSE")
            (repo / "tools").mkdir(); shutil.copyfile(ROOT / "tools/install.py", repo / "tools/install.py")
            for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            built = producer.build_release(repo, root / "release")
            manifest = json.loads(Path(built["manifest"]).read_bytes())
            records = {row["path"]: row for row in manifest["files"]}
            self.assertEqual(records["references/support-faq.json"]["sha256"], sha(canonical.read_bytes()))
            self.assertEqual(records["tools/aispa_core/support_faq.py"]["sha256"], sha((PACKAGE / "tools/aispa_core/support_faq.py").read_bytes()))
            tag = "v" + VERSION
            prefix = f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/"
            assets = {prefix + "aispa.zip": Path(built["archive"]).read_bytes(), prefix + "aispa-update.json": Path(built["manifest"]).read_bytes()}
            release = {"draft": False, "prerelease": False, "published_at": "2026-10-26T00:00:00Z", "tag_name": tag,
                       "html_url": f"https://github.com/{updates.REPOSITORY}/releases/tag/{tag}",
                       "assets": [{"name": name, "state": "uploaded", "browser_download_url": prefix + name} for name in ("aispa.zip", "aispa-update.json")]}
            original_loader = installer._load_pinned_verifier
            def clone(tag, commit, destination, timeout):
                self.assertEqual(commit, built["commit"])
                shutil.copytree(repo, destination, ignore=shutil.ignore_patterns(".git"))
            def verifier(source):
                loaded = original_loader(source)
                loaded._download = lambda url, limit, deadline: assets[url]
                loaded._source_commit = lambda tag, deadline: built["commit"]
                return loaded
            destination = root / "installed/aispa"
            with patch.object(installer, "_fetch", return_value=json.dumps(release).encode()), \
                 patch.object(installer, "_source_commit", return_value=built["commit"]), \
                 patch.object(installer, "_clone_verified", side_effect=clone), \
                 patch.object(installer, "_load_pinned_verifier", side_effect=verifier):
                prepared = installer.preview(destination=destination, cache_dir=root / "cache", tag=tag)
            installer.apply(prepared["candidateId"], cache_dir=root / "cache", confirmed=True)
            self.assertEqual((destination / "references/support-faq.json").read_bytes(), canonical.read_bytes())
            self.assertFalse((destination / "docs").exists())
            self.assertFalse((destination / "tools/render_support_faq.py").exists())
            project = root / "existing project"; project.mkdir()
            result = subprocess.run([sys.executable, "-B", str(destination / "tools/aispa.py"), "--root", str(project),
                "faq", "--version", VERSION, "--id", "ordinary-autonomy"], cwd=project,
                capture_output=True, text=True, encoding="utf-8", timeout=15,
                env={**os.environ, "PYTHONPATH": ""})
            self.assertEqual(result.returncode, 0, "Installed support CLI must read its own packaged canonical source: " + result.stderr)
            selected = json.loads(result.stdout)
            self.assertEqual(selected["canonicalSha256"], sha(canonical.read_bytes()))
            self.assertEqual([row["id"] for row in selected["entries"]], ["ordinary-autonomy"])
            self.assertEqual(list(project.iterdir()), [])
