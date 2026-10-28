"""Final-repair seats (0.5.0): the route table binds them like every subagent,
and a lost worker or checker is replaced by a fresh one without losing history."""
import argparse
import contextlib
import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

import test_final_repairs
from test_participants import host_model_source
import aispa
from aispa_core import final_repairs, model_routes
from aispa_core.store import Invalid, Journal
from aispa_core.workflow import Workflow

WORKER = model_routes.ROUTES["worker"]["codex"]
SENSITIVE = model_routes.ROUTES["sensitive_worker"]["codex"]
JUDGE = model_routes.ROUTES["judge"]["codex"]


class RepairSeatFixture(test_final_repairs.BranchFixture):
    def setUp(self):
        super().setUp()
        self.state["runtime"] = "codex"
        self.serial = 100

    def open_repair(self, zones=("app.txt",), routes=True):
        """The branch fixture's lens reviewers are host-model ones; the route table binds the repair seats."""
        self.warn()
        self.review_all()
        if routes:
            self.state["policy"]["modelRoutes"] = True
        return final_repairs.open(self.store, self.state, "findings", None, list(zones))

    def seat(self, role, *, native=None, model=None, reasoning=None):
        row = WORKER if role == "worker" else JUDGE
        self.serial += 1
        native = native or "repair-%s-%d" % (role, self.serial)
        return final_repairs.assign(self.store, self.state, role, runtime="codex", native_id=native,
                                    session="s-" + native, model=row["model"] if model is None else model,
                                    reasoning=row["effort"] if reasoning is None else reasoning)

    def submit(self, actor, text="Fixed\n"):
        (self.root / "app.txt").write_text(text, encoding="utf-8")
        return final_repairs.submit(self.store, self.state, {
            "repair": "F01", "worker": actor, "summary": "Renamed",
            "items": [{"id": "W001", "note": "Renamed the field"}]})

    def check(self, actor):
        return final_repairs.check(self.store, self.state, {
            "repair": "F01", "checker": actor, "independent": True, "candidate": self.store.code(),
            "verdict": "PASS", "items": [{"id": "W001", "status": "fixed", "note": "Read the diff"}]})


class RouteTableTests(RepairSeatFixture):
    REFUSED = (("fable", "medium"), ("claude-fable-5-1", "medium"), ("gpt-6-astra", "medium"),
               ("my-astra-preview", "medium"), ("inherit", "medium"), ("best", "medium"),
               ("", "medium"), (None, "medium"), ("MODEL", ""), ("MODEL", None), ("MODEL", "xhigh"),
               ("MODEL", "max"), ("MODEL", "ultra"), ("gpt-5-codex", "EFFORT"), ("MODEL", "OTHER"))

    def assert_refused(self, role):
        row = WORKER if role == "worker" else JUDGE
        other = "low" if row["effort"] != "low" else "medium"
        for model, reasoning in self.REFUSED:
            model = row["model"] if model == "MODEL" else model
            reasoning = {"EFFORT": row["effort"], "OTHER": other}.get(reasoning, reasoning)
            with self.subTest(role=role, model=model, reasoning=reasoning):
                before = copy.deepcopy(self.state.get("finalRepairs"))
                with self.assertRaises(Invalid):
                    final_repairs.assign(self.store, self.state, role, runtime="codex", native_id="refused-" + role,
                                         session="s-refused", model=model, reasoning=reasoning)
                self.assertEqual(self.state.get("finalRepairs"), before)
                self.assertNotIn("refused-" + role, final_repairs.used_identities(self.state))

    def test_worker_and_checker_off_the_table_are_refused_and_nothing_is_written(self):
        self.open_repair()
        self.assert_refused("worker")
        worker = self.seat("worker")
        self.submit(worker["actor"])
        self.assert_refused("checker")

    def test_approved_pairs_are_seated_with_their_route_class(self):
        self.open_repair()
        worker = self.seat("worker")
        record = self.state["finalRepairs"][0]["worker"]
        self.assertEqual((record["routeClass"], record["model"], record["reasoning"]),
                         ("worker", WORKER["model"], WORKER["effort"]))
        self.assertEqual((worker["route"]["model"], worker["route"]["effort"]), (WORKER["model"], WORKER["effort"]))
        self.submit(worker["actor"])
        with self.assertRaises(Invalid):  # a worker's pair is not a judge's
            self.seat("checker", model=WORKER["model"], reasoning=WORKER["effort"])
        checker = self.seat("checker")
        self.assertEqual(self.state["finalRepairs"][0]["rounds"][0]["checker"]["routeClass"], "judge")
        self.assertEqual(self.check(checker["actor"])["status"], "passed")

    def test_sensitive_repair_zones_need_the_sensitive_worker_route(self):
        (self.root / "credentials.json").write_text("{}\n", encoding="utf-8")
        self.state["tasks"][0]["zones"].append("credentials.json")
        self.open_repair(zones=("credentials.json",))
        with self.assertRaisesRegex(Invalid, SENSITIVE["model"]):
            self.seat("worker")
        self.seat("worker", model=SENSITIVE["model"], reasoning=SENSITIVE["effort"])
        self.assertEqual(self.state["finalRepairs"][0]["worker"]["routeClass"], "sensitive_worker")

    def test_without_the_flag_the_table_is_not_consulted(self):
        self.open_repair(routes=False)
        receipt = final_repairs.assign(self.store, self.state, "worker", runtime="codex", native_id="free-worker",
                                       session="s-free")
        self.assertIsNone(receipt["route"])
        self.assertNotIn("routeClass", self.state["finalRepairs"][0]["worker"])


