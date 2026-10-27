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
    branch_review = test_cli.CliTests.branch_review

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
        return self.call("participant", "--role", role, "--runtime", "codex", *test_cli.route_args(role),
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

    # Requirements without a critical user scenario: focused runs keep their tasks standard.
    flowless = ()

    def feature(self, state, rid):
        row = fixtures.scenarios(state, source_quote=SENTENCE[rid], spec_quote=HEAD[rid])["features"][0]
        for cell in row["dimensions"]:  # max asks a concrete counterexample for every covered dimension
            if cell["status"] == "covered":
                cell["counterexample"] = "If stdout printed anything but %s the scenario would fail." % OUTPUT[rid]
        if rid in self.flowless:
            row["productFlows"] = {"applicability": "not_applicable", "flows": [],
                                   "reason": "The command has no interactive user action beyond its output."}
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
            self.call("assign", tid, "--role", role, "--runtime", "codex", *test_cli.route_args(role),
                      "--native-id", "synthetic-%s-%s" % (tid, role),
                      "--session", "test-session")

    def assign(self, tid, role, suffix=""):
        self.call("assign", tid, "--role", role, "--runtime", "codex", *test_cli.route_args(role),
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
            {"id": finding, "severity": "blocking", "blockingClass": "failing_tests", "text": "Required correction",
             "criterion": CRITERION[rid]}])
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

    def no_wave_check(self, number):
        """Focused runs check no wave: the brief is checked at the start and by the blind acceptance."""
        refused = self.call("wave", str(number), "--file", self.input({}, "wave-%d.json" % number), expected=2)
        self.assertIn("волны не сверяются; бриф проверяют на входе и в слепой приёмке", refused.stderr)

    def record_knowledge(self):
        self.call("finish", expected=2)  # the knowledge of the finished code comes first
        writer = self.call("knowledge-assign", "--runtime", "codex", *test_cli.route_args("knowledge-writer"),
                           "--native-id", "synthetic-knowledge",
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

    def finish_delivery(self, failed, blind_status=None, accepted=True):
        """Lens reviews of the branch, handoff, clean clone, blind observations and acceptance.

        `failed` maps a requirement to its acceptance status, `blind_status` to its blind observation."""
        blocked = getattr(self, "blocked", ())
        documents = [{"path": "docs/open.md", "sha256": file_hash(self.root / "docs/open.md"),
                      "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                      "covers": {"glossary": "Blockers"}}] if blocked else []
        obligations = [{"status": "blocked", "task": tid, "document": "docs/open.md", "heading": "Blockers",
                        "text": "Task %s is deferred to the next run by the owner's rules." % tid} for tid in blocked]
        run = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        check = self.call("verify", "--purpose", "check", "--", sys.executable, "app.py")["receipt"]
        runner = self.runner
        refused = self.call("clean-prepare", expected=2)
        self.assertIn("проверка всей ветки", refused.stderr)
        self.branch_review()  # 1, 2 or 3 fresh lens reviewers by the mode, in every mode
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
        # Focused: the engine weighs the task; fast still gives a critic to a critical user scenario.
        MaxModeE2E.complete(self, "T01", "R01")
        attempt = self.attempt("T01")
        self.assertEqual((attempt["tier"], attempt["checkMode"]), ("heavy", "fast"))
        self.assertEqual(attempt["criticRoles"], ["worker", "critic1", "critic2"])
        self.assertEqual(set(attempt["roles"]), {"worker", "critic1"})
        # The next wave starts without a whole-brief check of the previous one.
        MaxModeE2E.complete(self, "T02", "R02")
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
        self.assertEqual(len(state["projectIntegrationReviewHistory"]), 1)  # fast: one reviewer with every axis
        # Fast repairs the divergence too: the failed acceptance opens a blind repair of exactly its item.
        repair = self.call("final-repair", "open", "--file",
                           self.input({"kind": "blind", "zones": [ZONE["R02"]]}, "repair.json"))["receipt"]["repair"]
        self.assertEqual((repair["kind"], repair["items"]), ("blind", ["R02"]))


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
        if self.attempt(tid).get("tier", "heavy") == "heavy":
            self.intent(tid, rid)
        self.verify_task(tid)
        self.submit(tid, rid)
        report, _ = self.report(tid, "critic1", rid)
        self.call("critic", tid, "--round", "1", "--file", self.input(report, "critic-%s.json" % tid))
        if self.task_view(tid)["phase"] == "critic_2":
            # max: a heavy task gets a second fresh critic after the first PASS.
            self.assign(tid, "critic2")
            first = self.attempt(tid)["rounds"][0]["report"]["sha256"]
            report, _ = self.report(tid, "critic2", rid, previousReport=first, repairReceipt=None, resolutions=[])
            self.call("critic", tid, "--round", "2", "--file", self.input(report, "critic2-%s.json" % tid))
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
        # No wave is checked; T02 starts and closes on its own criteria, without the deferred T01 criteria.
        self.no_wave_check(1)
        self.complete("T02", "R02")
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.no_wave_check(2)
        self.complete("T03", "R03")
        self.commit("tasks")
        self.finish_delivery({"R01": "not_tested"}, {"R01": "not_tested"}, accepted=False)
        self.assertEqual(len(Journal(self.root).load()["projectIntegrationReviewHistory"]), 3)  # max: three lenses
        self.call('finish', expected=2)
        state = Journal(self.root).load()
        self.assertEqual(state['tasks'][0]['status'], 'blocked')
        self.assertFalse(state['gates']['acceptance'].get('deferredSettled', False))
        self.assertTrue(any(f.get('kind') == 'product-flow' and f['requirement'] == 'R01'
                            for f in state['gates']['acceptance']['failures']))


class SwitchModeE2E(Scenario):
    complete = MaxModeE2E.complete  # a heavy task closed by its passing critic
    flowless = ("R02",)

    def test_switch_to_max_gives_the_next_task_a_heavy_attempt(self):
        self.prepare("balanced")
        second = dict(self.task("T02", "R02", ["T01"]), tier="standard", tierReason="Ordinary isolated module",
                      changeKind="feature")
        self.plan([self.task("T01", "R01"), second])
        status = self.status()
        self.assertEqual((status["checkMode"], len(status["checkModeHistory"])), ("balanced", 0))
        self.complete("T01", "R01")
        self.assertEqual(self.attempt("T01")["checkMode"], "balanced")  # the mode its reviews were frozen under
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.no_wave_check(1)
        words = "Давай теперь максимально строго проверим вторую команду."
        self.call("check-mode", "max", "--file", self.input(words, "owner-words.md"))
        status = self.status()
        self.assertEqual(status["checkMode"], "max")
        # The switch is recorded as the owner's own words, apart from the sealed brief.
        self.assertEqual([(row["previous"], row["mode"], row["text"]) for row in status["checkModeHistory"]],
                         [("balanced", "max", words)])
        # Finished work keeps the mode it was done under; the idle task takes the new number of reviews.
        self.assertEqual(self.attempt("T01")["checkMode"], "balanced")
        self.assertEqual(self.attempt("T01")["tier"], "heavy")
        self.seat("T02", "worker", "critic1")
        attempt = self.attempt("T02")
        # The engine keeps the weight (standard: no dependants, no critical scenario); max adds the critic.
        self.assertEqual((attempt["tier"], attempt["checkMode"], attempt["criticRoles"]),
                         ("standard", "max", ["worker", "critic1", "critic2"]))
        self.assertEqual(set(attempt["roles"]), {"worker", "critic1"})
        self.verify_task("T02")
        self.submit("T02", "R02")
        report, _ = self.report("T02", "critic1", "R02")
        self.call("critic", "T02", "--round", "1", "--file", self.input(report, "critic-T02.json"))
        view = self.task_view("T02")
        self.assertEqual((view["status"], view["completionKind"]), ("completed", "critic_acceptance"))
        # The attempt carries a check-mode authorization without an owner quote; delivery must still work.
        self.assertEqual(self.attempt("T02")["authorization"]["kind"], "check-mode")
        self.commit("tasks")
        self.finish_delivery({})
        self.record_knowledge()
        self.assertEqual(self.call("finish")["status"], "accepted")
        self.assertEqual(len(self.status()["checkModeHistory"]), 1)


class PromoteE2E(Scenario):
    complete = MaxModeE2E.complete
    flowless = ("R01",)

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


ADMISSION_T0 = "2026-10-26T01:00:00+00:00"
ADMISSION_T1 = "2026-10-26T01:01:00+00:00"
ADMISSION_T2 = "2026-10-26T01:02:00+00:00"
ADMISSION_T3 = "2026-10-26T01:02:30+00:00"


def managed_admission_fixture(case, *, session="admission-session"):
    """A real disposable journal/registry; no native model or browser fixture."""
    import shutil
    import aispa
    from aispa_core import preflight
    from aispa_core import resources
    runtime = case.store.run / "runtime"
    shutil.copytree(aispa.PACKAGE, runtime, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    case.state["runtimeDigest"] = aispa.runtime_digest()
    case.assertEqual(preflight._package_digest(runtime), case.state["runtimeDigest"])
    case.state.update(policy={"managedExecution": True}, runtime="codex",
                      startedAt=ADMISSION_T0, elapsedSeconds=0.0)
    if case.state["status"] == "running":
        case.state["activeSince"] = ADMISSION_T0
    else:
        case.state.pop("activeSince", None)
    if session is None:
        case.state.pop("hostSession", None)
    else:
        case.state["hostSession"] = session
    resources.configure(case.store, case.state, {"schema": 1, "scope": "project", "limits":
        {"agents": 1, "browsers": 1, "testProcesses": 1, "services": 1}})
    case.flow = workflow.Workflow(case.store, case.state)
    case.flow.save("managed-admission-fixture", ADMISSION_T0)


def recorded_resource_wait(case, kind="testProcesses", *, wait_kind="resource_wait"):
    from aispa_core import timing
    timing.begin_wait(case.state, wait_kind, ADMISSION_T1)
    if kind is not None:
        case.state["waiting"]["resourceKind"] = kind
    case.flow.save("resource-wait-fixture", ADMISSION_T1)


def admission_cli(case, *arguments, expected=0):
    """The real CLI parser/controller/lock path, with output captured in-process."""
    from contextlib import redirect_stdout
    import io
    import aispa
    output = io.StringIO()
    args = aispa.parser().parse_args(["--root", str(case.store.root), *arguments])
    with redirect_stdout(output):
        code = aispa.execute(args)
    case.assertEqual(code, expected, output.getvalue())
    return json.loads(output.getvalue())


class ResourceAdmissionRepairTests(unittest.TestCase):
    import test_workflow as _fixture
    setUp = _fixture.WorkflowTests.setUp
    tearDown = _fixture.WorkflowTests.tearDown
    git = _fixture.WorkflowTests.git
    commit = _fixture.WorkflowTests.commit

    def test_verify_capacity_retry_resumes_saved_clock_before_child_start(self):
        from aispa_core import process_runner, resources, timing
        managed_admission_fixture(self)
        ctx = resources.context(self.store, self.state)
        held = resources.reserve(ctx, "testProcesses")
        with mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T1), \
                mock.patch.object(process_runner, "_start") as blocked_start, \
                self.assertRaises(resources.CapacityRefused):
            self.flow.verify([sys.executable, "-c", "print('blocked')"], 5)
        blocked_start.assert_not_called()
        self.assertEqual(self.state["waiting"]["resourceKind"], "testProcesses")
        resources.finish(ctx, held["id"], outcome="launch_failed", confirmed=True)
        self.assertEqual(self.store.load()["waiting"]["since"], ADMISSION_T1,
                         "Freeing a slot is not an admission and must not close the wait")
        clock, starts = [ADMISSION_T2], []
        original = process_runner._start

        def starting(*args, **kwargs):
            saved = self.store.load()
            self.assertIsNone(saved.get("waiting"), "Successful admission must be saved BEFORE the child starts")
            self.assertEqual(saved.get("activeSince"), ADMISSION_T2)
            self.assertIsNone(saved["waitTiming"]["resource_wait"].get("activeSince"))
            starts.append(True)
            child = original(*args, **kwargs)
            clock[0] = ADMISSION_T3
            return child

        with mock.patch.object(workflow, "timestamp", side_effect=lambda: clock[0]), \
                mock.patch.object(process_runner, "_start", side_effect=starting):
            self.assertTrue(self.flow.verify([sys.executable, "-c", "print('admitted')"], 5))
        self.assertEqual(starts, [True])
        measured = timing.summary(self.state, ADMISSION_T3)
        self.assertEqual(measured["waitSeconds"]["resource_wait"], 60.0)
        self.assertEqual(measured["activeSeconds"], 90.0,
                         "The child execution interval belongs to active time, not resource_wait")

    def test_successful_test_admission_preserves_unrelated_and_unscoped_waits(self):
        from aispa_core import process_runner
        managed_admission_fixture(self)
        original = process_runner._start
        for kind, wait_kind in (("browsers", "resource_wait"), (None, "resource_wait"),
                                (None, "owner_wait")):
            with self.subTest(resource=kind, wait=wait_kind):
                recorded_resource_wait(self, kind, wait_kind=wait_kind)
                waiting = copy.deepcopy(self.state["waiting"])

                def starting(*args, **kwargs):
                    self.assertEqual(self.state["waiting"], waiting)
                    return original(*args, **kwargs)

                with mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T2), \
                        mock.patch.object(process_runner, "_start", side_effect=starting):
                    self.assertTrue(self.flow.verify([sys.executable, "-c", "print('other demand')"], 5))
                self.assertEqual(self.store.load()["waiting"], waiting)

    def test_cli_reserve_retry_ends_matching_wait_at_admission_only(self):
        import aispa
        from aispa_core import resources
        managed_admission_fixture(self)
        ctx = resources.context(self.store, self.state)
        held = resources.reserve(ctx, "testProcesses")
        with mock.patch.object(aispa, "timestamp", return_value=ADMISSION_T1), \
                mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T1):
            refused = admission_cli(self, "resources", "reserve", "--kind", "testProcesses", expected=2)
        self.assertEqual(refused["receipt"]["code"], "RESOURCE_CAPACITY")
        resources.finish(ctx, held["id"], outcome="launch_failed", confirmed=True)
        self.assertEqual(self.store.load()["waiting"]["kind"], "resource_wait")
        with mock.patch.object(aispa, "timestamp", return_value=ADMISSION_T2), \
                mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T2):
            admitted = admission_cli(self, "resources", "reserve", "--kind", "testProcesses")
        saved = self.store.load()
        self.assertIsNone(saved.get("waiting"), "CLI reserve success is the admission boundary")
        self.assertEqual(saved.get("activeSince"), ADMISSION_T2)
        resources.finish(ctx, admitted["receipt"]["id"], outcome="launch_failed", confirmed=True)

    def test_admission_journal_failure_does_not_start_child_or_leak_lease(self):
        from aispa_core import process_runner, resources
        managed_admission_fixture(self)
        recorded_resource_wait(self)
        ctx = resources.context(self.store, self.state)
        original_save, original_start = self.flow.save, process_runner._start

        def fail_admission(event, at=None):
            if event.startswith("resource-admitted:"):
                raise RuntimeError("admission-save-failed")
            return original_save(event, at)

        with mock.patch.object(self.flow, "save", side_effect=fail_admission), \
                mock.patch.object(process_runner, "_start", wraps=original_start) as start, \
                self.assertRaisesRegex(RuntimeError, "admission-save-failed"):
            self.flow.verify([sys.executable, "-c", "print('must not start')"], 5)
        start.assert_not_called()
        self.assertEqual(self.store.load()["waiting"]["kind"], "resource_wait")
        reusable = resources.reserve(ctx, "testProcesses")
        resources.finish(ctx, reusable["id"], outcome="launch_failed", confirmed=True)

    def test_runner_admission_oserror_propagates_and_releases_only_new_lease(self):
        import re
        from aispa_core import process_runner, resources
        managed_admission_fixture(self)
        ctx = resources.context(self.store, self.state)
        other = resources.reserve(ctx, "services")
        calls = []

        def refused(kind):
            calls.append(kind)
            raise OSError("admission-persistence-failed")

        with mock.patch.object(process_runner, "_start") as start, \
                self.assertRaisesRegex(OSError, "admission-persistence-failed"):
            process_runner.run([sys.executable, "-c", "print('must not start')"], self.root,
                os.environ.copy(), 5, max_output=1000, secret_pattern=re.compile(r"never-match-fixture"),
                secrets=(), resource_context=ctx, on_admitted=refused)
        start.assert_not_called()
        self.assertEqual(calls, ["testProcesses"])
        leases = resources.view(ctx)["leases"]
        self.assertEqual(leases[other["id"]]["status"], "reserved")
        reusable = resources.reserve(ctx, "testProcesses")
        resources.finish(ctx, reusable["id"], outcome="launch_failed", confirmed=True)
        resources.finish(ctx, other["id"], outcome="launch_failed", confirmed=True)


