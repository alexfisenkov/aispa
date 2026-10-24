"""Dashboard upgrades preserve read-only access to previous stable runs."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core.store import Journal, Invalid
from aispa_core.dashboard_reader import DisplayJournal

class LabelReleaseCompatibilityTests(unittest.TestCase):
    def test_previous_patch_runs_have_read_only_view_and_terminal_handoff(self):
        for version in ('0.3.2', '0.3.3', '0.3.4', '0.3.5', '0.3.6'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as root:
                store=Journal(root)
                with patch('aispa_core.store.VERSION',version):
                    state=store.create('old','Synthetic previous stable')
                path=store.run/'events.jsonl'; before=path.read_bytes()
                with self.assertRaises(Invalid):store.load()
                with self.assertRaises(Invalid):store.load(handoff_only=True)
                reader=DisplayJournal(root)
                self.assertEqual(reader.load()['version'],version)
                with self.assertRaises(Invalid):reader.append(state,'must-not-write')
                self.assertEqual(path.read_bytes(),before)
                state['status']='cancelled';store.append(state,'synthetic-terminal')
                before=path.read_bytes()
                self.assertEqual(store.load(handoff_only=True)['version'],version)
                self.assertEqual(path.read_bytes(),before)

if __name__=='__main__':unittest.main()
