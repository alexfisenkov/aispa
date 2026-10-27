"""Composite task commands (0.5.0): `task-open` and `task-close` change the number of coordinator
turns, not the journal. The real CLI runs end to end on the synthetic project of test_check_modes_e2e;
native participants and their reports are structural fixtures, like there."""
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest

import test_check_modes_e2e as e2e
import test_cli
from aispa_core import composites
from aispa_core.store import Invalid, Journal, file_hash
from aispa_core.verified_reads import ResourceLimit

HEAVY, STANDARD = "T01", "T02"  # T01 has a dependent task (heavy); T02 is a leaf without critical flows
REQUIREMENT = {"T01": "R01", "T02": "R02"}
TIME_KEY = re.compile(r"(?:^|[a-z])At$|^at$|Seconds$|Ms$|^elapsed")
HEX = re.compile(r"[0-9a-f]{32,64}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+(?:\+00:00)?")
RUN = re.compile(r"\d{8}T\d{6}-modes-[0-9a-f]+")


def scrub(value):
    """Drop what legitimately differs between two runs: clocks, hashes of code, ids of the run."""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if not TIME_KEY.search(key)}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return STAMP.sub("<time>", HEX.sub("<hash>", RUN.sub("<run>", value)))
    return value


def differences(new, old, path="state"):
    """Paths where two scrubbed states differ; short, so that a failure names the field."""
    if isinstance(new, dict) and isinstance(old, dict):
        return [row for key in sorted(set(new) | set(old))
                for row in differences(new.get(key, "<absent>"), old.get(key, "<absent>"), path + "." + str(key))]
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [row for index, (a, b) in enumerate(zip(new, old)) for row in differences(a, b, "%s[%d]" % (path, index))]
    return [] if new == old else ["%s: %r != %r" % (path, new, old)]


