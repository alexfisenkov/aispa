"""Owner-authorized retry preserves the existing plan and sealed scope."""
import copy
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.contracts import task_contract, validate_plan
from aispa_core.store import Invalid
from aispa_core.team import _history_valid, historical_proof_valid
from aispa_core import check_modes
from aispa import execute, parser, runtime_digest
import test_team
import test_check_modes


class OwnerTaskAttemptTests(unittest.TestCase):
    def fixture(self):
        f = test_team.TeamTests(methodName="test_new_attempt_requires_new_quoted_owner_brief")
        f.setUp()
        self.addCleanup(f.tearDown)
        # The shared team fixture predates strict plan coverage. Make this
        # isolated plan valid before exercising the owner route.
        f.state["tasks"][0]["requirements"].append("G01")
        f.team.prepare()
        return f

    def source(self, f, text="Да давай\n"):
        path = f.home / "owner-reply.txt"
        raw = text.encode("utf-8")
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def retry(self, f, tid, path, sha, decision="decision-001", quote="Да давай"):
        self.assertTrue(callable(getattr(f.team, "owner_new_attempt", None)),
                        "owner retry API is required")
        return f.team.owner_new_attempt(tid, owner_source_file=path, source_sha256=sha,
                                        owner_decision_id=decision, quote=quote)

    def blocked(self, f, tid="T01", *, deferred=False, terminal=True):
        f.attempt(tid)["terminal"] = terminal
        f.host.block(tid, "Owner review needed")
        if deferred:
            f.host.task(tid)["deferred"] = True
            f.host.save("task-deferred:" + tid)

    def test_scoped_retry_preserves_plan_brief_history_and_cards(self):
        f = self.fixture()
        f.state["tasks"].append(f.task("T06", "R02", "c.txt", ["T01"]))
        f.team.prepare()
        descendant = copy.deepcopy(f.attempt("T06"))
        descendant_task = copy.deepcopy(f.host.task("T06"))
        self.blocked(f, deferred=True)
        source, sha = self.source(f)
        old = copy.deepcopy(f.attempt())
        plan = copy.deepcopy(f.state["tasks"])
        briefs = copy.deepcopy(f.state["briefs"])
        scope = copy.deepcopy(f.state["requirements"])
        spec = copy.deepcopy(f.state["spec"])
        retry_id = self.retry(f, "T01", source, sha)
        task, current = f.host.task("T01"), f.attempt()
        self.assertEqual(current["id"], retry_id)
        self.assertEqual(f.state["taskHistory"]["T01"][0], old)
        self.assertEqual(f.state["briefs"], briefs)
        self.assertEqual(f.state["requirements"], scope)
        self.assertEqual(f.state["spec"], spec)
        self.assertTrue(f.state["sealed"])
        self.assertEqual([{k: v for k, v in t.items() if k not in ("status", "phase", "note", "deferred")}
                          for t in f.state["tasks"]],
                         [{k: v for k, v in t.items() if k not in ("status", "phase", "note", "deferred")}
                          for t in plan])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["phase"], "implementation")
        self.assertNotIn("deferred", task)
        self.assertEqual(current["contract"], task_contract(f.state, "T01"))
        self.assertEqual(current["briefCount"], old["briefCount"])
        self.assertEqual(set(current["roles"]), set(old["roles"]))
        self.assertNotEqual(current["roles"]["worker"]["sha256"], old["roles"]["worker"]["sha256"])
        self.assertEqual(current["authorization"]["kind"], "owner-task-attempt")
        self.assertEqual(f.store.artifact_bytes(current["authorization"]["source"]), source.read_bytes())
        self.assertTrue(_history_valid(f.store, f.state, "T01"))
        self.assertEqual(f.attempt("T06"), descendant)
        self.assertEqual(f.host.task("T06"), descendant_task)

    def test_unsubmitted_blocked_task_and_fresh_identical_decision(self):
        f = self.fixture()
        f.state["tasks"].append(f.task("T06", "R02", "c.txt"))
        f.team.prepare()
        self.blocked(f, "T06", terminal=False)
        source, sha = self.source(f)
        self.retry(f, "T06", source, sha)
        self.assertIsNone(f.attempt("T06")["submitted"])
        self.assertEqual(f.attempt("T06")["rounds"], [])
        self.blocked(f, "T06", terminal=False)
        self.retry(f, "T06", source, sha, decision="decision-002")
        self.assertEqual(len(f.state["taskHistory"]["T06"]), 3)

    def test_one_decision_may_cover_other_task_only_with_same_source_and_quote(self):
        f = self.fixture()
        self.blocked(f)
        self.blocked(f, "T02", terminal=False)
        source, sha = self.source(f)
        self.retry(f, "T01", source, sha)
        self.retry(f, "T02", source, sha)
        self.assertTrue(_history_valid(f.store, f.state, "T01"))
        self.assertTrue(_history_valid(f.store, f.state, "T02"))

    def test_replay_and_cross_task_conflict_fail_without_journal_append(self):
        f = self.fixture()
        self.blocked(f)
        source, sha = self.source(f)
        self.retry(f, "T01", source, sha)
        self.blocked(f)
        journal = f.store.run / "events.jsonl"
        before = journal.read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T01", source, sha)
        self.assertEqual(journal.read_bytes(), before)
        self.blocked(f, "T02", terminal=False)
        different, different_sha = self.source(f, "Да, повтори с другим источником\n")
        before = journal.read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T02", different, different_sha)
        self.assertEqual(journal.read_bytes(), before)

    def test_bad_source_quote_contract_and_status_are_rejected_without_effects(self):
        f = self.fixture()
        self.blocked(f)
        source, sha = self.source(f)
        journal = f.store.run / "events.jsonl"
        for path, expected, quote in ((source, "0" * 64, "Да давай"),
                                      (source, sha, "Нет")):
            before = journal.read_bytes()
            with self.assertRaises(Invalid):
                self.retry(f, "T01", path, expected, quote=quote)
            self.assertEqual(journal.read_bytes(), before)
        source.write_text("Tampered", encoding="utf-8")
        before = journal.read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T01", source, sha)
        self.assertEqual(journal.read_bytes(), before)
        source, sha = self.source(f)
        f.attempt()["contract"] = "stale"
        before = journal.read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T01", source, sha)
        self.assertEqual(journal.read_bytes(), before)
        f.attempt()["contract"] = task_contract(f.state, "T01")
        f.host.task("T01")["status"] = "pending"
        before = journal.read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T01", source, sha)
        self.assertEqual(journal.read_bytes(), before)

    def test_authorization_history_tampering_fails(self):
        f = self.fixture()
        self.blocked(f)
        source, sha = self.source(f, "Вопрос владельцу: открыть T01?\nОтвет владельца: Да давай\n")
        self.retry(f, "T01", source, sha)
        auth = f.attempt()["authorization"]
        for key, replacement in (("decisionId", "other"), ("task", "T02"),
                                 ("previousAttempt", "bad"), ("quote", "No")):
            saved = auth[key]
            auth[key] = replacement
            self.assertFalse(_history_valid(f.store, f.state, "T01"))
            auth[key] = saved
        self.assertTrue(_history_valid(f.store, f.state, "T01"))
        artifact_path = f.store.run / auth["source"]["file"]
        raw = artifact_path.read_bytes()
        artifact_path.write_bytes(b"Tampered")
        self.assertFalse(_history_valid(f.store, f.state, "T01"))
        artifact_path.write_bytes(raw)
        self.assertTrue(_history_valid(f.store, f.state, "T01"))

    def test_source_must_be_regular_and_cli_paths_are_separate(self):
        f = self.fixture()
        self.blocked(f)
        source, sha = self.source(f)
        link = f.home / "reply-link.txt"
        try:
            link.symlink_to(source)
        except OSError:  # Windows may not grant unprivileged symlink creation.
            pass
        else:
            with self.assertRaises(Invalid):
                self.retry(f, "T01", link, sha)
        with self.assertRaises(Invalid):
            self.retry(f, "T01", f.home, sha)
        owner = parser().parse_args(["new-attempt", "T01", "--owner-source-file", str(source),
                                     "--source-sha256", sha, "--owner-decision-id", "decision-001",
                                     "--quote", "Да давай"])
        legacy = parser().parse_args(["new-attempt", "T01", "--brief", "B002", "--quote", "Owner says retry"])
        self.assertEqual(owner.owner_source_file, str(source))
        self.assertEqual(legacy.brief, "B002")

    def test_fast_and_max_retry_retains_declared_class_and_mode(self):
        for mode, declared in (("fast", "heavy"), ("max", "standard")):
            with self.subTest(mode=mode):
                f = self.fixture()
                f.state.update(policy={"adaptiveCritique": True, "leanControl": True, "taskTiers": True},
                               taskHistory={}, actors={})
                check_modes.initialize(f.state, mode)
                f.state["tasks"][0].update(tier=declared, tierReason="Explicit plan class",
                                            changeKind="feature", risk="normal", riskReason="")
                f.state["tasks"][1].update(tier="heavy", tierReason="Independent sibling",
                                            changeKind="feature", risk="normal", riskReason="")
                f.team.prepare()
                previous = copy.deepcopy(f.attempt())
                self.assertEqual((previous["tier"], previous["plannedTier"], previous["checkMode"]),
                                 (("fast" if mode == "fast" else "heavy"), declared, mode))
                self.blocked(f)
                source, sha = self.source(f)
                self.retry(f, "T01", source, sha)
                current = f.attempt()
                self.assertEqual((current["tier"], current["plannedTier"], current["checkMode"]),
                                 (previous["tier"], previous["plannedTier"], previous["checkMode"]))
                f.team.validate_replan(copy.deepcopy(f.state["tasks"]))

    def test_retry_uses_current_mode_after_blocked_task_switch(self):
        for initial, current, initial_tier, next_tier in (("max", "fast", "heavy", "fast"),
                                                          ("fast", "max", "fast", "heavy")):
            with self.subTest(initial=initial, current=current):
                f = self.fixture()
                f.state.update(policy={"adaptiveCritique": True, "leanControl": True, "taskTiers": True},
                               taskHistory={}, actors={})
                check_modes.initialize(f.state, initial)
                f.state["tasks"][0].update(tier="standard", tierReason="Explicit plan class",
                                            changeKind="feature", risk="normal", riskReason="")
                f.state["tasks"][1].update(tier="heavy", tierReason="Independent sibling",
                                            changeKind="feature", risk="normal", riskReason="")
                f.team.prepare()
                self.assertEqual(f.attempt()["tier"], initial_tier)
                self.blocked(f)
                f.team.switch_check_mode(current, "Owner switched to " + current)
                self.assertEqual(check_modes.current(f.state), current)
                source, sha = self.source(f)
                self.retry(f, "T01", source, sha)
                self.assertEqual((f.attempt()["tier"], f.attempt()["plannedTier"], f.attempt()["checkMode"]),
                                 (next_tier, "standard", current))
                f.team.validate_replan(copy.deepcopy(f.state["tasks"]))

    def test_cross_task_reuse_rejects_damaged_previous_source(self):
        f = self.fixture()
        self.blocked(f)
        self.blocked(f, "T02", terminal=False)
        source, sha = self.source(f)
        self.retry(f, "T01", source, sha)
        saved = f.store.run / f.attempt()["authorization"]["source"]["file"]
        saved.write_bytes(b"corrupt")
        before = (f.store.run / "events.jsonl").read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T02", source, sha)
        self.assertEqual((f.store.run / "events.jsonl").read_bytes(), before)

    def test_secret_in_owner_source_is_rejected_before_copy(self):
        f = self.fixture()
        self.blocked(f)
        source, sha = self.source(f, "Да давай\nsk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890\n")
        before = (f.store.run / "events.jsonl").read_bytes()
        with self.assertRaises(Invalid):
            self.retry(f, "T01", source, sha)
        self.assertEqual((f.store.run / "events.jsonl").read_bytes(), before)
        self.assertFalse((f.store.run / "owner-attempt-requests").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo") and os.name == "posix", "POSIX FIFO race")
    def test_regular_file_swapped_for_fifo_never_hangs(self):
        f = self.fixture()
        source, sha = self.source(f)
        code = """import os, sys
sys.path.insert(0, sys.argv[3])
from aispa_core import owner_task_attempt as route
from aispa_core.store import Invalid
original = os.open
def raced(path, flags, *args, **kwargs):
    os.unlink(path)
    os.mkfifo(path)
    return original(path, flags, *args, **kwargs)
route.os.open = raced
try:
    route.read_source(sys.argv[1], sys.argv[2], 'Да давай', 'decision-race')
except Invalid:
    sys.exit(0)
sys.exit(3)
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(source), sha,
                                 str(Path(__file__).resolve().parents[1] / "skills/aispa/tools")],
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    def test_execute_owner_and_legacy_branches(self):
        f = self.fixture()
        self.blocked(f)
        f.state["runtimeDigest"] = runtime_digest()
        f.host.save("runtime-bound-for-cli-test")
        contextual = "Вопрос владельцу: открыть только T01?\nОтвет владельца: Да давай\n"
        source, sha = self.source(f, contextual)
        owner_args = parser().parse_args(["--root", str(f.root), "new-attempt", "T01",
                                          "--owner-source-file", str(source), "--source-sha256", sha,
                                          "--owner-decision-id", "decision-context-001", "--quote", "Да давай"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(execute(owner_args), 0)
        saved = f.store.load()
        self.assertEqual(f.store.artifact_bytes(saved["taskHistory"]["T01"][-1]["authorization"]["source"]),
                         contextual.encode("utf-8"))

        g = self.fixture()
        self.blocked(g)
        brief = g.store.artifact("briefs", b"Repeat under the legacy owner brief.\n", ".md")
        g.state["briefs"].append(dict(brief, id="B002"))
        g.state["runtimeDigest"] = runtime_digest()
        g.host.save("legacy-brief-for-cli-test")
        legacy_args = parser().parse_args(["--root", str(g.root), "new-attempt", "T01",
                                           "--brief", "B002", "--quote", "Repeat under the legacy owner brief."])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(execute(legacy_args), 0)
        self.assertEqual(g.store.load()["taskHistory"]["T01"][-1]["authorization"]["brief"], "B002")

    def test_real_deferred_chain_keeps_accepted_history_and_rebuilds_t06_closure(self):
        f = test_check_modes.DeferredDependencyTests(
            methodName="test_descendant_is_built_without_deferred_criteria")
        f.setUp()
        self.addCleanup(f.tearDown)
        plan = [f.task("T01", "R01", "a.txt")]
        plan[0]["requirements"].append("G01")
        plan.extend(f.task(tid, "R02", tid.lower() + ".txt", ["T01"])
                    for tid in ("T02", "T03", "T04", "T05", "T06"))
        f.state["tasks"] = validate_plan(f.state, plan)
        f.state.update(taskHistory={}, actors={})
        f.team.prepare()
        f.defer("T01")  # two non-PASS circles and the one extra wave
        self.assertTrue(f.host.task("T01")["deferred"])
        for tid in ("T02", "T03", "T04", "T05"):
            f.team.start_guard(tid)
            f.host.task(tid)["status"] = "running"
            for role in ("worker", "critic1"):
                f.serial += 1
                f.team.assign(tid, role, "fixture", str(f.serial), "session")
            f.verify(tid)
            self.assertTrue(f.first(tid=tid))
            self.assertTrue(historical_proof_valid(f.store, f.state, tid))
        f.host.block("T06", "Integration evidence not yet submitted")
        self.assertIsNone(f.attempt("T06")["submitted"])
        accepted = {tid: (copy.deepcopy(f.attempt(tid)), copy.deepcopy(f.host.task(tid)))
                    for tid in ("T02", "T03", "T04", "T05")}
        old_t01 = copy.deepcopy(f.state["taskHistory"]["T01"])
        plan_before = [copy.deepcopy(t) for t in f.state["tasks"]]
        source, sha = self.source(f, "Вопрос владельцу: открыть T01 и T06?\nОтвет владельца: Да давай\n")
        self.retry(f, "T01", source, sha)
        self.retry(f, "T06", source, sha)
        self.assertEqual(f.state["taskHistory"]["T01"][:-1], old_t01)
        self.assertEqual([task_contract(f.state, tid) for tid in ("T02", "T03", "T04", "T05")],
                         [attempt["contract"] for attempt, _ in accepted.values()])
        for tid, (attempt, task) in accepted.items():
            self.assertEqual(f.attempt(tid), attempt)
            self.assertEqual(f.host.task(tid), task)
            self.assertTrue(historical_proof_valid(f.store, f.state, tid))
        self.assertEqual([t["id"] for t in f.store.json(f.attempt("T06")["roles"]["worker"])["closure"]],
                         ["T01", "T06"])
        f.begin("T01")
        self.assertTrue(f.first(tid="T01"))
        for tid, (attempt, task) in accepted.items():
            self.assertEqual(f.attempt(tid), attempt)
            self.assertEqual(f.host.task(tid), task)
            self.assertTrue(historical_proof_valid(f.store, f.state, tid))
        self.assertFalse(f.team.start_guard("T06"))
        self.assertEqual([t["id"] for t in f.store.json(f.attempt("T06")["roles"]["worker"])["closure"]],
                         ["T01", "T06"])
        self.assertEqual([task["id"] for task in f.state["tasks"]], [task["id"] for task in plan_before])


if __name__ == "__main__":
    unittest.main()
