import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import discovery
from aispa_core.store import Invalid, Journal


def synthetic_report(evidence):
    """Minimal test fixture, explicitly not a claim of real project analysis."""
    observations = []
    if evidence["sources"]:
        source = evidence["sources"][0]
        observations.append({"area": "architecture", "text": "Synthetic fixture has this collected source; no architecture inferred.",
                             "sources": [{"path": source["path"], "sha256": source["sha256"]}]})
    covered = {row["area"] for row in observations}
    return {"basis": evidence["basis"], "summary": "Synthetic test-only discovery evidence.",
            "observations": observations,
            "uncertainties": [{"area": area, "text": "Not established by this synthetic fixture."}
                              for area in discovery.AREAS if area not in covered]}


def record_synthetic_discovery(store, state):
    """Synthetic test setup; caller still owns the journal append."""
    return discovery.record(store, state, synthetic_report(discovery.collect(store, state)))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-discovery-")
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.write(".gitignore", ".aispa/\nignored/\n")
        self.write("README.md", "# Fixture\n\nCLI in src/main.py uses a repository layer.\nRun `python3 -m unittest discover -s tests`.\n")
        self.write("src/main.py", "from .repository import load\n\ndef main():\n    return load()\n")
        self.write("src/repository.py", "def load():\n    return []\n")
        self.write("package.json", json.dumps({"scripts": {"test": "node --test tests/*.test.cjs"}, "dependencies": {"fixture": "1.0"}}))
        self.store = Journal(self.root)
        self.state = self.store.create("example", "Fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def collect(self):
        return discovery.collect(self.store, self.state)

    def test_collection_reads_real_evidence_and_exact_hashes(self):
        evidence = self.collect()
        sources = {row["path"]: row for row in evidence["sources"]}
        self.assertIn("repository layer", sources["README.md"]["excerpt"])
        self.assertIn("return load()", sources["src/main.py"]["excerpt"])
        for path, source in sources.items():
            self.assertEqual(source["sha256"], hashlib.sha256((self.root / path).read_bytes()).hexdigest())
        self.assertIn({"command": "node --test tests/*.test.cjs", "origin": "scripts.test"}, sources["package.json"]["commands"])
        self.assertIn({"command": "python3 -m unittest discover -s tests", "origin": "document-inline"}, sources["README.md"]["commands"])

    def test_reuse_current_snapshot_does_not_duplicate_artifacts_or_journal(self):
        before = (self.store.run / "events.jsonl").read_bytes()
        evidence = self.collect()
        collection = dict(self.state["discoveryCollection"])
        snapshot = discovery.record(self.store, self.state, synthetic_report(evidence))
        paths = sorted((self.store.run / "discovery").iterdir())
        self.assertEqual(discovery.collect(self.store, self.state), evidence)
        self.assertEqual(collection, self.state["discoveryCollection"])
        self.assertEqual(discovery.record(self.store, self.state, synthetic_report(evidence)), snapshot)
        self.assertEqual(sorted((self.store.run / "discovery").iterdir()), paths)
        self.assertEqual(before, (self.store.run / "events.jsonl").read_bytes())
        self.assertTrue(discovery.valid(self.store, self.state))
        view = discovery.view(self.store, self.state)
        self.assertTrue(view["fresh"])
        self.assertNotIn("excerpt", view["sources"][0])
        self.assertEqual(view["observations"], synthetic_report(evidence)["observations"])

    def test_named_secrets_and_user_data_never_open_even_when_tracked(self):
        names = [".env", ".env.production", "credentials/README.md", "docs/api-key.md", "docs/passwords.md",
                 "data/README.md", "users/README.md", "archive/README.md", "vendor/package.json", ".hidden/README.md",
                 "private.pem"]
        for name in names:
            self.write(name, "PRIVATE_VALUE_NEVER_CAPTURE")
        self.git("add", "-f", *names)
        original = Path.open
        forbidden = {self.root / name for name in names}
        def guarded(path, *args, **kwargs):
            if path in forbidden:
                self.fail("Opened forbidden source: " + str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            evidence = self.collect()
            record_synthetic_discovery(self.store, self.state)
            self.assertTrue(discovery.valid(self.store, self.state))
        self.assertFalse(set(names) & {row["path"] for row in evidence["sources"]})
        for path in (self.store.run / "discovery").iterdir():
            self.assertNotIn("PRIVATE_VALUE_NEVER_CAPTURE", path.read_text(encoding="utf-8"))

    def test_embedded_secret_material_is_not_copied(self):
        self.write("docs/setup.md", "api_key = '" + "sk-" + "fixtureSecret0123456789abcdefgh'\n")
        evidence = self.collect()
        self.assertNotIn("docs/setup.md", [row["path"] for row in evidence["sources"]])
        self.assertEqual(evidence["omitted"]["counts"]["sensitive-content"], 1)
        self.assertNotIn("sk-fixtureSecret", self.store.text(self.state["discoveryCollection"]))

    def test_ignored_tracked_file_is_not_read(self):
        path = self.write("ignored/README.md", "IGNORED_VALUE")
        self.git("add", "-f", "ignored/README.md")
        original = Path.open
        def guarded(candidate, *args, **kwargs):
            if candidate == path:
                self.fail("Opened ignored source")
            return original(candidate, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            evidence = self.collect()
        self.assertNotIn("ignored/README.md", [row["path"] for row in evidence["sources"]])

    def test_symlink_source_and_parent_cannot_escape(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)
            (outside / "README.md").write_text("EXTERNAL_VALUE", encoding="utf-8", newline="\n")
            (self.root / "architecture.md").symlink_to(outside / "README.md")
            (self.root / "doc").symlink_to(outside, target_is_directory=True)
            with patch.object(discovery, "_names", return_value=["architecture.md", "doc/README.md"]):
                evidence = self.collect()
            self.assertEqual(evidence["sources"], [])
            self.assertEqual(evidence["omitted"]["counts"]["unavailable-or-symlink"], 2)
            self.assertNotIn("EXTERNAL_VALUE", self.store.text(self.state["discoveryCollection"]))

    def test_capture_is_bounded_and_every_omission_is_counted(self):
        for i in range(12):
            self.write(f"docs/guide-{i:02}.md", "A" * 100)
        with patch.multiple(discovery, MAX_SOURCES=3, MAX_EXCERPT_CHARS=20, MAX_TOTAL_CHARS=45, MAX_OMITTED_DETAILS=2):
            evidence = self.collect()
        self.assertLessEqual(len(evidence["sources"]), 3)
        self.assertLessEqual(sum(len(row["excerpt"]) for row in evidence["sources"]), 45)
        self.assertTrue(any(row["omittedChars"] > 0 for row in evidence["sources"]))
        omitted = evidence["omitted"]
        self.assertEqual(sum(omitted["counts"].values()) + len(evidence["sources"]), evidence["candidateCount"])
        self.assertEqual(len(omitted["details"]) + omitted["unlisted"], sum(omitted["counts"].values()))

    def test_small_budget_keeps_representative_docs_manifest_and_code(self):
        for i in range(12):
            self.write(f"docs/guide-{i:02}.md", "A" * 100)
        with patch.multiple(discovery, MAX_SOURCES=3, MAX_EXCERPT_CHARS=20, MAX_TOTAL_CHARS=60):
            evidence = self.collect()
        self.assertEqual({row["kind"] for row in evidence["sources"]}, {"document", "manifest", "code"})

    def test_oversized_binary_and_missing_candidates_are_explicit(self):
        self.write("docs/big.md", "x" * (discovery.MAX_FILE_BYTES + 1))
        (self.root / "docs/binary.md").write_bytes(b"\0binary")
        self.write("docs/deleted.md", "Old source")
        self.git("add", "docs/deleted.md")
        (self.root / "docs/deleted.md").unlink()
        evidence = self.collect()
        for reason in ("oversized", "binary", "missing"):
            self.assertEqual(evidence["omitted"]["counts"][reason], 1)

    def test_source_changes_deletion_and_new_relevant_files_stale_snapshot(self):
        record_synthetic_discovery(self.store, self.state)
        self.write("src/main.py", "print('changed')\n")
        self.assertFalse(discovery.valid(self.store, self.state))
        self.assertTrue(discovery.valid(self.store, self.state, check_sources=False))
        self.assertFalse(discovery.view(self.store, self.state)["fresh"])
        record_synthetic_discovery(self.store, self.state)
        self.write("docs/architecture.md", "New architecture source")
        self.assertFalse(discovery.valid(self.store, self.state))
        record_synthetic_discovery(self.store, self.state)
        (self.root / "README.md").unlink()
        self.assertFalse(discovery.valid(self.store, self.state))

    def test_changed_omitted_source_also_requires_refresh(self):
        self.write("docs/z.md", "omitted source v1")
        with patch.object(discovery, "MAX_SOURCES", 1):
            record_synthetic_discovery(self.store, self.state)
            self.write("docs/z.md", "omitted source v2")
            self.assertFalse(discovery.valid(self.store, self.state))

    def test_excluded_or_unrelated_file_does_not_stale_snapshot(self):
        record_synthetic_discovery(self.store, self.state)
        self.write(".env", "TOKEN=private")
        self.write("uploads/user.json", '{"personal": true}')
        self.write("src/unrelated.py", "pass\n")
        self.assertTrue(discovery.valid(self.store, self.state))

    def test_report_cannot_reference_missing_source_wrong_hash_or_uncollected_quote(self):
        evidence = self.collect()
        for changes in ({"path": "missing.md"}, {"sha256": "0" * 64}, {"excerpt": "invented quote"}):
            report = synthetic_report(evidence)
            report["observations"][0]["sources"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                discovery.record(self.store, self.state, report)
        self.assertNotIn("discovery", self.state)

    def test_report_requires_all_areas_and_rejects_unfounded_command(self):
        evidence = self.collect()
        report = synthetic_report(evidence)
        report["uncertainties"] = []
        with self.assertRaisesRegex(Invalid, "не все области"):
            discovery.record(self.store, self.state, report)
        report = synthetic_report(evidence)
        report["observations"].append({"area": "commands", "text": "Run tests", "command": "npm test",
                                       "sources": copy.deepcopy(report["observations"][0]["sources"])})
        with self.assertRaisesRegex(Invalid, "команда"):
            discovery.record(self.store, self.state, report)
        source = next(row for row in evidence["sources"] if row["path"] == "package.json")
        report["observations"][-1].update(command="node --test tests/*.test.cjs",
            sources=[{"path": source["path"], "sha256": source["sha256"]}])
        discovery.record(self.store, self.state, report)
        self.assertTrue(discovery.valid(self.store, self.state))

    def test_cannot_record_after_collection_source_drift(self):
        evidence = self.collect()
        self.write("README.md", "Changed")
        with self.assertRaisesRegex(Invalid, "источники изменились"):
            discovery.record(self.store, self.state, synthetic_report(evidence))

    def test_artifact_tampering_is_not_valid(self):
        record_synthetic_discovery(self.store, self.state)
        ref = self.state["discovery"]["collection"]
        (self.store.run / ref["file"]).write_text("{}", encoding="utf-8", newline="\n")
        self.assertFalse(discovery.valid(self.store, self.state, check_sources=False))
        self.assertFalse(discovery.view(self.store, self.state)["integrityValid"])

    def test_collection_artifact_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            (self.store.run / "discovery").symlink_to(other, target_is_directory=True)
            with self.assertRaises(Invalid):
                self.collect()
            self.assertEqual(list(Path(other).iterdir()), [])

    def test_empty_project_is_honest_uncertainty_not_fabricated_architecture(self):
        for name in ("README.md", "src/main.py", "src/repository.py", "package.json"):
            (self.root / name).unlink()
        record_synthetic_discovery(self.store, self.state)
        view = discovery.view(self.store, self.state)
        self.assertEqual(view["observations"], [])
        self.assertEqual(len(view["uncertainties"]), len(discovery.AREAS))
        self.assertTrue(view["fresh"])

    def test_explicit_business_and_test_sources_persist_and_stale_on_change(self):
        self.write("src/order_totals.py", "def total(items):\n    return sum(items)\n")
        self.write("tests/test_order_totals.py", "def test_total():\n    assert True\n")
        self.write("notes/domain-rules.md", "Orders contain independent lines.")
        includes = ["src/order_totals.py", "tests/test_order_totals.py", "notes/domain-rules.md"]
        evidence = discovery.collect(self.store, self.state, paths=includes)
        self.assertEqual(evidence["includedPaths"], sorted(includes))
        self.assertTrue(set(includes) <= {row["path"] for row in evidence["sources"]})
        discovery.record(self.store, self.state, synthetic_report(evidence))
        self.assertTrue(discovery.valid(self.store, self.state))
        before = dict(self.state["discoveryCollection"])
        self.assertEqual(discovery.collect(self.store, self.state)["includedPaths"], sorted(includes))
        self.assertEqual(before, self.state["discoveryCollection"])
        self.write("src/order_totals.py", "def total(items):\n    return 0\n")
        self.assertFalse(discovery.valid(self.store, self.state))
        self.assertTrue(discovery.valid(self.store, self.state, check_sources=False))
        evidence = discovery.collect(self.store, self.state)
        discovery.record(self.store, self.state, synthetic_report(evidence))
        self.write("docs/new-design.md", "New automatically selected source")
        self.assertFalse(discovery.valid(self.store, self.state))
        self.assertEqual(discovery.collect(self.store, self.state, paths=[])["includedPaths"], [])

    def test_unsafe_missing_ignored_symlink_or_sensitive_includes_rejected(self):
        for name in (".env", "vendor/unsafe.py", "archive/old.md", "private/notes.md", "data/rows.py",
                     "docs/secrets.md", "ignored/module.py", "tests/fixtures/fake.py"):
            self.write(name, "Private content")
        self.write("src/embedded.py", "api_key = '" + "sk-" + "fixtureSecret0123456789abcdefgh'\n")
        self.write("src/config.json", "{}")
        (self.root / "src/linked.py").symlink_to(self.root / "src/main.py")
        for name in (".env", "vendor/unsafe.py", "archive/old.md", "private/notes.md", "data/rows.py",
                     "docs/secrets.md", "ignored/module.py", "tests/fixtures/fake.py", "src/embedded.py",
                     "src/config.json", "src/linked.py", "../outside.md", "/tmp/outside.md", "missing.py"):
            with self.subTest(name=name), self.assertRaises(Invalid):
                discovery.collect(self.store, self.state, paths=[name])
        self.assertNotIn("discoveryCollection", self.state)

    def test_explicit_include_omissions_are_reported_under_bounds(self):
        self.write("src/custom.py", "x" * (discovery.MAX_FILE_BYTES + 1))
        evidence = discovery.collect(self.store, self.state, paths=["src/custom.py"])
        self.assertIn({"path": "src/custom.py", "reason": "oversized"}, evidence["omitted"]["details"])
        self.assertEqual(evidence["includedPaths"], ["src/custom.py"])

    def test_literal_commands_not_pyproject_entrypoint_targets(self):
        self.write("pyproject.toml", '[project.scripts]\nhello = "app:main"\n[tool.hatch.envs.default.scripts]\ntest = "python -m unittest"\n')
        self.write("Makefile", 'test:\n\t@python3 -m unittest\n')
        evidence = self.collect()
        sources = {row["path"]: row for row in evidence["sources"]}
        self.assertEqual(sources["pyproject.toml"]["commands"], [{"command": "python -m unittest", "origin": "tool.hatch.envs.default.scripts.test"}])
        self.assertEqual(sources["Makefile"]["commands"], [{"command": "python3 -m unittest", "origin": "recipe"}])


class UnchangedInputsTests(unittest.TestCase):
    """0.5.0: the study is rebuilt only when its inputs change; the files the plan's tasks write are not inputs."""
    setUp = DiscoveryTests.setUp
    tearDown = DiscoveryTests.tearDown
    git = DiscoveryTests.git
    write = DiscoveryTests.write

    def focus(self, zones=("src/main.py",)):
        self.state["policy"] = {"focusedReview": True}
        self.state["tasks"] = [{"id": "T01", "title": "Task", "requirements": [], "dependsOn": [], "zones": list(zones),
                                "readZones": ["."], "criteria": ["Works"], "wave": 1, "status": "pending"}]

    def study(self):
        evidence = discovery.collect(self.store, self.state)
        discovery.record(self.store, self.state, synthetic_report(evidence))
        return evidence

    def test_unchanged_inputs_report_reuse_and_keep_the_saved_collection(self):
        for focused in (False, True):
            with self.subTest(focused=focused):
                self.state.pop("discoveryCollection", None)
                if focused:
                    self.focus()
                receipt = {}
                first = discovery.collect(self.store, self.state, reuse_receipt=receipt)
                self.assertFalse(receipt["reused"])
                saved = dict(self.state["discoveryCollection"])
                receipt = {}
                self.assertEqual(discovery.collect(self.store, self.state, reuse_receipt=receipt), first)
                self.assertTrue(receipt["reused"])
                self.assertEqual(self.state["discoveryCollection"], saved)
                self.write("README.md", "# Changed %s\n" % focused)
                receipt = {}
                discovery.collect(self.store, self.state, reuse_receipt=receipt)
                self.assertFalse(receipt["reused"])
                self.assertNotEqual(self.state["discoveryCollection"], saved)

    def test_edit_in_a_task_zone_keeps_the_study_fresh_but_an_edit_outside_does_not(self):
        self.focus(("src/main.py",))
        self.study()
        self.write("src/main.py", "def main():\n    return 'built by the task'\n")
        self.assertTrue(discovery.valid(self.store, self.state))
        self.assertTrue(discovery.view(self.store, self.state)["fresh"])
        self.write("src/repository.py", "def load():\n    return ['outside the zones']\n")
        self.assertFalse(discovery.valid(self.store, self.state))
        self.assertTrue(discovery.valid(self.store, self.state, check_sources=False))

    def test_a_zone_covers_its_directory_and_new_files_inside_it(self):
        self.focus(("src",))
        self.study()
        self.write("src/repository.py", "def load():\n    return [1]\n")
        self.write("src/app.py", "print('new file written by a task')\n")
        self.assertTrue(discovery.valid(self.store, self.state))
        self.write("README.md", "# Edited outside the zone\n")
        self.assertFalse(discovery.valid(self.store, self.state))

    def test_runs_without_the_flag_keep_the_previous_check(self):
        self.state["tasks"] = [{"id": "T01", "zones": ["src/main.py"]}]
        self.study()
        self.write("src/main.py", "def main():\n    return 'edited'\n")
        self.assertFalse(discovery.valid(self.store, self.state))

    def test_rediscovery_after_a_zone_edit_returns_the_saved_collection_and_after_an_outside_edit_a_new_one(self):
        self.focus(("src/main.py",))
        first = self.study()
        saved = dict(self.state["discoveryCollection"])
        self.write("src/main.py", "def main():\n    return 'built by the task'\n")
        receipt = {}
        self.assertEqual(discovery.collect(self.store, self.state, reuse_receipt=receipt), first)
        self.assertTrue(receipt["reused"])
        self.assertEqual(self.state["discoveryCollection"], saved)
        self.write("package.json", json.dumps({"scripts": {"test": "pytest"}}))
        receipt = {}
        discovery.collect(self.store, self.state, reuse_receipt=receipt)
        self.assertFalse(receipt["reused"])
        self.assertNotEqual(self.state["discoveryCollection"], saved)

    def test_a_new_include_is_never_a_reuse(self):
        self.focus(("src/main.py",))
        self.study()
        receipt = {}
        evidence = discovery.collect(self.store, self.state, paths=["src/repository.py"], reuse_receipt=receipt)
        self.assertFalse(receipt["reused"])
        self.assertEqual(evidence["includedPaths"], ["src/repository.py"])


if __name__ == "__main__":
    unittest.main()