class Driver(e2e.Scenario):
    """A run of two tasks (T01 heavy, T02 standard) driven by a scripted coordinator.

    With `composite` the coordinator uses task-open/task-close, otherwise the separate commands; every
    engine call it makes is counted in `calls[task]`."""
    flowless = ("R01", "R02")

    def runTest(self):  # a TestCase needs one method to be constructed outside the runner
        pass

    def begin(self, composite):
        self.composite = composite
        self.calls, self.receipts = {}, {}
        self.prepare("balanced", ("R01", "R02"))
        self.plan([self.task("T01", "R01"), self.task("T02", "R02", ["T01"])])

    def engine(self, tid, *args, expected=0):
        self.calls.setdefault(tid, []).append(args[0])
        return self.call(*args, expected=expected)

    def identity(self, tid, role, model=None):
        route = ("--model", model, "--reasoning", "high") if model else test_cli.route_args(role)
        return ("--runtime", "codex", *route, "--native-id", "synthetic-%s-%s" % (tid, role),
                "--session", "session-%s-%s" % (tid, role))

    def journal(self):
        store = Journal(self.root)
        store.load()
        return store

    def attempt_state(self, tid):
        return self.journal().load()["taskHistory"][tid][-1]

    def events(self):
        return [row["event"] for row in self.journal().read_journal(include_events=True)["events"]]

    def final_state(self):
        raw = json.dumps(self.journal().load(), ensure_ascii=False)
        return json.loads(raw.replace(json.dumps(str(self.root))[1:-1], "<root>").replace(self.temp.name, "<temp>"))

    def usage_log(self, tid, role):
        session = "session-%s-%s" % (tid, role)
        usage = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 4,
                 "total_tokens": 110}
        row = {"type": "token_usage_record", "payload": {"response_id": "resp-" + tid + role, "session_id": session,
                                                          "thread_id": session, "root_turn_id": "turn-" + tid,
                                                          "usage": usage}}
        return self.input(json.dumps(row), "usage-%s-%s.jsonl" % (tid, role))

    def fill(self, tid, form, role, verdict="PASS"):
        """Complete a form the engine prepared, as a participant would after doing the work."""
        evidence = self.attempt_state(tid)["verification"]["output"]["file"]
        report = json.loads(json.dumps(form))
        report["unmapped"] = []
        for check in report["checks"]:
            check.update(status="passed", note="Проверено тестовой командой", artifact=evidence)
        if role != "worker":
            report.update(independent=True, verdict=verdict, findings=[])
            for row in report.get("trace", []):
                row.update(result="aligned", note="Сопоставлено с брифом", artifact=evidence)
            if verdict != "PASS":
                report["findings"] = [{"id": "F1", "severity": "blocking", "blockingClass": "failing_tests",
                                       "text": "Required correction", "criterion": e2e.CRITERION[REQUIREMENT[tid]]}]
        return report

    def intent(self, tid, context_file):
        rid = REQUIREMENT[tid]
        packet = json.loads(Path(context_file).read_text(encoding="utf-8"))
        test = self.root / "tests/check_output.py"
        value = {"mode": "not_applicable", "attempt": packet["attempt"], "basis": packet["basis"],
                 "criteria": [e2e.CRITERION[rid]],
                 "notApplicable": [{"criterion": e2e.CRITERION[rid], "purpose": "existing_behavior",
                                    "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")},
                                    "reason": "The documented command already prints the output; this checks it without changing it."}],
                 "failureChecks": {
                     "cases": [{"id": "E01", "boundary": "output", "scenario": "Closed stdout has no readers",
                                "expected": "Actual child process must report failure rather than silently succeed",
                                "criterion": e2e.CRITERION[rid], "test": {"path": "tests/check_output.py", "sha256": file_hash(test)}}],
                     "notApplicable": [{"boundaries": ["input", "storage", "protocol", "dependency"],
                                        "reason": "The declared one-statement fixture has no inputs, persistence, protocol or external dependencies.",
                                        "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")}}]}}
        self.engine(tid, "test-intent", "--task", tid, "--file", self.input(value, "intent-%s.json" % tid))

    def open_worker(self, tid):
        """Start the task and seat its worker; returns (context file, actor id)."""
        identity = self.identity(tid, "worker")
        if self.composite:
            opened = self.engine(tid, "task-open", tid, "--role", "worker", *identity)["receipt"]
            self.receipts[tid, "open"] = opened
            return opened["context"]["absolutePath"], opened["actor"]
        basis = self.engine(tid, "context", "--kind", "task", "--task", tid)["basis"]
        self.engine(tid, "start", tid, "--context", basis)
        actor = self.engine(tid, "assign", tid, "--role", "worker", *identity)["receipt"]["actor"]
        return self.engine(tid, "context", "--kind", "task", "--task", tid, "--role", "worker", "--write")["absolutePath"], actor

    def worker_report(self, tid, context_file, *, heavy):
        """Test contract (heavy), a red and a green check, the form, and the filled report file."""
        if heavy:
            self.intent(tid, context_file)
            self.engine(tid, "verify", "--task", tid, "--", sys.executable, "-c", "raise SystemExit(1)", expected=2)
        self.engine(tid, "verify", "--task", tid, "--", sys.executable, "tests/check_output.py")
        form = self.engine(tid, "template", "worker", "--task", tid)
        return self.input(self.fill(tid, form, "worker"), "submit-%s.json" % tid)

    def worker_part(self, tid, heavy):
        context_file, actor = self.open_worker(tid)
        report, log = self.worker_report(tid, context_file, heavy=heavy), self.usage_log(tid, "worker")
        if self.composite:
            self.receipts[tid, "worker-close"] = self.engine(tid, "task-close", tid, "--role", "worker",
                                                              "--file", report, "--usage", log)["receipt"]
            return
        submitted = self.engine(tid, "submit", tid, "--file", report)["receipt"]
        self.engine(tid, "documents", "--refresh")
        self.engine(tid, "usage-import", "--file", log, "--format", "codex", "--participant", actor)
        # A task that is not complete has no next ready list to read.
        ready = None if heavy else self.engine(tid, "ready")
        self.receipts[tid, "worker-close"] = {"report": submitted["report"], "phase": submitted["phase"], "ready": ready}

    def open_critic(self, tid):
        """Seat critic 1; returns the path of its prepared form."""
        identity = self.identity(tid, "critic1")
        if self.composite:
            opened = self.engine(tid, "task-open", tid, "--role", "critic1", *identity)["receipt"]
            self.receipts[tid, "critic-open"] = opened
            return opened["template"]["absolutePath"]
        self.engine(tid, "assign", tid, "--role", "critic1", *identity)
        return self.engine(tid, "context", "--kind", "task", "--task", tid, "--role", "critic1", "--write",
                           "--with-template")["template"]["absolutePath"]

    def critic_part(self, tid):
        form = json.loads(Path(self.open_critic(tid)).read_text(encoding="utf-8"))
        report = self.input(self.fill(tid, form, "critic1"), "critic-%s.json" % tid)
        if self.composite:
            self.receipts[tid, "critic-close"] = self.engine(tid, "task-close", tid, "--role", "critic1",
                                                              "--file", report)["receipt"]
            return
        self.engine(tid, "critic", tid, "--round", "1", "--file", report)
        self.engine(tid, "documents", "--refresh")
        self.receipts[tid, "critic-close"] = {"ready": self.engine(tid, "ready")}

    def play(self):
        self.worker_part(HEAVY, heavy=True)
        self.critic_part(HEAVY)
        self.worker_part(STANDARD, heavy=False)


