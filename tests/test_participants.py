"""Requested routes, accepted native selection and observed model are distinct."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import knowledge, participants, telemetry
from aispa_core.store import Invalid
from test_preflight import ManagedFixture
import test_team


def model_source(store, state, native="native", session="session", *, model="gpt-6-sol",
                 observed=None, **changes):
    payload = {"schema": 1, "run": state["run"], "runtime": "codex", "nativeId": native,
        "session": session,
        "capability": {"tool": "spawn_agent", "parameters": ["model", "reasoning_effort"],
                       "models": ["gpt-6-sol", "gpt-6-luna"], "source": "host-api-schema"},
        "request": {"callId": "call-" + native, "tool": "spawn_agent",
                    "arguments": {"model": model, "reasoning_effort": "high"}},
        "response": {"callId": "call-" + native, "nativeId": native, "ok": True}}
    if observed is not None:
        payload["observed"] = {"model": observed, "reasoning": "high", "source": "provider-metadata"}
    payload.update(changes)
    return {"source": store.artifact("native-model-evidence", (json.dumps(payload) + "\n").encode(), ".json")}


def host_model_source(store, state, *, runtime, provider, requested, canonical=None,
                      native="host-native", session="host-session", observed=None,
                      phase="actual", intent_id=None, plan=None, resolution_scope="native-subagent",
                      resolution_authority="host-effective-model-resolution", selector=True):
    """Synthetic source-bound protocol fixture; no provider/model is actually called."""
    call_id = None if phase == "planned" else "real-shaped-call-" + native
    native_id = None if phase == "planned" else native
    capability = {"tool": "host-native-launch", "parameters": ["model"] if selector else [],
                  "models": [requested] if selector else [], "source": "host-api-schema"}
    if provider is not None:
        capability["provider"] = provider
    payload = {"schema": 1, "phase": phase, "run": state["run"], "runtime": runtime,
               "nativeId": native_id, "session": session, "capability": capability,
               "request": {"callId": call_id, "tool": "host-native-launch",
                           "arguments": {"model": requested} if selector else {}},
               "response": None if phase == "planned" else
                   {"callId": call_id, "nativeId": native_id, "ok": True}}
    if intent_id is not None:
        payload["request"]["intentId"] = intent_id
    if canonical is not None and canonical != requested:
        resolutions = {requested: canonical}
        resolution = {"schema": 1, "phase": phase, "run": state["run"], "runtime": runtime,
                      "provider": provider, "session": session, "nativeId": native_id,
                      "callId": call_id, "intentId": intent_id, "resolutions": resolutions,
                      "scope": resolution_scope, "authority": resolution_authority}
        capability["resolutions"] = resolutions
        capability["resolutionSource"] = store.artifact("model-resolutions",
            (json.dumps(resolution) + "\n").encode(), ".json")
    if observed is not None:
        payload["observed"] = {"model": observed, "reasoning": None, "source": "provider-metadata"}
    if plan is not None:
        payload["planSource"] = plan["source"]
    return {"source": store.artifact("native-model-evidence",
        (json.dumps(payload) + "\n").encode(), ".json")}


class ParticipantModelTests(ManagedFixture):
    def test_host_claude_opus_sensitive_selection_is_accepted_without_actual_echo(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5")
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="opus", evidence=source)
        actor = {"runtime": "claude-code", "nativeId": "host-native", "session": "host-session", **metadata}
        participants.require_model_route(self.store, self.state, actor, sensitive=True)
        self.assertEqual(metadata["requestedModel"], "opus")
        self.assertEqual(metadata.get("resolvedRequestedModel"), "claude-opus-5-5")
        self.assertEqual(metadata.get("provider"), "anthropic")
        self.assertEqual(metadata["selection"]["status"], "accepted")
        self.assertIsNone(metadata["actualModel"])
        self.assertEqual(metadata["actualModelStatus"], "unknown")

    def test_host_claude_sonnet_is_ordinary_and_cannot_satisfy_sensitive_route(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="sonnet", canonical="claude-sonnet-5-5")
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="sonnet", evidence=source)
        actor = {"runtime": "claude-code", "nativeId": "host-native", "session": "host-session", **metadata}
        participants.require_model_route(self.store, self.state, actor, sensitive=False)
        self.assertEqual(metadata.get("resolvedRequestedModel"), "claude-sonnet-5-5")
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.require_model_route(self.store, self.state, actor, sensitive=True)

    def test_host_alias_observed_canonical_id_is_not_an_automatic_mismatch(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5", observed="claude-opus-5-5")
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="opus", evidence=source)
        self.assertEqual(metadata["actualModelStatus"], "observed")
        self.assertEqual(metadata["actualModel"], "claude-opus-5-5")
        participants.require_model_route(self.store, self.state,
            {"runtime": "claude-code", "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)

    def test_host_opus_alias_overridden_to_sonnet_never_earns_sensitive_strength(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-sonnet-5-5", observed="claude-sonnet-5-5")
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="opus", evidence=source)
        self.assertEqual(metadata.get("resolvedRequestedModel"), "claude-sonnet-5-5")
        self.assertEqual(metadata["actualModelStatus"], "observed")
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.require_model_route(self.store, self.state,
                {"runtime": "claude-code", "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)

    def test_host_wrong_runtime_provider_cannot_reuse_codex_sol_strength(self):
        self.state["policy"] = {"managedExecution": True}
        for runtime, provider in (("claude-code", "anthropic"), ("gemini", "openai"),
                                  ("codex", "google"), ("openclaw", None)):
            with self.subTest(runtime=runtime, provider=provider):
                source = host_model_source(self.store, self.state, runtime=runtime, provider=provider, requested="gpt-6-sol")
                metadata = participants.model_metadata(self.store, self.state, runtime, "host-native", "host-session",
                                                       model="gpt-6-sol", evidence=source)
                with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                    participants.require_model_route(self.store, self.state,
                        {"runtime": runtime, "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)

    def test_host_gemini_approved_reasoning_ids_and_flash_have_distinct_routes(self):
        self.state["policy"] = {"managedExecution": True}
        for model in ("gemini-4-argon", "gemini-3.1-pro-preview"):
            with self.subTest(model=model):
                source = host_model_source(self.store, self.state, runtime="gemini", provider="google", requested=model)
                metadata = participants.model_metadata(self.store, self.state, "gemini", "host-native", "host-session",
                                                       model=model, evidence=source)
                participants.require_model_route(self.store, self.state,
                    {"runtime": "gemini", "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)
                self.assertIsNone(metadata["actualModel"])
        source = host_model_source(self.store, self.state, runtime="gemini", provider="google", requested="gemini-3.8-flash")
        metadata = participants.model_metadata(self.store, self.state, "gemini", "host-native", "host-session",
                                               model="gemini-3.8-flash", evidence=source)
        actor = {"runtime": "gemini", "nativeId": "host-native", "session": "host-session", **metadata}
        participants.require_model_route(self.store, self.state, actor, sensitive=False)
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.require_model_route(self.store, self.state, actor, sensitive=True)

    def test_host_multi_provider_runtime_requires_explicit_provider_and_known_canonical(self):
        self.state["policy"] = {"managedExecution": True}
        for runtime, provider, model in (("cursor", "anthropic", "claude-opus-5-5"),
                ("openclaw", "google", "gemini-4-argon"), ("antigravity", "openai", "gpt-6.1-sol")):
            with self.subTest(runtime=runtime, provider=provider):
                source = host_model_source(self.store, self.state, runtime=runtime, provider=provider, requested=model)
                metadata = participants.model_metadata(self.store, self.state, runtime, "host-native", "host-session",
                                                       model=model, evidence=source)
                participants.require_model_route(self.store, self.state,
                    {"runtime": runtime, "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)
                self.assertEqual(metadata.get("provider"), provider)

    def test_host_unknown_ids_or_reported_strong_labels_cannot_expand_catalogue(self):
        self.state["policy"] = {"managedExecution": True}
        for runtime, provider, model in (("claude-code", "anthropic", "claude-opus-unapproved-future"),
                ("gemini", "google", "gemini-999-pro"), ("codex", "openai", "gpt-999-sol")):
            source = host_model_source(self.store, self.state, runtime=runtime, provider=provider, requested=model)
            value = self.store.json(source["source"])
            value["capability"].update(strong=True, family="opus", modelClass="strong")
            source = {"source": self.store.artifact("native-model-evidence", json.dumps(value).encode(), ".json")}
            metadata = participants.model_metadata(self.store, self.state, runtime, "host-native", "host-session",
                                                   model=model, evidence=source)
            with self.subTest(model=model), self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                participants.require_model_route(self.store, self.state,
                    {"runtime": runtime, "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)

    def test_host_alias_resolution_scope_authority_and_sha_are_checked(self):
        self.state["policy"] = {"managedExecution": True}
        for scope, authority in (("main", "host-effective-model-resolution"),
                                  ("native-subagent", "self-reported-family")):
            source = host_model_source(self.store, self.state, runtime="gemini", provider="google",
                requested="pro", canonical="gemini-4-argon", resolution_scope=scope, resolution_authority=authority)
            with self.subTest(scope=scope, authority=authority), self.assertRaises(Invalid):
                participants.model_metadata(self.store, self.state, "gemini", "host-native", "host-session",
                                             model="pro", evidence=source)
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5")
        value = self.store.json(source["source"])
        value["capability"]["resolutionSource"]["sha256"] = "0" * 64
        source = {"source": self.store.artifact("native-model-evidence", json.dumps(value).encode(), ".json")}
        with self.assertRaises(Invalid):
            participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                         model="opus", evidence=source)

    def test_host_unresolved_alias_and_main_default_do_not_prove_sensitive_child_selection(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic", requested="opus")
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="opus", evidence=source)
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.require_model_route(self.store, self.state,
                {"runtime": "claude-code", "nativeId": "host-native", "session": "host-session", **metadata}, sensitive=True)
        source = host_model_source(self.store, self.state, runtime="gemini", provider="google",
            requested="gemini-4-argon", phase="planned", intent_id="main-default-only", selector=False)
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.launch_plan(self.store, self.state, runtime="gemini", session="host-session",
                                     model="gemini-4-argon", evidence=source, sensitive=True)

    def test_host_planned_nonce_is_not_a_native_call_and_actual_binding_correlates_plan(self):
        self.state["policy"] = {"managedExecution": True}
        planned = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5", phase="planned", intent_id="planned-intent-1")
        plan = participants.launch_plan(self.store, self.state, runtime="claude-code", session="host-session",
                                       model="opus", evidence=planned, sensitive=True)
        self.assertEqual(plan["selection"]["status"], "planned")
        self.assertIsNone(plan["selection"].get("callId"))
        self.assertEqual(plan["selection"].get("intentId"), "planned-intent-1")
        self.assertIsNone(plan["selection"].get("nativeId"))
        actual = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5", intent_id="planned-intent-1", plan=planned)
        metadata = participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                               model="opus", evidence=actual)
        self.assertEqual(metadata["selection"]["status"], "accepted")
        self.assertEqual(metadata["selection"]["callId"], "real-shaped-call-host-native")
        value = self.store.json(actual["source"])
        value["request"]["intentId"] = "another-plan"
        wrong = {"source": self.store.artifact("native-model-evidence", json.dumps(value).encode(), ".json")}
        with self.assertRaises(Invalid):
            participants.model_metadata(self.store, self.state, "claude-code", "host-native", "host-session",
                                         model="opus", evidence=wrong)

    def test_host_claude_knowledge_writer_uses_same_profile_and_unknown_actual(self):
        self.state["policy"] = {"managedExecution": True}
        self.state["requirements"] = [{"id": "R01", "scope": "active", "text": "Financial account permissions",
                                       "acceptance": ["Payment authorization"]}]
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5", native="knowledge")
        actor = knowledge.assign(self.store, self.state, "claude-code", "knowledge", "host-session",
                                 model="opus", model_evidence=source)
        self.assertEqual(actor.get("resolvedRequestedModel"), "claude-opus-5-5")
        self.assertIsNone(actor["actualModel"])

    def test_host_resolution_backed_mismatch_observation_preserves_original_author(self):
        self.state["policy"] = {"managedExecution": True}
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5")
        aid = participants.register(self.store, self.state, "source-reader", "claude-code", "host-native", "host-session",
                                    model="opus", model_evidence=source)
        original = copy.deepcopy(self.state["participants"][aid])
        observed = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5", observed="claude-sonnet-5-5")
        participants.observe_model(self.store, self.state, aid, observed)
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual(row["actualModelStatus"], "mismatch")
        self.assertEqual(self.state["participants"][aid], original)
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.require_model_route(self.store, self.state, row, sensitive=True)

    def test_sensitive_prelaunch_plans_only_supported_explicit_strong_request(self):
        self.state["policy"] = {"managedExecution": True}
        plan = getattr(participants, "launch_plan", None)
        self.assertTrue(callable(plan), "Sensitive routing must be checked before host launch, not only assignment")
        source = model_source(self.store, self.state, response=None, nativeId=None)
        planned = plan(self.store, self.state, runtime="codex", session="session", model="gpt-6-sol",
                       reasoning="high", evidence=source, sensitive=True)
        self.assertEqual(planned["selection"]["status"], "planned")
        self.assertIsNone(planned["actualModel"])
        for model, evidence in (("gpt-6-luna", source), ("strong", source), ("gpt-6-sol", None)):
            with self.subTest(model=model, evidence=evidence), self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                plan(self.store, self.state, runtime="codex", session="session", model=model,
                     reasoning="high", evidence=evidence, sensitive=True)

    def register(self, native="native", *, model="gpt-6-sol", evidence=None):
        kwargs = {"model": model}
        if evidence is not None:
            kwargs.update(reasoning="high", model_evidence=evidence)
        return participants.register(self.store, self.state, "source-reader", "codex", native, "session", **kwargs)

    def test_legacy_model_is_requested_not_observed_and_native_usage_stays_unknown(self):
        aid = self.register()
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual(row.get("requestedModel"), "gpt-6-sol")
        self.assertIsNone(row.get("actualModel"))
        self.assertEqual(row.get("actualModelStatus"), "unknown")
        telemetry.record(self.store, self.state, {"actor": aid, "callId": "measured-1",
            "source": "native:test", "inputTokens": 3, "outputTokens": 1, "cachedInputTokens": 0})
        report = telemetry.summary(self.store, self.state)
        self.assertEqual(set(report["byModel"]), {"unknown"})
        self.assertFalse(report["complete"])

    def test_supported_selection_without_echo_is_accepted_actual_is_unknown(self):
        self.state["policy"] = {"managedExecution": True}
        source = model_source(self.store, self.state)
        aid = self.register(evidence=source)
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual(row["requestedModel"], "gpt-6-sol")
        self.assertEqual(row["selection"]["mode"], "explicit")
        self.assertEqual(row["selection"]["status"], "accepted")
        self.assertIsNone(row["actualModel"])
        self.assertEqual(row["actualModelStatus"], "unknown")
        self.assertIsNone(row["actualReasoning"])

    def test_wrong_actor_session_call_or_capability_binding_is_rejected(self):
        self.state["policy"] = {"managedExecution": True}
        variants = [dict(session="wrong-session"), dict(run="wrong-run"), dict(nativeId="other-native"),
                    dict(response={"callId": "wrong-call", "nativeId": "native", "ok": True}),
                    dict(response={"callId": "call-native", "nativeId": "other", "ok": True}),
                    dict(response={"callId": "call-native", "nativeId": "native", "ok": False}),
                    dict(capability={"tool": "spawn_agent", "parameters": [], "models": ["gpt-6-sol"],
                                     "source": "host-api-schema"})]
        for changes in variants:
            evidence = model_source(self.store, self.state, **changes)
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                self.register(evidence=evidence)
        evidence = model_source(self.store, self.state)
        evidence["source"]["sha256"] = "0" * 64
        with self.assertRaises(Invalid):
            self.register(evidence=evidence)

    def test_later_mismatch_is_append_only_and_revokes_sensitive_eligibility(self):
        self.state["policy"] = {"managedExecution": True}
        aid = self.register(evidence=model_source(self.store, self.state))
        record = copy.deepcopy(self.state["participants"][aid])
        original = (self.store.run / record["record"]["file"]).read_bytes()
        observer = getattr(participants, "observe_model", None)
        self.assertTrue(callable(observer), "A later host observation must append rather than rewrite its author")
        observer(self.store, self.state, aid, model_source(self.store, self.state, observed="gpt-6-luna"))
        self.assertEqual(self.state["participants"][aid], record)
        self.assertEqual((self.store.run / record["record"]["file"]).read_bytes(), original)
        current = participants.resolve(self.store, self.state, aid)
        self.assertEqual(current["actualModel"], "gpt-6-luna")
        self.assertEqual(current["actualModelStatus"], "mismatch")
        guard = getattr(participants, "require_model_route", None)
        self.assertTrue(callable(guard))
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            guard(self.store, self.state, current, sensitive=True)

    def test_attested_strong_route_is_available_without_selectable_api(self):
        self.state["policy"] = {"managedExecution": True}
        evidence = model_source(self.store, self.state, observed="gpt-6-sol",
            capability={"tool": "spawn_agent", "parameters": [], "models": [], "source": "host-api-schema"},
            request={"callId": "call-native", "tool": "spawn_agent", "arguments": {}})
        aid = self.register(evidence=evidence)
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual(row["actualModel"], "gpt-6-sol")
        self.assertEqual(row["actualModelStatus"], "observed")
        guard = getattr(participants, "require_model_route", None)
        self.assertTrue(callable(guard))
        guard(self.store, self.state, row, sensitive=True)

    def test_knowledge_writer_uses_the_same_requested_and_observed_contract(self):
        self.state["policy"] = {"managedExecution": True}
        evidence = model_source(self.store, self.state, native="knowledge")
        actor = knowledge.assign(self.store, self.state, "codex", "knowledge", "session",
                                 model="gpt-6-sol", reasoning="high", model_evidence=evidence)
        self.assertEqual(actor["selection"]["status"], "accepted")
        self.assertEqual(actor["requestedModel"], "gpt-6-sol")
        self.assertIsNone(actor["actualModel"])


class SensitiveTaskModelTests(unittest.TestCase):
    setUp = test_team.TeamTests.setUp
    tearDown = test_team.TeamTests.tearDown
    task = test_team.TeamTests.task

    def test_host_claude_task_assignment_uses_same_sensitive_profile(self):
        self.sensitive()
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
            requested="opus", canonical="claude-opus-5-5")
        aid = self.team.assign("T01", "worker", "claude-code", "host-native", "host-session",
                               model="opus", model_evidence=source)
        actor = participants.resolve(self.store, self.state, aid)
        self.assertEqual(actor.get("resolvedRequestedModel"), "claude-opus-5-5")
        self.assertEqual(actor["selection"]["status"], "accepted")
        self.assertIsNone(actor["actualModel"])

    def sensitive(self):
        self.state["policy"] = {"managedExecution": True}
        self.state["requirements"][0]["text"] = "Calculate payment balance and enforce account permissions"
        self.state["tasks"][0]["title"] = "Implement the financial ledger"
        self.state["tasks"][0]["zones"] = ["ledger.py"]
        self.team.prepare()

    def test_neutral_filename_finance_rejects_weak_or_advisory_route(self):
        self.sensitive()
        for model in ("gpt-6-luna", "gpt-6-sol", "strong"):
            with self.subTest(model=model), self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                self.team.assign("T01", "worker", "codex", "advisory-" + model, "session", model=model)

    def test_explicit_supported_sol_selection_allows_sensitive_assignment_without_echo(self):
        self.sensitive()
        evidence = model_source(self.store, self.state)
        aid = self.team.assign("T01", "worker", "codex", "native", "session", model="gpt-6-sol",
                               reasoning="high", model_evidence=evidence)
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual(row["selection"]["status"], "accepted")
        self.assertIsNone(row["actualModel"])

    def test_incidental_word_in_an_unrelated_quote_does_not_make_task_sensitive(self):
        self.state["policy"] = {"managedExecution": True}
        self.state["requirements"][0]["quote"] = "The documentation word payment is only an example."
        self.team.prepare()
        aid = self.team.assign("T01", "worker", "codex", "mechanical", "session", model="gpt-6-luna")
        self.assertEqual(participants.resolve(self.store, self.state, aid).get("requestedModel"), "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
