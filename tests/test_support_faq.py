"""Canonical support facts, source binding and actual packaged CLI consumption."""
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
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
from aispa_core.verified_reads import ResourceLimit


def sha(data):
    return hashlib.sha256(data).hexdigest()


# Decision 16 of the 0.5.0 spec: 15 new entries and 11 rewritten; decision D07 rewrites pinned-runtime too (story 87).
NEW_ENTRIES = {"scripts-instead-of-agents", "stopped-midway", "subagents-disabled", "decisions-after-brief",
               "interview-length", "brief-size", "large-old-run", "small-change", "test-form", "dashboard-no-data",
               "dashboard-stop", "when-to-use", "own-skills-and-rules", "backup", "how-to-launch"}
# Blind acceptance of the build (question about large requests): one entry added after decision 16.
REPAIR_ENTRIES = {"long-context"}
REWRITTEN_ENTRIES = {"question-check-modes", "ordinary-autonomy", "audit-repair", "first-install", "parallel-projects",
                     "updates-backup", "resume", "visible-controls", "models", "checks-and-cost", "evidence-boundaries",
                     "pinned-runtime"}
# sha256(answer) of every rewritten entry in the 0.4.0 canon:
# git show b914fad:skills/aispa/references/support-faq.json
PREVIOUS_ANSWER_SHA256 = {
    "question-check-modes": "28b8c413113199c452c9238b69db710ea1cb7116d10d98eeee4b73b2411259e2",
    "ordinary-autonomy": "eff405a341f14cb0179ea59cdb6be8a659a88102903c11b787bb0c500093f289",
    "pinned-runtime": "44f70301248be1a213171585d5660d45103fa228ae3c6b5104d731f8feded7bb",
    "audit-repair": "33540b6b890f46b39d279d4112a8dece95b55d641aaf3d3f0ac93cfbec576fb4",
    "first-install": "9039b785597fc068b9856de69f71bac5271ea374fefcee60b8f6e029f4d473ac",
    "parallel-projects": "d95a88ad2e1c5eaaf545413a6e92350dfe1d48cf00ac9f432468fc4c3c898910",
    "updates-backup": "f8649fbd27cff67307a1f5da6754e2de0aaff6ba82d28162dbdd21d0af0d7b9a",
    "resume": "a82c2e8f760157363a1b7a397f383648e8a49bb1d89c8ab02e6af7c12c1dbb3a",
    "visible-controls": "f8bc85389853799baca69104944c3c5a46c7eb44bdfe49edf76663610b05f21b",
    "models": "79b9f11571cdcd6fece05c8d5b7aedc0c8d0f67ee12b86fb8be414252b0c740c",
    "checks-and-cost": "0225b6c53dee8af5f010b617f968a3e1185643b8e4629e6708c2ad1885b84a78",
    "evidence-boundaries": "75b235ef0bb9babb67e9bd5100ae30f14b8c68ab8eb1d2770d457b9f1ab674c8",
}
# Files the answers speak about (story 96 and decision 16).
BOUND_FILES = {"tools/aispa_core/__init__.py", "SKILL.md", "prompts/worker.md", "phases/04-build.md",
               "references/model-routing.md", "references/updates.md", "tools/aispa_core/updates.py", "tools/aispa.py",
               "tools/aispa_core/preflight.py", "tools/aispa_core/autonomy.py", "phases/06-continuity.md",
               "references/dashboard-opening.md", "phases/02-product.md", "prompts/brief-analyst.md"}
