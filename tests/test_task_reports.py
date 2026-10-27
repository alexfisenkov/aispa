import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import controls, integration, packets, preparation, task_reports, test_intent
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, file_hash
import test_team as helpers


class TaskReportTests(unittest.TestCase):
    setUp = helpers.TeamTests.setUp
    tearDown = helpers.TeamTests.tearDown
    task = helpers.TeamTests.task
    attempt = helpers.TeamTests.attempt
    start = helpers.TeamTests.start
    verify = helpers.TeamTests.verify
    report = helpers.TeamTests.report
    reviews = helpers.TeamTests.reviews
    accept = helpers.TeamTests.accept
    complete = helpers.TeamTests.complete

    def draft(self, role="worker", tid="T01"):
        return task_reports.template(self.team, tid, role)

    def input(self, report):
        self.serial += 1
        path = self.home / ("draft-input-" + str(self.serial) + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return path

    def assessed(self, draft):
        report = copy.deepcopy(draft)
        report.update(independent=True, unmapped=[])
        for row in report["checks"]:
            row.update(status="passed", note="Observed fixture", artifact=self.evidence["file"])
        if "findings" in report:
            report["findings"] = []
        for row in report.get("resolutions", []):
            row.update(status="resolved", note="Observed repaired fixture", artifact=self.evidence["file"])
        for row in report.get("trace", []):
            row.update(result="aligned", note="Compared exact source", artifact=self.evidence["file"])
        if "branchReview" in report:
            report["branchReview"].update(note="Reviewed full fixture", evidence=self.evidence["file"])
            for row in report["branchReview"]["axes"]:
                row.update(status="passed", note="Reviewed fixture axis", evidence=self.evidence["file"])
        return report

    def unchanged(self, operation):
        before = copy.deepcopy(self.state)
        files = {p.relative_to(self.store.home).as_posix(): p.read_bytes()
                 for p in self.store.home.rglob("*") if p.is_file()}
        result = operation()
        self.assertEqual(self.state, before)
        self.assertEqual(files, {p.relative_to(self.store.home).as_posix(): p.read_bytes()
                                for p in self.store.home.rglob("*") if p.is_file()})
        return result

    def reject_untouched(self, role, submit):
        draft = self.unchanged(lambda: self.draft(role))
        self.assertIs(draft["independent"], False)
        self.assertIsNone(draft["unmapped"])
        for row in draft["checks"]:
            self.assertEqual((row["status"], row["note"], row["artifact"]), ("not_tested", "", ""))
        if role.startswith("critic"):
            self.assertIsNone(draft["findings"])
        path = self.input(draft)
        def reject():
            with self.assertRaises(Invalid):
                submit(path)
        self.unchanged(reject)
        return draft

    def test_all_roles_require_real_assessment_and_do_not_consume_rounds(self):
        self.start()
        worker = self.reject_untouched("worker", lambda path: self.team.submit("T01", path))
        self.assertEqual(worker["basis"], task_basis(self.store, self.state, "T01"))
        self.assertEqual(worker["reviewer"], self.attempt()["actors"]["worker"])
        self.assertEqual({(r["requirement"], r["criterion"]) for r in worker["checks"]},
                         self.team.expected_checks("T01", "worker"))
        self.team.submit("T01", self.input(self.assessed(worker)))
        first = self.reject_untouched("critic1", lambda path: self.team.review("T01", 1, path))
        self.assertTrue(self.team.review("T01", 1, self.input(self.assessed(first))))
        second = self.reject_untouched("critic2", lambda path: self.team.review("T01", 2, path))
        self.assertEqual(second["previousReport"], self.attempt()["rounds"][0]["report"]["sha256"])
        self.assertIsNone(second["repairReceipt"])
        self.assertEqual(second["resolutions"], [])
        self.assertTrue(self.team.review("T01", 2, self.input(self.assessed(second))))
        accepted = self.reject_untouched("acceptor", lambda path: self.team.accept("T01", path))
        self.assertTrue(self.team.accept("T01", self.input(self.assessed(accepted))))

    def test_mixed_context_keeps_original_contract_checks_and_trace(self):
        from aispa_core import mixed_evidence
        self.start()
        original = packets.build(self.store, self.state, self.team, "task", "T01", "worker")
        summary = {"mode": "mixed", "counts": {"automated": 0, "browser": 1, "process": 0},
                   "routes": [{"criterion": "T01 works", "kind": "browser", "reason": "visible", "procedure": "open", "expected": "works"}]}
        observations = [{"schema": 1, "observed": "", "procedure": "", "source": {}, "attachments": []}]
        with patch.object(test_intent, "route_summary", return_value=summary), patch.object(
                mixed_evidence, "observation_templates", return_value=observations):
            packet = self.unchanged(lambda: packets.build(self.store, self.state, self.team, "task", "T01", "worker"))
        self.assertEqual(packet["evidenceSummary"], summary)
        self.assertEqual(packet["observationTemplates"], observations)
        for key in ("testIntent", "tasks", "expectedChecks", "traceRequirements", "inheritedProofs"):
            self.assertEqual(packet[key], original[key])
        self.assertNotIn("evidenceSummary", original)
        self.assertNotIn("observationTemplates", original)
        self.state.setdefault("policy", {})["errorBoundaryChecks"] = True
        from aispa_core import failure_checks
        with patch.object(test_intent, "route_summary", return_value=summary), patch.object(
                mixed_evidence, "observation_templates", return_value=observations):
            guarded = self.unchanged(lambda: packets.build(self.store, self.state, self.team, "task", "T01", "worker"))
        self.assertEqual(guarded["failureChecksTemplate"], failure_checks.template(mode="mixed"))
        self.assertEqual(set(guarded["failureChecksTemplate"]["caseShapes"]), {"automated", "browser", "process"})
        self.assertNotIn("failureChecksTemplate", packet)


    def test_mixed_drafts_include_unassessed_routes_without_changing_legacy(self):
        from aispa_core import mixed_evidence
        self.start()
        routes = [{"criterion": "T01 works", "kind": kind, "status": "not_tested", "note": "", "artifact": ""}
                  for kind in ("automated", "browser", "process")]
        for role in ("worker", "critic1", "critic2", "acceptor"):
            if role == "critic1":
                self.team.submit("T01", self.input(self.assessed(self.draft())))
            elif role == "critic2":
                self.team.review("T01", 1, self.input(self.assessed(self.draft("critic1"))))
            elif role == "acceptor":
                self.team.review("T01", 2, self.input(self.assessed(self.draft("critic2"))))
            with patch.object(mixed_evidence, "report_template", return_value=copy.deepcopy(routes)):
                draft = self.unchanged(lambda: self.draft(role))
            self.assertEqual(draft["routeChecks"], routes)
            self.assertIs(draft["independent"], False)
            self.assertTrue(all(row["status"] == "not_tested" for row in draft["routeChecks"]))
            self.assertNotIn("routeChecks", self.draft(role))

    def test_missing_task_actor_and_stage_fail_without_writing(self):
        self.host.task("T01")["status"] = "running"
        with self.assertRaisesRegex(Invalid, "actor"):
            self.unchanged(self.draft)
        self.start()
        for role in ("critic1", "critic2", "acceptor"):
            with self.subTest(role=role), self.assertRaises(Invalid):
                self.unchanged(lambda: self.draft(role))
        with self.assertRaises(Invalid):
            self.draft(tid="UNKNOWN")
        with self.assertRaises(Invalid):
            self.draft(role="unknown")
        self.reviews()
        with self.assertRaises(Invalid):
            self.draft("worker")
        with self.assertRaises(Invalid):
            self.draft("critic1")
        with self.assertRaises(Invalid):
            self.draft("critic2")

    def test_repair_links_only_blocking_ids_and_requires_recorded_receipt(self):
        self.start()
        self.team.submit("T01", self.report())
        first = self.assessed(self.draft("critic1"))
        first["findings"] = [
            {"id": "F1", "severity": "blocking", "text": "Repair fixture", "criterion": "T01 works"},
            {"id": "N1", "severity": "note", "text": "Optional observation", "criterion": "T01 works"},
        ]
        self.assertFalse(self.team.review("T01", 1, self.input(first)))
        with self.assertRaisesRegex(Invalid, "receipt"):
            self.unchanged(lambda: self.draft("critic2"))
        (self.root / "a.txt").write_text("repaired", encoding="utf-8")
        self.verify()
        worker = self.reject_untouched("worker", lambda path: self.team.repair("T01", path))
        self.assertEqual(worker["previousReport"], self.attempt()["rounds"][0]["report"]["sha256"])
        self.assertEqual(worker["addresses"], ["F1"])
        self.team.repair("T01", self.input(self.assessed(worker)))
        second = self.reject_untouched("critic2", lambda path: self.team.review("T01", 2, path))
        self.assertEqual(second["repairReceipt"], self.attempt()["repair"]["report"]["sha256"])
        self.assertEqual(second["basis"], self.attempt()["repair"]["basis"])
        self.assertEqual(second["resolutions"], [{"id": "F1", "status": "not_tested", "note": "", "artifact": ""}])
        self.assertTrue(self.team.review("T01", 2, self.input(self.assessed(second))))

    def test_acceptor_covers_dependency_constraints_addition_parent_and_exact_trace(self):
        self.state["requirements"].append({
            "id": "R03", "text": "Addition", "acceptance": ["Added behavior"],
            "kind": "addition", "parent": "R02", "scope": "active", "core": False,
        })
        final = self.host.task("T02")
        final.update(requirements=["R03"], dependsOn=["T01"], wave=2)
        self.team.prepare()
        self.complete()
        self.start("T02")
        self.reviews("T02")
        report = self.unchanged(lambda: self.draft("acceptor", "T02"))
        self.assertEqual({(r["requirement"], r["criterion"]) for r in report["checks"]},
                         self.team.expected_checks("T02", "acceptor"))
        self.assertIn(("T01", "T01 works"), {(r["requirement"], r["criterion"]) for r in report["checks"]})
        self.assertIn(("G01", "No private data"), {(r["requirement"], r["criterion"]) for r in report["checks"]})
        self.assertEqual(report["inheritedProofs"], self.team.inherited_proofs("T02"))
        self.assertEqual({r["requirement"]: (r["brief"], r["quote"]) for r in report["trace"]},
                         {"R01": ("B001", "Build A."), "R02": ("B001", "Build B."),
                          "G01": ("B001", "Keep data private.")})
        self.assertTrue(all(r["result"] == "not_tested" and not r["note"] and not r["artifact"]
                            for r in report["trace"]))
        self.assertTrue(self.team.accept("T02", self.input(self.assessed(report))))

    def test_stale_filled_draft_is_rejected_and_current_stage_mismatch_fails(self):
        self.start()
        old = self.assessed(self.draft())
        (self.root / "a.txt").write_text("changed", encoding="utf-8")
        self.verify()
        with self.assertRaisesRegex(Invalid, "устарел"):
            self.team.submit("T01", self.input(old))
        self.team.submit("T01", self.input(self.assessed(self.draft())))
        (self.root / "a.txt").write_text("changed again", encoding="utf-8")
        with self.assertRaisesRegex(Invalid, "Код"):
            self.unchanged(lambda: self.draft("critic1"))

    def test_test_source_changes_are_computed_not_attested(self):
        self.start()
        path = self.root / "a.txt"
        original = file_hash(path)
        source = {"schema": 1, "mode": "behavior_change",
                  "report": {"newTestFiles": [{"path": "a.txt", "sha256": original}]}}
        self.attempt()["testIntent"] = self.store.artifact("test-intent", json.dumps(source).encode(), ".json")
        path.write_text("changed test source", encoding="utf-8")
        draft = self.unchanged(self.draft)
        self.assertEqual(draft["testSourceChanges"], test_intent.source_changes(self.store, self.state, "T01"))
        self.assertEqual(draft["testSourceChanges"], [{"path": "a.txt", "before": original, "after": file_hash(path)}])
        self.assertIs(draft["independent"], False)

    def test_final_critics_include_exact_branch_review_and_five_unassessed_axes(self):
        self.state["policy"] = {"integrationReview": True}
        integration.capture(self.store, self.state)
        self.state["integrationTask"] = "T02"
        self.host.task("T02").update(dependsOn=["T01"], readZones=["."], wave=2)
        self.team.prepare()
        self.complete()
        self.start("T02")
        (self.root / "b.txt").write_text("integrated change", encoding="utf-8")
        self.verify("T02")
        self.team.submit("T02", self.report("T02"))
        for number in (1, 2):
            role = "critic" + str(number)
            draft = self.unchanged(lambda: self.draft(role, "T02"))
            branch = draft["branchReview"]
            context = integration.context(self.store, self.state, "T02")
            self.assertEqual({k: branch[k] for k in ("basis", "code", "files", "changes")},
                             {k: context[k] for k in ("basis", "code", "files", "changes")})
            self.assertEqual([r["axis"] for r in branch["axes"]], list(integration.AXES))
            self.assertTrue(all(r["status"] == "not_tested" and not r["note"] and not r["evidence"]
                                for r in branch["axes"]))
            self.assertFalse(branch["note"])
            self.assertFalse(branch["evidence"])
            self.assertTrue(self.team.review("T02", number, self.input(self.assessed(draft))))

    def test_focused_final_critic_gets_no_whole_branch_checks_or_form(self):
        # The lens reviewers read the whole branch; the final task's critic fills only its own attempt.
        self.state["policy"] = {"integrationReview": True, "focusedReview": True}
        integration.capture(self.store, self.state)
        self.state["integrationTask"] = "T02"
        self.host.task("T02").update(dependsOn=["T01"], readZones=["."], wave=2)
        self.team.prepare()
        self.complete()
        self.start("T02")
        (self.root / "b.txt").write_text("integrated change", encoding="utf-8")
        self.verify("T02")
        self.team.submit("T02", self.report("T02"))
        draft = self.unchanged(lambda: self.draft("critic1", "T02"))
        self.assertNotIn("branchReview", draft)
        self.assertFalse([c for c in draft["checks"] if c["criterion"].startswith("Вся ветка")])
        card = self.store.json(self.state["taskHistory"]["T02"][-1]["roles"]["critic1"])
        self.assertFalse([c for c in card["checks"] if c.startswith("Вся ветка")])
        self.assertTrue(self.team.review("T02", 1, self.input(self.assessed(draft))))

    def parser(self):
        parser = argparse.ArgumentParser()
        controls.parsers(parser.add_subparsers(dest="action", required=True))
        return parser

    def execute(self, *args):
        self.host.pipeline = self.team
        return controls.execute(self.store, self.state, self.host, self.parser().parse_args(args))

    def test_controls_parser_and_existing_template_json_are_compatible(self):
        for kind in ("brief-extract", "brief-review", "interfaces", "scenarios"):
            result = self.unchanged(lambda: self.execute("template", kind, "--actor", "fixture"))
            self.assertEqual(result, preparation.template(self.store, self.state, kind, actor="fixture"))
        self.start()
        self.assertEqual(self.execute("template", "worker", "--task", "T01"), self.draft())
        with self.assertRaisesRegex(Invalid, "--task"):
            self.execute("template", "worker")
        with self.assertRaisesRegex(Invalid, "--actor"):
            self.execute("template", "worker", "--task", "T01", "--actor", "forged")
        prepared = self.execute("template", "interfaces", "--write")
        self.assertEqual(prepared["kind"], "interfaces")
        self.assertEqual(json.loads((self.root / prepared["path"]).read_text(encoding="utf-8")),
                         preparation.template(self.store, self.state, "interfaces"))
        with self.assertRaises(Invalid):
            self.execute("template", "interfaces", "--task", "T01")
        parsed = self.parser().parse_args(["template", "critic2", "--task", "T01", "--write"])
        self.assertEqual((parsed.kind, parsed.task, parsed.write), ("critic2", "T01", True))

    def test_write_creates_unique_editable_private_drafts_only(self):
        self.start()
        before = copy.deepcopy(self.state)
        journal = {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                   for p in self.store.run.rglob("*") if p.is_file()}
        first = self.execute("template", "worker", "--task", "T01", "--write")
        second = self.execute("template", "worker", "--task", "T01", "--write")
        self.assertNotEqual(first["path"], second["path"])
        path = self.root / first["path"]
        self.assertEqual(Path(first["absolutePath"]), path.resolve())
        self.assertEqual(path.parent.resolve(), self.store.home / "inbox")
        self.assertEqual(first["status"], "draft-not-evidence")
        self.assertEqual(first["sha256"], file_hash(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.draft())
        self.assertEqual(first["counts"], {"checks": len(self.draft()["checks"]), "trace": 0,
                                           "resolutions": 0, "branchAxes": 0})
        self.assertEqual(self.state, before)
        self.assertEqual(journal, {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                                  for p in self.store.run.rglob("*") if p.is_file()})
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text("{}\n", encoding="utf-8")
        self.assertEqual(path.read_text(encoding="utf-8"), "{}\n")

    def test_existing_destination_never_overwritten_and_task_id_is_not_a_path(self):
        self.start()
        with patch.object(task_reports.uuid, "uuid4") as uuid:
            uuid.return_value.hex = "fixed"
            result = task_reports.write(self.store, self.draft(), "../../outside", "worker")
            path = self.root / result["path"]
            prior = path.read_bytes()
            with self.assertRaisesRegex(Invalid, "перезапись"):
                task_reports.write(self.store, self.draft(), "T01", "worker")
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(path.name, "report-worker-fixed.json")
        with self.assertRaises(Invalid):
            task_reports.write(self.store, self.draft(), "T01", "../outside")

    def test_linked_inbox_and_replaced_home_are_rejected(self):
        self.start()
        draft = self.draft()
        outside = self.home / "outside"
        outside.mkdir()
        inbox = self.store.home / "inbox"
        if os.name == "nt":
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(inbox), str(outside)],
                           check=True, capture_output=True)
        else:
            inbox.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(Invalid):
                task_reports.write(self.store, draft, "T01", "worker")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            inbox.rmdir() if os.name == "nt" else inbox.unlink()
        home = self.store.home
        moved = self.root / ".aispa-moved"
        home.rename(moved)
        if os.name == "nt":
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(home), str(moved)],
                           check=True, capture_output=True)
        else:
            home.symlink_to(moved, target_is_directory=True)
        try:
            with self.assertRaises(Invalid):
                task_reports.write(self.store, draft, "T01", "worker")
        finally:
            home.rmdir() if os.name == "nt" else home.unlink()
            moved.rename(home)

    def test_linked_destination_cannot_overwrite_target(self):
        self.start()
        draft = self.draft()
        inbox = self.store.home / "inbox"
        inbox.mkdir()
        destination = inbox / "report-worker-fixed.json"
        outside = self.home / "external.json"
        outside.write_text("keep", encoding="utf-8")
        # Hard links work without Windows Developer Mode and test O_EXCL too.
        os.link(outside, destination)
        with patch.object(task_reports.uuid, "uuid4") as uuid:
            uuid.return_value.hex = "fixed"
            with self.assertRaises(Invalid):
                task_reports.write(self.store, draft, "T01", "worker")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        if os.name != "nt":
            destination.unlink()
            destination.symlink_to(outside)
            with patch.object(task_reports.uuid, "uuid4") as uuid:
                uuid.return_value.hex = "fixed"
                with self.assertRaises(Invalid):
                    task_reports.write(self.store, draft, "T01", "worker")
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
