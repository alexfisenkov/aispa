"""Справочник для учеников: один канон, привязка к файлам пакета, простые слова, только действующий путь."""
import copy
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))
from aispa_core import VERSION
from aispa_core import support_faq
from aispa_core.store import Invalid


def sha(data):
    return hashlib.sha256(data).hexdigest()


# Ровно эти 25 записей: 22 под маршрут 1.0, одна о режимах, две для проектов с папкой .aispa/.
ENTRY_IDS = {
    "when-to-use", "how-to-launch", "first-install", "updates-backup", "backup", "ordinary-autonomy",
    "stopped-midway", "subagents-disabled", "checks-and-cost", "long-context", "scripts-instead-of-agents",
    "models", "visible-controls", "dashboard-stop", "resume", "small-change", "parallel-projects",
    "own-skills-and-rules", "evidence-boundaries", "interview-length", "brief-size", "decisions-after-brief",
    "modes", "pinned-runtime", "large-old-run",
}
DROPPED_IDS = {"question-check-modes", "test-form", "dashboard-no-data", "audit-repair"}
# Файлы пакета, о которых говорят ответы.
BOUND_FILES = {
    "tools/aispa_core/__init__.py", "SKILL.md", "tools/aispa.py", "tools/aispa_core/updates.py",
    "tools/aispa_core/support_faq.py", "tools/ap.py", "phases/0-modes.md", "phases/0-preflight.md",
    "phases/0-resume.md", "phases/0-instruments.md", "phases/2-briefing.md", "phases/4-plan.md",
    "phases/5-subagents.md", "phases/6-review.md", "phases/8-final.md", "prompts/executor.md",
    "prompts/review.md", "agents/openai.yaml",
}
HASH = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")
INTERNAL_ID = re.compile(r"(?<![\w-])[A-Z]\d{1,2}(?:\.\d+)?(?![\w])")
NICKNAME = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}")
JARGON = re.compile(r"(?i)\b(?:basis|receipts?|gates?|packets?|verdicts?|seal|assess|zoneChanges|closure)\b")
ENGINE_WORDS = re.compile(r"(?i)\b(?:runtime|policy|policies|source\s+SHA)\b|контракт")
# Снятое не называется: ни слов об истории, ни прежних режимов проверки.
REMOVED = re.compile(r"(?i)раньше|больше не|заменил|в прежней версии было|сбалансирован|«быстро»|«быстрый»"
                     r"|«максимум»|«максимальный»|режим[^.]{0,30}(?:быстр|максимальн)")
# Что должен сказать каждый ответ (ищется без учёта регистра).
FACTS = {
    "when-to-use": ("нескольких связанных", "видео", "обычным чатом"),
    "how-to-launch": ("/aispa", "$aispa", "Claude Code", "Codex", "не поддерживается для аккаунта ChatGPT",
                      "новый чат"),
    "first-install": ("аккаунт", "токен", "gh", "PAT", "120", "терминал", "после вашего «да»"),
    "updates-backup": ("обнови AISpa", "после вашего «да»", "update-agents --confirm", "посреди сборки"),
    "backup": ("--tag", "MIT", "официальн", ".aispa-work/"),
    "ordinary-autonomy": ("в том же ходе", "необратим", "включая полный автомат"),
    "stopped-midway": ("«продолжай»", "доступа или полномочия", "новую начинать не нужно"),
    "subagents-disabled": ("настройках Codex", "Agent", "CLAUDE.md", "новый чат"),
    "checks-and-cost": ("лимит", "одним шагом", "общая проверка", "ту же сборку"),
    "long-context": ("условиях своего тарифа", "только свою задачу", "изменения своего шага", "новый"),
    "scripts-instead-of-agents": ("командой или скриптом", "низким усилием"),
    "models": ("Opus", "Sonnet", "Haiku", "Fable", "Astra", "усилие не применено"),
    "visible-controls": ("кнопк", "другим способом", "частично", "без отправки настоящих"),
    "dashboard-stop": ("остановить дашборд", "подними дашборд"),
    "resume": ("без задачи", "продолжи AISpa", ".aispa-work/", "в другом окне"),
    "small-change": ("PROGRESS.md", "копию", "найди и почини", "без AISpa"),
    "parallel-projects": ("один корень Git", "лимит"),
    "own-skills-and-rules": ("CLAUDE.md", "AGENTS.md", "не меняет"),
    "evidence-boundaries": ("только свой сценарий", "чистой копии", "«Что нужно от тебя»"),
    "interview-length": ("до четырёх", "«ок»", "ничего вне вашего проекта"),
    "brief-size": ("одна проверяемая просьба", "вашими же словами", "может снять только вы"),
    "decisions-after-brief": ("«Дополнения»", "Решения, принятые за тебя", "стоит денег"),
    "modes": ("полный автомат", "полуавтомат", "интервью", "ручной", "глубина", "строго по брифу",
              "проработай глубоко", "в любой момент"),
    "pinned-runtime": (".aispa/", "закончи старую", "начни новую", ".aispa-work/"),
    "large-old-run": ("256", "начни новую", ".aispa-work/"),
}