class CleanAdmissionRepairTests(unittest.TestCase):
    import test_cleanroom as _fixture
    setUp = _fixture.CleanroomTests.setUp
    tearDown = _fixture.CleanroomTests.tearDown
    git = _fixture.CleanroomTests.git
    commit = _fixture.CleanroomTests.commit
    prepare = _fixture.CleanroomTests.prepare
    execute = _fixture.CleanroomTests.execute

    def test_cleanrun_ends_matching_wait_before_real_clone_child(self):
        from aispa_core import cleanroom, process_runner
        clone = self.prepare()
        managed_admission_fixture(self)
        recorded_resource_wait(self)
        original, started = process_runner._start, []

        def starting(*args, **kwargs):
            self.assertIsNone(self.state.get("waiting"), "Clean-run must leave resource_wait before clone execution")
            self.assertEqual(self.state.get("activeSince"), ADMISSION_T2)
            self.assertEqual(Path(args[1]), clone)
            started.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(cleanroom, "timestamp", return_value=ADMISSION_T2), \
                mock.patch.object(process_runner, "_start", side_effect=starting):
            self.assertEqual(self.execute()["exitCode"], 0)
        self.assertEqual(started, [True])
        self.store.append(self.state, "clean-verification-recorded")
        self.assertIsNone(self.store.load()["waiting"])


