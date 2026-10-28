"""A check the environment forbids should not stop the run (ticket B2; pinned as an expected failure, D09).

The worker's tool is denied by the host (for example the built-in browser
refuses `file://`); no project-local check could replace it, so the core
task is blocked with that reason. Every state change goes through the real
CLI in a subprocess (participants and reports are synthetic structural
fixtures, as in test_check_modes_e2e): the other task closes, the branch
lenses, clean clone, blind acceptance with this item blocked and the
acceptance follow, and `finish` delivers with the remainder. «accepted» is
unreachable while the item stays unchecked.
"""
import sys
import unittest

import test_check_modes_e2e
from aispa_core.store import Journal

REASON = ("инструмент среды запрещает проверку: встроенный браузер не открывает file://, "
          "проектная проверка невозможна в этой среде")


class EnvironmentBlockedE2E(test_check_modes_e2e.Scenario):
    flowless = ("R01", "R02")  # no critical user flow: the remainder comes from the blocked task alone

    complete = test_check_modes_e2e.MaxModeE2E.complete  # T01 has a dependant: heavy, closed by its critic

    # Known engine defect (decision D09): in 0.5.0 an environment-blocked task stops the final («сдача
    # невозможна»). `integration._project_ready` requires a current proof of the final task and
    # `Workflow._dependencies_ready` does not admit it while a dependency is blocked; then
    # `final_repairs.repairable_items` / `blind_settled` count such a failure as repairable. The path
    # «с остатками» is the next iteration; an unexpected success here says the engine has been fixed.
    @unittest.expectedFailure
    def test_environment_blocked_core_task_is_delivered_with_the_remainder(self):
        self.prepare("balanced")
        # T02 is the final task (it depends on every task and reads the whole tree); the environment blocks it.
        self.plan([self.task("T01", "R01"), self.task("T02", "R02", ["T01"])])
        self.complete("T01", "R01")
        self.seat("T02", "worker")
        self.call("block", "T02", "--note", REASON)
        self.assertEqual(self.task_view("T01")["status"], "completed")
        self.assertEqual(self.task_view("T02")["status"], "blocked")
        self.commit("tasks")
        self.document_blockers("T02")
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        # Branch lenses, handoff, clean-prepare, clean-verify, blind with the item blocked, acceptance.
        self.finish_delivery({"R02": "blocked"}, {"R02": "blocked"}, accepted=False)
        gate = Journal(self.root).load()["gates"]["acceptance"]
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["unmapped"], [])
        self.record_knowledge()
        answer = self.call("finish")
        self.assertEqual(answer["status"], "delivered_with_remaining")
        state = Journal(self.root).load()
        self.assertEqual(state["status"], "delivered_with_remaining")
        self.assertEqual(state["remaining"]["blockedTasks"], ["T02"])
        self.assertEqual(next(t for t in state["tasks"] if t["id"] == "T02")["note"], REASON)
        self.assertTrue(any(row.get("requirement") == "R02" for row in state["remaining"]["checks"]),
                        "the unchecked item is named in the remainder")
        self.assertNotEqual(self.status()["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
