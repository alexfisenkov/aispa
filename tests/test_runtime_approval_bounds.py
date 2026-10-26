"""Refusal boundaries for both historical runtime-repair approval readers.

Each public apply preflight runs in a disposable child. A FIFO race may block
the old implementation; the parent always enforces a short deadline.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIMIT = 131072
MODULES = ("runtime_owner_attempt_repair", "runtime_zone_repair")
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core.store import Invalid

CHILD = r'''
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from aispa_core import runtime_owner_attempt_repair, runtime_zone_repair
from aispa_core.store import Invalid

module = {"runtime_owner_attempt_repair": runtime_owner_attempt_repair,
          "runtime_zone_repair": runtime_zone_repair}[sys.argv[2]]
case = sys.argv[3]
folder = Path(sys.argv[4])
approval = folder / "approval.txt"
target = folder / "other.txt"
run = folder / "run"
run.mkdir()
target.write_text("Another ordinary file\n", encoding="utf-8")
body = b"Owner: allow exact repair\n"
if case == "oversized":
    body += b"x" * 131073
elif case == "invalid_utf8":
    body = b"\xff"
approval.write_bytes(body)
expected_sha = hashlib.sha256(approval.read_bytes()).hexdigest()
if case == "bad_hash":
    expected_sha = "0" * 64
manifest = run / "manifest.json"

class PastApproval(Exception):
    pass

class UnboundedRead(Exception):
    pass

class Store:
    def __init__(self):
        self.run = run

    @contextlib.contextmanager
    def lock(self):
        yield

store = Store()
calls = 0
def selected(_root):
    global calls
    calls += 1
    if calls > 1:
        raise PastApproval()
    return (store, {}, {"count": 0}, None, None)

original_read_bytes = Path.read_bytes
def guarded_read_bytes(path):
    if path == approval:
        raise UnboundedRead()
    return original_read_bytes(path)

original_is_link = module.is_link
swapped = False
def swap_after_link_check(path):
    global swapped
    result = original_is_link(path)
    if Path(path) == approval and not swapped:
        swapped = True
        if case == "growth":
            with approval.open("ab") as output:
                output.write(b"x" * 131073)
        elif case == "fifo":
            approval.unlink()
            os.mkfifo(approval)
        elif case == "symlink":
            approval.unlink()
            approval.symlink_to(target)
        elif case == "missing":
            approval.unlink()
    return result

original_os_read = os.read
read_sizes = []
def bounded_read(fd, size):
    read_sizes.append(size)
    return original_os_read(fd, size)

kwargs = {"manifest_file": manifest,
          "expected_manifest_sha256": "0" * 64,
          "expected_journal_sha256": "1" * 64,
          "approval_file": approval,
          "confirm": True}
if module is runtime_owner_attempt_repair:
    kwargs.update(expected_approval_sha256=expected_sha,
                  owner_quote="Different exact quote" if case == "bad_quote" else "Owner: allow exact repair")
selected_name = "_selected" if module is runtime_owner_attempt_repair else "_load"
def maybe_guarded_read_bytes(path):
    if case in ("invalid_utf8", "bad_quote", "bad_hash", "missing", "fifo"):
        return original_read_bytes(path)
    return guarded_read_bytes(path)

with patch.object(module, selected_name, side_effect=selected), \
     patch.object(module, "_cas", return_value=None), \
     patch.object(module, "is_link", side_effect=swap_after_link_check), \
     patch.object(Path, "read_bytes", maybe_guarded_read_bytes), \
     patch.object(os, "read", side_effect=bounded_read):
    try:
        module.apply(folder, **kwargs)
    except PastApproval:
        status = "past_approval"
    except Invalid:
        status = "refused"
    except UnboundedRead:
        status = "unbounded_read"
    except Exception as exc:
        status = "unexpected:" + type(exc).__name__
    else:
        status = "unexpected_success"
print(json.dumps({"status": status, "readSizes": read_sizes, "calls": calls,
                  "runFiles": [p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()]}))
'''


class RuntimeApprovalBoundsTests(unittest.TestCase):
    def probe(self, module, case):
        with tempfile.TemporaryDirectory(prefix="aispa-approval-bound-") as temp:
            command = [sys.executable, "-B", "-c", CHILD,
                       str(ROOT / "skills/aispa/tools"), module, case, temp]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                        text=True, timeout=3, check=False)
            except subprocess.TimeoutExpired:
                self.fail(f"{module}/{case}: approval preflight exceeded 3-second deadline")
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_oversized_file_is_rejected_before_unbounded_read(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "oversized")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertEqual(observed["readSizes"], [])
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    def test_growth_after_metadata_check_is_rejected_with_bounded_read(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "growth")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertTrue(all(size <= LIMIT + 1 for size in observed["readSizes"]))
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_fifo_replacement_never_blocks_preflight(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "fifo")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    def test_symlink_replacement_is_refused_without_following_target(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "symlink")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    def test_invalid_utf8_is_a_bounded_protocol_refusal(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "invalid_utf8")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    def test_disappearing_approval_is_a_dependency_refusal(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "missing")
                self.assertEqual(observed["status"], "refused", observed)
                self.assertEqual(observed["runFiles"], [])
                self.assertEqual(observed["calls"], 1)

    def test_quote_mismatch_still_refuses_owner_bridge(self):
        observed = self.probe("runtime_owner_attempt_repair", "bad_quote")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertEqual(observed["runFiles"], [])

    def test_hash_mismatch_still_refuses_owner_bridge(self):
        observed = self.probe("runtime_owner_attempt_repair", "bad_hash")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertEqual(observed["runFiles"], [])

    def test_ordinary_file_crosses_preflight_with_bounded_physical_read(self):
        for module in MODULES:
            with self.subTest(module=module):
                observed = self.probe(module, "ordinary")
                self.assertEqual(observed["status"], "past_approval", observed)
                self.assertGreaterEqual(observed["calls"], 2)
                self.assertTrue(observed["readSizes"], observed)
                self.assertTrue(all(0 < size <= LIMIT + 1 for size in observed["readSizes"]))


class RuntimeDigestHistoryTests(unittest.TestCase):
    """A transient digest change is invalid even when the final value recovers."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_runtime_owner_attempt_repair as owner_tests
        import test_runtime_zone_repair as zone_tests
        cls.owner_tests = owner_tests
        cls.zone_tests = zone_tests
        owner_tests.OwnerAttemptRuntimeRepairTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.owner_tests.OwnerAttemptRuntimeRepairTests.tearDownClass()

    def append_transient(self, fixture, state, journal, label):
        original = state["runtimeDigest"]
        state["runtimeDigest"] = "0" * 64
        fixture.store.append(state, "synthetic-" + label + "-digest-mutation")
        state["runtimeDigest"] = original
        fixture.store.append(state, "synthetic-" + label + "-digest-restore")
        frozen = journal.read_bytes()
        self.assertEqual(fixture.store.load()["runtimeDigest"], original)
        return frozen

    def test_zone_reader_refuses_transient_digest_before_first_repair(self):
        fixture = self.zone_tests.ZoneRuntimeRepairTests(
            methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        fixture.setUp()
        try:
            frozen = self.append_transient(fixture, fixture.before_state, fixture.history, "pre-zone")
            with self.assertRaises(Invalid):
                self.zone_tests.repair.inspect(fixture.root)
            self.assertEqual(fixture.history.read_bytes(), frozen)
        finally:
            fixture.tearDown()

    def test_zone_reader_refuses_transient_digest_between_repairs(self):
        fixture = self.zone_tests.ZoneRuntimeRepairTests(
            methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        fixture.setUp()
        try:
            fixture.first_maintenance()
            state = fixture.store.load()
            frozen = self.append_transient(fixture, state, fixture.history, "between-zone")
            with self.assertRaises(Invalid):
                self.zone_tests.repair.inspect(fixture.root)
            self.assertEqual(fixture.history.read_bytes(), frozen)
        finally:
            fixture.tearDown()

    def test_owner_reader_refuses_transient_digest_before_owner_repair(self):
        fixture = self.owner_tests.OwnerAttemptRuntimeRepairTests(
            methodName="test_new_selected_copy_preserves_old_history_and_runtime")
        fixture.setUp()
        try:
            frozen = self.append_transient(fixture, fixture.state, fixture.journal, "pre-owner")
            with self.assertRaises(Invalid):
                self.owner_tests.repair.inspect(fixture.root)
            self.assertEqual(fixture.journal.read_bytes(), frozen)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