HASH = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")
INTERNAL_ID = re.compile(r"(?<![\w-])[A-Z]\d{2}(?:\.\d+)?(?![\w])")
NICKNAME = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}")
JARGON = re.compile(r"(?i)\b(?:basis|receipts?|gates?|packets?|verdicts?|zoneChanges|closure)\b")
# Engine words a learner should not have to decode (story 87, decision D07).
ENGINE_WORDS = re.compile(r"(?i)\b(?:runtime|policy|policies|source\s+SHA|prechangeExecution)\b|контракт")
# Facts each answer must state, taken from the acceptance column of its story in the 0.5.0 spec.
STORY_FACTS = {
    "checks-and-cost": ("«Быстро»", "прогон", "лимит"),
    "scripts-instead-of-agents": ("командой или скриптом",),
    "stopped-midway": ("«продолжай»", "доступа или полномочия"),
    "subagents-disabled": ("настройках Codex", "Agent", "CLAUDE.md", "новый чат"),
    "interview-length": ("длительность", "допущения"),
    "brief-size": ("одно требование",),
    "large-old-run": ("256", "init", "тем же брифом"),
    "small-change": ("init", "«Быстро»"),
    "test-form": ("до первой правки", "один раз", "отклонит"),
    "pinned-runtime": ("закреплённой копией", "новый прогон"),
    "dashboard-stop": ("dashboard stop",),
    "resume": ("update-check", "resume"),
    "when-to-use": ("нескольких связанных", "видео", "обычным чатом"),
    "first-install": ("аккаунт", "токен", "gh", "PAT", "120", "терминал"),
    "parallel-projects": ("один корень Git — один прогон",),
    "updates-backup": ("после вашего выбора", "update-agents --confirm", "новом сеансе"),
    "backup": ("--tag", "MIT", "официальн", ".aispa/"),
    "audit-repair": ("осмотр", "адресный ремонт", "копи", "боевых ключей"),
    "how-to-launch": ("Claude Code", "/aispa", "Codex", "$aispa", "не поддерживается", "аккаунта ChatGPT",
                      "обновите Codex до последней версии", "консольный клиент", "новый чат"),
    "question-check-modes": ("«Сбалансированно»", "по умолчанию"),
    "models": ("Fable", "Astra", "неизвестно"),
    "visible-controls": ("кнопк", "правильный ввод"),
    "long-context": ("условиях своего тарифа", "только свою задачу", "изменения своей задачи", "новом чате",
                     "тот же прогон"),
}


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

    def test_selection_uses_one_operation_budget_across_source_files(self):
        from aispa_core import scale
        original = scale.Budget
        created = []

        def budget():
            instance = original()
            created.append(instance)
            return instance

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch.object(scale, "Budget", side_effect=budget):
                self.module().select(root, version="0.4.0")
        self.assertEqual(len(created), 1)

    def test_resource_limit_is_not_mislabeled_as_version_or_json_error(self):
        from aispa_core import scale
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            expired = scale.Budget()
            expired.deadline = -1.0
            with patch.object(scale, "Budget", return_value=expired):
                with self.assertRaises(ResourceLimit):
                    module.select(root, version="0.4.0")

            original = module._file

            def expire_after_canonical(package, name, *, budget=None):
                data = original(package, name, budget=budget)
                if name == "references/support-faq.json":
                    budget.deadline = -1.0
                return data

            with patch.object(module, "_file", side_effect=expire_after_canonical):
                with self.assertRaises(ResourceLimit):
                    module.select(root, version="0.4.0")

    def test_selection_checks_deadline_after_result_materialization(self):
        from aispa_core import scale
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            budget = scale.Budget()
            original = budget.check
            passed_last_check = False

            def expire_after_old_last_check(size=0, **kwargs):
                nonlocal passed_last_check
                result = original(size, **kwargs)
                if kwargs.get("path") == "FAQ selection" and not passed_last_check:
                    passed_last_check = True
                    budget.deadline = -1.0
                return result

            with patch.object(budget, "check", side_effect=expire_after_old_last_check), \
                    patch.object(scale, "Budget", return_value=budget):
                with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                    module.select(root, version="0.4.0")
            self.assertTrue(passed_last_check)

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

    def canonical_entries(self):
        # Content checks read the canon directly; select() and its source binding are checked above.
        canonical = json.loads((PACKAGE / "references/support-faq.json").read_text(encoding="utf-8"))
        return {row["id"]: row for row in canonical["entries"]}

    def test_actual_canonical_entries_follow_decision_16(self):
        entries = self.canonical_entries()
        self.assertEqual(set(entries), NEW_ENTRIES | REWRITTEN_ENTRIES | REPAIR_ENTRIES | {"pinned-runtime"})
        self.assertEqual(len(entries), 28)
        self.assertEqual(len(NEW_ENTRIES), 15)
        self.assertEqual(set(PREVIOUS_ANSWER_SHA256), REWRITTEN_ENTRIES)
        for row_id, previous in PREVIOUS_ANSWER_SHA256.items():
            with self.subTest(rewritten=row_id):
                self.assertNotEqual(sha(entries[row_id]["answer"].encode("utf-8")), previous,
                                    "Запись должна быть переписана относительно канона 0.4.0")

    def test_actual_canonical_binds_files_its_answers_describe(self):
        canonical = json.loads((PACKAGE / "references/support-faq.json").read_text(encoding="utf-8"))
        paths = {row["path"] for row in canonical["source"]["files"]}
        self.assertLessEqual(BOUND_FILES, paths)
        self.assertLessEqual(len(paths), 48)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((PACKAGE / path).is_file())

    def test_actual_canonical_answers_use_plain_language(self):
        for row_id, row in self.canonical_entries().items():
            text = row["question"] + "\n" + row["answer"]
            with self.subTest(entry=row_id):
                self.assertIsNone(HASH.search(text), "Хеши — не для ответа ученику")
                self.assertIsNone(INTERNAL_ID.search(text), "ID требований и задач — не для ответа ученику")
                self.assertIsNone(NICKNAME.search(text), "Ники в справочник не попадают")
                self.assertIsNone(JARGON.search(text), "Внутренние термины движка — не для ответа ученику")
                self.assertIsNone(ENGINE_WORDS.search(text), "Слова движка — не для ответа ученику")

    def test_actual_canonical_answers_cover_their_stories(self):
        entries = self.canonical_entries()
        for row_id, fragments in STORY_FACTS.items():
            answer = entries[row_id]["answer"].casefold()
            for fragment in fragments:
                with self.subTest(entry=row_id, fragment=fragment):
                    self.assertIn(fragment.casefold(), answer)

    def test_repo_markdown_is_generated_from_actual_canon(self):
        canonical = json.loads((PACKAGE / "references/support-faq.json").read_text(encoding="utf-8"))
        selected = {"packageVersion": canonical["packageVersion"], "canonicalSha256": "", "sourceSha256": "",
                    "source": canonical["source"], "entries": canonical["entries"]}
        rendered = self.module().render(selected, format="markdown")
        self.assertEqual((ROOT / "docs/support-faq.md").read_bytes(), rendered["text"].encode("utf-8"),
                         "docs/support-faq.md пересобирается только tools/render_support_faq.py")

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
