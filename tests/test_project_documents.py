import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import project_documents as docs
from aispa_core.store import Invalid, Journal


class ProjectDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-documents-")
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.write(".gitignore", ".aispa/\nignored/\n")
        self.store = Journal(self.root)
        self.state = self.store.create("first", "Initial scope")
        self.state["policy"] = {"projectDocuments": True}

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def bind(self, role, name, reason="This existing document owns this project's purpose."):
        return docs.bind(self.store, self.state, role, name, hashlib.sha256((self.root / name).read_bytes()).hexdigest(), reason)

    def test_unique_root_and_docs_aliases_selected_without_document_changes(self):
        sources = {"INTENT.md": "# Project purpose\n", "docs/Интервью.md": "# Interview\n",
                   "Spec.MD": "# Specification\n", "docs/План разработки.md": "# Plan\n", "INDEX.md": "# Files\n"}
        for path, content in sources.items():
            self.write(path, content)
        view = docs.initialize(self.store, self.state)
        self.assertEqual({row["status"] for row in view["roles"].values()}, {"fresh"})
        for path, content in sources.items():
            self.assertEqual((self.root / path).read_text(encoding="utf-8"), content)
        self.assertEqual(len(self.state["projectDocuments"]["bindings"]), 5)
        docs.require_fresh_intent(self.store, self.state)

    def test_ambiguous_sources_not_arbitrarily_chosen_or_concatenated(self):
        self.write("INTENT.md", "# One intent\n")
        self.write("docs/замысел.md", "# Another intent\n")
        result = docs.initialize(self.store, self.state)
        self.assertEqual(result["roles"]["intent"]["status"], "ambiguous")
        self.assertNotIn("intent", self.state["projectDocuments"]["bindings"])
        with self.assertRaises(Invalid):
            docs.require_fresh_intent(self.store, self.state)
        self.bind("intent", "docs/замысел.md", "The owner identifies this document as the maintained project intent.")
        self.assertEqual(docs.view(self.store, self.state)["roles"]["intent"]["path"], "docs/замысел.md")

    def test_custom_analogue_can_share_one_document_with_explicit_role_reason(self):
        self.write("README.md", "# Purpose\nExisting project goal.\n# File map\n")
        docs.initialize(self.store, self.state)
        self.bind("intent", "README.md", "The Purpose section contains the project intent; retain this single source.")
        self.bind("index", "README.md", "The File map section is the existing map in the same source.")
        self.assertEqual(docs.required_paths(self.state), ["README.md"])
        self.assertFalse((self.root / "INTENT.md").exists())

    def test_absent_intent_requires_deliberate_authoring_never_automatic_file(self):
        before = set(self.root.iterdir())
        docs.initialize(self.store, self.state)
        self.assertEqual(set(self.root.iterdir()), before)
        with self.assertRaises(Invalid):
            docs.require_fresh_intent(self.store, self.state)
        docs.require_fresh_intent(self.store, self.state, require=False)
        docs.require_fresh_intent(self.store, {**self.state, "policy": {}})

    def test_changed_deleted_and_rebound_sources_are_freshness_sensitive(self):
        path = self.write("INTENT.md", "# Original\n")
        docs.initialize(self.store, self.state)
        old_fp = docs.fingerprint(self.store, self.state)
        old_basis = docs.accepted_basis(self.state)
        path.write_text("# Changed\n", encoding="utf-8")
        self.assertEqual(docs.view(self.store, self.state)["roles"]["intent"]["status"], "changed")
        self.assertNotEqual(docs.fingerprint(self.store, self.state), old_fp)
        self.assertEqual(docs.accepted_basis(self.state), old_basis)
        with self.assertRaises(Invalid):
            docs.require_fresh_intent(self.store, self.state)
        self.bind("intent", "INTENT.md")
        self.assertNotEqual(docs.accepted_basis(self.state), old_basis)
        self.assertEqual(len(self.state["projectDocuments"]["history"]), 2)
        path.unlink()
        self.assertEqual(docs.view(self.store, self.state)["roles"]["intent"]["status"], "missing")

    def test_read_views_and_idempotent_bind_do_not_write(self):
        self.write("INTENT.md", "# Purpose\n")
        docs.initialize(self.store, self.state)
        baseline = copy.deepcopy(self.state)
        files = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for _ in range(2):
            docs.discover(self.store)
            docs.view(self.store, self.state)
            docs.fingerprint(self.store, self.state)
            self.bind("intent", "INTENT.md")
        self.assertEqual(self.state, baseline)
        self.assertEqual(files, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_three_runs_keep_project_intent_without_previous_scope_or_pass(self):
        self.write("docs/замысел.md", "# Stable project purpose\n")
        docs.initialize(self.store, self.state)
        original = copy.deepcopy(self.state["projectDocuments"]["bindings"]["intent"])
        self.state["requirements"] = [{"id": "old"}]
        self.state["gates"] = {"accepted": "old-pass"}
        for i in (2, 3):
            self.state["status"] = "cancelled"
            self.store.append(self.state, "cancelled")
            previous = copy.deepcopy(self.state)
            state = self.store.create("run-" + str(i), "New narrow task")
            state["policy"] = {"projectDocuments": True}
            docs.initialize(self.store, state, previous)
            self.state = state
            self.assertEqual(self.state["requirements"], [])
            self.assertEqual(self.state["gates"], {})
            binding = state["projectDocuments"]["bindings"]["intent"]
            self.assertEqual(binding["id"], original["id"])
            self.assertEqual(binding["originRun"], original["originRun"])
            self.assertEqual(docs.view(self.store, state)["roles"]["intent"]["status"], "fresh")

    def test_inheritance_retains_accepted_sha_even_when_source_changed_or_missing(self):
        path = self.write("INTENT.md", "# Prior purpose\n")
        docs.initialize(self.store, self.state)
        sha = self.state["projectDocuments"]["bindings"]["intent"]["acceptedSha256"]
        self.state["status"] = "cancelled"
        self.store.append(self.state, "cancelled")
        previous = copy.deepcopy(self.state)
        path.write_text("# Externally changed\n", encoding="utf-8")
        self.state = self.store.create("new-run", "Other scope")
        docs.initialize(self.store, self.state, previous)
        self.assertEqual(self.state["projectDocuments"]["bindings"]["intent"]["acceptedSha256"], sha)
        self.assertEqual(docs.view(self.store, self.state)["roles"]["intent"]["status"], "changed")

    def test_bad_hash_and_unsafe_paths_rejected_without_registry_mutation(self):
        self.write("INTENT.md", "# Purpose\n")
        docs.initialize(self.store, self.state)
        before = copy.deepcopy(self.state)
        for name in ("../INTENT.md", "/INTENT.md", "C:/INTENT.md", ".aispa/context/brief.md", "docs/secret.md", "ignored/goal.md", "data/goal.md"):
            with self.subTest(name=name), self.assertRaises(Invalid):
                docs.bind(self.store, self.state, "intent", name, "0" * 64, "Existing purpose source")
        with self.assertRaises(Invalid):
            docs.bind(self.store, self.state, "intent", "INTENT.md", "0" * 64, "Existing purpose source")
        self.assertEqual(self.state, before)

    def test_sensitive_content_oversized_and_ignored_docs_never_bound(self):
        for content in ("api_key=sk-" + "a" * 32, "x" * (docs.MAX_FILE_BYTES + 1), "nul\0data"):
            self.write("INTENT.md", content)
            self.assertEqual(docs.discover(self.store)["roles"]["intent"][0]["status"], "unsafe")
        self.write("INTENT.md", "# Ignored purpose\n")
        self.write(".gitignore", ".aispa/\nINTENT.md\n")
        with patch.object(Path, "open", wraps=Path.open) as opened:
            # Guarded ignored discovery does not read the source body.
            self.assertEqual(docs.discover(self.store)["roles"]["intent"][0]["reason"], "ignored-by-git")
            self.assertEqual(opened.call_count, 0)
        docs.initialize(self.store, self.state)
        self.assertNotIn("intent", self.state["projectDocuments"]["bindings"])

    def test_symbolic_link_source_and_parent_forbidden(self):
        source = self.write("real.md", "# Real source\n")
        try:
            (self.root / "INTENT.md").symlink_to(source)
        except OSError as error:
            self.skipTest("OS does not permit symlink creation: " + str(error))
        self.assertEqual(docs.discover(self.store)["roles"]["intent"][0]["status"], "unsafe")
        with self.assertRaises(Invalid):
            self.bind("intent", "INTENT.md")
        (self.root / "docs").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Invalid):
            self.bind("intent", "docs/real.md")

    def test_unicode_custom_path_and_newline_bytes_preserved_in_snapshot(self):
        path = self.write("документы/Смысл проекта.md", "# Цель\nДля учеников.\n")
        raw = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(raw)
        docs.initialize(self.store, self.state)
        binding = self.bind("intent", "документы/Смысл проекта.md")
        self.assertEqual((self.store.run / binding["snapshot"]["file"]).read_bytes(), raw)
        self.assertEqual(path.read_bytes(), raw)

    def test_history_cannot_be_rewound_to_old_matching_manifest(self):
        self.write("INTENT.md", "# First purpose\n")
        docs.initialize(self.store, self.state)
        first = copy.deepcopy(self.state["projectDocuments"]["bindings"]["intent"])
        self.write("INTENT.md", "# New purpose\n")
        self.bind("intent", "INTENT.md")
        self.state["projectDocuments"]["bindings"]["intent"] = first
        with self.assertRaises(Invalid):
            docs.view(self.store, self.state)

    def test_old_history_artifacts_remain_verified_after_rebinding(self):
        self.write("INTENT.md", "# First purpose\n")
        docs.initialize(self.store, self.state)
        first = copy.deepcopy(self.state["projectDocuments"]["bindings"]["intent"])
        self.write("INTENT.md", "# New purpose\n")
        self.bind("intent", "INTENT.md")
        for key in ("snapshot", "manifest"):
            path = self.store.run / first[key]["file"]
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"corrupt")
                with self.subTest(key=key), self.assertRaises(Invalid):
                    docs.view(self.store, self.state)
                with self.assertRaises(Invalid):
                    docs.require_fresh_intent(self.store, self.state)
            finally:
                path.write_bytes(original)

    def test_nested_ignored_intent_is_an_obstacle_not_absence(self):
        self.write(".gitignore", ".aispa/\nDocs/internal/\n")
        self.write("Docs/internal/INTENT.md", "Private existing purpose\n")
        result = docs.initialize(self.store, self.state)
        row = result["roles"]["intent"]
        self.assertEqual(row["status"], "unsafe")
        self.assertEqual(row["candidates"][0]["path"], "Docs/internal/INTENT.md")
        self.assertNotIn("intent", self.state["projectDocuments"]["bindings"])
        self.assertFalse((self.root / "INTENT.md").exists())

    def test_credentials_and_empty_documents_never_become_snapshots(self):
        samples = ["123456789:" + "x" * 35, "Cookie: session=" + "a" * 40,
                   "session_token=" + "b" * 32, "Authorization: Bearer " + "c" * 40,
                   "eyJ" + "a" * 15 + "." + "b" * 15 + "." + "c" * 15, "\ufeff   \n"]
        docs.initialize(self.store, self.state)
        for text in samples:
            self.write("INTENT.md", text)
            with self.subTest(kind=text[:12]):
                self.assertEqual(docs.view(self.store, self.state)["roles"]["intent"]["status"], "unsafe")
                with self.assertRaises(Invalid):
                    self.bind("intent", "INTENT.md")
                self.assertFalse((self.store.run / "project-documents").exists())

    def test_ordinary_contact_text_is_not_treated_as_a_credential(self):
        self.write("INTENT.md", "# Purpose\nPublic contact: author@example.invalid\n")
        self.assertEqual(docs.initialize(self.store, self.state)["roles"]["intent"]["status"], "fresh")

    def test_snapshot_manifest_or_binding_tampering_is_rejected(self):
        self.write("INTENT.md", "# Purpose\n")
        docs.initialize(self.store, self.state)
        baseline = copy.deepcopy(self.state)
        binding = self.state["projectDocuments"]["bindings"]["intent"]
        for field in ("reason", "path", "acceptedSha256"):
            self.state = copy.deepcopy(baseline)
            self.state["projectDocuments"]["bindings"]["intent"][field] = "tampered"
            with self.subTest(field=field), self.assertRaises(Invalid):
                docs.view(self.store, self.state)
        self.state = baseline
        artifact = self.store.run / binding["snapshot"]["file"]
        artifact.write_text("modified evidence", encoding="utf-8")
        with self.assertRaises(Invalid):
            docs.view(self.store, self.state)


if __name__ == "__main__":
    unittest.main()
