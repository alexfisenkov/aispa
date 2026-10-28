"""Pinned source hashes of the 0.4.0 repair tools must match the package files.

A change to a pinned file (aispa.py, team.py, workflow.py, owner_task_attempt.py)
makes the repair tools refuse every 0.4.0 run. This test names the drifted file
and the module to re-pin instead of failing deep inside a repair scenario.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))

from aispa_core import runtime_final_retry_repair as final_retry
from aispa_core import runtime_owner_attempt_repair as owner
from aispa_core.store import file_hash

REPIN = "перезакрепи SOURCE_HASHES (старый набор перенеси в HISTORICAL_SOURCE_HASHES)"


def drift(package, pins):
    """Pinned relative paths whose current hash differs from the pin."""
    return sorted(name for name, pinned in pins.items()
                  if file_hash(package / name) != pinned)


class RuntimeRepairPinTests(unittest.TestCase):
    def test_owner_attempt_pins_match_package_files(self):
        self.assertEqual([], drift(PACKAGE, owner.SOURCE_HASHES),
                         "runtime_owner_attempt_repair: " + REPIN)

    def test_final_retry_pin_matches_package_file(self):
        self.assertEqual([], drift(PACKAGE, final_retry.SOURCE_HASHES),
                         "runtime_final_retry_repair: " + REPIN)

    def test_both_tools_pin_the_same_workflow_source(self):
        file = "tools/aispa_core/workflow.py"
        self.assertEqual(owner.SOURCE_HASHES[file], final_retry.SOURCE_HASHES[file])

    def test_historical_sets_are_complete_and_differ_from_current(self):
        for old in owner.HISTORICAL_SOURCE_HASHES:
            self.assertEqual(set(owner.SOURCE_HASHES), set(old))
            self.assertNotEqual(owner.SOURCE_HASHES, old)
        for old in final_retry.HISTORICAL_SOURCE_HASHES:
            self.assertEqual(set(final_retry.SOURCE_HASHES), set(old))
            self.assertNotEqual(final_retry.SOURCE_HASHES, old)

    def test_edit_of_a_pinned_file_is_reported_with_its_name(self):
        names = ("tools/aispa.py", "tools/aispa_core/team.py", "tools/aispa_core/workflow.py")
        for name in names:
            with self.subTest(file=name), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                for pinned in names:
                    (package / pinned).parent.mkdir(parents=True, exist_ok=True)
                    (package / pinned).write_bytes(b"# pinned source " + pinned.encode() + b"\n")
                pins = {pinned: file_hash(package / pinned) for pinned in names}
                self.assertEqual([], drift(package, pins))
                with (package / name).open("ab") as handle:
                    handle.write(b"# edited\n")
                self.assertEqual([name], drift(package, pins))


if __name__ == "__main__":
    unittest.main()
