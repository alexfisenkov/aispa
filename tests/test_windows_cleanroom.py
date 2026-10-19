import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import cleanroom, portable
from aispa_core.store import Invalid, Journal


class _Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa clean Русский space-🙂-")
        self.root = Path(self.temp.name).resolve()
        self.junctions = []
        self.git("init", "-q")
        (self.root / ".gitignore").write_bytes(b".aispa/\n.venv/\nbuild/\n")
        (self.root / "app.py").write_bytes(b"print('committed')\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.store = Journal(self.root)
        self.state = self.store.create("windows", "Проверка Windows 🙂")

    def tearDown(self):
        for link in reversed(self.junctions):
            if portable.is_link(link):
                link.rmdir()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True).stdout

    def prepare(self):
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        return Path(self.state["cleanroom"]["path"])

    def execute(self, argv, timeout=30):
        receipt = cleanroom.execute(self.store, self.state, argv, timeout)
        self.state["cleanrun"] = receipt
        return receipt

    def junction(self, path, target):
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, "mklink /J must not require Developer Mode")
        self.junctions.append(path)


class CleanroomAdapterTests(_Fixture, unittest.TestCase):
    def test_run_delegates_to_portable_runner_without_changing_contract(self):
        expected = (b"output", 7, False, True, False)
        with patch.object(cleanroom.process_runner, "run", return_value=expected) as run:
            actual = cleanroom._run([sys.executable], self.root, {"PATH": "test"}, 4, ["secret"])
        self.assertEqual(actual, expected)
        run.assert_called_once_with([sys.executable], self.root, {"PATH": "test"}, 4,
                                    max_output=cleanroom.MAX_OUTPUT, secret_pattern=cleanroom.SECRET,
                                    secrets=["secret"])

    def test_git_binary_stdin_and_nul_delimited_output(self):
        result = cleanroom._git(self.root, "check-attr", "-z", "--stdin", "text", input_data=b"app.py\0")
        self.assertEqual(result.stdout, b"app.py\0text\0unspecified\0")

    def test_windows_device_tree_paths_are_rejected_on_every_platform(self):
        for name in ("C:source", "NUL", "src/AUX.md", "src/readme.md:stream", "src/trailing."):
            with self.subTest(name=name), self.assertRaises(Invalid):
                cleanroom._safe_name(name)


@unittest.skipUnless(os.name == "nt", "native Windows cleanroom integration")
class WindowsCleanroomTests(_Fixture, unittest.TestCase):
    def test_actual_clean_environment_uses_isolated_homes_and_utf8(self):
        clone = self.prepare()
        with patch.dict(os.environ, {"SECRET_TOKEN": "private-sentinel-test", "PYTHONPATH": "bad-modules",
                                     "APPDATA": "bad-host-config", "LOCALAPPDATA": "bad-host-cache"}):
            receipt = self.execute([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ))); print('Русский 🙂')"])
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        lines = self.store.text(receipt["output"]).splitlines()
        environment = {key.upper(): value for key, value in json.loads(lines[0]).items()}
        home = clone.parent / "home"
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP"):
            self.assertTrue(Path(environment[key]).is_relative_to(home), key)
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(Path(environment["SYSTEMROOT"]), cleanroom._windows_root())
        self.assertEqual(Path(environment["COMSPEC"]), cleanroom._windows_root() / "System32/cmd.exe")
        self.assertEqual(lines[1], "Русский 🙂")

    def test_clone_relative_venv_executable_runs_with_backslashes(self):
        self.prepare()
        built = self.execute([sys.executable, "-c", "import venv; venv.EnvBuilder(with_pip=False).create('.venv')"])
        self.assertTrue(built["passed"], self.store.text(built["output"]))
        receipt = self.execute([r".venv\Scripts\python.exe", "-c", "print('clone executable')"])
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        self.assertEqual(self.store.text(receipt["output"]).strip(), "clone executable")

    def test_source_paths_case_separators_raw_strings_and_attached_flags_are_rejected(self):
        self.prepare()
        source = str(self.root / "app.py")
        for argument in (source.swapcase(), source.replace("\\", "/"), "--input=" + source,
                         "from pathlib import Path; Path(" + repr(source) + ").read_text()",
                         "from pathlib import Path; Path(r'" + source + "').read_text()",
                         "type '" + source + "'", (self.root / "app.py").as_uri(),
                         source + ":stream", "C:app.py"):
            with self.subTest(argument=argument), self.assertRaises(Invalid):
                self.execute([sys.executable, "-c", argument])

    def test_junction_alias_to_source_venv_is_refused(self):
        source = self.root / ".venv/Scripts"
        source.mkdir(parents=True)
        shutil.copyfile(sys.executable, source / "python.exe")
        clone = self.prepare()
        self.junction(clone / ".venv", source.parent)
        with self.assertRaises(Invalid):
            self.execute([r".venv\Scripts\python.exe", "-c", "print('must not execute')"])

    def test_direct_batch_requires_explicit_shell_and_cmd_remains_available(self):
        clone = self.prepare()
        (clone / "build").mkdir()
        (clone / "build/check.cmd").write_bytes(b"@echo explicit batch\r\n")
        with self.assertRaisesRegex(Invalid, "cmd.exe"):
            self.execute([r"build\check.cmd"])
        receipt = self.execute(["cmd.exe", "/d", "/c", r"build\check.cmd"])
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        self.assertIn("explicit batch", self.store.text(receipt["output"]))

    def test_host_path_or_controller_cwd_cannot_supply_missing_executable(self):
        self.prepare()
        host_dir = self.root / ".venv"
        host_dir.mkdir()
        shutil.copyfile(sys.executable, host_dir / "private-fixture.exe")
        with patch.dict(os.environ, {"PATH": str(host_dir)}):
            receipt = self.execute(["private-fixture.exe", "-c", "print('must not run')"])
        self.assertEqual(receipt["exitCode"], 127)
        self.assertFalse(receipt["passed"])


if __name__ == "__main__":
    unittest.main()
