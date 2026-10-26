import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import hashlib
import shutil
import os
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import carryover, project_context
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, Journal, digest
from aispa_core.team import ROLES, TaskPipeline, task_proof_valid
from test_team import Host
from aispa_core import scale
from aispa_core.store import file_hash


class ManifestCarryoverTests(unittest.TestCase):
    def test_manifest_capture_resource_refusal_propagates_without_partial_handoff(self):
        from aispa_core.verified_reads import ResourceLimit
        self.pack(self.state["briefs"][0])
        self.state["status"] = "cancelled"
        self.store.append(self.state, "bounded-manifest-capture")
        before = set(self.store.run.rglob("*"))
        with patch.dict(os.environ, {"AISPA_MAX_INPUT_BYTES": "8"}), self.assertRaises(ResourceLimit):
            carryover.capture(self.store, self.state)
        self.assertEqual(set(self.store.run.rglob("*")), before)

    setUp = lambda self: CarryoverTests.setUp(self)
    tearDown = lambda self: CarryoverTests.tearDown(self)
    add_brief = lambda self, *a, **kw: CarryoverTests.add_brief(self, *a, **kw)
    add_requirement = lambda self, *a, **kw: CarryoverTests.add_requirement(self, *a, **kw)
    close = lambda self, *a, **kw: CarryoverTests.close(self, *a, **kw)
    next_run = lambda self, *a, **kw: CarryoverTests.next_run(self, *a, **kw)

    def pack(self, ref):
        path = self.store.run / ref["file"]
        raw = scale.read_file(path)
        self.serial += 1
        packed = path.parent / ("manifest-" + str(self.serial) + ".json")
        value = scale.write_manifest(packed, raw, kind="text", basis=digest("manifest-fixture"), chunk_bytes=12)
        ref.update(file=packed.relative_to(self.store.run).as_posix(), sha256=file_hash(packed))
        self.state["policy"] = {"scalableInputs": True}
        self.store.scale_enabled = True
        return raw, value

    def test_manifest_brief_preserves_decoded_text_and_physical_origin_through_three_runs_offline(self):
        original_run = self.store.run
        ref = self.state["briefs"][0]
        original, _manifest = self.pack(ref)
        physical = ref["sha256"]
        payload = self.close()
        source = next(row for row in payload["sources"] if row["kind"] == "owner_brief")
        self.assertEqual(source["text"], original.decode())
        self.assertEqual(source["origin"]["sha256"], physical)
        self.assertEqual(source["payloadSha256"], hashlib.sha256(original).hexdigest())
        self.assertNotEqual(source["payloadSha256"], physical)
        first = self.next_run(payload)
        second_payload = self.close("accepted")
        second = self.next_run(second_payload)
        shutil.move(str(original_run), str(Path(self.temp.name) / "old-run-offline"))
        self.assertEqual(carryover.view(self.store, self.state)["items"], second)
        third = self.next_run(self.close("delivered_with_remaining"))
        self.assertEqual([row["id"] for row in first], [row["id"] for row in third])
        self.assertEqual(self.state["requirements"], [])
        self.assertEqual(self.state["gates"], {})
        self.assertFalse(any(row["currentProof"] for row in third))

    def test_manifest_chunk_tampering_and_secret_after_first_chunk_abort_capture(self):
        ref = self.state["briefs"][0]
        original, value = self.pack(ref)
        self.store.append(self.state, "manifest-fixture")
        chunk = self.store.run / Path(ref["file"]).parent / value["chunks"][-1]["file"]
        before = chunk.read_bytes()
        chunk.write_bytes(before + b"tampered")
        self.state["status"] = "cancelled"
        self.store.append(self.state, "closed-fixture")
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)
        chunk.write_bytes(before)
        path = self.store.run / "briefs/secret-manifest.json"
        raw = original + b"\npassword=" + b"z" * 30
        scale.write_manifest(path, raw, kind="text", basis=digest("secret-fixture"), chunk_bytes=12)
        ref.update(file=path.relative_to(self.store.run).as_posix(), sha256=file_hash(path))
        self.store.append(self.state, "secret-manifest-fixture")
        with self.assertRaisesRegex(Invalid, "секрет|secret"):
            carryover.capture(self.store, self.state)

    def test_recomputed_outer_and_payload_digests_do_not_authorize_forged_manifest_text(self):
        self.pack(self.state["briefs"][0])
        payload = self.close()
        source = next(row for row in payload["sources"] if row["kind"] == "owner_brief")
        self.assertEqual(source["text"], "Build A. Build B. Keep data private.")
        forged = copy.deepcopy(payload)
        changed = next(row for row in forged["sources"] if row["kind"] == "owner_brief")
        changed["text"] += " Forged new authorization."
        changed["payloadSha256"] = hashlib.sha256(changed["text"].encode()).hexdigest()
        forged["digest"] = digest({k: v for k, v in forged.items() if k != "digest"})
        self.state = self.store.create("next", "New empty scope")
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            carryover.attach(self.store, self.state, forged)
        self.assertEqual(self.state, before)

    def test_same_decoded_owner_words_repacked_are_not_new_authorization(self):
        old_text, _manifest = self.pack(self.state["briefs"][0])
        item = self.next_run(self.close())[0]
        new = self.add_brief(old_text.decode())
        self.pack(self.state["briefs"][-1])
        self.store.append(self.state, "repacked-identical-owner-words")
        with self.assertRaisesRegex(Invalid, "старого брифа|новые|new|копия"):
            carryover.decide(self.store, self.state, item["id"], "defer", brief=new, quote="Build A.")


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-carryover-test-")
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8", newline="\n")
        (self.root / "app.py").write_text("print('fixture')\n", encoding="utf-8", newline="\n")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        self.store = Journal(self.root)
        self.state = self.store.create("first", "First run")
        self.serial = 0
        self.add_brief("Build A. Build B. Keep data private.")
        self.add_requirement()
        self.store.append(self.state, "fixture-source")

    def tearDown(self):
        self.temp.cleanup()

    def add_brief(self, text):
        artifact = self.store.artifact("briefs", text.encode(), ".md")
        artifact["id"] = "B%03d" % (len(self.state["briefs"]) + 1)
        self.state["briefs"].append(artifact)
        return artifact["id"]

    def add_requirement(self, rid="R01", text="A", brief="B001", quote="Build A."):
        req = {"id": rid, "text": text, "acceptance": [text + " works"], "kind": "requested",
               "scope": "active", "core": True, "brief": brief, "quote": quote}
        self.state["requirements"].append(req)
        return req

    def close(self, status="cancelled"):
        self.state["status"] = status
        self.store.append(self.state, "fixture-closed")
        return carryover.capture(self.store, self.state)

    def next_run(self, payload=None):
        payload = payload or self.close()
        self.state = self.store.create("next", "Next run")
        carryover.attach(self.store, self.state, payload)
        self.store.append(self.state, "handoff-attached")
        return carryover.view(self.store, self.state)["items"]

    def map_current(self, item, text="A"):
        brief = self.add_brief("New owner instruction: continue Build " + text + ". with a new attempt.")
        req = self.add_requirement(text=text, brief=brief, quote="Build " + text + ".")
        self.store.append(self.state, "new-owner-brief")
        receipt = carryover.decide(self.store, self.state, item["id"], "map", req["id"], brief,
                                   "continue Build " + text + ". with a new attempt.")
        self.store.append(self.state, "mapped")
        return receipt

    def complete(self, fail_second=False):
        self.state["sealed"] = True
        self.state["core"] = [r["id"] for r in self.state["requirements"]]
        self.state["spec"] = self.store.artifact("specs", b"Build the fixture", ".md")
        self.state["tasks"] = [{"id": "T01", "title": "Build fixture", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "readZones": [], "criteria": ["fixture works"],
            "wave": 1, "status": "running"}]
        host = Host(self.store, self.state)
        team = TaskPipeline(host)
        team.prepare()
        for role in ROLES:
            team.assign("T01", role, "fixture", role, self.state["run"])
        proof = self.store.artifact("evidence", b"Verified fixture output\n", ".log")
        attempt = self.state["taskHistory"]["T01"][-1]
        attempt["verification"] = {"candidate": task_basis(self.store, self.state, "T01"),
            "exitCode": 0, "changedCode": False, "output": proof, "command": ["fixture"]}

        def report(role, **extra):
            self.serial += 1
            path = Path(self.temp.name) / (str(self.serial) + ".json")
            value = {"basis": task_basis(self.store, self.state, "T01"), "reviewer": attempt["actors"][role],
                "independent": role != "worker", "unmapped": [], "checks": [
                    {"requirement": rid, "criterion": criterion, "status": "passed", "note": "fixture",
                     "artifact": proof["file"]} for rid, criterion in sorted(team.expected_checks("T01", role))]}
            value.update(extra)
            path.write_text(json.dumps(value), encoding="utf-8", newline="\n")
            return path

        team.submit("T01", report("worker"))
        team.review("T01", 1, report("critic1", findings=[]))
        second = team.review("T01", 2, report("critic2", findings=[
            {"id": "F1", "severity": "blocking", "text": "Unresolved", "criterion": "fixture works"}
        ] if fail_second else [], previousReport=attempt["rounds"][0]["report"]["sha256"],
            repairReceipt=None, resolutions=[]))
        if fail_second:
            self.assertFalse(second)
            return
        trace = [{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                  "note": "source compared", "artifact": proof["file"]} for r in team.trace_requirements("T01")]
        self.assertTrue(team.accept("T01", report("acceptor", previousReport=attempt["rounds"][1]["report"]["sha256"],
                                                 inheritedProofs=[], trace=trace)))
        self.assertTrue(task_proof_valid(self.store, self.state, "T01"))

    def test_three_generations_preserve_unmapped_obligations_without_duplication(self):
        original = self.state["run"]
        items = self.next_run()
        self.assertEqual(items[0]["origin"]["run"], original)
        ident = items[0]["id"]
        carryover.decide(self.store, self.state, ident, "keep")
        self.add_brief("Build unrelated C.")
        self.add_requirement(text="C", quote="Build unrelated C.")
        self.store.append(self.state, "new-work")
        items = self.next_run(self.close("accepted"))
        self.assertEqual(len(items), 2)
        self.assertEqual(sum(i["id"] == ident for i in items), 1)
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 2)
        self.assertEqual(carryover.view(self.store, self.state)["summary"]["runs"], 2)
        self.assertFalse(self.state["requirements"])
        self.assertFalse(self.state["gates"])

    def test_historical_acceptance_never_becomes_current_green(self):
        self.complete()
        items = self.next_run(self.close("accepted"))
        self.assertEqual(items[0]["status"], "accepted_historical")
        self.assertFalse(items[0]["currentProof"])
        self.assertEqual(carryover.unresolved(self.store, self.state), [])
        self.assertEqual(self.state["tasks"], [])
        self.assertIsNone(self.state["verification"])
        self.assertFalse(carryover.view(self.store, self.state)["summary"]["currentProofInherited"])

    def test_cancelled_consumed_attempt_requires_new_owner_and_fresh_roles(self):
        self.complete(fail_second=True)
        old_attempt = copy.deepcopy(self.state["taskHistory"]["T01"][-1])
        item = self.next_run()[0]
        self.assertEqual(item["attempts"][0]["rounds"], 2)
        self.assertTrue(item["attempts"][0]["terminal"])
        self.add_brief("Build A. Build B. Keep data private.")
        self.add_requirement()
        self.store.append(self.state, "copied-old-brief")
        with self.assertRaises(Invalid):
            carryover.guard_current(self.store, self.state)
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "map", "R01", "B001", "Build A.")
        brief = self.add_brief("Owner now requests: Build A. Retry the previous failed attempt.")
        self.state["requirements"][0]["brief"] = brief
        self.store.append(self.state, "new-owner-command")
        carryover.decide(self.store, self.state, item["id"], "map", "R01", brief, "Retry the previous failed attempt.")
        self.store.append(self.state, "mapped")
        self.assertTrue(carryover.guard_current(self.store, self.state))
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "resolve")
        self.complete()
        self.assertFalse(set(old_attempt["actors"].values()) & set(self.state["taskHistory"]["T01"][-1]["actors"].values()))
        carryover.decide(self.store, self.state, item["id"], "resolve")
        self.store.append(self.state, "resolved")
        self.assertEqual(carryover.unresolved(self.store, self.state), [])
        third = self.next_run(self.close("accepted"))
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["id"], item["id"])
        self.assertEqual(third[0]["status"], "resolved")
        self.assertEqual(len(third[0]["attempts"]), 2)
        self.assertEqual(carryover.unresolved(self.store, self.state), [])

    def test_failed_reason_survives_cancelled_run_and_appears_in_memory(self):
        self.complete(fail_second=True)
        original_run = self.state["run"]
        report = copy.deepcopy(self.state["taskHistory"]["T01"][-1]["rounds"][1]["report"])
        item = self.next_run()[0]
        reasons = copy.deepcopy(item["openReasons"])
        failure = next(reason for reason in reasons if reason["kind"] == "critic_finding")
        self.assertEqual(failure["text"], "Unresolved")
        self.assertEqual(failure["source"], {"run": original_run, **report})
        self.assertEqual(failure["pointer"], "/findings/0/text")
        self.assertEqual(failure["omittedChars"], 0)
        self.assertTrue(any(reason["kind"] == "task_blocker" for reason in reasons))
        project_context.initialize(self.store, self.state, "codex")
        self.store.append(self.state, "context-initialized")
        project_context.refresh(self.store, self.state)
        memory = (self.store.home / "context/carryover.md").read_text(encoding="utf-8")
        self.assertIn("Unresolved", memory)
        self.assertIn(report["sha256"], memory)
        third = self.next_run()[0]
        self.assertEqual(third["openReasons"], reasons)
        self.assertEqual(third["requirement"]["acceptance"], ["A works"])

    def test_corrupt_failed_report_stops_capture(self):
        self.complete(fail_second=True)
        report = self.state["taskHistory"]["T01"][-1]["rounds"][1]["report"]
        (self.store.run / report["file"]).write_text('{"findings": []}', encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            self.close()

    def test_new_failure_for_mapped_work_accumulates_without_duplicate_old_reason(self):
        self.complete(fail_second=True)
        item = self.next_run()[0]
        old_reason = next(r for r in item["openReasons"] if r["kind"] == "critic_finding")
        self.map_current(item)
        self.complete(fail_second=True)
        third = self.next_run()[0]
        findings = [r for r in third["openReasons"] if r["kind"] == "critic_finding"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings.count(old_reason), 1)
        self.assertEqual(len({r["source"]["run"] for r in findings}), 2)

    def test_final_failed_checks_and_unmapped_acceptance_have_exact_report_sources(self):
        self.complete()
        attempt = self.state["taskHistory"]["T01"][-1]
        acceptance_report = {"unmapped": ["Missing owner edge case"]}
        source = self.store.artifact("reviews", json.dumps(acceptance_report).encode(), ".json")
        attempt["acceptance"] = {"passed": False, "report": source}
        final_report = {"checks": [{"requirement": "R01", "criterion": "A works", "status": "failed",
                                     "note": "The result returns the wrong value"}], "unmapped": []}
        final_source = self.store.artifact("reviews", json.dumps(final_report).encode(), ".json")
        self.state["gates"]["acceptance"] = {"passed": False, "report": final_source,
                                               "failures": final_report["checks"]}
        item = self.next_run()[0]
        unmapped = next(r for r in item["openReasons"] if r["kind"] == "unmapped")
        failed = next(r for r in item["openReasons"] if r["kind"] == "failed_check")
        self.assertEqual(unmapped["text"], "Missing owner edge case")
        self.assertEqual(unmapped["source"]["sha256"], source["sha256"])
        self.assertEqual(failed["text"], "The result returns the wrong value")
        self.assertEqual(failed["source"]["file"], final_source["file"])
        self.assertTrue(all(r["historical"] for r in item["openReasons"]))

    def test_long_blocker_excerpt_marks_omission_and_retains_checkpoint_pointer(self):
        note = "x" * (carryover.MAX_REASON_CHARS + 400)
        self.state["tasks"] = [{"id": "T01", "requirements": ["R01"], "status": "blocked", "note": note}]
        item = self.next_run()[0]
        reason = item["openReasons"][0]
        self.assertEqual(len(reason["text"]), carryover.MAX_REASON_CHARS)
        self.assertEqual(reason["omittedChars"], 400)
        self.assertEqual(reason["source"]["file"], "events.jsonl")
        self.assertIn("eventDigest", reason["source"])
        self.assertEqual(reason["pointer"], "/state/tasks/0/note")

    def test_secret_after_blocker_excerpt_boundary_is_not_silently_cut_off(self):
        note = "x" * carryover.MAX_REASON_CHARS + " api_key=" + "A" * 30
        self.state["tasks"] = [{"id": "T01", "requirements": ["R01"], "status": "blocked", "note": note}]
        with self.assertRaises(Invalid):
            self.close()

    def test_unresolved_mapping_stays_one_item_next_generation(self):
        item = self.next_run()[0]
        self.map_current(item)
        third = self.next_run(self.close("accepted"))
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["id"], item["id"])
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "resolve")

    def test_blind_sources_are_original_owner_words_without_ledger(self):
        old_run = self.state["run"]
        item = self.next_run()[0]
        self.assertEqual(carryover.brief_sources(self.store, self.state), [])
        self.map_current(item)
        sources = carryover.brief_sources(self.store, self.state)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "H-" + item["source"]["source"])
        self.assertEqual(sources[0]["text"], "Build A. Build B. Keep data private.")
        self.assertEqual(sources[0]["origin"]["run"], old_run)
        self.assertEqual(sources[0]["applicableQuotes"], ["Build A."])
        self.assertEqual(set(sources[0]), {"id", "text", "origin", "applicableQuotes"})
        self.complete()
        carryover.decide(self.store, self.state, item["id"], "resolve")
        self.assertEqual(carryover.brief_sources(self.store, self.state), sources)

    def test_resolved_current_work_reopens_after_code_change(self):
        item = self.next_run()[0]
        self.map_current(item)
        self.complete()
        receipt = carryover.decide(self.store, self.state, item["id"], "resolve")
        self.store.append(self.state, "resolved")
        original_receipt = (self.store.run / receipt["receipt"]["file"]).read_bytes()
        (self.root / "app.py").write_text("print('regression')\n", encoding="utf-8", newline="\n")
        reopened = carryover.unresolved(self.store, self.state)
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]["status"], "mapped")
        self.assertIn("resolutionStale", reopened[0])
        self.assertEqual((self.store.run / receipt["receipt"]["file"]).read_bytes(), original_receipt)
        next_items = self.next_run(self.close("accepted"))
        self.assertEqual(len(next_items), 1)
        self.assertEqual(next_items[0]["status"], "mapped")
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)

    def test_resolved_current_work_reopens_after_cancellation(self):
        item = self.next_run()[0]
        self.map_current(item)
        self.complete()
        carryover.decide(self.store, self.state, item["id"], "resolve")
        self.assertEqual(carryover.unresolved(self.store, self.state), [])
        next_items = self.next_run(self.close("cancelled"))
        self.assertEqual(next_items[0]["status"], "mapped")
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)

    def test_changed_mapped_contract_reopens_and_keeps_valid_receipt_chain(self):
        item = self.next_run()[0]
        self.map_current(item)
        self.complete()
        carryover.decide(self.store, self.state, item["id"], "resolve")
        self.state["requirements"][0]["acceptance"].append("new owner criterion")
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)
        carryover.decide(self.store, self.state, item["id"], "keep")
        self.assertEqual(carryover.view(self.store, self.state)["items"][0]["status"], "outstanding")

    def test_historical_scope_decisions_are_available_as_source_only_briefs(self):
        brief = self.add_brief("Defer A.")
        self.state["requirements"][0].update(scope="deferred", decision={"brief": brief, "quote": "Defer A.", "action": "defer"})
        self.store.append(self.state, "defer")
        brief = self.add_brief("Restore A.")
        self.state["requirements"][0].update(scope="active", decision={"brief": brief, "quote": "Restore A.", "action": "restore"})
        self.store.append(self.state, "restore")
        item = self.next_run()[0]
        self.map_current(item)
        sources = carryover.brief_sources(self.store, self.state)
        self.assertEqual({s["text"] for s in sources}, {"Build A. Build B. Keep data private.", "Defer A.", "Restore A."})
        self.assertTrue(all(set(s) == {"id", "text", "origin", "applicableQuotes"} for s in sources))
        for source in sources:
            if source["applicableQuotes"]:
                quote = source["applicableQuotes"][0]
                self.assertTrue(carryover.source_matches(self.store, self.state, source["id"], quote, "R01"))
                self.assertFalse(carryover.source_matches(self.store, self.state, source["id"], quote, "R99"))
            else:
                self.assertFalse(carryover.source_matches(self.store, self.state, source["id"], source["text"], "R01"))
        original = next(s for s in sources if "Build B." in s["text"])
        self.assertFalse(carryover.source_matches(self.store, self.state, original["id"], "Build B.", "R01"))

    def test_remapping_does_not_duplicate_the_replaced_requirement(self):
        item = self.next_run()[0]
        self.map_current(item)
        self.state["requirements"][0]["scope"] = "deferred"
        brief = self.add_brief("Owner now requests Build A. under the updated plan.")
        self.add_requirement(rid="R02", brief=brief)
        carryover.decide(self.store, self.state, item["id"], "map", "R02", brief, "under the updated plan.")
        self.store.append(self.state, "remapped")
        third = self.next_run()[0]
        self.assertEqual(third["id"], item["id"])
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)

    def test_owner_defer_drop_and_agent_keep_remain_distinct(self):
        item = self.next_run()[0]
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "drop")
        brief = self.add_brief("Defer A until next month.")
        carryover.decide(self.store, self.state, item["id"], "defer", brief=brief, quote="Defer A until next month.")
        self.assertEqual(carryover.unresolved(self.store, self.state)[0]["status"], "deferred")
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "drop", brief=brief, quote="Defer A until next month.")
        self.store.append(self.state, "deferred")
        third = self.next_run(self.close("accepted"))[0]
        self.assertEqual(third["status"], "deferred")
        self.assertEqual(third["history"][-1]["quote"], "Defer A until next month.")
        brief = self.add_brief("Drop A permanently.")
        carryover.decide(self.store, self.state, item["id"], "drop", brief=brief, quote="Drop A permanently.")
        self.assertEqual(carryover.unresolved(self.store, self.state), [])
        self.store.append(self.state, "dropped")
        fourth = self.next_run(self.close())[0]
        self.assertEqual(fourth["status"], "dropped")
        self.assertEqual([x["action"] for x in fourth["history"]], ["defer", "drop"])

    def test_map_cannot_weaken_criteria_kind_core_or_text(self):
        item = self.next_run()[0]
        brief = self.add_brief("New owner: Build A. Try again.")
        req = self.add_requirement(brief=brief)
        for key, value in (("acceptance", ["different"]), ("core", False), ("kind", "addition"), ("text", "weaker")):
            before = copy.deepcopy(req)
            req[key] = value
            with self.subTest(key=key), self.assertRaises(Invalid):
                carryover.decide(self.store, self.state, item["id"], "map", "R01", brief, "Try again.")
            req.clear(); req.update(before)
        req["acceptance"].append("additional verification")
        carryover.decide(self.store, self.state, item["id"], "map", "R01", brief, "Try again.")
        mapped = carryover.view(self.store, self.state)["items"][0]
        self.assertEqual(mapped["mapping"]["originalSource"], item["source"])
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)

    def test_mapping_after_completed_reviews_is_rejected(self):
        item = self.next_run()[0]
        brief = self.add_brief("New owner: Build A. Retry it.")
        self.add_requirement(brief=brief)
        self.complete()
        with self.assertRaises(Invalid):
            carryover.decide(self.store, self.state, item["id"], "map", "R01", brief, "Retry it.")

    def test_shared_generic_criterion_does_not_claim_semantic_identity(self):
        self.state["requirements"][0]["acceptance"] = ["tests pass"]
        self.store.append(self.state, "criterion")
        self.next_run()
        self.add_brief("New unrelated feature.")
        self.add_requirement(text="unrelated", quote="New unrelated feature.")["acceptance"] = ["tests pass"]
        self.assertTrue(carryover.guard_current(self.store, self.state))

    def test_changed_source_rejected_before_capture_and_after_capture(self):
        brief = self.store.run / self.state["briefs"][0]["file"]
        original = brief.read_bytes()
        self.state["status"] = "cancelled"
        self.store.append(self.state, "closed")
        brief.write_text("changed", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)
        brief.write_bytes(original)
        payload = carryover.capture(self.store, self.state)
        self.state = self.store.create("second", "Second")
        brief.write_text("changed again", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            carryover.attach(self.store, self.state, payload)
        self.assertNotIn("carryover", self.state)

    def test_changed_handoff_or_receipt_rejected(self):
        item = self.next_run()[0]
        receipt = carryover.decide(self.store, self.state, item["id"], "keep")
        path = self.store.run / receipt["receipt"]["file"]
        path.write_text("{}", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            carryover.view(self.store, self.state)

    def test_in_root_symlink_source_and_write_directory_rejected(self):
        brief = self.store.run / self.state["briefs"][0]["file"]
        original = brief.read_bytes()
        destination = self.store.run / "source.md"
        destination.write_bytes(original)
        brief.unlink(); brief.symlink_to(destination)
        with self.assertRaises(Invalid):
            self.close()
        brief.unlink(); brief.write_bytes(original)
        payload = carryover.capture(self.store, self.state)
        self.state = self.store.create("second", "Second")
        target = self.store.run / "real"
        target.mkdir()
        (self.store.run / "carryover").symlink_to(target, target_is_directory=True)
        with self.assertRaises(Invalid):
            carryover.attach(self.store, self.state, payload)

    def test_secret_and_tampered_payload_are_rejected(self):
        self.add_brief("api_key=" + "A" * 30)
        self.state["status"] = "cancelled"
        self.store.append(self.state, "closed")
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)
        self.state["briefs"].pop()
        self.store.append(self.state, "fixture-removed-secret")
        payload = carryover.capture(self.store, self.state)
        payload["items"].clear()
        self.state = self.store.create("second", "Second")
        with self.assertRaises(Invalid):
            carryover.attach(self.store, self.state, payload)

    def test_unsaved_or_nonterminal_state_cannot_be_captured(self):
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)
        self.state["status"] = "cancelled"
        with self.assertRaises(Invalid):
            carryover.capture(self.store, self.state)

    def test_initializer_attaches_before_first_event(self):
        payload = self.close()
        self.state = self.store.create("atomic-next", "Atomic next", initializer=lambda state:
                                       carryover.attach(self.store, state, payload))
        saved = self.store.load()
        self.assertEqual(saved["carryover"], self.state["carryover"])
        self.assertEqual(len(carryover.unresolved(self.store, saved)), 1)

    def test_initializer_failure_keeps_previous_active_pointer(self):
        payload = self.close()
        pointer = (self.store.home / "active.json").read_bytes()
        source = self.store.run / self.state["briefs"][0]["file"]
        source.write_text("tampered after capture", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            self.store.create("failed-next", "Failed next", initializer=lambda state:
                              carryover.attach(self.store, state, payload))
        self.assertEqual((self.store.home / "active.json").read_bytes(), pointer)

    def test_old_owner_decisions_and_original_sources_keep_lineage(self):
        brief = self.add_brief("Defer A.")
        self.state["requirements"][0].update(scope="deferred", decision={"brief": brief, "quote": "Defer A.", "action": "defer"})
        self.store.append(self.state, "scope-deferred")
        brief = self.add_brief("Restore A.")
        self.state["requirements"][0].update(scope="active", decision={"brief": brief, "quote": "Restore A.", "action": "restore"})
        self.store.append(self.state, "scope-restored")
        original_run = self.state["run"]
        self.next_run()
        result = carryover.view(self.store, self.state)
        decisions = [d for d in result["decisions"] if d["kind"] == "owner_scope"]
        self.assertEqual([d["action"] for d in decisions], ["defer", "restore"])
        self.assertTrue(all(d["run"] == original_run for d in decisions))
        source_ids = {s["id"] for s in result["sources"]}
        self.assertTrue(all(d["source"] in source_ids for d in decisions))

    def test_handoff_remains_readable_without_old_run_files(self):
        old_path = self.store.run
        self.next_run()
        renamed = old_path.with_name(old_path.name + "-offline")
        old_path.rename(renamed)
        result = carryover.view(self.store, self.state)
        self.assertEqual(result["sources"][0]["text"], "Build A. Build B. Keep data private.")
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)
        self.next_run()
        self.assertEqual(len(carryover.unresolved(self.store, self.state)), 1)


if __name__ == "__main__":
    unittest.main()