class RecoveryAdmissionRepairTests(unittest.TestCase):
    import test_recovery_workflow as _fixture
    setUp = _fixture.RecoveryWorkflowTests.setUp
    tearDown = _fixture.RecoveryWorkflowTests.tearDown

    def test_recovery_cli_admission_keeps_paused_anchor_and_closes_wait_before_child(self):
        from aispa_core import process_runner, recovery
        managed_admission_fixture(self)
        recorded_resource_wait(self)
        core = {k: self.state["journalRecovery"][k] for k in recovery.CORE_FIELDS}
        original, before_child = process_runner._start, []

        def starting(*args, **kwargs):
            saved = self.store.load()
            self.assertIsNone(saved.get("waiting"), "Recovery admission is recorded before executing the child")
            self.assertEqual(saved["status"], "paused")
            self.assertTrue(saved["journalRecovery"]["pending"])
            self.assertIsNone(saved.get("activeSince"), "Admission must not resume a paused recovery")
            before_child.append(self.store.read_journal()["last"]["hash"])
            return original(*args, **kwargs)

        with mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T2), \
                mock.patch.object(recovery, "timestamp", return_value=ADMISSION_T2), \
                mock.patch.object(process_runner, "_start", side_effect=starting):
            admission_cli(self, "journal-verify", "--timeout", "5", "--", sys.executable, "-c", "print('recovery')")
        saved = self.store.load()
        self.assertEqual(saved["status"], "paused")
        self.assertTrue(saved["journalRecovery"]["pending"])
        self.assertEqual({k: saved["journalRecovery"][k] for k in recovery.CORE_FIELDS}, core)
        receipt = saved["journalRecovery"]["verification"]
        self.assertEqual(before_child, [receipt["journalAtStart"]])
        self.assertEqual(receipt["exitCode"], 0)
        self.assertTrue(recovery.context(self.store, saved)["verification"], "The immediate-predecessor anchor must remain valid")


