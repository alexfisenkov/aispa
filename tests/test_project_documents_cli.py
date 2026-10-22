"""Deterministic workflow boundaries; no model calls or new benchmark projects."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import contracts, discovery, ideas, project_documents, packets
from aispa_core.store import Journal, Invalid
from aispa_core.workflow import Workflow

CLI = ROOT / "skills/aispa/tools/aispa.py"


class ProjectDocumentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-project-docs-cli-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.cli = CLI

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root), *args],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout) if code == 0 else result

    def start(self, slug="first"):
        self.cli = CLI
        result = self.call("init", "--slug", slug, "--title", "Project docs", "--mode", "auto",
                           "--runtime", "codex", "--check", "balanced")
        self.cli = self.root / ".aispa" / result["runDirectory"] / "runtime/tools/aispa.py"
        store = Journal(self.root)
        return store, store.load()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_existing_lesson_documents_are_reused_and_persist_without_scope(self):
        originals = {name: self.write(name, "# " + name + "\nOriginal project source\n").read_bytes()
                     for name in ("INTENT.md", "INTERVIEW.md", "SPEC.md", "PLAN.md", "INDEX.md")}
        store, state = self.start()
        self.assertEqual(set(state["projectDocuments"]["bindings"]), set(project_documents.ROLES))
        self.assertEqual(state["briefs"], [])
        self.assertEqual(state["tasks"], [])
        self.assertIsNone(state["spec"])
        found = self.call("discover")["receipt"]
        scan = json.loads(Path(found["absolutePath"]).read_text())
        self.assertTrue(set(originals) <= {r["path"] for r in scan["sources"]})
        self.call("close", "cancelled", "--note", "End synthetic first run")
        store, next_state = self.start("second")
        self.assertEqual(next_state["projectDocuments"]["bindings"]["intent"]["path"], "INTENT.md")
        self.assertEqual(next_state["briefs"], [])
        self.assertEqual(next_state["requirements"], [])
        self.assertEqual(next_state["tasks"], [])
        for name, raw in originals.items():
            self.assertEqual((self.root / name).read_bytes(), raw)
        self.assertFalse((self.root / ".aispa/context/intent.md").exists())

    def test_missing_intent_needs_one_explicit_source_not_an_automatic_task_summary(self):
        self.start()
        self.assertFalse((self.root / "INTENT.md").exists())
        self.assertEqual(self.call("project-documents")["roles"]["intent"]["status"], "absent")
        result = self.call("seal", code=2)
        self.assertIn("замысел", result.stderr + result.stdout)
        path = self.write("docs/product-purpose.md", "# Product purpose\nLong-lived project goal.\n")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.call("project-documents", "--role", "intent", "--path", "docs/product-purpose.md",
                  "--sha256", sha, "--reason", "Existing product purpose, not the current task")
        self.assertEqual(self.call("project-documents")["roles"]["intent"]["status"], "fresh")
        self.assertFalse((self.root / "INTENT.md").exists())

    def test_intent_drift_and_rebinding_invalidate_scopes_and_task_packets(self):
        path = self.write("INTENT.md", "# Intent\nKeep project data local.\n")
        store, state = self.start()
        self.write("app.py", "print('fixture')\n")
        state["spec"] = store.artifact("specs", b"The fixture app must work.", ".md")
        state["tasks"] = [{"id": "T01", "title": "Change app", "requirements": [], "dependsOn": [],
                           "zones": ["app.py"], "criteria": ["Works"], "wave": 1, "status": "pending"}]
        pipeline = Workflow(store, state).pipeline
        pipeline.prepare()
        before_scope = contracts.scope_basis(state)
        before_idea = ideas.basis(state)
        before_task = contracts.task_basis(store, state, "T01")
        packet = packets.build(store, state, pipeline, "task", "T01", source_paths=["app.py"])
        self.assertNotIn("documentCatalog", packet)
        self.assertEqual(packet["projectIntent"]["path"], "INTENT.md")
        path.write_text("# Intent\nNew owner-approved project direction.\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            project_documents.require_fresh_intent(store, state)
        self.assertNotEqual(before_task, contracts.task_basis(store, state, "T01"))
        project_documents.bind(store, state, "intent", "INTENT.md", hashlib.sha256(path.read_bytes()).hexdigest(), "Source reread")
        self.assertNotEqual(before_scope, contracts.scope_basis(state))
        self.assertNotEqual(before_idea, ideas.basis(state))

    def test_custom_intent_is_a_common_read_dependency(self):
        self.write("docs/purpose.md", "Project direction\n")
        store, state = self.start()
        project_documents.bind(store, state, "intent", "docs/purpose.md",
                               hashlib.sha256((self.root / "docs/purpose.md").read_bytes()).hexdigest(), "Explicit analogue")
        state["requirements"] = [{"id": "R01", "scope": "active", "kind": "requested", "core": True}]
        tasks = [{"id": "T01", "title": "Intent", "requirements": ["R01"], "zones": ["docs/purpose.md"], "criteria": ["Aligned"], "dependsOn": []},
                 {"id": "T02", "title": "App", "requirements": ["R01"], "zones": ["app.py"], "criteria": ["Works"], "dependsOn": []}]
        with self.assertRaisesRegex(Invalid, "dependsOn"):
            contracts.validate_plan(state, tasks)
        tasks[1]["dependsOn"] = ["T01"]
        self.assertEqual(len(contracts.validate_plan(state, tasks)), 2)

    def test_refused_init_preserves_gitignore_before_private_snapshots(self):
        path = self.write(".gitignore", "cache/\n")
        args = ("--title", "Fixture", "--mode", "auto", "--runtime", "codex", "--check", "balanced")
        self.call("init", "--slug", "INVALID!", *args, code=2)
        self.assertEqual(path.read_text(), "cache/\n")
        self.start()
        path.write_text("cache/\n", encoding="utf-8")
        self.cli = CLI
        self.call("init", "--slug", "second", *args, code=2)
        self.assertEqual(path.read_text(), "cache/\n")

    def test_negated_ignore_and_tracked_history_refuse_init_without_snapshots(self):
        path = self.write(".gitignore", ".aispa/\n!.aispa/\n")
        self.write("INTENT.md", "Do not copy until private history is excluded.\n")
        args = ("init", "--slug", "private", "--title", "Fixture", "--mode", "auto", "--runtime", "codex", "--check", "balanced")
        self.call(*args, code=2)
        self.assertEqual(path.read_text(), ".aispa/\n!.aispa/\n")
        self.assertFalse((self.root / ".aispa/runs").exists())
        path.write_text(".aispa/\n", encoding="utf-8")
        self.write(".aispa/manual.txt", "tracked fixture")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".aispa/manual.txt"], check=True)
        self.call(*args, code=2)
        self.assertFalse((self.root / ".aispa/runs").exists())


if __name__ == "__main__":
    unittest.main()