def drive(composite):
    driver = Driver()
    driver.setUp()
    driver.begin(composite)
    return driver


class SameJournalTests(unittest.TestCase):
    """The composite sequence and the separate commands, run on the same fixture, leave the same journal."""

    @classmethod
    def setUpClass(cls):
        cls.new, cls.old = drive(True), drive(False)
        for driver in (cls.new, cls.old):
            driver.play()

    @classmethod
    def tearDownClass(cls):
        cls.new.tearDown()
        cls.old.tearDown()

    def test_same_event_names_in_the_same_order(self):
        self.assertEqual(self.new.events(), self.old.events())
        self.assertEqual(self.new.events().count("task-started:T01"), 1)
        self.assertEqual(self.new.events().count("actor-assigned:T01:worker"), 1)

    def test_same_final_state_without_clocks(self):
        self.assertEqual(differences(scrub(self.new.final_state()), scrub(self.old.final_state())), [])

    def test_the_hash_chain_is_accepted_by_the_journal(self):
        for driver in (self.new, self.old):
            journal = driver.journal()
            self.assertEqual(journal.read_journal()["count"], len(driver.events()))
            self.assertEqual([t["status"] for t in journal.load()["tasks"]], ["completed", "completed"])

    def test_the_receipts_say_what_the_separate_commands_say(self):
        for tid in (HEAVY, STANDARD):
            new, old = self.new.receipts[tid, "worker-close"], self.old.receipts[tid, "worker-close"]
            self.assertEqual(scrub(new["report"]), scrub(old["report"]))
            self.assertEqual((new["ok"], new["phase"]), (True, old["phase"]))
            self.assertEqual([(row["step"], row["status"]) for row in new["steps"]],
                             [("report", "done"), ("documents", "done"), ("usage", "done")])
        self.assertEqual(self.new.receipts[HEAVY, "worker-close"]["next"], {"role": "critic1"})
        self.assertEqual(self.new.receipts[STANDARD, "worker-close"]["next"]["ready"]["tasks"],
                         self.old.receipts[STANDARD, "worker-close"]["ready"]["tasks"])
        ready = self.new.receipts[HEAVY, "critic-close"]["next"]["ready"]["tasks"]
        old_ready = self.old.receipts[HEAVY, "critic-close"]["ready"]["tasks"]
        self.assertEqual([row["id"] for row in ready], [row["id"] for row in old_ready])
        self.assertEqual([row["id"] for row in ready], [STANDARD])

    def test_open_gives_the_paths_a_participant_needs(self):
        opened = self.new.receipts[HEAVY, "open"]
        self.assertEqual([(row["step"], row["status"]) for row in opened["steps"]],
                         [("context", "done"), ("start", "done"), ("assign", "done"), ("card", "done"), ("packet", "done")])
        for path in (opened["card"]["absolutePath"], opened["context"]["absolutePath"]):
            self.assertTrue(Path(path).is_file())
        self.assertEqual(opened["basis"], opened["context"]["basis"])
        self.assertNotIn("template", opened)  # the worker's form comes after the targeted check
        critic = self.new.receipts[HEAVY, "critic-open"]
        self.assertEqual([row["step"] for row in critic["steps"]], ["assign", "card", "packet"])
        for path in (critic["card"]["absolutePath"], critic["context"]["absolutePath"], critic["template"]["absolutePath"]):
            self.assertTrue(Path(path).is_file())

    def test_a_task_costs_far_fewer_engine_calls_than_the_separate_sequence(self):
        counts = {name: {"standard": len(driver.calls[STANDARD]), "heavy": len(driver.calls[HEAVY])}
                  for name, driver in (("composite", self.new), ("separate", self.old))}
        print("engine calls per task, composite vs separate:", counts)
        self.assertEqual(self.new.calls[STANDARD], ["task-open", "verify", "template", "task-close"])
        self.assertEqual(self.new.calls[HEAVY], ["task-open", "test-intent", "verify", "verify", "template",
                                                 "task-close", "task-open", "task-close"])
        self.assertLessEqual(counts["composite"]["standard"], 4)
        self.assertLessEqual(counts["composite"]["heavy"], 8)
        self.assertGreaterEqual(counts["separate"]["standard"], 10)
        self.assertGreaterEqual(counts["separate"]["heavy"], 15)


