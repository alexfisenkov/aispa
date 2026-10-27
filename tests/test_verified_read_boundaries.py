"""Bounded read regressions plus the frozen runtime-approval regression suite.

Potential FIFO and device reads execute only in disposable child processes with
a parent deadline. The child never opens an actual project journal or pin.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import test_runtime_approval_bounds as approval_regressions


ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from aispa_core import scale, verified_reads
from aispa_core.store import Invalid

case = sys.argv[2]
folder = Path(sys.argv[3]).resolve()
target = folder / "input.bin"
original = b"stable source bytes\n"
target.write_bytes(original)
history = folder / "history.bin"
history.write_bytes(b"unchanged synthetic history\n")
expected = hashlib.sha256(original).hexdigest()
reader = verified_reads.VerifiedReads()
if case.startswith("revalidate") or case.endswith("revalidate"):
    reader.hashes[target] = expected

original_os_open = os.open
original_path_open = Path.open
swapped = False
class ReadAfterDeadline(Exception):
    pass
def swap_once():
    global swapped
    if swapped:
        return
    swapped = True
    if case.endswith("fifo"):
        target.unlink()
        os.mkfifo(target)
    elif case.endswith("same_bytes"):
        replacement = folder / "replacement.bin"
        replacement.write_bytes(original)
        target.unlink()
        replacement.rename(target)

def guarded_os_open(path, flags, *args, **kwargs):
    if Path(path) == target and case.startswith("deadline"):
        raise ReadAfterDeadline()
    if Path(path) == target and (case.endswith("fifo") or case.endswith("same_bytes")):
        swap_once()
    return original_os_open(path, flags, *args, **kwargs)

def guarded_path_open(path, *args, **kwargs):
    if Path(path) == target and case.startswith("deadline"):
        raise ReadAfterDeadline()
    if Path(path) == target and (case.endswith("fifo") or case.endswith("same_bytes")):
        swap_once()
    return original_path_open(path, *args, **kwargs)

if case == "hash_nonregular":
    target = Path("/dev/zero")

budget = scale.Budget()
if case.startswith("deadline"):
    budget.deadline = -1.0
try:
    with patch.object(os, "open", side_effect=guarded_os_open), \
         patch.object(Path, "open", guarded_path_open), \
         patch.object(scale, "Budget", return_value=budget):
        if case.startswith("physical"):
            value = verified_reads.physical_read(target, budget, limit=1024)
            accepted = value == original
        elif case.startswith("hash") or case.startswith("deadline_hash"):
            accepted = reader.hash_valid(target, expected)
        elif case.startswith("revalidate") or case.startswith("deadline_revalidate"):
            reader.revalidate()
            accepted = True
        elif case == "regular":
            value = verified_reads.physical_read(target, budget, limit=1024)
            accepted = value == original and reader.hash_valid(target, expected)
            reader.revalidate()
        else:
            raise AssertionError("unknown case")
except Invalid as exc:
    status = "resource_limit" if isinstance(exc, verified_reads.ResourceLimit) else "refused"
except Exception as exc:
    status = "unexpected:" + type(exc).__name__
else:
    status = "accepted" if accepted else "unexpected_false"
print(json.dumps({"status": status, "historyPreserved": history.read_bytes() == b"unchanged synthetic history\n",
                  "sourceWasSwapped": swapped}))
'''

ADJACENT_CHILD = r'''
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from aispa_core import preflight, scale, support_faq, verified_reads
from aispa_core.store import Invalid, Journal

case = sys.argv[2]
folder = Path(sys.argv[3]).resolve()
history = folder / "history.bin"
history.write_bytes(b"unchanged synthetic history\n")
raw = b"stable source bytes\n"
if case.startswith("package"):
    package = folder / "package"
    target = package / "module.py"
elif case.startswith("faq"):
    package = folder / "package"
    target = package / "references/support-faq.json"
    if case == "faq_oversize":
        raw = b"x" * (support_faq.MAX_BYTES + 1)
elif case.startswith("source"):
    run = folder / "run"
    target = run / "briefs/source.md"
elif case.startswith("rule"):
    run = folder
    target = folder / "AGENTS.md"
    (folder / "runtime").mkdir()
elif case.startswith("artifact"):
    run = folder / ".aispa/runs/demo"
    target = run / "evidence/blob.bin"
else:
    raise AssertionError("unknown case")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(raw)
expected = hashlib.sha256(raw).hexdigest()
original_os_open = os.open
original_path_open = Path.open
original_read_bytes = Path.read_bytes
original_scale_read_file = scale.read_file
swapped = False

class UnboundedRead(Exception):
    pass

def swap_once():
    global swapped
    if not swapped:
        swapped = True
        target.unlink()
        os.mkfifo(target)

def guarded_os_open(path, flags, *args, **kwargs):
    if Path(path) == target and case.endswith("_fifo") and case != "source_after_read_fifo":
        swap_once()
    return original_os_open(path, flags, *args, **kwargs)

def guarded_path_open(path, *args, **kwargs):
    if Path(path) == target and case.endswith("_fifo") and case != "source_after_read_fifo":
        swap_once()
    return original_path_open(path, *args, **kwargs)

def guarded_read_bytes(path):
    if Path(path) == target and case == "source_growth":
        raise UnboundedRead()
    return original_read_bytes(path)

def after_source_read(path, *args, **kwargs):
    global swapped
    value = original_scale_read_file(path, *args, **kwargs)
    if Path(path) == target and not swapped:
        if case == "source_after_read_fifo":
            swap_once()
        elif case == "source_growth":
            with target.open("ab") as stream:
                stream.write(b"x" * 2048)
            swapped = True
    return value

if case == "source_growth":
    os.environ["AISPA_MAX_INPUT_BYTES"] = "1024"

try:
    with patch.object(os, "open", side_effect=guarded_os_open), \
         patch.object(Path, "open", guarded_path_open), \
         patch.object(Path, "read_bytes", guarded_read_bytes), \
         patch.object(scale, "read_file", side_effect=after_source_read):
        if case.startswith("package"):
            value = preflight._package_digest(package)
            accepted = isinstance(value, str) and len(value) == 64
        elif case.startswith("faq"):
            value = support_faq._file(package, "references/support-faq.json")
            accepted = value == raw
        elif case.startswith("source"):
            store = Journal(folder)
            store.run = run
            refs = preflight._sources(store, {"briefs": [{"id": "B001", "file": "briefs/source.md",
                "sha256": expected}], "spec": None, "tasks": []})
            accepted = len(refs) == 1 and refs[0]["representation"] == "plain"
        elif case.startswith("artifact"):
            store = Journal(folder)
            store.run = run
            accepted = store.artifact_hash_valid({"file": "evidence/blob.bin", "sha256": expected})
        else:
            identity = {"root": str(folder), "run": "demo", "runtimeDigest": "pin",
                "runtimePath": "runtime", "journalSequence": 1, "journalHash": "h", "stateDigest": "s"}
            host = {"identity": identity, "runtime": "codex", "session": "fixture",
                "observedAt": "2026-10-27T00:00:00Z",
                "capabilities": {name: {"status": "allowed", "source": "fixture"} for name in
                                 ("delegation", "independentRoles", "processes")},
                "tools": [{"name": "spawn_agent", "status": "available"},
                          {"name": "exec", "status": "available"}],
                "rules": [{"file": "AGENTS.md", "sha256": expected, "decisions": []}],
                "assignments": []}
            state = {"run": "demo", "runtimeDigest": "pin", "runtime": "codex",
                "projectContext": {"instructionFiles": ["AGENTS.md"]}, "tasks": []}
            store = SimpleNamespace(root=folder, run=run)
            original_file_hash = preflight.file_hash
            def checked_hash(path):
                return expected if Path(path) == target else original_file_hash(path)
            with patch.object(preflight, "identity", return_value=identity), \
                 patch.object(preflight, "_sources", return_value=[]), \
                 patch.object(preflight, "_package_digest", return_value="pin"), \
                 patch.object(preflight, "_host", return_value=host), \
                 patch.object(preflight, "_relocation", return_value=None), \
                 patch.object(preflight, "file_hash", side_effect=checked_hash), \
                 patch.object(preflight.participants, "list_participants", return_value=[]):
                result = preflight._check(store, state, {"source": "fixture"}, "continue")
            accepted = result["ok"]
            if not accepted:
                raise Invalid("preflight blocked an unsafe source")
except Invalid as exc:
    status = "resource_limit" if isinstance(exc, verified_reads.ResourceLimit) else "refused"
except Exception as exc:
    status = "unexpected:" + type(exc).__name__
else:
    status = "accepted" if accepted else "unexpected_false"
print(json.dumps({"status": status, "historyPreserved": history.read_bytes() ==
                  b"unchanged synthetic history\n", "sourceWasSwapped": swapped}))
'''


class ReaderBoundaryTests(unittest.TestCase):
    def probe(self, case):
        with tempfile.TemporaryDirectory(prefix="aispa-read-boundary-") as temp:
            command = [sys.executable, "-B", "-c", CHILD,
                       str(ROOT / "skills/aispa/tools"), case, temp]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                        text=True, timeout=3, check=False)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{case}: reader exceeded 3-second child deadline") from None
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(observed["historyPreserved"], observed)
            return observed

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_physical_read_replaced_by_fifo_never_blocks(self):
        observed = self.probe("physical_fifo")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_hash_valid_replaced_by_fifo_never_blocks(self):
        observed = self.probe("hash_fifo")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_revalidate_replaced_by_fifo_never_blocks(self):
        observed = self.probe("revalidate_fifo")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    @unittest.skipUnless(Path("/dev/zero").exists(), "nonregular device is POSIX-only")
    def test_hash_valid_rejects_nonregular_device_before_read(self):
        observed = self.probe("hash_nonregular")
        self.assertEqual(observed["status"], "refused", observed)

    def test_hash_valid_rejects_same_bytes_new_inode(self):
        observed = self.probe("hash_same_bytes")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    def test_revalidate_rejects_same_bytes_new_inode(self):
        observed = self.probe("revalidate_same_bytes")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    def test_physical_read_keeps_existing_inode_guard(self):
        observed = self.probe("physical_same_bytes")
        self.assertEqual(observed["status"], "refused", observed)
        self.assertTrue(observed["sourceWasSwapped"])

    def test_expired_hash_deadline_refuses_before_read(self):
        observed = self.probe("deadline_hash")
        self.assertEqual(observed["status"], "resource_limit", observed)

    def test_expired_revalidate_deadline_refuses_before_read(self):
        observed = self.probe("deadline_revalidate")
        self.assertEqual(observed["status"], "resource_limit", observed)

    def test_ordinary_file_reads_and_hashes_without_mutation(self):
        observed = self.probe("regular")
        self.assertEqual(observed["status"], "accepted", observed)


class AdjacentReaderTests(unittest.TestCase):
    def probe(self, case):
        with tempfile.TemporaryDirectory(prefix="aispa-adjacent-read-") as temp:
            command = [sys.executable, "-B", "-c", ADJACENT_CHILD,
                       str(ROOT / "skills/aispa/tools"), case, temp]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                        text=True, timeout=3, check=False)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{case}: reader exceeded 3-second child deadline") from None
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(observed["historyPreserved"], observed)
            return observed

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_outside_scope_artifact_hash_refuses_fifo_swap(self):
        observed = self.probe("artifact_fifo")
        self.assertEqual(observed["status"], "refused", observed)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_preflight_pinned_package_hash_refuses_fifo_swap(self):
        observed = self.probe("package_fifo")
        self.assertEqual(observed["status"], "refused", observed)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_preflight_source_after_bounded_read_refuses_fifo_swap(self):
        observed = self.probe("source_after_read_fifo")
        self.assertEqual(observed["status"], "refused", observed)

    def test_preflight_source_growth_obeys_input_budget(self):
        observed = self.probe("source_growth")
        self.assertEqual(observed["status"], "resource_limit", observed)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_preflight_rule_second_read_refuses_fifo_swap(self):
        observed = self.probe("rule_fifo")
        self.assertEqual(observed["status"], "refused", observed)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_support_faq_source_refuses_fifo_swap(self):
        observed = self.probe("faq_fifo")
        self.assertEqual(observed["status"], "refused", observed)

    def test_support_faq_oversize_is_still_rejected(self):
        observed = self.probe("faq_oversize")
        self.assertEqual(observed["status"], "refused", observed)

    def test_regular_legacy_callers_remain_usable(self):
        for case in ("artifact_regular", "package_regular", "source_regular",
                     "rule_regular", "faq_regular"):
            with self.subTest(case=case):
                observed = self.probe(case)
                self.assertEqual(observed["status"], "accepted", observed)


def load_tests(loader, _tests, _pattern):
    """Always execute the prior frozen approval source before new reader cases."""
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(approval_regressions.RuntimeApprovalBoundsTests))
    suite.addTests(loader.loadTestsFromTestCase(approval_regressions.RuntimeDigestHistoryTests))
    suite.addTests(loader.loadTestsFromTestCase(ReaderBoundaryTests))
    suite.addTests(loader.loadTestsFromTestCase(AdjacentReaderTests))
    return suite


if __name__ == "__main__":
    unittest.main()