class ServiceAdmissionRepairTests(unittest.TestCase):
    import test_dashboard_server as _fixture
    setUp = _fixture.DashboardServerTests.setUp
    tearDown = _fixture.DashboardServerTests.tearDown

    def test_owned_service_admission_and_reuse_end_wait_without_native_session(self):
        from aispa_core import dashboard_server as dashboard
        managed_admission_fixture(self, session=None)
        recorded_resource_wait(self, "services")
        original, starts = subprocess.Popen, []

        def spawning(*args, **kwargs):
            if "aispa_core.dashboard_server" in args[0]:
                saved = self.store.load()
                self.assertIsNone(saved.get("waiting"), "Service admission precedes daemon Popen")
                self.assertEqual(saved.get("activeSince"), ADMISSION_T2)
                self.assertNotIn("hostSession", saved)
                starts.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(dashboard, "timestamp", return_value=ADMISSION_T2, create=True), \
                mock.patch.object(workflow, "timestamp", return_value=ADMISSION_T2), \
                mock.patch.object(dashboard.subprocess, "Popen", side_effect=spawning):
            first = dashboard.start(self.store)
            self.state = self.store.load()
            self.flow = workflow.Workflow(self.store, self.state)
            recorded_resource_wait(self, "services")
            second = dashboard.start(self.store)
        self.assertEqual(first["instance"], second["instance"])
        self.assertEqual(starts, [True], "Owned live service reuse must not reserve or spawn another daemon")
        self.assertIsNone(self.store.load().get("waiting"))

    def test_refresh_view_does_not_modify_wait_or_canonical_journal(self):
        from aispa_core import dashboard_server as dashboard
        managed_admission_fixture(self, session=None)
        dashboard.start(self.store)
        self.state = self.store.load()
        self.flow = workflow.Workflow(self.store, self.state)
        recorded_resource_wait(self, "services")
        path = self.store.run / "events.jsonl"
        before, waiting = path.read_bytes(), copy.deepcopy(self.state["waiting"])
        journaling_hook = mock.Mock(side_effect=AssertionError("Refresh-view must not invoke a journaling hook"))
        dashboard.start(self.store, refresh_view=True, on_admitted=journaling_hook)
        journaling_hook.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.load()["waiting"], waiting)

    def test_service_admission_invalid_is_not_retried_as_a_port_fallback(self):
        from aispa_core import dashboard_server as dashboard, resources
        from aispa_core.store import Invalid
        managed_admission_fixture(self, session=None)
        calls = []

        def refused(kind):
            calls.append(kind)
            raise Invalid("admission-refused")

        original = subprocess.Popen
        starts = []

        def spawning(*args, **kwargs):
            if "aispa_core.dashboard_server" in args[0]:
                starts.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(dashboard, "_last_port", return_value=12345), \
                mock.patch.object(dashboard, "_port_closed", return_value=False), \
                mock.patch.object(dashboard.subprocess, "Popen", side_effect=spawning), \
                self.assertRaisesRegex(Invalid, "admission-refused"):
            dashboard.start(self.store, on_admitted=refused)
        self.assertEqual(calls, ["services"], "Callback refusal must bypass the legacy Invalid port-retry handler")
        self.assertEqual(starts, [])
        ctx = resources.context(self.store, self.state, owner="dashboard-service", service_instance="probe-after-refusal")
        reusable = resources.reserve(ctx, "services")
        resources.finish(ctx, reusable["id"], outcome="launch_failed", confirmed=True)

    def test_reuse_callback_failure_preserves_the_owned_running_service_and_lease(self):
        from aispa_core import dashboard_server as dashboard, resources
        from aispa_core.store import Invalid
        managed_admission_fixture(self, session=None)
        first = dashboard.start(self.store)
        before = self.store.load()
        ctx = resources.context(self.store, before, owner="dashboard-service", service_instance=first["instance"])
        leases = resources.view(ctx)["leases"]
        running = {key for key, lease in leases.items() if lease["status"] == "running"}
        self.assertEqual(len(running), 1)
        refused = mock.Mock(side_effect=Invalid("reuse-admission-refused"))
        with self.assertRaisesRegex(Invalid, "reuse-admission-refused"):
            dashboard.start(self.store, on_admitted=refused)
        refused.assert_called_once_with("services")
        observed = dashboard.inspect(self.store)
        self.assertTrue(observed["running"])
        self.assertEqual(observed["instance"], first["instance"])
        after = resources.view(ctx)["leases"]
        self.assertTrue(all(after[key]["status"] == "running" for key in running))