class SeatFlow:
    """The saving seam of `final-repair`: every event goes to the journal and to ``events``."""

    def __init__(self, test, sources=None):
        self.test, self.sources, self.events = test, sources or {}, []

    def build_ready(self):
        pass

    def save(self, event):
        self.events.append(event)
        self.test.store.append(self.test.state, event)

    def read_input(self, name):
        return json.dumps(self.sources[name])


class SeatRefusalTests(RepairSeatFixture):
    def assign_args(self, role, model, reasoning, evidence=None):
        return argparse.Namespace(repair_action="assign", role=role, runtime="codex", native_id="n-" + role,
                                  session="s-" + role, model=model, reasoning=reasoning, model_evidence=evidence)

    def missing(self, model):
        return {"offered": host_model_source(self.store, self.state, runtime="codex", provider="openai",
                                             requested=model, native="n-worker", session="s-worker")}

    def test_a_missing_approved_model_for_a_repair_seat_is_a_journaled_blocker(self):
        self.open_repair()
        self.store.append(self.state, "fixture-repair-opened")
        flow = SeatFlow(self, self.missing("gpt-5-codex"))
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY.*" + WORKER["model"]):
            final_repairs.execute(self.store, self.state, flow,
                                  self.assign_args("worker", WORKER["model"], WORKER["effort"], "offered"))
        self.assertEqual(flow.events, ["model-capability-blocked:worker"])
        saved = self.store.load()["modelBlocks"]
        self.assertEqual([(row["role"], row["task"], row["model"]) for row in saved],
                         [("worker", "F01", WORKER["model"])])
        self.assertIsNone(self.state["finalRepairs"][0]["worker"])

    def test_other_refusals_write_no_blocker_even_with_evidence(self):
        self.open_repair()
        self.store.append(self.state, "fixture-repair-opened")
        flow = SeatFlow(self, self.missing("gpt-5-codex"))
        for model, reasoning in (("gpt-6-astra", WORKER["effort"]), ("fable", WORKER["effort"]),
                                 ("inherit", WORKER["effort"]), (WORKER["model"], "xhigh"),
                                 ("gpt-5-codex", WORKER["effort"]), (JUDGE["model"], JUDGE["effort"])):
            with self.subTest(model=model, reasoning=reasoning):
                with self.assertRaises(Invalid):
                    final_repairs.execute(self.store, self.state, flow,
                                          self.assign_args("worker", model, reasoning, "offered"))
        self.assertEqual(flow.events, [])
        self.assertNotIn("modelBlocks", self.store.load())

    def test_the_refusal_names_the_seat_it_is_for(self):
        self.open_repair()
        with self.assertRaisesRegex(Invalid, "^Исполнитель финального ремонта: .*" + WORKER["model"]):
            final_repairs.assign(self.store, self.state, "worker", runtime="codex", native_id="w", session="s",
                                 model="gpt-5-codex", reasoning=WORKER["effort"])
        worker = self.seat("worker")
        self.submit(worker["actor"])
        with self.assertRaisesRegex(Invalid, "^Проверяющий финального ремонта: .*" + JUDGE["model"]):
            final_repairs.assign(self.store, self.state, "checker", runtime="codex", native_id="c", session="s",
                                 model=WORKER["model"], reasoning=WORKER["effort"])


