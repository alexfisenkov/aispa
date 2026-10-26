"""Diagnostic proposals preserve identities and never rewrite the owner's scope."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import scale
from aispa_core.store import Invalid, digest
from test_preflight import ManagedFixture, optional_module


class BriefDiagnosticsTests(ManagedFixture):
    def prepare(self):
        self.state["policy"] = {"scalableInputs": True}
        self.flow.brief(self.file("Export a CSV. Keep data private.\n", "brief.md"))
        self.state["requirements"] = [
            {"id": f"R{i:02d}", "text": "Export a CSV", "kind": "requested", "scope": "active",
             "core": i == 1, "brief": "B001", "quote": "Export a CSV.",
             "acceptance": ["CSV works", "Same shared criterion"]} for i in range(1, 7)]
        self.state["requirements"].extend([
            {"id": f"G{i:02d}", "text": "Keep data private", "kind": "constraint", "scope": "active",
             "core": True, "brief": "B001", "quote": "Keep data private.", "acceptance": ["No network"]}
            for i in range(1, 3)])
        self.flow.save("diagnostic-fixture")

    def analyze(self):
        module = optional_module("brief_diagnostics")
        self.assertIsNotNone(module, "A pure diagnostic view must identify duplicated conditions without deleting them")
        return module.analyze(self.store, self.state)

    def test_duplicate_constraint_and_fragmentation_diagnostics_are_read_only(self):
        self.prepare()
        before, journal = copy.deepcopy(self.state), (self.store.run / "events.jsonl").read_bytes()
        report = self.analyze()
        self.assertTrue(report["exactDuplicates"])
        self.assertTrue(report["repeatedConstraints"])
        self.assertTrue(report["fragmentation"])
        self.assertTrue(all(row.get("reason") for row in report["fragmentation"]))
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)

    def test_group_proposal_retains_every_id_and_indexed_criterion(self):
        self.prepare()
        report = self.analyze()
        proposal = report["groupingProposal"]
        self.assertEqual(set(proposal["requirements"]), {r["id"] for r in self.state["requirements"]})
        expected = {(r["id"], i, text) for r in self.state["requirements"]
                    for i, text in enumerate(r["acceptance"])}
        actual = {(row["requirement"], row["index"], row["text"]) for row in proposal["criteria"]}
        self.assertEqual(actual, expected)
        self.assertIn("dependencies", proposal)
        self.assertIn("risks", proposal)
        self.assertFalse(proposal["applied"])

    def test_manifest_and_plain_sources_match_and_corrupt_chunk_fails_closed(self):
        self.prepare()
        plain = self.analyze()
        ref = self.state["briefs"][0]
        raw = self.store.text(ref).encode()
        packed = self.store.run / "briefs/packed.json"
        manifest = scale.write_manifest(packed, raw, kind="text", basis=digest("fixture"), chunk_bytes=8)
        from aispa_core.store import file_hash
        self.state["briefs"][0] = {"id": "B001", "file": "briefs/packed.json", "sha256": file_hash(packed)}
        self.assertEqual(self.analyze()["groupingProposal"], plain["groupingProposal"])
        (packed.parent / manifest["chunks"][-1]["file"]).write_bytes(b"corrupt")
        with self.assertRaises(Invalid):
            self.analyze()


if __name__ == "__main__":
    unittest.main()
