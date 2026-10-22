"""Human project navigation: real sources, bounded memory, no generated duplicates."""
import copy
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import project_context as context, project_documents as documents
from aispa_core.store import Journal


class ProjectFileMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-file-map-")
        self.root = Path(self.temp.name) / "проект с пробелами 🙂"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.write(".gitignore", ".aispa/\n")
        self.store = Journal(self.root)
        self.state = self.store.create("map", "Карта проекта")
        self.state["policy"] = {"projectDocuments": True}
        self.context_root = self.store.home / "context"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def bind(self, role, name):
        return documents.bind(self.store, self.state, role, name,
                              hashlib.sha256((self.root / name).read_bytes()).hexdigest(),
                              "The named section in this existing document owns this role.")

    def start(self):
        documents.initialize(self.store, self.state)
        self.store.append(self.state, "synthetic-project-documents-initialized")
        context.initialize(self.store, self.state, "codex")
        self.store.append(self.state, "synthetic-project-context-initialized")
        context.refresh(self.store, self.state)

    def text(self, name="INDEX.md"):
        return (self.context_root / name).read_text(encoding="utf-8")

    def refresh(self):
        self.store.append(self.state, "synthetic-map-update")
        context.refresh(self.store, self.state)

    def links(self, text):
        return [(label, self.context_root / unquote(url))
                for label, url in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text)]

    def test_map_links_originals_and_separates_them_from_generated_views(self):
        originals = {"docs/Интервью.md": "# Interview\nSource only: interview details.\n",
                     "docs/Замысел проекта.md": "# Purpose\nSource only: persistent purpose.\n",
                     "docs/Спецификация.md": "# Spec\n", "docs/План разработки.md": "# Plan\n",
                     "README.md": "# Maintained file map\n"}
        for name, text in originals.items():
            self.write(name, text)
        self.start()
        self.bind("index", "README.md")
        self.refresh()
        text = self.text()
        self.assertLess(text.index("## Исходные документы проекта"), text.index("## Представления текущего прогона"))
        self.assertIn("| Роль | Файл проекта | Назначение | Когда читать | Состояние |", text)
        self.assertIn("не редактируйте их вручную", text)
        links = dict(self.links(text))
        for name, original in originals.items():
            self.assertEqual(links[name].resolve(), (self.root / name).resolve())
            self.assertEqual(links[name].read_text(encoding="utf-8"), original)
        self.assertNotIn("Source only:", text)
        self.assertNotIn("Source only:", self.text("memory.md"))
        self.assertIn("Замысел проекта:", self.text("memory.md"))
        self.assertIn("docs/Замысел проекта.md", self.text("memory.md"))
        for _label, target in self.links(text):
            self.assertTrue(target.is_file(), str(target))
        self.assertFalse((self.root / "INDEX.md").exists())
        self.assertFalse((self.root / "INTENT.md").exists())
        self.assertFalse((self.context_root / "intent.md").exists())

    def test_one_readme_can_serve_intent_and_index_without_duplicates(self):
        original = "# Purpose\nThe maintained goal.\n# File map\nExisting source locations.\n"
        self.write("README.md", original)
        self.start()
        for role in ("intent", "index"):
            self.bind(role, "README.md")
        self.refresh()
        text = self.text()
        self.assertIn("| Замысел проекта | [README.md](../../README.md)", text)
        self.assertIn("| Карта проекта | [README.md](../../README.md)", text)
        self.assertEqual((self.root / "README.md").read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "INDEX.md").exists())
        self.assertFalse((self.root / "INTENT.md").exists())

    def test_ambiguous_candidates_are_visible_but_never_chosen(self):
        self.write("INTENT.md", "# First possible intent\n")
        self.write("docs/замысел.md", "# Other possible intent\n")
        self.start()
        line = next(line for line in self.text().splitlines() if line.startswith("| Замысел проекта |"))
        self.assertIn("Не выбран: INTENT.md; docs/замысел.md", line)
        self.assertIn("Несколько кандидатов", line)
        self.assertNotIn("](", line)
        self.assertNotIn("intent", self.state["projectDocuments"]["bindings"])
        self.assertNotIn("[INTENT.md]", self.text("memory.md"))

    def test_changed_missing_unsafe_and_absent_are_explicit_and_stale_map_detected(self):
        path = self.write("INTENT.md", "# Initial purpose\n")
        self.start()
        self.assertTrue(context.info(self.store, self.state)["fresh"])
        path.write_text("# Changed purpose\n", encoding="utf-8")
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        context.refresh(self.store, self.state)
        self.assertIn("Изменён — перечитать", self.text())
        self.assertIn("[INTENT.md](../../INTENT.md)", self.text())
        path.unlink()
        self.assertFalse(context.info(self.store, self.state)["fresh"])
        context.refresh(self.store, self.state)
        self.assertIn("| Замысел проекта | INTENT.md |", self.text())
        self.assertIn("Файл не найден", self.text())
        self.assertNotIn("[INTENT.md]", self.text())
        path.write_bytes(b"\xff\x00")
        context.refresh(self.store, self.state)
        self.assertIn("Недопустимый источник", self.text())
        self.assertNotIn("[INTENT.md]", self.text())
        self.assertIn("Не сопоставлен", self.text())

    def test_refresh_changes_only_hidden_views_and_preserves_sources_and_rules(self):
        self.write("INTENT.md", "# Owner's purpose\nKeep exact bytes.\n")
        self.write("docs/Интервью.md", "# Existing interview\n")
        self.start()
        before_state = copy.deepcopy(self.state)
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file() and not p.is_relative_to(self.context_root)}
        for _ in range(2):
            context.refresh(self.store, self.state)
        self.assertEqual(before_state, self.state)
        self.assertEqual(before, {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                                 for p in self.root.rglob("*") if p.is_file() and not p.is_relative_to(self.context_root)})
        self.assertEqual(set(context.VIEWS), {p.name for p in self.context_root.iterdir()})

    def test_names_only_inventory_recognizes_aliases_without_reading_files(self):
        names = ["INTENT.md", "INTERVIEW.MD", "SPEC.md", "PLAN.md", "INDEX.md", "docs/Замысел проекта.md",
                 "docs/План_разработки.md", "docs/Карта файлов.md", "docs/Интервью.md", "docs/Спецификация.md"]
        for name in names:
            self.write(name, "# Existing document\n")
        with patch.object(Path, "open", side_effect=AssertionError("inventory must not read source contents")):
            found = context.inventory(self.root)
        self.assertEqual(set(found["documents"]), set(names))

    def test_memory_remains_bounded_and_does_not_expand_original_intent(self):
        self.write("docs/замысел.md", "# Purpose\n" + "Preserve this large source only in its original.\n" * 4000)
        self.start()
        self.state["title"] = "Large title " * 300
        self.state["tasks"] = [{"id": "T" + str(i), "title": "Long task " * 500, "status": "blocked", "note": "Long note " * 1000}
                               for i in range(100)]
        self.state["notes"] = ["Long decision " * 1000] * 50
        self.refresh()
        memory = self.text("memory.md")
        self.assertLessEqual(len(memory), context.MEMORY_LIMIT)
        self.assertIn("Замысел проекта:", memory)
        self.assertNotIn("Preserve this large source", memory)
        self.assertNotIn("Preserve this large source", self.text())

    def test_unbound_candidate_is_not_presented_as_adopted_and_legacy_has_no_map(self):
        self.start()
        self.write("INTENT.md", "# Found after initialization\n")
        context.refresh(self.store, self.state)
        self.assertIn("Найден — ещё не сопоставлен", self.text())
        self.assertNotIn("[INTENT.md]", self.text())
        self.state["policy"] = {}
        self.refresh()
        self.assertNotIn("## Исходные документы проекта", self.text())
        self.assertNotIn("Замысел проекта:", self.text("memory.md"))


if __name__ == "__main__":
    unittest.main()
