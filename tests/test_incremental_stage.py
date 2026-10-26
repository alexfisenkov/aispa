"""Repair admission needs an actual sealed first non-PASS, not state markers.

This independent engineering regression source does not amend the active pinned
schema-one task intent or claim canonical task acceptance.
"""
import copy
import unittest

import test_incremental_test_intent as fixture
from aispa_core import test_intent
from aispa_core import team


class IncrementalStageTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.IncrementalTestIntentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def revised(self, first, original):
        row = self.fx.source("stage_repair")
        real_red = self.fx.red(row)
        return self.fx.report([*self.fx.inherited(first, original["newTestFiles"]),
                               dict(row, redExecution=real_red)])

    def refuse(self, report):
        self.fx.refuse(report)

    def test_round_marker_without_original_submission_does_not_open_repair(self):
        first, original = self.fx.initial()
        self.fx.task["phase"] = "repair"
        self.fx.attempt["rounds"] = [{"number": 1, "passed": False}]
        self.fx.store.append(self.fx.state, "counterfeit-round-without-submission")
        self.refuse(self.revised(first, original))

    def test_unsealed_submission_and_critic_markers_do_not_open_repair(self):
        first, original = self.fx.initial()
        self.fx.task["phase"] = "repair"
        self.fx.attempt["submitted"] = {"testIntent": copy.deepcopy(first)}
        self.fx.store.append(self.fx.state, "task-submitted:T01")
        self.fx.attempt["rounds"] = [{"number": 1, "passed": False,
                                      "testIntent": copy.deepcopy(first)}]
        self.fx.store.append(self.fx.state, "counterfeit-unsealed-first-critic")
        self.refuse(self.revised(first, original))

    def test_sealed_first_nonpass_does_not_authorize_wrong_task_phase(self):
        _team, first, original, _report = self.fx.reviewed_pipeline()
        self.fx.task["phase"] = "implementation"
        self.fx.store.append(self.fx.state, "wrong-repair-phase")
        self.refuse(self.revised(first, original))

    def test_changed_sealed_critic_receipt_does_not_open_repair(self):
        _team, first, original, _report = self.fx.reviewed_pipeline()
        self.fx.attempt["rounds"][0]["basis"] = "0" * 64
        self.fx.store.append(self.fx.state, "changed-first-critic-receipt")
        self.refuse(self.revised(first, original))

    def test_sealed_critic_must_be_the_bound_independent_critic_actor(self):
        _team, first, original, _report = self.fx.reviewed_pipeline()
        self.fx.attempt["actors"]["critic1"] = self.fx.attempt["actors"]["worker"]
        self.fx.store.append(self.fx.state, "first-critic-actor-substituted")
        self.refuse(self.revised(first, original))

    def test_new_seal_without_actual_critic_event_cannot_replace_first_decision(self):
        _team, first, original, _report = self.fx.reviewed_pipeline()
        copied = {key: copy.deepcopy(value) for key, value in self.fx.attempt["rounds"][0].items()
                  if key != "record"}
        copied["unrecordedReplacement"] = True
        replacement = team._seal(self.fx.store, copied)
        self.assertTrue(team._record_valid(self.fx.store, replacement))
        self.fx.attempt["rounds"][0] = replacement
        self.fx.store.append(self.fx.state, "sealed-but-unexecuted-critic-replacement")
        self.refuse(self.revised(first, original))

    def test_supported_pipeline_sealed_first_nonpass_allows_revision(self):
        _team, first, original, _report = self.fx.reviewed_pipeline()
        submitted = copy.deepcopy(self.fx.attempt["submitted"])
        critic = copy.deepcopy(self.fx.attempt["rounds"][0])
        second = self.fx.save(self.revised(first, original))
        self.assertNotEqual(first, second)
        self.assertEqual(self.fx.attempt["submitted"], submitted)
        self.assertEqual(self.fx.attempt["rounds"][0], critic)
        self.assertTrue(test_intent.history_valid(self.fx.store, self.fx.state, "T01"))


if __name__ == "__main__":
    unittest.main()
