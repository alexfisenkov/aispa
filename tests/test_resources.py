"""Managed project leases: atomic admission is separate from host-wide control."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.store import Invalid, Journal
from test_preflight import optional_module


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa resources space 🙂 ")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("leases", "Synthetic managed resources")
        self.state.update(policy={"managedExecution": True}, hostSession="native-session")
        self.resources = optional_module("resources")
        self.profile = {"schema": 1, "scope": "project", "limits":
            {"agents": 1, "browsers": 1, "testProcesses": 1, "services": 1}}

    def tearDown(self):
        self.temp.cleanup()

    def api(self):
        self.assertIsNotNone(self.resources, "Managed launches require an atomic lease governor")
        return self.resources

    def context(self):
        self.api().configure(self.store, self.state, self.profile)
        return self.api().context(self.store, self.state)

    def test_profile_rejects_invalid_and_above_host_caps_without_mutation(self):
        for bad in (-1, 0, True, 1.5, float("inf"), float("nan"), "2"):
            profile = copy.deepcopy(self.profile)
            profile["limits"]["agents"] = bad
            before = copy.deepcopy(self.state)
            with self.subTest(cap=bad), self.assertRaises(Invalid):
                self.api().configure(self.store, self.state, profile)
            self.assertEqual(self.state, before)
        profile = copy.deepcopy(self.profile)
        profile["limits"]["agents"], profile["hostLimits"] = 2, {"agents": 1}
        with self.assertRaises(Invalid):
            self.api().configure(self.store, self.state, profile)

    def test_type_caps_are_separate_and_foreign_finish_cannot_free_slot(self):
        ctx = self.context()
        first = self.api().reserve(ctx, "agents")
        self.api().reserve(ctx, "browsers")
        self.api().reserve(ctx, "testProcesses")
        with self.assertRaises(Invalid):
            self.api().reserve(ctx, "agents")
        handle = {"runtime": "codex", "nativeId": "actual-native", "launchId": "call-1"}
        self.api().bind(ctx, first["id"], handle)
        for key, value in (("owner", "other"), ("run", "other"), ("session", "other")):
            foreign = dict(ctx, **{key: value})
            with self.subTest(key=key), self.assertRaises(Invalid):
                self.api().finish(foreign, first["id"], handle=handle, confirmed=True)
        with self.assertRaises(Invalid):
            self.api().finish(ctx, first["id"], handle={**handle, "nativeId": "other"}, confirmed=True)
        with self.assertRaises(Invalid):
            self.api().reserve(ctx, "agents")
        self.api().finish(ctx, first["id"], handle=handle, confirmed=True)
        self.api().finish(ctx, first["id"], handle=handle, confirmed=True)
        self.assertTrue(self.api().reserve(ctx, "agents")["id"])

    def test_unconfirmed_stop_and_partial_inventory_retain_the_slot_after_restart(self):
        ctx = self.context()
        lease = self.api().reserve(ctx, "agents")
        handle = {"runtime": "codex", "nativeId": "surviving-agent", "launchId": "call-2"}
        self.api().bind(ctx, lease["id"], handle)
        self.api().finish(ctx, lease["id"], handle=handle, confirmed=False)
        fresh = self.api().context(Journal(self.store.root), self.state)
        self.api().reconcile(fresh, [], complete=False)
        with self.assertRaises(Invalid):
            self.api().reserve(fresh, "agents")
        self.api().reconcile(fresh, [{"leaseId": lease["id"], "handle": handle, "status": "completed"}], complete=True)
        self.assertTrue(self.api().reserve(fresh, "agents")["id"])

    def test_failed_launch_releases_only_reserved_lease_and_pid_is_not_handle(self):
        ctx = self.context()
        lease = self.api().reserve(ctx, "testProcesses")
        with self.assertRaises(Invalid):
            self.api().bind(ctx, lease["id"], {"pid": 123})
        self.api().finish(ctx, lease["id"], outcome="launch_failed", confirmed=True)
        self.assertTrue(self.api().reserve(ctx, "testProcesses")["id"])

    def test_two_processes_racing_for_one_slot_admit_exactly_one(self):
        ctx = self.context()
        base = Path(self.temp.name)
        script = ("import json,sys,time; from pathlib import Path; from aispa_core import resources; "
            "ctx=json.loads(sys.argv[1]); gate=Path(sys.argv[2]); "
            "\nwhile not gate.exists(): time.sleep(.005)"
            "\ntry:\n resources.reserve(ctx,'agents'); print('admitted')"
            "\nexcept ValueError:\n print('refused')")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
        children = [subprocess.Popen([sys.executable, "-B", "-c", script, json.dumps(ctx), str(base / "go")],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
        try:
            (base / "go").touch()
            results = [child.communicate(timeout=15) for child in children]
            self.assertTrue(all(child.returncode == 0 for child in children), results)
            self.assertEqual(sorted(out.strip() for out, _err in results), ["admitted", "refused"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_missing_session_is_not_invented_and_legacy_has_no_managed_context(self):
        self.assertIsNone(self.api().context(self.store, {"policy": {}}))
        self.state.pop("hostSession")
        with self.assertRaises(Invalid):
            self.api().context(self.store, self.state)

    def test_capacity_has_a_distinct_typed_refusal_and_service_bootstrap_is_not_native_session(self):
        ctx = self.context()
        self.api().reserve(ctx, "agents")
        capacity = getattr(self.api(), "CapacityRefused", None)
        self.assertTrue(isinstance(capacity, type) and issubclass(capacity, Invalid))
        with self.assertRaises(capacity) as error:
            self.api().reserve(ctx, "agents")
        self.assertEqual(error.exception.code, "RESOURCE_CAPACITY")
        self.state.pop("hostSession")
        with self.assertRaises(Invalid) as error:
            self.api().context(self.store, self.state)
        self.assertNotIsInstance(error.exception, capacity)
        local = self.api().context(self.store, self.state, owner="dashboard-service", service_instance="actual-service-instance")
        self.assertEqual(local["sessionKind"], "local-service")
        self.assertEqual(local["serviceInstance"], "actual-service-instance")
        self.assertNotIn("hostSession", self.state)
        lease = self.api().reserve(local, "services")
        handle = {"runtime": "local-service", "nativeId": "exact-process-handle", "launchId": "actual-service-instance"}
        self.api().bind(local, lease["id"], handle)
        self.state["hostSession"] = "later-real-native-session"
        same = self.api().context(self.store, self.state, owner="dashboard-service", service_instance="actual-service-instance")
        self.api().finish(same, lease["id"], handle=handle, confirmed=True)


if __name__ == "__main__":
    unittest.main()