class StopAndRepeatTests(unittest.TestCase):
    """A failing step stops the command by name; what was done stays recorded; a repeat goes on from there."""

    @classmethod
    def setUpClass(cls):
        cls.driver = drive(True)

    @classmethod
    def tearDownClass(cls):
        cls.driver.tearDown()

    def count(self, event):
        return self.driver.events().count(event)

    def test_1_a_failed_step_names_itself_and_the_repeat_continues_without_repeating_work(self):
        d = self.driver
        refused = d.call("task-open", HEAVY, "--role", "worker", *d.identity(HEAVY, "worker", model="no-such-model"), expected=2)
        self.assertIn("остановлено на шаге «assign»", refused.stderr)
        self.assertIn("Выполненные шаги записаны: context, start", refused.stderr)
        self.assertEqual(d.journal().load()["tasks"][0]["status"], "running")  # the started task stays started
        self.assertEqual((self.count("task-started:T01"), self.count("actor-assigned:T01:worker")), (1, 0))
        opened = d.call("task-open", HEAVY, "--role", "worker", *d.identity(HEAVY, "worker"))["receipt"]
        self.assertEqual([(row["step"], row["status"]) for row in opened["steps"]],
                         [("context", "skipped"), ("start", "skipped"), ("assign", "done"), ("card", "done"), ("packet", "done")])
        self.assertEqual((self.count("task-started:T01"), self.count("actor-assigned:T01:worker")), (1, 1))
        before = d.events()
        again = d.call("task-open", HEAVY, "--role", "worker", *d.identity(HEAVY, "worker"))["receipt"]
        self.assertEqual([row["status"] for row in again["steps"]], ["skipped", "skipped", "skipped", "done", "done"])
        self.assertEqual(again["actor"], opened["actor"])
        self.assertEqual(d.events(), before)  # nothing was written twice

    def test_2_a_refused_report_writes_nothing_and_a_failed_late_step_keeps_the_accepted_report(self):
        d = self.driver
        context_file = d.call("context", "--kind", "task", "--task", HEAVY, "--role", "worker", "--write")["absolutePath"]
        report = d.worker_report(HEAVY, context_file, heavy=True)
        unfinished = d.input(d.call("template", "worker", "--task", HEAVY), "unfinished.json")
        before = d.events()
        refused = d.call("task-close", HEAVY, "--role", "worker", "--file", unfinished, expected=2)
        self.assertIn("остановлено на шаге «report»", refused.stderr)
        self.assertEqual(d.events(), before)
        missing = str(Path(d.inbox) / "absent-log.jsonl")
        late = d.call("task-close", HEAVY, "--role", "worker", "--file", report, "--usage", missing, expected=2)
        self.assertIn("остановлено на шаге «usage»", late.stderr)
        self.assertIn("Выполненные шаги записаны: report, documents", late.stderr)
        self.assertEqual((self.count("task-submitted:T01"), self.count("document-metadata-refreshed")), (1, 1))
        log = d.usage_log(HEAVY, "worker")
        done = d.call("task-close", HEAVY, "--role", "worker", "--file", report, "--usage", log)["receipt"]
        self.assertEqual([(row["step"], row["status"]) for row in done["steps"]],
                         [("report", "skipped"), ("documents", "done"), ("usage", "done")])
        self.assertEqual(done["next"], {"role": "critic1"})
        repeated = d.call("task-close", HEAVY, "--role", "worker", "--file", report, "--usage", log)["receipt"]
        self.assertEqual([row["status"] for row in repeated["steps"]], ["skipped", "done", "skipped"])
        self.assertEqual((self.count("task-submitted:T01"), self.count("native-usage-imported")), (1, 1))

    def test_3_a_refusing_critic_gives_ok_false_and_sends_the_task_back_to_its_worker(self):
        d = self.driver
        form = json.loads(Path(d.open_critic(HEAVY)).read_text(encoding="utf-8"))
        report = d.input(d.fill(HEAVY, form, "critic1", verdict="FAIL"), "refusal.json")
        result = d.call("task-close", HEAVY, "--role", "critic1", "--file", report, expected=2)
        receipt = json.loads(result.stdout)["receipt"]
        self.assertIs(receipt["ok"], False)
        self.assertEqual((receipt["phase"], receipt["next"]), ("repair", {"role": "worker"}))
        self.assertEqual(self.count("task-critic-1:T01"), 1)
        again = json.loads(d.call("task-close", HEAVY, "--role", "critic1", "--file", report, expected=2).stdout)["receipt"]
        self.assertIs(again["ok"], False)
        self.assertEqual(again["steps"][0], {"step": "report", "status": "skipped"})
        self.assertEqual(self.count("task-critic-1:T01"), 1)