def removed_paths():
    """Пути, которые опись отдаёт на удаление (тег 0.5), для проверки привязок."""
    path = ROOT / "docs/опись-0.5.md"
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    block = re.search(r"```опись:удалить\n(.*?)\n```", text, re.S)
    return {line.strip() for line in block.group(1).splitlines() if line.strip()} if block else set()


class SupportFaqTests(unittest.TestCase):
    def fixture(self, root):
        (root / "references").mkdir(parents=True)
        source = root / "tools/aispa_core/__init__.py"
        source.parent.mkdir(parents=True)
        source.write_text('VERSION = "1.0.0"\n', encoding="utf-8")
        value = {"schema": 1, "packageVersion": "1.0.0",
                 "source": {"repository": "alexfisenkov/aispa",
                            "files": [{"path": "tools/aispa_core/__init__.py", "sha256": sha(source.read_bytes())}]},
                 "axes": {"questions": ["auto", "important", "interview"], "checks": ["fast", "balanced", "max"],
                          "interviewScopes": ["compact", "standard", "deep"], "independent": True},
                 "entries": [{"id": "modes", "question": "Режимы?", "answer": "Режим и глубина выбираются отдельно."},
                             {"id": "ordinary-autonomy", "question": "Нужно разрешение?",
                              "answer": "Нет; <повтор> спросит только перед необратимым."}]}
        path = root / "references/support-faq.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return value, path

    def canon(self):
        return json.loads((PACKAGE / "references/support-faq.json").read_text(encoding="utf-8"))

    def entries(self):
        return {row["id"]: row for row in self.canon()["entries"]}

    # ── разбор и привязка: чужая или устаревшая версия не принимается ──

    def test_selection_is_exact_version_source_bound_and_render_keeps_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, path = self.fixture(root)
            selected = support_faq.select(root, version="1.0.0", ids=["ordinary-autonomy"],
                                          expected_sha256=sha(path.read_bytes()))
            self.assertEqual([row["id"] for row in selected["entries"]], ["ordinary-autonomy"])
            rendered = support_faq.render(selected, format="html")
            self.assertIn("&lt;повтор&gt;", rendered["text"])
            self.assertNotIn("1.0.0", rendered["text"], "В видимом тексте нет номера версии")
            self.assertEqual(rendered["receipt"]["entryIds"], ["ordinary-autonomy"])
            self.assertEqual(rendered["receipt"]["canonicalSha256"], sha(path.read_bytes()))
            self.assertEqual(rendered["receipt"]["outputSha256"], sha(rendered["text"].encode("utf-8")))
            self.assertNotIn("canonicalSha256", value)

    def test_wrong_version_sha_ids_schema_tamper_and_private_metadata_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, path = self.fixture(root)
            for kwargs in ({"version": "0.5.0"}, {"version": "1.0.0", "expected_sha256": "0" * 64},
                           {"version": "1.0.0", "ids": ["missing"]},
                           {"version": "1.0.0", "ids": ["modes", "modes"]}):
                with self.subTest(kwargs=kwargs), self.assertRaises(Invalid):
                    support_faq.select(root, **kwargs)
            variants = []
            duplicate = copy.deepcopy(value); duplicate["entries"].append(copy.deepcopy(value["entries"][0]))
            schema = copy.deepcopy(value); schema["schema"] = 2
            private = copy.deepcopy(value); private["studentMessages"] = []
            mixed = copy.deepcopy(value); mixed["entries"][0]["packageVersion"] = "0.5.0"
            escaped = copy.deepcopy(value); escaped["source"]["files"][0]["path"] = "../outside"
            for variant in (duplicate, schema, private, mixed, escaped):
                path.write_text(json.dumps(variant), encoding="utf-8")
                with self.subTest(variant=variant), self.assertRaises(Invalid):
                    support_faq.select(root, version="1.0.0")
            path.write_text(json.dumps(value), encoding="utf-8")
            (root / "tools/aispa_core/__init__.py").write_text('VERSION = "1.0.0"\n# source changed\n')
            with self.assertRaises(Invalid):
                support_faq.select(root, version="1.0.0")

    # ── канон: набор записей, версия, привязки ──

    def test_canon_has_exactly_the_25_entries_of_the_route(self):
        canon = self.canon()
        ids = [row["id"] for row in canon["entries"]]
        self.assertEqual(len(ids), 25)
        self.assertEqual(set(ids), ENTRY_IDS)
        self.assertFalse(DROPPED_IDS & set(ids), "Убранные записи не должны вернуться")
        self.assertEqual(set(FACTS), ENTRY_IDS, "Для каждой записи в тесте названы факты")

    def test_canon_version_is_1_0_0_and_installed_package_selects_it(self):
        self.assertEqual(self.canon()["packageVersion"], "1.0.0")
        self.assertEqual(VERSION, "1.0.0")
        selected = support_faq.select(PACKAGE, version=VERSION)
        self.assertEqual({row["id"] for row in selected["entries"]}, ENTRY_IDS)

    def test_bound_files_are_current_package_files_and_none_is_scheduled_for_removal(self):
        paths = [row["path"] for row in self.canon()["source"]["files"]]
        self.assertLessEqual(BOUND_FILES, set(paths))
        self.assertLessEqual(len(paths), 48)
        doomed = removed_paths()
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((PACKAGE / path).is_file())
                self.assertNotIn("skills/aispa/" + path, doomed, "Привязка ведёт к файлу, который уйдёт по описи")
        for row in self.canon()["source"]["files"]:
            with self.subTest(sha=row["path"]):
                self.assertEqual(row["sha256"], sha((PACKAGE / row["path"]).read_bytes()),
                                 "Привязка устарела: пересоберите справочник")

    # ── тексты ──

    def test_answers_use_plain_language(self):
        for row_id, row in self.entries().items():
            text = row["question"] + "\n" + row["answer"]
            with self.subTest(entry=row_id):
                self.assertIsNone(HASH.search(text), "Хеши — не для ответа ученику")
                self.assertIsNone(INTERNAL_ID.search(text), "Номера требований и шагов — не для ответа ученику")
                self.assertIsNone(NICKNAME.search(text), "Ники в справочник не попадают")
                self.assertIsNone(JARGON.search(text), "Внутренние слова движка — не для ответа ученику")
                self.assertIsNone(ENGINE_WORDS.search(text), "Слова движка — не для ответа ученику")

    def test_answers_show_only_the_current_path(self):
        for row_id, row in self.entries().items():
            text = row["question"] + "\n" + row["answer"]
            with self.subTest(entry=row_id):
                self.assertIsNone(REMOVED.search(text), "Ни истории, ни названий снятых режимов проверки")

    def test_each_answer_states_its_facts(self):
        entries = self.entries()
        for row_id, fragments in FACTS.items():
            answer = entries[row_id]["answer"].casefold()
            for fragment in fragments:
                with self.subTest(entry=row_id, fragment=fragment):
                    self.assertIn(fragment.casefold(), answer)

    def test_modes_entry_names_the_four_modes_and_the_depth_as_separate_dials(self):
        answer = self.entries()["modes"]["answer"].casefold()
        self.assertIn("две отдельные настройки", answer)
        for mode in ("полный автомат", "полуавтомат", "интервью", "ручной"):
            self.assertIn(mode, answer)

    # ── документ и инструмент ──

    def test_repo_markdown_is_generated_from_the_canon(self):
        canon = self.canon()
        selected = {"packageVersion": canon["packageVersion"], "canonicalSha256": "", "sourceSha256": "",
                    "source": canon["source"], "entries": canon["entries"]}
        rendered = support_faq.render(selected, format="markdown")
        self.assertEqual((ROOT / "docs/support-faq.md").read_bytes(), rendered["text"].encode("utf-8"),
                         "docs/support-faq.md пересобирается только tools/render_support_faq.py")

    def run_renderer(self, *extra, output, receipt, package=PACKAGE, version=VERSION):
        renderer = ROOT / "tools/render_support_faq.py"
        argv = [sys.executable, "-B", str(renderer), "--package", str(package), "--version", version,
                "--output", str(output), "--receipt", str(receipt), *extra]
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_renderer_check_passes_on_repo_doc_and_fails_on_hand_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipt.json"
            written = Path(tmp) / "support-faq.md"
            made = self.run_renderer(output=written, receipt=receipt)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertEqual(written.read_bytes(), (ROOT / "docs/support-faq.md").read_bytes())
            evidence = json.loads(receipt.read_bytes())
            self.assertEqual(evidence["canonicalSha256"], sha((PACKAGE / "references/support-faq.json").read_bytes()))
            self.assertEqual(self.run_renderer("--check", output=written, receipt=receipt).returncode, 0)
            written.write_text("правка руками", encoding="utf-8")
            checked = self.run_renderer("--check", output=written, receipt=receipt)
            self.assertNotEqual(checked.returncode, 0, "Расхождение должно падать, а не молча перезаписываться")
            self.assertEqual(written.read_text(encoding="utf-8"), "правка руками")

    # ── штатная пересборка привязок: --rebind ──

    def rebind_fixture(self, root):
        """Пакет с двумя привязанными файлами; возвращает путь к канону."""
        value, canon = self.fixture(root)
        (root / "SKILL.md").write_text("текст\n", encoding="utf-8")
        value["source"]["files"].append({"path": "SKILL.md", "sha256": sha((root / "SKILL.md").read_bytes())})
        canon.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return canon

    def test_rebind_restores_a_stale_canon_after_a_bound_file_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pkg"
            canon = self.rebind_fixture(root)
            out, receipt = Path(tmp) / "faq.md", Path(tmp) / "receipt.json"
            (root / "SKILL.md").write_text("изменённый текст\n", encoding="utf-8")
            with self.assertRaisesRegex(Invalid, "stale package source: SKILL.md"):
                support_faq.select(root, version="1.0.0")
            plain = self.run_renderer(output=out, receipt=receipt, package=root, version="1.0.0")
            self.assertNotEqual(plain.returncode, 0)
            self.assertIn("stale package source", plain.stderr)
            made = self.run_renderer("--rebind", output=out, receipt=receipt, package=root, version="1.0.0")
            self.assertEqual(made.returncode, 0, made.stderr)
            files = json.loads(canon.read_bytes())["source"]["files"]
            self.assertEqual([row["path"] for row in files], ["tools/aispa_core/__init__.py", "SKILL.md"])
            self.assertEqual(files[1]["sha256"], sha((root / "SKILL.md").read_bytes()))
            self.assertEqual(len(support_faq.select(root, version="1.0.0")["entries"]), 2)
            self.assertEqual(self.run_renderer("--check", output=out, receipt=receipt, package=root,
                                               version="1.0.0").returncode, 0)

    def test_rebind_refuses_a_missing_or_linked_path_and_leaves_the_canon_untouched(self):
        for kind in ("missing", "link"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "pkg"
                canon = self.rebind_fixture(root)
                before = canon.read_bytes()
                (root / "SKILL.md").unlink()
                if kind == "link":
                    (root / "SKILL.md").symlink_to(root / "tools/aispa_core/__init__.py")
                made = self.run_renderer("--rebind", output=Path(tmp) / "faq.md", receipt=Path(tmp) / "r.json",
                                         package=root, version="1.0.0")
                self.assertEqual(made.returncode, 2)
                self.assertIn("SKILL.md", made.stderr)
                self.assertEqual(canon.read_bytes(), before)
                self.assertFalse((Path(tmp) / "faq.md").exists())

    def test_rebind_on_a_fresh_canon_changes_nothing_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "aispa"
            shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
            canon = root / "references/support-faq.json"
            before = canon.read_bytes()
            made = self.run_renderer("--rebind", output=Path(tmp) / "faq.md", receipt=Path(tmp) / "r.json",
                                     package=root)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertEqual(canon.read_bytes(), before)

    def test_aispa_faq_command_returns_json_for_1_0_0(self):
        result = subprocess.run([sys.executable, "-B", str(PACKAGE / "tools/aispa.py"), "faq", "--version", "1.0.0"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = json.loads(result.stdout)
        self.assertEqual(selected["packageVersion"], "1.0.0")
        self.assertEqual({row["id"] for row in selected["entries"]}, ENTRY_IDS)
        old = subprocess.run([sys.executable, "-B", str(PACKAGE / "tools/aispa.py"), "faq", "--version", "0.5.0"],
                             capture_output=True, text=True, encoding="utf-8", timeout=30,
                             env={**os.environ, "PYTHONPATH": ""})
        self.assertNotEqual(old.returncode, 0, "Ответы 1.0 не выдаются за ответы другой версии")

    def test_copied_package_answers_from_its_own_canon_without_repo_docs(self):
        producer_spec = importlib.util.spec_from_file_location("faq_release_producer", ROOT / "tools/package_update.py")
        producer = importlib.util.module_from_spec(producer_spec)
        producer_spec.loader.exec_module(producer)
        canonical = PACKAGE / "references/support-faq.json"
        with tempfile.TemporaryDirectory(prefix="FAQ пакет ") as tmp:
            root = Path(tmp)
            repo = root / "source"
            shutil.copytree(PACKAGE, repo / "skills/aispa", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copyfile(ROOT / "LICENSE", repo / "LICENSE")
            for args in (("init", "-q"), ("add", "."),
                         ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            built = producer.build_release(repo, root / "release")
            manifest = json.loads(Path(built["manifest"]).read_bytes())
            records = {row["path"]: row for row in manifest["files"]}
            self.assertEqual(records["references/support-faq.json"]["sha256"], sha(canonical.read_bytes()))
            project = root / "проект"
            project.mkdir()
            result = subprocess.run([sys.executable, "-B", str(repo / "skills/aispa/tools/aispa.py"), "faq",
                                     "--version", VERSION, "--id", "ordinary-autonomy"], cwd=project,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                                    env={**os.environ, "PYTHONPATH": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            selected = json.loads(result.stdout)
            self.assertEqual(selected["canonicalSha256"], sha(canonical.read_bytes()))
            self.assertEqual([row["id"] for row in selected["entries"]], ["ordinary-autonomy"])
            self.assertEqual(list(project.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
