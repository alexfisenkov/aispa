"""End-to-end runs of the real AISpa CLI under the owner's check modes.

Every state change goes through a CLI command in a subprocess; the native
participants and their reports are synthetic structural fixtures (they prove
the engine's guards and bookkeeping, not model judgment), like in test_cli.
"""
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from unittest import mock

import test_check_modes
import test_cli
from tests import alpha10_fixtures as fixtures
from aispa_core import design_contract, workflow
from aispa_core.contracts import basis
from aispa_core.store import Journal, file_hash

SENTENCE = {"R01": "Первая команда выводит ready. Используй README.md как образец для команды.",
            "R02": "Вторая команда выводит done.", "R03": "Третья команда выводит final."}
BRIEF = "\n\n".join(SENTENCE.values())
HEAD = {r: s.split(".")[0] for r, s in SENTENCE.items()}  # the part the spec and the blind review quote
CRITERION = {"R01": "Выводит ready", "R02": "Выводит done", "R03": "Выводит final"}
ZONE = {"R01": "app.py", "R02": "second.py", "R03": "final.py"}
NOTES = {"R01": "notes1.md", "R02": "notes2.md", "R03": "notes3.md"}  # documentation in each zone: repairs touch it
OUTPUT = {"R01": "ready", "R02": "done", "R03": "final"}
REFERENCE = "Используй README.md как образец для команды."
COMMIT = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm")


