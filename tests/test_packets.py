import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import packets
from aispa_core.store import Journal, Invalid


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Journal(self.temp.name)
        self.state = self.store.create("packets", "Packet fixture")
        self.store.code = lambda: "synthetic-code-snapshot"

    def tearDown(self):
        self.temp.cleanup()

    def test_large_packet_is_bounded_without_losing_any_source_or_check(self):
        packet = {"basis": "current", "kind": "task", "briefs": ["Original source " * 10000],
                  "expectedChecks": [{"requirement": f"R{i:02}", "criterion": "Criterion " * 200} for i in range(60)]}
        receipt = packets.write(self.store, packet)
        envelope = json.loads(Path(receipt["absolutePath"]).read_text())
        self.assertLessEqual(receipt["envelopeBytes"], packets.MAX_PACKET_BYTES)
        self.assertLess(receipt["envelopeBytes"], receipt["inlineBytesBefore"] / 10)
        self.assertIsNone(receipt["tokenCount"])
        restored = {k: v for k, v in envelope.items() if k not in ("sections", "readingRule")}
        for key, ref in envelope["sections"].items():
            restored[key] = json.loads(self.store.text(ref))
        self.assertEqual(restored, packet)

    def test_small_packet_does_not_force_extra_reads(self):
        packet = {"basis": "x", "kind": "task", "briefs": [], "expectedChecks": []}
        receipt = packets.write(self.store, packet)
        self.assertEqual(receipt["sectionCount"], 0)
        self.assertEqual(json.loads(Path(receipt["absolutePath"]).read_text()), packet)

    def test_blind_packet_contains_no_registry_spec_or_old_reviews(self):
        self.state["briefs"] = [dict(self.store.artifact("briefs", b"Original request", ".md"), id="B001")]
        self.state["spec"] = {"file": "do-not-show-spec"}
        self.state["cleanroom"] = {"path": "clone", "head": "commit", "tree": "tree"}
        with patch.object(self.store, "git", return_value=b"synthetic-commit\n"):
            result = packets.build(self.store, self.state, None, "blind")
        self.assertEqual(set(result), {"kind", "basis", "briefs", "inheritedBriefs", "clone", "instructions"})
        self.assertNotIn("do-not-show-spec", json.dumps(result))

    def test_only_explicit_low_risk_isolated_worker_gets_standard_recommendation(self):
        task = {"id": "T01", "dependsOn": [], "risk": "low"}
        self.state["tasks"] = [task]
        self.assertEqual(packets.routing(self.state, task, "worker")["tier"], "standard")
        for role in ("critic1", "critic2", "acceptor"):
            self.assertEqual(packets.routing(self.state, task, role)["tier"], "strong")
            self.assertEqual(packets.routing(self.state, task, role)["criticRounds"], 2)
        self.state["tasks"].append({"id": "T02", "dependsOn": ["T01"]})
        self.assertEqual(packets.routing(self.state, task, "worker")["tier"], "strong")
        self.assertTrue(packets.routing(self.state, task, "worker")["advisory"])

    def test_usage_is_measured_not_invented_and_duplicate_calls_rejected(self):
        self.assertIsNone(packets.usage_summary(self.store, self.state)["inputTokens"])
        self.state["actors"] = {"synthetic": {}}
        value = dict(actor="synthetic", callId="call-1", source="synthetic native usage fixture",
                     inputTokens=100, outputTokens=25, cachedInputTokens=60)
        receipt = packets.record_usage(self.store, self.state, value)
        self.assertEqual(receipt["inputTokens"], 100)
        self.assertFalse(receipt["complete"])
        with self.assertRaises(Invalid):
            packets.record_usage(self.store, self.state, value)
        self.state["usage"][0]["inputTokens"] = 200
        with self.assertRaises(Invalid):
            packets.usage_summary(self.store, self.state)

    def test_inherited_retry_also_requires_strong_worker(self):
        task = {"id": "T01", "requirements": ["R01"], "dependsOn": [], "risk": "low"}
        self.state.update(tasks=[task], carryover={"fixture": True})
        ledger = {"items": [{"mapping": {"run": self.state["run"], "requirement": "R01"}, "attempts": [{"rounds": 2}]}]}
        with patch("aispa_core.carryover.view", return_value=ledger):
            self.assertEqual(packets.routing(self.state, task, "worker", store=self.store)["tier"], "strong")
            task["requirements"] = ["R02"]
            self.assertEqual(packets.routing(self.state, task, "worker", store=self.store)["tier"], "standard")

    def test_invalid_usage_values_are_rejected(self):
        self.state["actors"] = {"synthetic": {}}
        for value in (-1, True, 1.2):
            with self.subTest(value=value), self.assertRaises(Invalid):
                packets.record_usage(self.store, self.state, dict(actor="synthetic", callId="x", source="fixture",
                    inputTokens=value, outputTokens=1, cachedInputTokens=0))


if __name__ == "__main__":
    unittest.main()