class UsageAttributionRepairTests(unittest.TestCase):
    import test_team as _fixture
    setUp = _fixture.TeamTests.setUp
    tearDown = _fixture.TeamTests.tearDown
    task = _fixture.TeamTests.task
    attempt = _fixture.TeamTests.attempt

    def author(self):
        self.state["policy"] = {"managedExecution": True}
        return self.team.assign("T01", "worker", "codex", "usage-task-worker", "usage-task-session")

    def later_attempt(self):
        quote = "Synthetic fixture owner authorizes one later T01 attempt."
        source = self.store.artifact("briefs", (quote + "\n").encode(), ".md")
        self.state["briefs"].append({**source, "id": "B002"})
        self.team.new_attempt("T01", "B002", quote)
        self.team.prepare()
        return self.attempt("T01")["id"]

    def value(self, actor, call, phase, *, task=None, attempt=None):
        from aispa_core import participants
        author = participants.resolve(self.store, self.state, actor)
        binding = {"actor": actor, "callId": call, "phase": phase, "run": self.state["run"],
                   "session": author["session"]}
        if task is not None:
            binding["task"] = task
        if attempt is not None:
            binding["attempt"] = attempt
        return {"actor": actor, "callId": call, "source": "explicit measured protocol fixture",
                "inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 0,
                "attribution": {**binding, "source": self.store.artifact("phase-usage",
                    json.dumps(binding).encode(), ".json")}}

    def test_task_author_rejects_foreign_task_attempt_and_missing_scope_before_append(self):
        from aispa_core import telemetry
        from aispa_core.store import Invalid
        actor = self.author()
        own = self.attempt("T01")["id"]
        foreign = self.attempt("T02")["id"]
        later = self.later_attempt()
        for call, task, attempt in (("foreign-task", "T02", foreign), ("foreign-attempt", "T01", later),
                                    ("missing-scope", None, None)):
            with self.subTest(call=call):
                before = copy.deepcopy(self.state.get("usage", []))
                # Same phase as the author's role: rejecting a role label alone cannot fix these cases.
                value = self.value(actor, call, "implementation", task=task, attempt=attempt)
                with self.assertRaises(Invalid):
                    telemetry.record(self.store, self.state, value)
                self.assertEqual(self.state.get("usage", []), before)
        self.assertNotEqual(own, later)

    def test_historical_author_general_and_knowledge_sources_keep_valid_phase_totals(self):
        from aispa_core import knowledge, participants, telemetry
        actor = self.author()
        own = self.attempt("T01")["id"]
        original = copy.deepcopy(self.state["actors"][actor])
        self.later_attempt()
        current = self.team.assign("T01", "worker", "codex", "usage-new-worker", "usage-new-session")
        coordinator = participants.register(self.store, self.state, "coordinator", "codex", "usage-general", "usage-general-session")
        author = knowledge.assign(self.store, self.state, "codex", "usage-knowledge", "usage-knowledge-session")
        values = [self.value(actor, "historical", "repair", task="T01", attempt=own),
                  self.value(current, "current", "implementation", task="T01", attempt=self.attempt("T01")["id"]),
                  self.value(coordinator, "general", "idea"),
                  self.value(coordinator, "general-scoped", "spec", task="T02", attempt=self.attempt("T02")["id"]),
                  self.value(author["id"], "knowledge", "knowledge")]
        for value in values:
            telemetry.record(self.store, self.state, value)
        report = telemetry.summary(self.store, self.state)
        self.assertEqual(report["inputTokens"], 50)
        self.assertEqual(sum(group["inputTokens"] for group in report["byPhase"].values()), 50)
        self.assertEqual(set(report["byPhase"]), {"repair", "implementation", "idea", "spec", "knowledge"})
        self.assertEqual(self.state["actors"][actor], original)
        self.assertFalse(report["complete"])