class Scenario(unittest.TestCase):
    """Shared steps; the CLI plumbing is borrowed from test_cli by attribute."""
    # Whole-run steps (acceptance, clean clone) exceed 20 s on slow Windows CI.
    cli_timeout = 120
    setUp = test_cli.CliTests.setUp
    tearDown = test_cli.CliTests.tearDown
    git = test_cli.CliTests.git
    call = test_cli.CliTests.call
    input = test_cli.CliTests.input
    context = test_cli.CliTests.context
    discover = test_cli.CliTests.discover
    idea = test_cli.CliTests.idea
    plan_with_review = test_cli.CliTests.plan_with_review

    def verdict(self, *args, ok):
        """Commands that report a refusal (ok false) exit 2 yet still print their JSON result."""
        r = self.call(*args, expected=0 if ok else 2)
        if not ok:
            self.assertTrue(r.stdout.strip(), r.stderr)
            r = json.loads(r.stdout)
        result = r
        self.assertIs(result["ok"], ok)
        return result

    def start_run(self, check, requirements):
        state = self.call("init", "--slug", "modes", "--title", "Режимы", "--mode", "auto",
                          "--runtime", "codex", "--check", check)
        self.run = self.root / ".aispa" / state["runDirectory"]
        self.cli = self.run / "runtime/tools/aispa.py"
        self.inbox = self.root / ".aispa/inbox"
        self.inbox.mkdir()
        self.git("add", ".gitignore", "AGENTS.md")
        self.git(*COMMIT, "ignore local records")
        self.requirements = requirements
        self.runner = "python" if os.name == "nt" else "python3"
        return state

    def participant(self, role):
        return self.call("participant", "--role", role, "--runtime", "codex",
                         "--native-id", "synthetic-" + role, "--session", "test-session")["receipt"]["actor"]

    def write_project(self):
        for rid in self.requirements:
            (self.root / ZONE[rid]).write_text("print('%s')\n" % OUTPUT[rid], encoding="utf-8", newline="\n")
            (self.root / NOTES[rid]).write_text("# Notes\n", encoding="utf-8", newline="\n")
        (self.root / "README.md").write_text(
            "# CLI fixture\n\n## Quickstart\nPython 3.11+; run `python3 app.py` on macOS or `python app.py` on Windows.\n"
            "The program prints ready. Python standard library only.\n"
            "The whole one-statement program is the check.\n"
            "One print statement has no separate architecture.\n"
            "No material technical alternative in this synthetic fixture.\n"
            "No domain terminology beyond standard output.\n", encoding="utf-8", newline="\n")
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/check_output.py").write_text(
            "import os, subprocess, sys\n"
            "assert subprocess.check_output([sys.executable, 'app.py'], text=True).strip() == 'ready'\n"
            "read_fd, write_fd = os.pipe()\n"
            "os.close(read_fd)\n"
            "try:\n"
            "    result = subprocess.run([sys.executable, 'app.py'], stdout=write_fd, stderr=subprocess.PIPE)\n"
            "    assert result.returncode != 0, 'closed stdout must not silently succeed'\n"
            "finally:\n"
            "    os.close(write_fd)\n"
            "print('ready and closed-output checks passed')\n", encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git(*COMMIT, "synthetic project")

    def source_map(self, context, actor):
        rows = []
        for brief in context["briefs"]:
            for unit in brief["units"]:
                rid = next((r for r in self.requirements if SENTENCE[r] in unit["text"]), "R01")
                rows.append({"brief": unit["brief"], "start": unit["start"], "end": unit["end"],
                             "disposition": "requirement", "requirements": [rid],
                             "reason": "This source unit maps to the stated output requirement."})
        return {"basis": context["basis"], "actor": actor, "rows": rows}

    def feature(self, state, rid):
        row = fixtures.scenarios(state, source_quote=SENTENCE[rid], spec_quote=HEAD[rid])["features"][0]
        for cell in row["dimensions"]:  # max asks a concrete counterexample for every covered dimension
            if cell["status"] == "covered":
                cell["counterexample"] = "If stdout printed anything but %s the scenario would fail." % OUTPUT[rid]
        return dict(row, requirement=rid)

    def prepare(self, check, requirements=("R01", "R02")):
        """Brief, requirements, source map, idea, discovery, spec, coverage, scenarios, interfaces."""
        self.start_run(check, requirements)
        self.write_project()
        coordinator = self.participant("coordinator")
        analyst = self.participant("brief-analyst")
        reviewer = self.participant("brief-reviewer")
        brief = "\n\n".join(SENTENCE[r] for r in requirements)
        self.call("brief", "--file", self.input(brief))
        self.call("requirements", "--file", self.input([
            {"id": r, "text": "Команда " + r, "brief": "B001", "quote": SENTENCE[r], "core": True,
             "acceptance": [CRITERION[r]]} for r in requirements]))
        self.call("reference", "--file", self.input({
            "path": "README.md", "sha256": file_hash(self.root / "README.md"), "brief": "B001", "quote": REFERENCE,
            "requirements": ["R01"], "use": "Use the owner's local README quickstart as a concrete reference."},
            "owner-reference.json"))
        original = self.call("brief-source")
        self.call("brief-extract", "--file", self.input(fixtures.source_report(original, analyst), "source-analysis.json"))
        independent = self.call("brief-source")
        self.call("brief-review", "--file", self.input(fixtures.source_report(independent, reviewer), "source-review.json"))
        mapping = self.call("brief-map-context")
        self.call("brief-map", "--file", self.input(self.source_map(mapping, reviewer), "source-map.json"))
        self.idea()
        self.call("seal")
        self.discover()
        challenge = self.call("idea", "--full")["report"]["challenge"]
        spec = " ".join(HEAD[r] + "." for r in requirements) + "\n" + "\n".join(
            row["decision"] + "\n" + row["validation"] for row in challenge)
        self.call("spec", "--file", self.input(spec))
        ctx = self.context("coverage")
        self.call("coverage", "--file", self.input({
            "basis": ctx["basis"], "reviewer": self.participant("coverage"), "independent": True, "unmapped": [],
            "checks": [{"requirement": r, "status": "covered", "specQuote": HEAD[r]} for r in requirements],
            "ideaChecks": [{"probe": row["probe"], "status": "covered", "specQuote": row["decision"] + "\n" + row["validation"],
                            "note": "Synthetic correspondence; this test is not semantic review."} for row in challenge]}))
        state = Journal(self.root).load()
        scenarios = fixtures.scenarios(state, source_quote=BRIEF, spec_quote="x")
        scenarios["features"] = [self.feature(state, r) for r in requirements]
        self.call("scenarios", "--file", self.input(scenarios, "scenarios.json"))
        self.call("interfaces", "--file", self.input(fixtures.interfaces(Journal(self.root).load()), "interfaces.json"))
        return coordinator

    def plan(self, tasks):
        self.call("plan", "--file", self.input(tasks), expected=2)  # a plan without the size review is refused
        self.plan_with_review(tasks)

    def task(self, tid, rid, deps=()):
        return {"id": tid, "title": "Задача " + tid, "requirements": [rid], "dependsOn": list(deps),
                "zones": [ZONE[rid], NOTES[rid]], "readZones": ["."], "criteria": [CRITERION[rid]]}

    def seat(self, tid, *roles):
        ctx = self.context("task", "--task", tid)
        self.call("start", tid, "--context", ctx["basis"])
        for role in roles:
            self.call("assign", tid, "--role", role, "--runtime", "codex", "--native-id", "synthetic-%s-%s" % (tid, role),
                      "--session", "test-session")

    def assign(self, tid, role, suffix=""):
        self.call("assign", tid, "--role", role, "--runtime", "codex",
                  "--native-id", "synthetic-%s-%s%s" % (tid, role, suffix), "--session", "test-session")

    def intent(self, tid, rid):
        """Heavy work states its test contract; these tasks keep existing behavior, so it is not applicable."""
        ctx = self.context("task", "--task", tid)
        test = self.root / "tests/check_output.py"
        value = {"mode": "not_applicable", "attempt": ctx["attempt"], "basis": ctx["basis"], "criteria": [CRITERION[rid]],
                 "notApplicable": [{"criterion": CRITERION[rid], "purpose": "existing_behavior",
                                    # The contract freezes before the critics, so its source is a file no repair touches.
                                    "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")},
                                    "reason": "The documented command already prints the output; this checks it without changing it."}],
                 "failureChecks": {
                     "cases": [{"id": "E01", "boundary": "output", "scenario": "Closed stdout has no readers",
                                "expected": "Actual child process must report failure rather than silently succeed",
                                "criterion": CRITERION[rid], "test": {"path": "tests/check_output.py", "sha256": file_hash(test)}}],
                     "notApplicable": [{"boundaries": ["input", "storage", "protocol", "dependency"],
                                        "reason": "The declared one-statement fixture has no inputs, persistence, protocol or external dependencies.",
                                        "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")}}]}}
        self.call("test-intent", "--task", tid, "--file", self.input(value, "intent-%s.json" % tid))

    def verify_task(self, tid):
        return self.call("verify", "--task", tid, "--", sys.executable, "tests/check_output.py")

    def report(self, tid, role, rid, **extra):
        """A structurally complete report built from the real task context of this participant."""
        bundle = self.context("task", "--task", tid, "--role", role, "--write", "--with-template")
        ctx = json.loads(Path(bundle["context"]["absolutePath"]).read_text(encoding="utf-8"))
        for key, section in ctx.pop("sections", {}).items():
            ctx[key] = json.loads(Path(section["absolutePath"]).read_text(encoding="utf-8"))
        evidence = ctx["verification"]["output"]["file"]
        checks = ctx.get("expectedChecks", [{"requirement": rid, "criterion": CRITERION[rid]}])
        report = {"basis": ctx["basis"], "reviewer": ctx["actors"][role], "independent": role != "worker",
                  "unmapped": [], "checks": [dict(c, status="passed", artifact=evidence, note="Проверено тестовой командой")
                                             for c in checks]}
        if role != "worker":
            report.update(testSourceChanges=[], verdict="PASS", findings=[], inheritedProofs=ctx["inheritedProofs"],
                          trace=[{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                                  "note": "Сопоставлено с брифом", "artifact": evidence} for r in ctx["traceRequirements"]])
            if ctx.get("zoneChangesScope") == "branch":
                report["branchReview"] = fixtures.branch_review(self.root, tid, evidence)
        report.update(extra)
        return report, ctx

    def submit(self, tid, rid, **extra):
        report, _ = self.report(tid, "worker", rid, **extra)
        return self.call("submit", tid, "--file", self.input(report, "submit-%s.json" % tid))

    def refuse(self, tid, rid, round_number, finding, previous=None, repair=None, resolutions=()):
        """A critic round that refuses: FAIL with one finding that names the task's own criterion."""
        role = "critic%d" % round_number
        report, ctx = self.report(tid, role, rid, verdict="FAIL", findings=[
            {"id": finding, "severity": "blocking", "text": "Required correction", "criterion": CRITERION[rid]}])
        if round_number == 2:
            report.update(previousReport=previous, repairReceipt=repair, resolutions=list(resolutions))
        return self.verdict("critic", tid, "--round", str(round_number),
                            "--file", self.input(report, "critic-%s-%d.json" % (tid, round_number)), ok=False)

    def attempt(self, tid):
        history = Journal(self.root).load()["taskHistory"][tid]
        return history[-1]

    def repair(self, tid, rid, finding):
        """The worker changes the zone's notes (the checked behavior stays), re-verifies and files the repair."""
        first = self.attempt(tid)["rounds"][0]["report"]["sha256"]
        diagnosis = self.call("repair-plan", tid)
        self.call("repair-plan", tid, "--file", self.input({
            "basis": diagnosis["basis"], "cause": "implementation_gap", "route": "same", "addresses": [finding],
            "diagnosis": "The synthetic worker left a local gap in its own zone.",
            "reason": "A local defect: the original worker repairs it.",
            "changes": "Touch the zone file and re-run the task verification.", "context": []}, "repair-plan-%s.json" % tid))
        notes = self.root / NOTES[rid]
        notes.write_text(notes.read_text(encoding="utf-8") + "Repaired.\n", encoding="utf-8", newline="\n")
        self.verify_task(tid)
        report, _ = self.report(tid, "worker", rid, previousReport=first, addresses=[finding])
        self.call("repair", tid, "--file", self.input(report, "repair-%s.json" % tid))

    def status(self):
        return self.call("status")

    def task_view(self, tid):
        return next(t for t in self.status()["tasks"] if t["id"] == tid)

    def commit(self, message):
        self.git("add", "-A")
        staged = subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--quiet"])
        if staged.returncode:  # nothing to commit when the workers changed no file
            self.git(*COMMIT, message)

    def wave(self, number, failed=None):
        """Wave check against the whole brief; requirements named in failed are reported as not tested."""
        failed = failed or {}
        ctx = self.context("wave")
        report = {"basis": ctx["basis"], "reviewer": "synthetic", "independent": True, "unmapped": [],
                  "checks": [{"requirement": r, "criterion": CRITERION[r], "status": failed.get(r, "passed"),
                              "artifact": ctx["verification"]["output"]["file"], "note": "Проверено тестовой командой"}
                             for r in self.requirements]}
        return self.call("wave", str(number), "--file", self.input(report, "wave-%d.json" % number))

    def record_knowledge(self):
        self.call("finish", expected=2)  # the knowledge of the finished code comes first
        writer = self.call("knowledge-assign", "--runtime", "codex", "--native-id", "synthetic-knowledge",
                           "--session", "fresh-knowledge-session")["receipt"]["actor"]
        learned = self.context("knowledge")
        self.call("knowledge-record", "--file", self.input({"writer": writer["id"], "basis": learned["basis"],
            "entries": [{"key": "cli.ready", "kind": "command", "text": "Synthetic: stdout check of this CLI.",
                         "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                         "command": [sys.executable, "app.py"],
                         "verification": {"kind": "execution", "sha256": self.run_receipt["sha256"]}}],
            "uncertainties": {k: "Synthetic fixture did not establish additional knowledge in this category."
                              for k in ("trap", "decision", "glossary")}}))

    def document_blockers(self, *blocked):
        """Every blocked task must be documented as a blocker in a tracked document of the handoff.

        Written before the final task is reviewed: later repository changes would stale that review."""
        (self.root / "docs").mkdir(exist_ok=True)
        text = "# Open items\n\n## Blockers\n" + "".join(
            "Task %s is deferred to the next run by the owner's rules.\n" % tid for tid in blocked)
        (self.root / "docs/open.md").write_text(text, encoding="utf-8", newline="\n")
        self.commit("document blockers")
        self.blocked = blocked

    def finish_delivery(self, failed, blind_status=None, accepted=True, last_wave=None):
        """Handoff, clean clone, blind observations and acceptance where `failed` maps requirement to status."""
        blocked = getattr(self, "blocked", ())
        documents = [{"path": "docs/open.md", "sha256": file_hash(self.root / "docs/open.md"),
                      "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                      "covers": {"glossary": "Blockers"}}] if blocked else []
        obligations = [{"status": "blocked", "task": tid, "document": "docs/open.md", "heading": "Blockers",
                        "text": "Task %s is deferred to the next run by the owner's rules." % tid} for tid in blocked]
        run = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        check = self.call("verify", "--purpose", "check", "--", sys.executable, "app.py")["receipt"]
        runner = self.runner
        if last_wave:  # the last wave's check is bound to the verification it follows
            self.wave(*last_wave)
        self.call("handoff", "--file", self.input({"schema": 1, "profile": "small",
            "documents": [{"path": "README.md", "sha256": file_hash(self.root / "README.md"),
                           "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                           "covers": {"quickstart": "Quickstart"}}] + documents,
            "notApplicable": {k: v for k, v in {
                "architecture": "One print statement has no separate architecture.",
                "decisions": "No material technical alternative in this synthetic fixture.",
                "glossary": "No domain terminology beyond standard output."}.items() if not (blocked and k == "glossary")},
            "commands": {"check": {"receipt": check, "document": "README.md", "heading": "Quickstart",
                                  "display": runner + " app.py", "runner": runner},
                         "run": {"receipt": run, "document": "README.md", "heading": "Quickstart",
                                 "display": runner + " app.py", "runner": runner},
                         "install": {"notApplicable": "Python standard library only."},
                         "singleTest": {"notApplicable": "The whole one-statement program is the check."}},
            "obligations": obligations}))
        self.run_receipt = run
        self.call("clean-prepare")
        clean = self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")["receipt"]
        blind_actor = self.participant("blind")
        blind = self.context("blind")
        observations = [{"brief": "B001", "quote": HEAD[r], "status": (blind_status or {}).get(r, "passed"),
                         "note": "Read original brief first; synthetic clean clone assertion", "artifact": clean["output"]["file"]}
                        for r in self.requirements]
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": blind_actor, "independent": True,
                                                 "observations": observations}))
        ctx = self.context("acceptance")
        evidence = ctx["verification"]["output"]["file"]
        checks = [{"requirement": r, "criterion": CRITERION[r], "status": failed.get(r, "passed"), "artifact": evidence,
                   "note": "Проверено тестовой командой"} for r in self.requirements]
        report = {"basis": ctx["basis"], "reviewer": blind_actor, "independent": True, "unmapped": [], "checks": checks,
                  "cleanrun": (ctx.get("cleanrun") or {}).get("artifact", {}).get("sha256"),
                  "blindReport": ctx["blindReview"]["report"]["sha256"],
                  "blindMapping": [{"observation": i, "requirement": r} for i, r in enumerate(self.requirements)]}
        report['productFlowReview'] = fixtures.final_flows(ctx, self.run, checks)
        return self.verdict("acceptance", "--file", self.input(report, "acceptance.json"), ok=accepted)


class FastModeE2E(Scenario):
    def test_fast_run_cannot_deliver_an_unchecked_requested_primary_flow(self):
        self.prepare("fast")
        self.plan([self.task("T01", "R01"), self.task("T02", "R02", ["T01"])])
        self.assertEqual(self.status()["checkMode"], "fast")
        self.seat("T01", "worker")
        attempt = self.attempt("T01")
        self.assertEqual((attempt["tier"], attempt["checkMode"]), ("fast", "fast"))
        self.assertEqual(set(attempt["roles"]), {"worker"})
        # No test contract and no critics: a verified worker submission completes the task.
        self.call("assign", "T01", "--role", "critic1", "--runtime", "codex", "--native-id", "x", "--session", "s", expected=2)
        self.verify_task("T01")
        self.submit("T01", "R01")
        view = self.task_view("T01")
        self.assertEqual((view["status"], view["completionKind"]), ("completed", "verified_completion"))
        # The next wave starts without a whole-brief check of the previous one.
        self.seat("T02", "worker")
        self.verify_task("T02")
        self.submit("T02", "R02")
        self.assertEqual(self.task_view("T02")["status"], "completed")
        self.commit("fast tasks")
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.call("acceptance", "--file", self.input({}), expected=2)
        self.finish_delivery({"R02": "not_tested"}, {"R02": "not_tested"}, accepted=False)
        self.assertEqual(self.status()["checkMode"], "fast")
        self.call('finish', expected=2)
        state = Journal(self.root).load()
        self.assertFalse(self.status()['gates']['acceptance'])
        self.assertTrue(any(row.get('kind') == 'product-flow' and row['requirement'] == 'R02'
                            for row in state['gates']['acceptance']['failures']))
        self.assertNotIn('remaining', state)


class MaxModeE2E(Scenario):
    def heavy_round(self, tid, rid, suffix=""):
        """Seat worker and critic, state the test contract, verify, submit and get critic 1's refusal."""
        self.seat(tid, "worker", "critic1") if not suffix else (
            self.seat(tid), self.assign(tid, "worker", suffix), self.assign(tid, "critic1", suffix))
        self.intent(tid, rid)
        self.verify_task(tid)
        self.submit(tid, rid)
        self.verdict_refusal = self.refuse(tid, rid, 1, "F1")
        self.assertEqual(self.task_view(tid)["phase"], "repair")
        self.repair(tid, rid, "F1")
        self.assign(tid, "critic2", suffix)
        attempt = self.attempt(tid)
        self.refuse(tid, rid, 2, "F2", previous=attempt["rounds"][0]["report"]["sha256"],
                    repair=attempt["repair"]["report"]["sha256"],
                    resolutions=[{"id": "F1", "status": "resolved", "note": "Checked repair",
                                  "artifact": self.context("task", "--task", tid)["verification"]["output"]["file"]}])

    def complete(self, tid, rid):
        self.seat(tid, "worker", "critic1")
        self.intent(tid, rid)
        self.verify_task(tid)
        self.submit(tid, rid)
        report, _ = self.report(tid, "critic1", rid)
        self.call("critic", tid, "--round", "1", "--file", self.input(report, "critic-%s.json" % tid))
        view = self.task_view(tid)
        self.assertEqual((view["status"], view["completionKind"]), ("completed", "critic_acceptance"))

    def test_deferred_primary_flow_blocks_finish_without_stopping_dependants(self):
        self.prepare("max", ("R01", "R02", "R03"))
        self.plan([self.task("T01", "R01"), self.task("T02", "R02", ["T01"]), self.task("T03", "R03", ["T02"])])
        self.assertEqual(self.status()["checkMode"], "max")
        self.heavy_round("T01", "R01")
        view = self.task_view("T01")
        self.assertEqual(view["status"], "blocked")
        self.assertEqual(len(self.attempt("T01")["rounds"]), 2)
        self.assertFalse(self.task_view("T01").get("deferred"))
        # The one extra wave: allowed once, only after both circles were spent.
        self.call("extra-wave", "T01")
        self.assertEqual(self.attempt("T01")["authorization"]["kind"], "extra-wave")
        self.heavy_round("T01", "R01", "-w2")
        self.assertTrue(self.task_view("T01")["deferred"])
        self.call("extra-wave", "T01", expected=2)
        self.commit("T01 attempts")
        self.document_blockers("T01")
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        # Nothing of wave 1 was delivered, so its undelivered requirements do not fail the wave check.
        self.assertTrue(self.wave(1, {r: "not_tested" for r in self.requirements})["ok"])
        # T02 starts and closes on its own criteria, without the deferred T01 criteria.
        self.complete("T02", "R02")
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.wave(2, {"R01": "not_tested", "R03": "not_tested"})
        self.complete("T03", "R03")
        self.commit("tasks")
        self.finish_delivery({"R01": "not_tested"}, {"R01": "not_tested"}, accepted=False,
                             last_wave=(3, {"R01": "not_tested"}))
        self.call('finish', expected=2)
        state = Journal(self.root).load()
        self.assertEqual(state['tasks'][0]['status'], 'blocked')
        self.assertFalse(state['gates']['acceptance'].get('deferredSettled', False))
        self.assertTrue(any(f.get('kind') == 'product-flow' and f['requirement'] == 'R01'
                            for f in state['gates']['acceptance']['failures']))


class SwitchModeE2E(Scenario):
    complete = MaxModeE2E.complete  # a heavy task closed by its passing critic

    def test_switch_to_max_gives_the_next_task_a_heavy_attempt(self):
        self.prepare("balanced")
        second = dict(self.task("T02", "R02", ["T01"]), tier="standard", tierReason="Ordinary isolated module",
                      changeKind="feature")
        self.plan([self.task("T01", "R01"), second])
        status = self.status()
        self.assertEqual((status["checkMode"], len(status["checkModeHistory"])), ("balanced", 0))
        self.complete("T01", "R01")
        self.assertNotIn("checkMode", self.attempt("T01"))  # balanced keeps the plan's class and leaves no mark
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.wave(1, {"R02": "not_tested"})
        words = "Давай теперь максимально строго проверим вторую команду."
        self.call("check-mode", "max", "--file", self.input(words, "owner-words.md"))
        status = self.status()
        self.assertEqual(status["checkMode"], "max")
        # The switch is recorded as the owner's own words, apart from the sealed brief.
        self.assertEqual([(row["previous"], row["mode"], row["text"]) for row in status["checkModeHistory"]],
                         [("balanced", "max", words)])
        # Finished work keeps the mode it was done under; the idle task takes the new class.
        self.assertNotIn("checkMode", self.attempt("T01"))
        self.assertEqual(self.attempt("T01")["tier"], "heavy")
        self.seat("T02", "worker", "critic1")
        attempt = self.attempt("T02")
        self.assertEqual((attempt["tier"], attempt["plannedTier"], attempt["checkMode"]), ("heavy", "standard", "max"))
        self.assertEqual(set(attempt["roles"]), {"worker", "critic1", "critic2"})
        self.intent("T02", "R02")
        self.verify_task("T02")
        self.submit("T02", "R02")
        report, _ = self.report("T02", "critic1", "R02")
        self.call("critic", "T02", "--round", "1", "--file", self.input(report, "critic-T02.json"))
        view = self.task_view("T02")
        self.assertEqual((view["status"], view["completionKind"]), ("completed", "critic_acceptance"))
        # The attempt carries a check-mode authorization without an owner quote; delivery must still work.
        self.assertEqual(self.attempt("T02")["authorization"]["kind"], "check-mode")
        self.commit("tasks")
        self.finish_delivery({}, last_wave=(2, {}))
        self.record_knowledge()
        self.assertEqual(self.call("finish")["status"], "accepted")
        self.assertEqual(len(self.status()["checkModeHistory"]), 1)


class PromoteE2E(Scenario):
    complete = MaxModeE2E.complete

    def test_promoted_standard_task_is_delivered(self):
        self.prepare("balanced", ("R01",))
        self.plan([dict(self.task("T01", "R01"), tier="standard", tierReason="Ordinary isolated module",
                        changeKind="feature")])
        self.seat("T01", "worker")
        self.assertEqual(self.attempt("T01")["tier"], "standard")
        self.call("promote-task", "T01", "--reason", "The work turned out to touch shared output handling")
        promoted = self.attempt("T01")
        self.assertEqual((promoted["tier"], promoted["authorization"]["kind"]), ("heavy", "tier-escalation"))
        self.seat("T01")  # fresh actors: the old worker belongs to the light attempt
        for role in ("worker", "critic1"):
            self.assign("T01", role, "-promoted")
        self.intent("T01", "R01")
        self.verify_task("T01")
        self.submit("T01", "R01")
        report, _ = self.report("T01", "critic1", "R01")
        self.call("critic", "T01", "--round", "1", "--file", self.input(report, "critic-T01.json"))
        self.assertEqual(self.task_view("T01")["completionKind"], "critic_acceptance")
        self.commit("tasks")
        self.finish_delivery({})
        self.record_knowledge()
        self.assertEqual(self.call("finish")["status"], "accepted")


class StartOrderTests(test_check_modes.ModeCase):
    """Workflow level: dependency proofs are read before start_guard adds its unsaved baseline."""

    def test_dependency_proofs_are_checked_before_the_guard_mutates_the_attempt(self):
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('a')\n", encoding="utf-8")
        (self.root / "lib").mkdir()
        (self.root / "lib/m.py").write_text("x = 1\n", encoding="utf-8")
        self.run_as("max", [self.standard("T01", "src"), self.standard("T02", "lib", ["T01"])])
        self.host.task("T02")["wave"] = 1
        self.begin("T01")
        self.assertTrue(self.first())
        order = []
        proofs, guard = workflow.task_proof_valid, self.team.start_guard

        def proof(*args, **kwargs):
            order.append("proof")
            return proofs(*args, **kwargs)

        def guarded(tid):
            order.append("guard")
            return guard(tid)

        with mock.patch.object(workflow, "task_proof_valid", proof), \
                mock.patch.object(self.team, "start_guard", guarded):
            self.host.start("T02", basis(self.store, self.state, "task", "T02"))
        self.assertEqual(order, ["proof", "guard"])


if __name__ == "__main__":
    unittest.main()
