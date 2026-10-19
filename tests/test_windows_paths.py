import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import portable
from aispa_core.store import Journal, Invalid, canonical_zone, confined


class PortablePathTests(unittest.TestCase):
    def test_device_drive_ads_and_alias_paths_rejected_on_every_host(self):
        for name in ("C:relative", "C:/root", "/root", "//server/share", "foo:stream", "con", "AUX.txt",
                     "x/NUL.log", "COM1", "LPT².txt", "CONIN$", "CONOUT$", "file.", "file ", "x/../y", "x\\y", "x?", "x|y"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                portable.relative_name(name)
            with self.subTest(zone=name), self.assertRaises(Invalid):
                canonical_zone(name)
        self.assertEqual(portable.relative_name("./проект с пробелами//файл.py"), "проект с пробелами/файл.py")
        self.assertEqual(canonical_zone("./"), ".")

    def test_junction_detected_but_non_redirecting_cloud_file_allowed(self):
        for tag, expected in ((0xA0000003, True), (0xA000000C, True), (0, True), (0x9000001A, False)):
            value = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_reparse_tag=tag)
            with self.subTest(tag=tag), patch.object(Path, "lstat", return_value=value):
                self.assertEqual(portable.is_link(Path("fixture")), expected)

    def test_utf8_journal_roundtrip_is_independent_of_locale(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Journal(temp)
            state = store.create("unicode", "Русский проект 🎈")
            state["notes"].append("Решение: сохранить пользователя")
            store.append(state, "кириллица")
            self.assertEqual(store.load()["notes"], state["notes"])

    @unittest.skipUnless(os.name == "nt", "Native junction creation")
    def test_windows_junction_cannot_redirect_managed_home(self):
        with tempfile.TemporaryDirectory(prefix="aispa-junction-") as temp:
            base = Path(temp); project = base / "project"; external = base / "external"
            project.mkdir(); external.mkdir()
            link = project / ".aispa"
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
                                    capture_output=True, check=True)
            try:
                self.assertTrue(portable.is_link(link))
                with self.assertRaises(Invalid):
                    Journal(project)
                with self.assertRaises(Invalid):
                    confined(project, ".aispa/events.jsonl")
            finally:
                link.rmdir()


if __name__ == "__main__":
    unittest.main()