class TaskPhaseAttributionRepairTests(unittest.TestCase):
    """Task phase authority comes from the exact historical attempt's contract."""
    setUp = UsageAttributionRepairTests.setUp
    tearDown = UsageAttributionRepairTests.tearDown
    task = UsageAttributionRepairTests.task
    attempt = UsageAttributionRepairTests.attempt
    value = UsageAttributionRepairTests.value

    def next_attempt(self, policy, *, worker_only=False):
        self.state["policy"] = {"managedExecution": True, **policy}
        task = self.state["tasks"][0]
        if worker_only:
            task.update(tier="standard", tierReason="Ordinary fixture module", changeKind="feature", risk="normal")
        else:
            for key in ("tier", "tierReason", "changeKind"):
                task.pop(key, None)
        number = len(self.state["briefs"]) + 1
        bid = "B%03d" % number
        quote = "Synthetic fixture owner authorizes historical phase attempt %d." % number
        source = self.store.artifact("briefs", (quote + "\n").encode(), ".md")
        self.state["briefs"].append({**source, "id": bid})
        self.team.new_attempt("T01", bid, quote)
        self.team.prepare()
        return self.attempt("T01")["id"]

    def test_own_worker_and_critics_cannot_attribute_another_role_or_round(self):
        from aispa_core import telemetry
        from aispa_core.store import Invalid
        self.state["policy"] = {"managedExecution": True}
        attempt = self.attempt()["id"]
        for role, wrong_phase in (("worker", "critic_1"), ("critic1", "critic_2"), ("critic2", "critic_1")):
            actor = self.team.assign("T01", role, "codex", "phase-" + role, "session-" + role)
            with self.subTest(role=role, phase=wrong_phase):
                before = copy.deepcopy(self.state.get("usage", []))
                row = self.value(actor, "wrong-" + role, wrong_phase, task="T01", attempt=attempt)
                with self.assertRaises(Invalid):
                    telemetry.record(self.store, self.state, row)
                self.assertEqual(self.state.get("usage", []), before)

    def test_adaptive_worker_and_critic_need_the_separate_task_acceptor(self):
        from aispa_core import telemetry
        from aispa_core.store import Invalid
        attempt = self.next_attempt({"adaptiveCritique": True})
        for role in ("worker", "critic1"):
            actor = self.team.assign("T01", role, "codex", "adaptive-" + role, "session-" + role)
            with self.subTest(role=role):
                before = copy.deepcopy(self.state.get("usage", []))
                row = self.value(actor, "acceptance-" + role, "task_acceptance", task="T01", attempt=attempt)
                with self.assertRaises(Invalid):
                    telemetry.record(self.store, self.state, row)
                self.assertEqual(self.state.get("usage", []), before)
        acceptor = self.team.assign("T01", "acceptor", "codex", "adaptive-acceptor", "acceptor-session")
        telemetry.record(self.store, self.state,
                         self.value(acceptor, "separate-acceptance", "task_acceptance", task="T01", attempt=attempt))
        self.assertEqual(telemetry.summary(self.store, self.state)["byPhase"]["task_acceptance"]["inputTokens"], 10)

    def test_worker_only_acceptance_uses_historical_tier_instead_of_current_attempt(self):
        from aispa_core import telemetry
        from aispa_core.store import Invalid
        old = self.next_attempt({"taskTiers": True, "adaptiveCritique": True, "leanControl": True}, worker_only=True)
        worker = self.team.assign("T01", "worker", "codex", "standard-worker", "standard-session")
        later = self.team.promote("T01", "Synthetic fixture promotes the later attempt to heavy")
        self.assertEqual(self.attempt("T01")["id"], later)
        self.assertEqual(self.attempt("T01")["tier"], "heavy")
        current = self.team.assign("T01", "worker", "codex", "heavy-worker", "heavy-session")
        telemetry.record(self.store, self.state,
                         self.value(worker, "old-worker-acceptance", "task_acceptance", task="T01", attempt=old))
        before = copy.deepcopy(self.state["usage"])
        with self.assertRaises(Invalid):
            telemetry.record(self.store, self.state,
                             self.value(current, "heavy-worker-acceptance", "task_acceptance", task="T01", attempt=later))
        self.assertEqual(self.state["usage"], before)

    def test_task_actor_without_attribution_keeps_unknown_phase_and_totals(self):
        from aispa_core import telemetry
        self.state["policy"] = {"managedExecution": True}
        actor = self.team.assign("T01", "worker", "codex", "unattributed-worker", "unattributed-session")
        telemetry.record(self.store, self.state, {"actor": actor, "callId": "unknown-phase", "source": "native fixture",
            "inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 0})
        report = telemetry.summary(self.store, self.state)
        self.assertEqual(report["inputTokens"], 10)
        self.assertEqual(set(report["byPhase"]), {"unknown"})
        self.assertEqual(report["byPhase"]["unknown"]["inputTokens"], 10)
        self.assertEqual(report["coverage"]["unattributedCalls"], 1)


class LeanCombinedPhaseRepairTests(unittest.TestCase):
    from test_lean_control import LeanReviewTests as _fixture
    setUp = _fixture.setUp
    tearDown = _fixture.tearDown
    task = _fixture.task
    attempt = _fixture.attempt
    start = _fixture.start
    verify = _fixture.verify
    report = _fixture.report
    input = _fixture.input
    finding = _fixture.finding
    first = _fixture.first
    lean_fields = _fixture.lean_fields
    repair = _fixture.repair
    second_actor = _fixture.second_actor
    value = UsageAttributionRepairTests.value
    next_attempt = TaskPhaseAttributionRepairTests.next_attempt

    def test_both_lean_critics_keep_combined_acceptance_in_their_historical_attempt(self):
        from aispa_core import participants, telemetry
        from aispa_core.store import Invalid
        self.state["policy"]["managedExecution"] = True
        self.team.prepare()
        self.start()
        old = self.attempt()["id"]
        critic1 = self.attempt()["actors"]["critic1"]
        self.assertFalse(self.first("WARN"))
        self.repair()
        self.second_actor()
        critic2 = self.attempt()["actors"]["critic2"]
        later = self.next_attempt({"adaptiveCritique": True})
        adaptive = self.team.assign("T01", "critic1", "codex", "later-adaptive-critic", "later-session")
        for actor in (critic1, critic2):
            for phase in ("critic_1" if actor == critic1 else "critic_2", "task_acceptance"):
                telemetry.record(self.store, self.state,
                    self.value(actor, actor + "-" + phase, phase, task="T01", attempt=old))
        coordinator = participants.register(self.store, self.state, "coordinator", "codex", "phase-general", "general-session")
        telemetry.record(self.store, self.state, self.value(coordinator, "general-task-phase", "task_acceptance"))
        report = telemetry.summary(self.store, self.state)
        self.assertEqual(report["inputTokens"], 50)
        self.assertEqual(report["byPhase"]["task_acceptance"]["inputTokens"], 30)
        before = copy.deepcopy(self.state["usage"])
        with self.assertRaises(Invalid):
            telemetry.record(self.store, self.state,
                self.value(adaptive, "adaptive-not-combined", "task_acceptance", task="T01", attempt=later))
        self.assertEqual(self.state["usage"], before)


class CliExecutionRegistrationRepairTests(unittest.TestCase):
    """Admission events must preserve strict T03 execution-start provenance."""
    setUp = CleanAdmissionRepairTests.setUp
    tearDown = CleanAdmissionRepairTests.tearDown
    git = CleanAdmissionRepairTests.git
    commit = CleanAdmissionRepairTests.commit
    prepare = CleanAdmissionRepairTests.prepare

    def cli(self, *arguments, expected=0):
        cli = self.store.run / "runtime/tools/aispa.py"
        result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.store.root), *arguments],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout) if expected == 0 else result

    def registered_execution(self, response, kind):
        from aispa_core import executions
        state = self.store.load()
        ref = response["receipt"]["execution"] if kind == "cleanrun" else response["receipt"]
        record = executions.resolve(self.store, state, ref)
        self.assertEqual(record["kind"], kind)
        self.assertTrue(record["successful"])
        completed, started = self.store.event_pair(record["journal"]["sequence"])
        expected = "clean-verification-started" if kind == "cleanrun" else "verification-started"
        self.assertEqual(started["event"], expected)
        self.assertEqual(started["sequence"] + 1, completed["sequence"])
        self.assertIsNone(started["state"].get("waiting"))
        self.assertTrue(started["state"].get("activeSince"))
        history = self.store.read_journal(include_events=True)["events"]
        admission = history[started["sequence"] - 2]
        self.assertEqual(admission["event"], "resource-admitted:testProcesses")
        self.assertGreaterEqual(sum(row["event"] == expected for row in history), 2,
                                "The earlier intent/start remains in history; the adjacent start snapshot is refreshed")
        return record

    def test_cli_verify_retry_registers_execution_after_saved_admission(self):
        from aispa_core import resources
        managed_admission_fixture(self)
        ctx = resources.context(self.store, self.state)
        held = resources.reserve(ctx, "testProcesses")
        refused = self.cli("verify", "--timeout", "5", "--", sys.executable, "-c", "print('blocked')", expected=2)
        self.assertIn("RESOURCE_CAPACITY", refused.stderr)
        resources.finish(ctx, held["id"], outcome="launch_failed", confirmed=True)
        self.assertEqual(self.store.load()["waiting"]["resourceKind"], "testProcesses")
        response = self.cli("verify", "--timeout", "5", "--", sys.executable, "-c", "print('CLI retry checked')")
        self.assertTrue(response["ok"])
        record = self.registered_execution(response, "project")
        self.assertEqual(record["exitCode"], 0)

    def test_cli_cleanrun_registration_keeps_adjacent_start_after_admission(self):
        self.prepare()
        managed_admission_fixture(self)
        recorded_resource_wait(self)
        response = self.cli("clean-verify", "--timeout", "5", "--", sys.executable, "-c", "print('CLI clean checked')")
        self.assertTrue(response["ok"])
        record = self.registered_execution(response, "cleanrun")
        self.assertEqual(record["exitCode"], 0)


if __name__ == "__main__":
    unittest.main()
