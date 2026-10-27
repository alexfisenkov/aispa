"""Codex Terra routing of runs without ``modelRoutes`` (the 0.4.0 path) and the bounded-reader suite.

New runs take models only from the route table (model_routes, tests/test_model_enforcement.py);
these tests pin that the previous advice and checks stay unchanged for runs without the flag.
Fixtures describe synthetic host evidence; they never launch a model.
"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests import test_verified_read_boundaries as reader_regressions
from tests.test_participants import ManagedFixture, host_model_source
from aispa_core import model_routes, participants
from aispa_core.store import Invalid

LEGACY = {"managedExecution": True}


class LegacyCodexTerraPolicyTests(ManagedFixture):
    """Old path: no modelRoutes flag in the frozen policy."""

    def test_codex_preference_is_terra_for_each_work_kind_without_claiming_availability(self):
        for need in ("mechanical", "ordinary", "complex"):
            with self.subTest(need=need):
                advice = participants.model_advice("codex", need)
                self.assertEqual(advice["preferredFamilies"], ["terra"])
                self.assertEqual(advice["availability"], "unknown")

    def test_other_host_preferences_are_unchanged(self):
        for runtime, need, expected in (("claude-code", "complex", ["opus"]),
                                       ("claude-code", "mechanical", ["sonnet"]),
                                       ("gemini", "complex", ["argon", "pro"])):
            with self.subTest(runtime=runtime, need=need):
                self.assertEqual(participants.model_advice(runtime, need)["preferredFamilies"], expected)

    def test_terra_explicit_sensitive_route_preserves_unknown_actual_until_observed(self):
        self.state["policy"] = dict(LEGACY)
        for observed in (None, "gpt-6.1-terra"):
            with self.subTest(observed=observed):
                source = host_model_source(self.store, self.state, runtime="codex", provider="openai",
                    requested="gpt-6.1-terra", observed=observed)
                metadata = participants.model_metadata(self.store, self.state, "codex", "host-native",
                    "host-session", model="gpt-6.1-terra", evidence=source)
                actor = {"runtime": "codex", "nativeId": "host-native", "session": "host-session", **metadata}
                participants.require_model_route(self.store, self.state, actor, sensitive=True)
                self.assertEqual(metadata["actualModelStatus"], "unknown" if observed is None else "observed")
                self.assertEqual(metadata["actualModel"], observed)

    def test_unclassified_future_terra_and_mismatched_actual_are_blocked(self):
        self.state["policy"] = dict(LEGACY)
        for requested, observed in (("gpt-7-terra", None), ("gpt-6.1-terra", "gpt-6-luna")):
            with self.subTest(requested=requested, observed=observed):
                source = host_model_source(self.store, self.state, runtime="codex", provider="openai",
                    requested=requested, observed=observed)
                metadata = participants.model_metadata(self.store, self.state, "codex", "host-native",
                    "host-session", model=requested, evidence=source)
                actor = {"runtime": "codex", "nativeId": "host-native", "session": "host-session", **metadata}
                with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                    participants.require_model_route(self.store, self.state, actor, sensitive=True)

    def test_prelaunch_requires_real_terra_host_capability(self):
        self.state["policy"] = dict(LEGACY)
        source = host_model_source(self.store, self.state, runtime="codex", provider="openai",
            requested="gpt-6.1-terra", phase="planned", intent_id="terra-intent")
        plan = participants.launch_plan(self.store, self.state, runtime="codex", session="host-session",
            model="gpt-6.1-terra", evidence=source, sensitive=True)
        self.assertEqual(plan["selection"]["status"], "planned")
        self.assertIsNone(plan["actualModel"])
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            participants.launch_plan(self.store, self.state, runtime="codex", session="host-session",
                model="gpt-6.1-terra", evidence=None, sensitive=True)

    def test_terra_for_every_role_is_only_the_old_path(self):
        self.state["policy"] = dict(LEGACY)
        aid = participants.register(self.store, self.state, "brief-reviewer", "codex", "legacy-judge", "host-session",
                                    model="gpt-6.1-terra")
        self.assertNotIn("routeClass", participants.resolve(self.store, self.state, aid))
        self.state["policy"] = dict(LEGACY, modelRoutes=True)
        with self.assertRaisesRegex(Invalid, "Для роли brief-reviewer в среде codex нужна gpt-6.1-sol · high"):
            participants.register(self.store, self.state, "brief-reviewer", "codex", "routed-judge", "host-session",
                                  model="gpt-6.1-terra", reasoning="high")
        self.assertEqual(model_routes.ROUTES["worker"]["codex"]["model"], "gpt-6.1-terra")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(reader_regressions.load_tests(loader, unittest.TestSuite(), pattern))
    suite.addTests(tests)
    return suite
