"""T04: source-bound host reports are evidence, never newly granted authority."""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import aispa
from aispa_core import autonomy, participants
from aispa_core.store import Invalid, digest, file_hash
import test_workflow as helpers


def optional_module(name):
    try:
        return importlib.import_module("aispa_core." + name)
    except ModuleNotFoundError as error:
        if error.name != "aispa_core." + name:
            raise
        return None


def synthetic_host_snapshot(store, state, *, session="synthetic-cli-host"):
    """Positive host observation for disposable CLI fixtures, never live proof."""
    api = optional_module("preflight")
    if api is None:
        return None
    rules = store.root / "AGENTS.md"
    marker = "Synthetic fixture permits independent delegation and test processes."
    text = rules.read_text(encoding="utf-8") if rules.exists() else ""
    if marker not in text:
        rules.write_text(text + "\n" + marker + "\n", encoding="utf-8", newline="\n")
    payload = {"schema": 1, "identity": api.identity(store, state), "runtime": state.get("runtime", "codex"),
        "session": session, "tools": [{"name": name, "status": "available"}
            for name in ("spawn_agent", "exec_command", "browser")],
        "capabilities": {name: {"status": "allowed", "source": "synthetic-host-api"}
            for name in ("delegation", "independentRoles", "processes", "browser")},
        "rules": [{"file": "AGENTS.md", "sha256": file_hash(rules), "decisions": [
            {"action": "delegation", "decision": "allow", "quote": marker}]}],
        "assignments": [{"actor": row["id"], "nativeId": row["nativeId"], "session": row["session"],
                         "status": "live"} for row in participants.list_participants(store, state)],
        "observedAt": "2026-10-26T00:00:00+00:00"}
    return {"source": store.artifact("host-evidence", (json.dumps(payload) + "\n").encode(), ".json")}


