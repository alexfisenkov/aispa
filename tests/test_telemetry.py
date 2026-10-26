import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import participants, telemetry
from aispa_core.store import Journal, Invalid, digest


class TelemetryTests(unittest.TestCase):
    def test_per_call_phase_bindings_preserve_totals_and_unknown_coverage_gaps(self):
        self.state["policy"] = {"managedExecution": True}
        actor = self.register()
        for call, phase in (("phase-call-1", "idea"), ("phase-call-2", "spec"), ("unattributed-call", None)):
            value = {"actor": actor, "callId": call, "source": "native fixture",
                     "inputTokens": 10, "outputTokens": 3, "cachedInputTokens": 2}
            if phase:
                binding = {"actor": actor, "callId": call, "phase": phase, "run": self.state["run"],
                           "session": "session-1"}
                value["attribution"] = {**binding, "source": self.store.artifact("phase-usage",
                    json.dumps(binding).encode(), ".json")}
            telemetry.record(self.store, self.state, value)
        report = telemetry.summary(self.store, self.state)
        self.assertEqual(set(report.get("byPhase", {})), {"idea", "spec", "unknown"})
        self.assertEqual(sum(row["inputTokens"] for row in report["byPhase"].values()), report["inputTokens"])
        self.assertEqual(report["coverage"]["unattributedCalls"], 1)
        self.assertFalse(report["complete"])
        self.assertNotIn("cost", report)

    def test_wrong_session_or_source_phase_attribution_fails_before_record(self):
        self.state["policy"] = {"managedExecution": True}
        actor = self.register()
        binding = {"actor": actor, "callId": "phase-call", "phase": "spec", "run": self.state["run"],
                   "session": "wrong-session"}
        value = {"actor": actor, "callId": "phase-call", "source": "native fixture",
                 "inputTokens": 10, "outputTokens": 3, "cachedInputTokens": 2,
                 "attribution": {**binding, "source": self.store.artifact("phase-usage", json.dumps(binding).encode(), ".json")}}
        with self.assertRaises(Invalid):
            telemetry.record(self.store, self.state, value)
        self.assertEqual(self.state.get("usage", []), [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Journal(self.temp.name)
        self.state = self.store.create("telemetry", "Telemetry fixture")

    def tearDown(self):
        self.temp.cleanup()

    def register(self, role="coordinator", native="native-1", runtime="codex"):
        return participants.register(self.store, self.state, role, runtime, native,
                                     "session-1", model="host-model")

    def write_jsonl(self, rows):
        path = Path(self.temp.name) / "native.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_general_registry_and_legacy_authoritative_records(self):
        coordinator = self.register()
        self.assertTrue(participants.actor_check(self.store, self.state, coordinator, "coordinator"))
        self.assertFalse(participants.actor_check(self.store, self.state, coordinator, "brief-reviewer"))
        self.assertEqual(participants.resolve(self.store, self.state, coordinator)["nativeId"], "native-1")
        self.assertEqual(len(participants.list_participants(self.store, self.state)), 1)
        with self.assertRaises(Invalid):
            self.register("brief-reviewer", "native-1")
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "unknown-reviewer", "codex", "native-2", "session-2")
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "brief-reviewer", "codex", "native-2", "session-2",
                                  external=True)
        external = participants.register(self.store, self.state, "unknown-reviewer", "codex",
                                         "native-2", "session-2", external=True)
        self.assertTrue(participants.resolve(self.store, self.state, external)["external"])
        self.assertNotIn(coordinator, self.state.get("actors", {}))

        task_id = digest(["codex", "native-task", "task-session"])
        task = {"id": task_id, "runtime": "codex", "nativeId": "native-task",
                "session": "task-session", "role": "worker", "run": self.state["run"]}
        task["record"] = self.store.artifact("team-records", telemetry._encoded(task), ".json")
        self.state["actors"] = {task_id: task}
        self.assertEqual(participants.resolve(self.store, self.state, task_id)["role"], "worker")
        self.assertNotIn(task_id, self.state["participants"])
        task["role"] = "critic1"
        self.assertIsNone(participants.resolve(self.store, self.state, task_id))

        author_id = digest(["codex", "native-author", "author-session"])
        author = {"id": author_id, "runtime": "codex", "nativeId": "native-author",
                  "session": "author-session", "role": "knowledge-writer", "run": self.state["run"]}
        author["artifact"] = self.store.artifact("knowledge", telemetry._encoded(author), ".json")
        self.state["knowledge"] = {"writer": author}
        self.assertEqual(participants.resolve(self.store, self.state, author_id)["role"], "knowledge-writer")
        self.assertNotIn(author_id, self.state["participants"])

    def test_claude_stream_final_record_cache_and_idempotent_checkpoint(self):
        actor = self.register(runtime="claude-code")
        rows = [
            {"type": "assistant", "sessionId": "session-1", "message": {"id": "msg-1", "model": "claude-test", "usage":
             {"input_tokens": 10}}},
            {"type": "assistant", "sessionId": "session-1", "message": {"id": "msg-1", "model": "claude-test", "usage":
             {"input_tokens": 10, "cache_creation_input_tokens": 4,
              "cache_read_input_tokens": 6, "output_tokens": 1}}},
            {"type": "assistant", "sessionId": "session-1", "message": {"id": "msg-1", "model": "claude-test", "usage":
             {"input_tokens": 10, "cache_creation_input_tokens": 4,
              "cache_read_input_tokens": 6, "output_tokens": 8}}},
        ]
        path = self.write_jsonl(rows)
        result = telemetry.import_log(self.store, self.state, path, format="claude", participant=actor,
                                      final=True, includes_final_coordinator_reply=True)
        self.assertEqual(result["measuredCalls"], 1)
        self.assertEqual(result["inputTokens"], 20)
        self.assertEqual(result["uncachedInputTokens"], 10)
        self.assertEqual(result["cacheCreationInputTokens"], 4)
        self.assertEqual(result["cacheReadInputTokens"], 6)
        self.assertEqual(result["outputTokens"], 8)
        self.assertIsNone(result["reasoningOutputTokens"])
        self.assertEqual(result["coverage"]["incompleteCalls"], 0)
        self.assertFalse(result["complete"])  # the run is still active
        self.assertEqual(result["byRole"]["coordinator"]["measuredCalls"], 1)
        self.assertEqual(len(self.state["usageImports"]), 1)
        telemetry.import_log(self.store, self.state, path, format="claude", participant=actor,
                             final=True, includes_final_coordinator_reply=True)
        self.assertEqual(len(self.state["usageImports"]), 1)
        self.assertEqual(len(self.state["usage"]), 1)
        self.assertNotIn(str(path), json.dumps(self.state["usageImports"]))

    def test_codex_requires_explicit_call_id_and_marks_unsupported(self):
        actor = self.register()
        path = self.write_jsonl([
            {"type": "event_msg", "payload": {"type": "token_count", "info":
             {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}}}},
            {"type": "event_msg", "payload": {"type": "token_usage_record",
             "response_id": "resp-1", "session_id": "session-1", "thread_id": "session-1",
             "usage": {"input_tokens": 40,
             "cached_input_tokens": 12, "output_tokens": 9, "reasoning_output_tokens": 3}}},
        ])
        result = telemetry.import_log(self.store, self.state, path, format="codex", participant=actor,
                                      final=True)
        self.assertEqual(result["measuredCalls"], 1)
        self.assertEqual(result["inputTokens"], 40)
        self.assertEqual(result["cachedInputTokens"], 12)
        self.assertEqual(result["reasoningOutputTokens"], 3)
        self.assertIsNone(result["cacheCreationInputTokens"])
        self.assertEqual(result["coverage"]["unsupportedCalls"], 0)
        self.assertEqual(result["coverage"]["ignoredCumulativeEvents"], 1)
        self.assertFalse(result["complete"])

    def test_unmapped_and_incomplete_are_not_silent(self):
        self.register()
        path = self.write_jsonl([
            {"type": "assistant", "message": {"id": "m1", "usage":
             {"input_tokens": 2, "output_tokens": 1}}},
            {"type": "assistant", "message": {"id": "m2", "usage":
             {"input_tokens": -1, "output_tokens": 1}}},
        ])
        result = telemetry.import_log(self.store, self.state, path, format="claude")
        self.assertIsNone(result["inputTokens"])
        self.assertEqual(result["coverage"]["unmappedCalls"], 1)
        self.assertEqual(result["coverage"]["incompleteCalls"], 1)
        self.assertFalse(result["complete"])

    def test_checkpoint_can_finalize_same_file_without_recounting_calls(self):
        actor = self.register(runtime="claude-code")
        path = self.write_jsonl([{"type": "assistant", "sessionId": "session-1", "message": {"id": "final-1", "usage":
                                 {"input_tokens": 5, "output_tokens": 2}}}])
        telemetry.import_log(self.store, self.state, path, format="claude", participant=actor)
        self.state["status"] = "accepted"
        result = telemetry.import_log(self.store, self.state, path, format="claude", participant=actor,
                                      final=True, includes_final_coordinator_reply=True)
        self.assertEqual(result["measuredCalls"], 1)
        self.assertEqual(result["coverage"]["sourceCheckpoints"], 1)
        self.assertTrue(result["complete"])

    def test_legacy_three_counter_payload_and_tamper_detection(self):
        actor = self.register()
        value = {"actor": actor, "callId": "call-1", "source": "native fixture",
                 "inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 30}
        result = telemetry.record(self.store, self.state, value)
        self.assertEqual(result["uncachedInputTokens"], 70)
        self.assertIsNone(result["cacheReadInputTokens"])
        with self.assertRaises(Invalid):
            telemetry.record(self.store, self.state, value)
        self.state["usage"][0]["inputTokens"] = 999
        with self.assertRaises(Invalid):
            telemetry.summary(self.store, self.state)

    def test_legacy_saved_row_is_read_without_invented_breakdowns(self):
        self.register()
        legacy = {"actor": next(iter(self.state["participants"])), "callId": "old-call",
                  "source": "old native receipt", "inputTokens": 30,
                  "outputTokens": 7, "cachedInputTokens": 12, "at": "2026-01-01T00:00:00Z"}
        legacy["record"] = self.store.artifact("usage", telemetry._encoded(legacy), ".json")
        self.state["usage"] = [legacy]
        result = telemetry.summary(self.store, self.state)
        self.assertEqual(result["inputTokens"], 30)
        self.assertEqual(result["uncachedInputTokens"], 18)
        self.assertIsNone(result["cacheCreationInputTokens"])
        self.assertIsNone(result["cacheReadInputTokens"])
        self.assertIsNone(result["reasoningOutputTokens"])


if __name__ == "__main__":
    unittest.main()
