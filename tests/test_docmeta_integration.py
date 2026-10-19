import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "skills/aispa/tools"
sys.path.insert(0, str(TOOLS))
from aispa_core import discovery, docmeta, packets, project_context
from aispa_core.store import Invalid, Journal


class DocumentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-documents-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.write(".gitignore", ".aispa/\n")
        self.store = Journal(self.root)
        self.state = self.store.create("documents", "Document fixture")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def start(self):
        project_context.initialize(self.store, self.state, "codex")
        self.store.append(self.state, "context-initialized")
        project_context.refresh(self.store, self.state)

    def catalog(self):
        return {row["path"]: row for row in docmeta.catalog(self.store, self.state)["documents"]}

    def test_generated_headers_precede_source_map_hashes_without_changing_canon(self):
        original = "Exact owner request.\n  Keep whitespace.\n"
        spec = "# Specification\n\nExact specification body.\n"
        self.state["briefs"] = [{**self.store.artifact("briefs", original.encode(), ".md"), "id": "B001"}]
        self.state["spec"] = self.store.artifact("specs", spec.encode(), ".md")
        self.start()
        saved_state = copy.deepcopy(self.state)
        views = self.store.home / "context"
        source_map = json.loads((views / "source-map.json").read_text(encoding="utf-8"))
        for name in project_context.VIEW_SUMMARIES:
            raw = (views / name).read_bytes()
            self.assertTrue(raw.startswith(b"---\n"), name)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), source_map["views"][name])
        self.assertEqual(self.store.text(self.state["briefs"][0]), original)
        self.assertEqual(self.store.text(self.state["spec"]), spec)
        self.assertTrue((views / "spec.md").read_text(encoding="utf-8").endswith(spec))
        self.assertLessEqual(len((views / "memory.md").read_text(encoding="utf-8")), project_context.MEMORY_LIMIT)
        self.assertIn("documents.yaml", (views / "INDEX.md").read_text(encoding="utf-8"))
        canonical = json.loads((views / "documents.yaml").read_text(encoding="utf-8"))["canonicalSources"]
        self.assertEqual({row["kind"] for row in canonical}, {"brief", "spec"})
        self.assertTrue(all(row["immutable"] for row in canonical))
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in views.iterdir()}
        rules = (self.root / "AGENTS.md").read_bytes()
        project_context.refresh(self.store, self.state)
        self.assertEqual(saved_state, self.state)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in views.iterdir()})
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), rules)
        self.assertFalse(rules.startswith(b"---\n"))
        self.assertTrue(project_context.info(self.store, self.state)["fresh"])

    def test_collection_reuse_registers_missing_metadata_and_keeps_semantic_summary(self):
        path = self.write("README.md", "# Example\n\nRun the existing project.\n")
        original = path.read_bytes()
        first = discovery.collect(self.store, self.state)
        self.assertIn("README.md", self.catalog())
        self.state.pop("documents")
        second = discovery.collect(self.store, self.state)
        self.assertEqual(first, second)
        self.assertIn("README.md", self.catalog())
        docmeta.register(self.store, self.state, "README.md", expected_sha256=hashlib.sha256(original).hexdigest(),
                         summary="Instructions for running this fixture.")
        before = copy.deepcopy(self.state)
        artifacts = list((self.store.run / "documents").iterdir())
        discovery.collect(self.store, self.state)
        self.assertEqual(before, self.state)
        self.assertEqual(artifacts, list((self.store.run / "documents").iterdir()))
        self.assertEqual(self.catalog()["README.md"]["summary"], "Instructions for running this fixture.")
        self.assertEqual(path.read_bytes(), original)

    def test_generated_catalog_freshness_does_not_hide_stale_sources(self):
        path = self.write("README.md", "# Old title\n\nOld content.\n")
        self.start()
        docmeta.register(self.store, self.state, "README.md", expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         summary="Old semantic description.")
        self.store.append(self.state, "document-indexed")
        self.write("README.md", "# New title\n\nChanged content.\n")
        project_context.refresh(self.store, self.state)
        self.assertTrue(project_context.info(self.store, self.state)["fresh"])
        view = json.loads((self.store.home / "context/documents.yaml").read_text(encoding="utf-8"))
        row = next(row for row in view["documents"] if row["path"] == "README.md")
        self.assertEqual(row["status"], "stale")
        self.assertNotIn("summary", row)
        self.assertNotIn("Old semantic description.", json.dumps(view))

    def test_collection_reports_index_omission_without_changing_evidence_basis(self):
        self.write("README.md", "# Fixture\n\nA safe discovery source.\n")
        receipt = {}
        with patch.object(docmeta, "_source", side_effect=Invalid("Synthetic source drift")):
            evidence = discovery.collect(self.store, self.state, metadata_receipt=receipt)
        self.assertEqual(receipt["omitted"]["count"], 1)
        self.assertEqual(receipt["omitted"]["details"], [{"path": "README.md", "status": "not-registered"}])
        self.assertEqual(receipt["registered"], [])
        ref = copy.deepcopy(self.state["discoveryCollection"])
        self.assertNotIn("documentRegistration", evidence)
        repeated = discovery.collect(self.store, self.state, metadata_receipt=receipt)
        self.assertEqual(repeated, evidence)
        self.assertEqual(self.state["discoveryCollection"], ref)
        self.assertEqual(receipt["omitted"]["count"], 0)
        self.assertEqual([row["path"] for row in receipt["registered"]], ["README.md"])

    def test_task_catalog_filters_zones_reads_common_and_blind_omits_it(self):
        for name in ("src/main.py", "docs/relevant.md", "docs/unrelated.md", "AGENTS.md", "README.md"):
            self.write(name, "# Fixture\n\nSafe source text.\n")
            docmeta.register(self.store, self.state, name)
        task = {"id": "T01", "title": "Fixture", "requirements": [], "dependsOn": [],
                "zones": ["src"], "readZones": ["docs/relevant.md"], "criteria": ["Works"], "wave": 1, "status": "pending"}
        self.state["tasks"] = [task]
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "roles": {}, "actors": {}, "rounds": []}]}
        self.store.code = lambda *args, **kwargs: "synthetic-code"
        class Pipeline:
            def trace_requirements(self, _): return []
            def inherited_proofs(self, _): return []
        packet = packets.build(self.store, self.state, Pipeline(), "task", "T01")
        self.assertEqual({r["path"] for r in packet["documentCatalog"]["documents"]},
                         {"src/main.py", "docs/relevant.md", "AGENTS.md"})
        with patch("aispa_core.blind.basis", return_value="synthetic-blind-basis"):
            blind = packets.build(self.store, self.state, None, "blind")
        self.assertEqual(set(blind), {"kind", "basis", "briefs", "inheritedBriefs", "clone", "instructions"})
        self.assertNotIn("documentCatalog", blind)


class DocumentCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-documents-cli-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.path = self.root / "README.md"
        self.path.write_text("# Initial\n\nInitial body.\n", encoding="utf-8", newline="\n")
        self.call("init", "--slug", "documents", "--title", "Documents fixture", "--mode", "auto", "--runtime", "codex")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(TOOLS / "aispa.py"), "--root", str(self.root), *args],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8")) if success else result.stderr.decode("utf-8", "replace")

    def test_cli_index_read_refresh_and_header_compare_and_swap(self):
        raw = self.path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        self.call("document", "--path", "README.md", "--sha256", sha, "--summary", "Startup instructions.")
        store = Journal(self.root)
        store.load()
        before_journal = (store.run / "events.jsonl").read_bytes()
        rows = self.call("documents", "--path", "README.md")["documents"]
        self.assertEqual(rows[0]["status"], "fresh")
        self.assertEqual(rows[0]["summary"], "Startup instructions.")
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual((store.run / "events.jsonl").read_bytes(), before_journal)
        self.path.write_bytes(b"# Changed\n\nNew instructions.\n")
        stale = self.call("documents")["documents"][0]
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("summary", stale)
        self.call("document", "--path", "README.md", "--sha256", sha, "--write-header", success=False)
        changed = self.path.read_bytes()
        self.call("documents", "--refresh", "--path", "README.md")
        refreshed = self.call("documents")["documents"][0]
        self.assertEqual(refreshed["status"], "fresh")
        self.assertNotEqual(refreshed.get("summary"), "Startup instructions.")
        self.assertEqual(self.path.read_bytes(), changed)
        sha = hashlib.sha256(changed).hexdigest()
        self.call("document", "--path", "README.md", "--sha256", sha, "--summary", "Updated instructions.", "--write-header")
        self.assertTrue(self.path.read_bytes().startswith(b"---\n"))
        self.assertTrue(self.path.read_bytes().endswith(changed))
        self.assertEqual(self.call("documents")["documents"][0]["status"], "fresh")
        state = store.load()
        self.assertTrue(project_context.info(store, state)["fresh"])

    def test_cli_compact_detailed_and_known_source_dispatch(self):
        self.call("document", "--path", "README.md")
        compact = self.call("documents")
        full = self.call("documents", "--detailed")
        self.assertEqual(compact["format"], "compact")
        self.assertNotIn("sourceSha256", compact["documents"][0])
        self.assertEqual(full["format"], "detailed")
        self.assertIn("sourceSha256", full["documents"][0])
        self.assertIn("header", full["documents"][0])
        packet = self.call("context", "--kind", "coverage", "--source", "README.md")
        self.assertNotIn("documentCatalog", packet)
        self.assertEqual(packet["documentRouting"], {"catalog": "omitted", "reason": "explicit-files", "sources": ["README.md"]})
        self.assertEqual(packet["briefs"], [])
        self.call("context", "--kind", "coverage", "--source", ".", success=False)

    def test_read_only_catalog_allowed_but_mutations_block_runtime_drift(self):
        store = Journal(self.root)
        with store.lock():
            state = store.load()
            state["runtimeDigest"] = "different-runtime"
            store.append(state, "synthetic-runtime-drift")
        self.call("documents")
        self.assertIn("Пакет изменён", self.call("documents", "--refresh", success=False))
        self.assertIn("Пакет изменён", self.call("document", "--path", "README.md", success=False))

    def test_refresh_path_updates_only_selected_source_and_discovery_reports_index(self):
        other = self.root / "docs/other.md"
        other.parent.mkdir()
        other.write_text("# Other\n\nInitial other body.\n", encoding="utf-8", newline="\n")
        collected = self.call("discover")["receipt"]
        registration = collected["documentRegistration"]
        self.assertEqual(registration["omitted"]["count"], 0)
        self.assertIn("docs/other.md", {row["path"] for row in registration["registered"]})
        store = Journal(self.root)
        before = store.load()["documents"]["docs/other.md"]
        self.path.write_bytes(b"# Changed main\n\nNew main body.\n")
        other.write_bytes(b"# Changed other\n\nNew other body.\n")
        output = self.call("documents", "--refresh", "--path", "README.md")["receipt"]
        self.assertEqual([row["path"] for row in output["refresh"]["updated"]], ["README.md"])
        self.assertEqual(store.load()["documents"]["docs/other.md"], before)
        statuses = {row["path"]: row["status"] for row in self.call("documents")["documents"]}
        self.assertEqual(statuses["README.md"], "fresh")
        self.assertEqual(statuses["docs/other.md"], "stale")


if __name__ == "__main__":
    unittest.main()
