import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import packets, participants, telemetry
from aispa_core.store import Journal, Invalid, digest


class RoleUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-all-roles-")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("roles", "All roles fixture")

    def tearDown(self):
        self.temp.cleanup()

    def test_cumulative_totals_with_response_ids_are_not_per_call_usage(self):
        actor = participants.register(self.store, self.state, "coordinator", "codex", "native", "session")
        path = Path(self.temp.name) / "cumulative.jsonl"
        path.write_text("\n".join(json.dumps({
            "type": "event_msg", "payload": {"type": "token_count", "response_id": "R" + str(i),
            "session_id": "session", "info": {"total_token_usage": {
                "input_tokens": i * 100, "cached_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": i * 100}}}
        }) for i in (1, 2)), encoding="utf-8")
        result = telemetry.import_log(self.store, self.state, path, format="codex", participant=actor)
        self.assertEqual(result["measuredCalls"], 0)
        self.assertEqual(result["coverage"]["incompleteCalls"], 2)
        self.assertFalse(result["complete"])

    def _legacy_actor(self, role, number, *, knowledge=False):
        native = f"native-{number}"
        aid = digest(["codex", native, "session"])
        row = {"id": aid, "runtime": "codex", "nativeId": native, "session": "session",
               "role": role, "run": self.state["run"]}
        field = "artifact" if knowledge else "record"
        row[field] = self.store.artifact("knowledge" if knowledge else "team-records",
                                         telemetry._encoded(row), ".json")
        if knowledge:
            self.state["knowledge"] = {"writer": row, "writerHistory": []}
        else:
            self.state.setdefault("actors", {})[aid] = row
        return aid

    def test_packets_usage_counts_every_registered_role(self):
        ids = {}
        index = 0
        for role in sorted(participants.ROLES):
            index += 1
            if role in participants.TASK_ROLES or role == "knowledge-writer":
                aid = self._legacy_actor(role, index, knowledge=role == "knowledge-writer")
            else:
                aid = participants.register(self.store, self.state, role, "codex",
                                            f"native-{index}", "session", model="host-model")
            ids[role] = aid
            receipt = packets.record_usage(self.store, self.state,
                                           {"actor": aid, "callId": f"call-{index}",
                                            "source": "native fixture", "inputTokens": 10,
                                            "outputTokens": 2, "cachedInputTokens": 3})
            self.assertEqual(receipt["measuredCalls"], index)
        report = packets.usage_summary(self.store, self.state)
        self.assertEqual(set(report["byRole"]), set(participants.ROLES))
        self.assertEqual(set(report["byParticipant"]), set(ids.values()))
        self.assertEqual(report["inputTokens"], 10 * len(ids))
        self.assertEqual(report["outputTokens"], 2 * len(ids))
        self.assertEqual(report["cachedInputTokens"], 3 * len(ids))
        self.assertFalse(report["complete"])
        self.assertEqual(report["coverage"]["missingParticipants"], [])
        self.assertIsNone(report["cacheReadInputTokens"])

    def test_cross_registry_identity_guard_uses_runtime_and_native_id(self):
        task = self._legacy_actor("worker", 1)
        self.assertEqual(participants.identity_conflicts(self.state, "codex", "native-1"), [task])
        self.assertEqual(participants.identity_conflicts(self.state, "claude-code", "native-1"), [])
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "brief-reviewer", "codex",
                                  "native-1", "different-session")
        with self.assertRaises(Invalid):
            participants.require_unused_identity(self.state, "CODEX", "native-1")
        self.assertEqual(participants.identity_conflicts(self.state, "codex", "native-1",
                                                         exclude_actor=task), [])
        other = participants.register(self.store, self.state, "brief-reviewer", "claude-code",
                                      "native-1", "different-session")
        self.assertTrue(participants.actor_check(self.store, self.state, other, "brief-reviewer"))
        self.state["knowledge"] = {"writerHistory": [None, {"id": None}, {"id": 5}, {"id": []}]}
        self.assertEqual(len(participants.list_participants(self.store, self.state)), 2)

    def test_recovery_reviewer_can_register_only_for_pending_paused_recovery(self):
        self.state["status"] = "paused"
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "recovery-reviewer", "codex", "repair-1", "s")
        self.state["journalRecovery"] = {"pending": True}
        aid = participants.register(self.store, self.state, "recovery-reviewer", "codex", "repair-1", "s")
        self.assertTrue(participants.actor_check(self.store, self.state, aid, "recovery-reviewer"))
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "coordinator", "codex", "other", "s")

    def test_actual_codex_envelope_shape_and_native_session_mapping(self):
        actor = participants.register(self.store, self.state, "coordinator", "codex", "native-codex", "session-X")
        usage = {"input_tokens": 100, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 5, "output_tokens": 10,
                 "reasoning_output_tokens": 4, "total_tokens": 110}
        rows = [
            {"type": "event_msg", "payload": {"type": "token_count", "info":
             {"last_token_usage": usage, "total_token_usage": usage}}},
            {"type": "token_usage_record", "payload": {"response_id": "resp-1",
             "session_id": "session-X", "thread_id": "session-X", "root_turn_id": "turn-A",
             "usage": {**usage, "output_tokens": 2, "total_tokens": 102}}},
            {"type": "token_usage_record", "payload": {"response_id": "resp-1",
             "session_id": "session-X", "thread_id": "session-X", "root_turn_id": "turn-A",
             "usage": usage}},
        ]
        path = Path(self.temp.name) / "exact-codex.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        report = telemetry.import_log(self.store, self.state, path, format="codex", participant=actor)
        self.assertEqual(report["measuredCalls"], 1)
        self.assertEqual(report["inputTokens"], 100)
        self.assertEqual(report["cachedInputTokens"], 25)
        self.assertEqual(report["uncachedInputTokens"], 75)
        self.assertEqual(report["cacheCreationInputTokens"], 5)
        self.assertEqual(report["cacheReadInputTokens"], 20)
        self.assertEqual(report["reasoningOutputTokens"], 4)
        self.assertEqual(report["coverage"]["unsupportedCalls"], 0)
        self.assertEqual(report["coverage"]["ignoredCumulativeEvents"], 1)
        self.assertEqual(report["coverage"]["unmappedCalls"], 0)

        other = participants.register(self.store, self.state, "source-reader", "codex", "native-other", "session-Y")
        second = telemetry.import_log(self.store, self.state, path, format="codex", participant=other)
        self.assertEqual(second["measuredCalls"], 1)
        self.assertEqual(second["coverage"]["unmappedCalls"], 1)
        self.assertIn(other, second["coverage"]["missingParticipants"])

    def test_shared_native_session_cannot_be_silently_attributed(self):
        first = participants.register(self.store, self.state, "coordinator", "codex", "one", "shared")
        participants.register(self.store, self.state, "source-reader", "codex", "two", "shared")
        path = Path(self.temp.name) / "one.jsonl"
        path.write_text(json.dumps({"type": "token_usage_record", "payload": {
            "response_id": "r1", "session_id": "shared", "thread_id": "shared",
            "usage": {"input_tokens": 3, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                      "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 4}}}), encoding="utf-8")
        with self.assertRaises(Invalid):
            telemetry.import_log(self.store, self.state, path, format="codex", participant=first)
        self.assertEqual(self.state.get("usage", []), [])

    def test_cumulative_diagnostics_do_not_make_canonical_source_incomplete(self):
        actor = participants.register(self.store, self.state, "coordinator", "codex", "native", "session")
        usage = {"input_tokens": 3, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                 "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 4}
        path = Path(self.temp.name) / "with-cumulative.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in [
            {"type": "event_msg", "payload": {"type": "token_count", "info":
             {"last_token_usage": usage, "total_token_usage": usage}}},
            {"type": "token_usage_record", "payload": {"response_id": "response-1",
             "session_id": "session", "thread_id": "session", "usage": usage}},
        ]), encoding="utf-8")
        self.state["status"] = "accepted"
        result = telemetry.import_log(self.store, self.state, path, format="codex", participant=actor,
                                      final=True, includes_final_coordinator_reply=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["measuredCalls"], 1)
        self.assertEqual(result["coverage"]["ignoredCumulativeEvents"], 1)

    def test_task_packet_includes_compact_source_and_design_refs_only_when_present(self):
        task = {"id": "T01", "requirements": [], "zones": ["src"], "readZones": [],
                "status": "pending", "dependsOn": []}
        self.state["tasks"] = [task]
        self.state["taskHistory"] = {"T01": [{"id": "T01-A01", "verification": None,
                                              "roles": {}, "actors": {}, "rounds": []}]}
        self.state["spec"] = self.store.artifact("specs", b"spec", ".md")

        class Pipeline:
            def trace_requirements(self, _tid):
                return []

            def inherited_proofs(self, _tid):
                return []

        def build():
            with patch.object(packets, "task_closure", return_value=[task]), \
                 patch.object(packets, "active", return_value=[]), \
                 patch.object(packets, "definition", side_effect=lambda item: item), \
                 patch.object(packets, "basis", return_value="basis"), \
                 patch.object(packets, "document_context", return_value={}):
                return packets.build(self.store, self.state, Pipeline(), "task", "T01")

        absent = build()
        self.assertFalse(any(key in absent for key in ("briefMap", "featureScenarios", "interfaceContract")))
        for key in ("briefMap", "featureScenarios", "interfaceContract"):
            self.state[key] = self.store.artifact("phase-reports", b"{}", ".json")
        from aispa_core import design_contract
        with patch.object(design_contract, "reference_snapshots", return_value=[]):
            present = build()
        for key in ("briefMap", "featureScenarios", "interfaceContract"):
            self.assertEqual(present[key], self.state[key])
        self.assertNotIn("briefMapHistory", present)


if __name__ == "__main__":
    unittest.main()