class StepNamingTests(unittest.TestCase):
    """The sequencing alone, with a stand-in engine: names of steps and what passes through untouched."""

    def open(self, error):
        def assign(*args, **kwargs):
            raise error
        task = {"status": "running", "phase": "critic_1"}
        flow = SimpleNamespace(task=lambda tid: task, store=None, s={"taskHistory": {"T01": [{"actors": {}}]}},
                               pipeline=SimpleNamespace(assign=assign))
        return composites.open_task(flow, "T01", "critic1", runtime="codex", native_id="n", session="s",
                                    model=None, reasoning=None)

    def test_a_plain_refusal_is_wrapped_with_the_step_and_what_is_done(self):
        with self.assertRaises(Invalid) as caught:
            self.open(Invalid("Причина отказа"))
        text = str(caught.exception)
        self.assertIn("task-open: остановлено на шаге «assign»: Причина отказа", text)
        self.assertIn("Выполненные шаги записаны: нет", text)

    def test_resource_refusals_are_not_wrapped(self):
        for error in (ResourceLimit("RESOURCE_LIMIT чтение превысило бюджет"), Invalid("RESOURCE_LIMIT отдельный отказ")):
            with self.subTest(error=type(error).__name__), self.assertRaises(Invalid) as caught:
                self.open(error)
            self.assertIs(caught.exception, error)

    def test_an_unknown_role_is_refused_before_any_step(self):
        flow = SimpleNamespace(task=lambda tid: {})
        with self.assertRaisesRegex(Invalid, "Неизвестная роль"):
            composites.close_task(flow, "T01", "manager", "report.json")


if __name__ == "__main__":
    unittest.main()