class ManagedFixture(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def managed(self):
        self.state.update(runtime="codex", mode="auto", policy={"managedExecution": True,
                          "firstBriefingClosure": True})
        pin = self.store.run / "runtime"
        (pin / "tools").mkdir(parents=True)
        (pin / "tools/aispa.py").write_text("# synthetic pinned package\n", encoding="utf-8")
        self.state["runtimeDigest"] = digest({"tools/aispa.py": file_hash(pin / "tools/aispa.py")})
        (self.root / "AGENTS.md").write_text("Delegation allowed. Independent roles required.\n", encoding="utf-8")
        self.flow.brief(self.file("Build export. Keep data private.", "brief.md"))
        self.flow.requirements(self.file([{"id": "R01", "text": "Export", "core": True,
          "brief": "B001", "quote": "Build export.", "acceptance": ["Local CSV"]}]))
        self.preflight = optional_module("preflight")

    def api(self):
        self.assertIsNotNone(self.preflight, "T04 requires a callable host preflight, not blind resume")
        return self.preflight

    def host(self, **changes):
        identity = self.api().identity(self.store, self.state)
        payload = {"schema": 1, "identity": identity, "runtime": "codex", "session": "new-chat",
          "tools": [{"name": "spawn_agent", "status": "available"},
                    {"name": "exec_command", "status": "available"}],
          "capabilities": {name: {"status": "allowed", "source": "host-tool-schema"}
              for name in ("delegation", "independentRoles", "processes")},
          "rules": [{"file": "AGENTS.md", "sha256": file_hash(self.root / "AGENTS.md"),
                     "decisions": [{"action": "delegation", "decision": "allow",
                                    "quote": "Delegation allowed."}]}],
          "assignments": [], "observedAt": "2026-10-26T00:00:00+00:00"}
        payload.update(changes)
        return {"source": self.store.artifact("host-evidence", (json.dumps(payload) + "\n").encode(), ".json")}

    def payload(self, envelope):
        return self.store.json(envelope["source"])

    def reseal(self, payload):
        return {"source": self.store.artifact("host-evidence", (json.dumps(payload) + "\n").encode(), ".json")}


class PreflightTests(ManagedFixture):
    def test_browser_identity_adoption_does_not_require_unrelated_capabilities_or_grant_seal(self):
        self.managed()
        host = self.host(capabilities={"browser": {"status": "allowed", "source": "native-browser-api"},
            "delegation": {"status": "unknown", "source": "not-observed"},
            "remote": {"status": "unknown", "source": "not-observed"}},
            tools=[{"name": "browser", "status": "available"}])
        report = self.api().adopt(self.flow, host, purpose="browser")
        self.assertTrue(report["ok"])
        self.assertEqual(self.state["hostSession"], "new-chat")
        self.assertNotIn("firstBriefingClosedAt", self.state)
        with self.assertRaises(Invalid):
            self.flow.seal()

    def test_new_policy_seal_refuses_missing_host_evidence_before_any_mutation(self):
        self.managed()
        before, journal = copy.deepcopy(self.state), (self.store.run / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(Invalid, "[Pp]reflight|host|возможност"):
            self.flow.seal()
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)

    def test_identity_mismatches_and_stale_basis_fail_without_writes(self):
        self.managed()
        api, good = self.api(), self.host()
        for field, value in (("root", str(self.root / "other")), ("run", "other-run"),
                             ("runtimeDigest", "0" * 64)):
            with self.subTest(field=field):
                raw = self.payload(good)
                raw["identity"][field] = value
                host = self.reseal(raw)
                before = (self.store.run / "events.jsonl").read_bytes()
                report = api.check(self.store, self.state, host)
                self.assertFalse(report["ok"])
                self.assertTrue(report["blockers"])
                self.assertEqual((self.store.run / "events.jsonl").read_bytes(), before)
        stale = self.host()
        self.flow.decision("Another actor saved an event")
        self.assertFalse(api.check(self.store, self.state, stale)["ok"])

    def test_delegation_denial_unknown_and_required_approval_are_explicit(self):
        self.managed()
        for status in ("denied", "unknown", "approval_required"):
            raw = self.payload(self.host())
            raw["capabilities"]["delegation"]["status"] = status
            report = self.api().check(self.store, self.state, self.reseal(raw), purpose="seal")
            self.assertFalse(report["ok"])
            self.assertIn(status, json.dumps(report))
            self.assertTrue(report["actions"])

    def test_rules_sha_and_pin_bytes_are_rechecked_without_cached_permission(self):
        self.managed()
        host = self.host()
        self.assertTrue(self.api().check(self.store, self.state, host)["ok"])
        (self.root / "AGENTS.md").write_text("Delegation denied.\n", encoding="utf-8")
        self.assertFalse(self.api().check(self.store, self.state, host)["ok"])
        (self.root / "AGENTS.md").write_text("Delegation allowed. Independent roles required.\n", encoding="utf-8")
        (self.store.run / "runtime/tools/aispa.py").write_text("# changed pin\n", encoding="utf-8")
        self.assertFalse(self.api().check(self.store, self.state, host)["ok"])

    def test_actual_delegation_denial_is_not_cleared_by_allowed_host_declaration(self):
        self.managed()
        (self.root / "AGENTS.md").write_text("Delegation is forbidden. Do not spawn sub-agents.\n", encoding="utf-8")
        host = self.host(rules=[{"file": "AGENTS.md", "sha256": file_hash(self.root / "AGENTS.md"),
            "decisions": [{"action": "delegation", "decision": "allow", "quote": "Delegation is forbidden."}]}])
        report = self.api().check(self.store, self.state, host, purpose="seal")
        self.assertFalse(report["ok"])
        self.assertIn("AGENTS.md", json.dumps(report["blockers"]))

    def test_missing_required_tool_or_forged_source_is_not_a_success(self):
        self.managed()
        host = self.host(tools=[])
        self.assertFalse(self.api().check(self.store, self.state, host, purpose="seal")["ok"])
        host = self.host()
        host["source"]["sha256"] = "0" * 64
        self.assertFalse(self.api().check(self.store, self.state, host)["ok"])

    def test_relocation_needs_current_owner_quote_and_exact_identity_binding(self):
        self.managed()
        previous_root = str(self.root / "old-location")
        self.state["projectRoot"] = previous_root
        self.flow.brief(self.file("Relocate this run to the current physical project root.", "relocate.md"))
        host = self.host()
        raw = self.payload(host)
        raw["identity"]["root"] = str(self.root)
        self.assertFalse(self.api().check(self.store, self.state, self.reseal(raw))["ok"])
        binding = {"fromRoot": previous_root, "toRoot": str(self.root), "run": self.state["run"],
            "runtimeDigest": self.state["runtimeDigest"], "brief": "B002",
            "quote": "Relocate this run to the current physical project root."}
        raw["relocation"] = {"source": self.store.artifact("relocations", json.dumps(binding).encode(), ".json")}
        self.assertTrue(self.api().check(self.store, self.state, self.reseal(raw))["ok"])

    def test_source_refs_have_confined_relative_absolute_and_physical_sha(self):
        self.managed()
        report = self.api().check(self.store, self.state, self.host())
        self.assertTrue(report["ok"], report)
        refs = report["requiredSources"]
        self.assertTrue(any(row.get("kind") == "brief" for row in refs))
        for ref in refs:
            self.assertFalse(Path(ref["file"]).is_absolute())
            path = self.store.run / ref["file"]
            self.assertEqual(Path(ref["absolutePath"]), path.resolve())
            self.assertEqual(file_hash(path), ref["sha256"])
            self.assertIn(ref["representation"], ("plain", "manifest"))

    def test_running_continuation_preserves_attempts_gates_and_clocks(self):
        self.managed()
        self.api().adopt(self.flow, self.host(), expected_basis=None)
        self.flow.seal()
        before = copy.deepcopy(self.state)
        host = self.host(session="next-chat")
        self.flow.resume(host=host)
        self.assertEqual(self.state["status"], before["status"])
        self.assertEqual(self.state["gates"], before["gates"])
        self.assertEqual(self.state.get("taskHistory"), before.get("taskHistory"))
        self.assertEqual(self.state.get("activeSince"), before.get("activeSince"))
        self.assertEqual(self.state["firstBriefingClosedAt"], before["firstBriefingClosedAt"])
        self.assertFalse(autonomy.view(self.state)["mayAsk"])
        self.assertEqual(self.state["hostSession"], "next-chat")
        self.assertEqual(len(self.state["continuations"]), 1)
        self.assertTrue(autonomy.view(self.state)["authority"]["existingPermissionsOnly"])

    def test_terminal_run_and_pending_recovery_cannot_be_resumed(self):
        self.managed()
        for status in ("accepted", "delivered_with_remaining", "cancelled"):
            self.state["status"] = status
            self.store.append(self.state, "terminal-fixture")
            before = copy.deepcopy(self.state)
            with self.subTest(status=status), self.assertRaises(Invalid):
                self.flow.resume()
            self.assertEqual(self.state, before)
        self.state.update(status="paused", journalRecovery={"pending": True})
        with self.assertRaises(Invalid):
            self.flow.resume()

    def test_missing_live_assignment_is_stale_not_erased_or_independent(self):
        self.managed()
        aid = participants.register(self.store, self.state, "brief-reviewer", "codex", "old-native", "old-chat")
        self.flow.save("existing-actor")
        actor = copy.deepcopy(self.state["participants"][aid])
        report = self.api().check(self.store, self.state, self.host())
        self.assertFalse(report["ok"])
        self.assertEqual(self.state["participants"][aid], actor)
        self.assertIn(aid, json.dumps(report["blockers"]))
        with self.assertRaises(Invalid):
            participants.register(self.store, self.state, "brief-reviewer", "codex", "old-native", "new-chat")

    def test_protected_remote_blocker_does_not_reopen_interview(self):
        self.managed()
        self.api().adopt(self.flow, self.host(), expected_basis=None)
        self.flow.seal()
        raw = self.payload(self.host())
        raw["capabilities"]["remote"] = {"status": "denied", "source": "host-denial"}
        report = self.api().check(self.store, self.state, self.reseal(raw), purpose="remote")
        self.assertFalse(report["ok"])
        self.assertFalse(autonomy.view(self.state)["mayAsk"])
        self.flow.decision("Protected route blocked; continue independent work")

    def test_cli_preflight_is_read_only_and_unknown_host_is_not_success(self):
        self.managed()
        before = (self.store.run / "events.jsonl").read_bytes()
        result = subprocess.run([sys.executable, "-B", str(Path(aispa.__file__)), "--root", str(self.root),
                                 "preflight"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertTrue(result.stdout, "Read-only preflight should provide an actionable JSON refusal")
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