class ReplacementTests(RepairSeatFixture):
    def test_a_lost_worker_is_replaced_and_the_repair_goes_on(self):
        self.open_repair()
        lost = self.seat("worker")
        lost_native = self.state["finalRepairs"][0]["worker"]["nativeId"]
        fresh = self.seat("worker")
        repair = self.state["finalRepairs"][0]
        self.assertEqual(fresh["replaced"], lost["actor"])
        self.assertEqual([(row["role"], row["person"]["id"]) for row in repair["replaced"]], [("worker", lost["actor"])])
        self.assertEqual((repair["rounds"], repair["status"]), ([], "open"))
        self.assertIn(lost_native, final_repairs.used_identities(self.state))
        with self.assertRaisesRegex(Invalid, "свежий"):
            self.seat("worker", native=lost_native)
        with self.assertRaisesRegex(Invalid, "не назначенным"):
            self.submit(lost["actor"])
        self.submit(fresh["actor"])
        with self.assertRaisesRegex(Invalid, "сдал"):
            self.seat("worker")  # after the submission the worker owes nothing to replace
        checker = self.seat("checker")
        self.assertEqual(self.check(checker["actor"])["status"], "passed")
        self.assertEqual(len(self.state["finalRepairs"][0]["rounds"]), 1)

    def test_a_lost_checker_is_replaced_without_a_new_round(self):
        self.open_repair()
        worker = self.seat("worker")
        self.submit(worker["actor"])
        lost = self.seat("checker")
        lost_native = self.state["finalRepairs"][0]["rounds"][0]["checker"]["nativeId"]
        for native in (lost_native, self.state["finalRepairs"][0]["worker"]["nativeId"]):
            with self.assertRaisesRegex(Invalid, "свежий"):
                self.seat("checker", native=native)
        fresh = self.seat("checker")
        repair = self.state["finalRepairs"][0]
        self.assertEqual([(row["role"], row["round"], row["person"]["id"]) for row in repair["replaced"]],
                         [("checker", 1, lost["actor"])])
        self.assertEqual((len(repair["rounds"]), repair["status"]), (1, "submitted"))
        with self.assertRaisesRegex(Invalid, "не назначенным"):
            self.check(lost["actor"])
        self.assertEqual(self.check(fresh["actor"])["status"], "passed")
        self.assertEqual(self.state["finalRepairs"][0]["rounds"][0]["number"], 1)
        self.assertIn(lost_native, final_repairs.used_identities(self.state))


class CliRefusalTests(RepairSeatFixture):
    """`final-repair assign` off the table: exit code 2, the journal and finalRepairs stay as they were."""

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(aispa, "runtime_digest", return_value="fixture-runtime"), \
                patch.object(Workflow, "build_ready", lambda flow: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = aispa.execute(aispa.parser().parse_args(["--root", str(self.root), "--full", *args]))
            except (Invalid, OSError, ValueError) as error:
                print("AISpa: " + str(error), file=sys.stderr)
                code = 2
        return code, out.getvalue(), err.getvalue()

    def journal(self):
        store = Journal(self.root)
        state = store.load()
        return store.read_journal()["count"], state.get("finalRepairs")

    def test_refused_seat_exits_2_and_writes_nothing(self):
        self.open_repair()
        self.state["runtimeDigest"] = "fixture-runtime"
        self.store.append(self.state, "fixture-repair-opened")
        before = self.journal()
        for model, reasoning in (("gpt-6-astra", WORKER["effort"]), (WORKER["model"], "xhigh"),
                                 ("inherit", WORKER["effort"]), (JUDGE["model"], JUDGE["effort"])):
            with self.subTest(model=model, reasoning=reasoning):
                code, _out, err = self.cli("final-repair", "assign", "--role", "worker", "--runtime", "codex",
                                           "--native-id", "cli-worker", "--session", "cli-session",
                                           "--model", model, "--reasoning", reasoning)
                self.assertEqual(code, 2, err)
                self.assertIn("AISpa:", err)
                self.assertEqual(self.journal(), before)
        code, out, err = self.cli("final-repair", "assign", "--role", "worker", "--runtime", "codex",
                                  "--native-id", "cli-worker", "--session", "cli-session",
                                  "--model", WORKER["model"], "--reasoning", WORKER["effort"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["receipt"]["route"]["model"], WORKER["model"])
        count, repairs = self.journal()
        self.assertEqual((count, repairs[0]["worker"]["routeClass"]), (before[0] + 1, "worker"))


if __name__ == "__main__":
    unittest.main()
