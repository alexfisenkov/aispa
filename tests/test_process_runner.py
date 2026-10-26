import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import process_runner
from test_preflight import optional_module
from aispa_core.store import Invalid, Journal


PATTERN = re.compile(r"ghp_[A-Za-z0-9]{20,}")


class ManagedProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa managed runner 🙂 ")
        self.root = Path(self.temp.name)
        self.resources = optional_module("resources")

    def tearDown(self):
        self.temp.cleanup()

    def context(self):
        self.assertIsNotNone(self.resources, "Runner must reserve before creating a process")
        store = Journal(self.root)
        state = store.create("runner", "Managed runner")
        state.update(policy={"managedExecution": True}, hostSession="fixture-session")
        self.resources.configure(store, state, {"schema": 1, "scope": "project", "limits":
            {"agents": 1, "browsers": 1, "testProcesses": 1, "services": 1}})
        return self.resources.context(store, state)

    def call(self, ctx, command=None):
        return process_runner.run(command or [sys.executable, "-c", "print('managed')"],
            self.root, os.environ.copy(), 3, max_output=1000, secret_pattern=PATTERN,
            secrets=(), resource_context=ctx)

    def test_budget_refusal_happens_before_start_and_success_releases(self):
        ctx = self.context()
        held = self.resources.reserve(ctx, "testProcesses")
        with patch.object(process_runner, "_start") as start, self.assertRaises(Invalid):
            self.call(ctx)
        start.assert_not_called()
        self.resources.finish(ctx, held["id"], outcome="launch_failed", confirmed=True)
        self.assertEqual(self.call(ctx)[1], 0)
        self.assertTrue(self.resources.reserve(ctx, "testProcesses")["id"])

    def test_missing_executable_releases_its_reservation(self):
        ctx = self.context()
        self.assertEqual(self.call(ctx, [str(self.root / "missing")])[1], 127)
        self.assertTrue(self.resources.reserve(ctx, "testProcesses")["id"])

    @unittest.skipUnless(os.name == "posix", "POSIX cleanup uncertainty fixture")
    def test_cleanup_uncertainty_keeps_an_orphaned_reservation(self):
        ctx = self.context()
        original = process_runner._PosixGroup.close

        def uncertain(owner):
            original(owner)
            raise OSError("Synthetic missing termination acknowledgement")

        with patch.object(process_runner._PosixGroup, "close", uncertain), self.assertRaises(OSError):
            self.call(ctx)
        with self.assertRaises(Invalid):
            self.resources.reserve(ctx, "testProcesses")


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa process space-🙂-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, script, *, timeout=5, cap=1048576, secrets=(), args=(), env=None):
        return process_runner.run(
            [sys.executable, "-c", script, *args], self.root,
            os.environ.copy() if env is None else env, timeout,
            max_output=cap, secret_pattern=PATTERN, secrets=secrets)

    def test_binary_stdout_stderr_exit_code_and_literal_arguments(self):
        result = self.run_script(
            "import os,sys; os.write(1,b'out\\x00'); os.write(2,b'err'); "
            "os.write(1,sys.argv[1].encode('utf-8')); sys.exit(7)",
            args=("space 🙂 & echo shell-must-not-run",))
        self.assertEqual(result, (
            "out\0errspace 🙂 & echo shell-must-not-run".encode(), 7, False, False, False))

    def test_stdin_is_closed_and_environment_is_explicit(self):
        env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR")}
        env["AISPA_VALUE"] = "provided"
        with patch.dict(os.environ, {"AISPA_INHERITED": "blocked"}):
            result = self.run_script(
                "import os,sys; assert sys.stdin.buffer.read()==b''; "
                "assert os.environ['AISPA_VALUE']=='provided'; "
                "assert 'AISPA_INHERITED' not in os.environ; print('ok')", env=env)
        self.assertEqual(result[1:], (0, False, False, False))
        self.assertEqual(result[0].strip(), b"ok")

    def test_missing_executable_returns_sanitized_failure(self):
        result = process_runner.run(
            [str(self.root / "missing-private-name")], self.root, os.environ.copy(), 5,
            max_output=100, secret_pattern=PATTERN, secrets=())
        self.assertEqual(result, (b"AISpa: command could not start.\n", 127, False, False, False))

    def test_large_output_stays_bounded_and_does_not_deadlock(self):
        result = self.run_script("import os; os.write(1,b'x'*3000000)", cap=1000)
        self.assertEqual(result[1:], (0, False, True, False))
        self.assertTrue(result[0].startswith(b"x" * 1000))
        self.assertLess(len(result[0]), 1100)

    def test_pattern_and_unicode_literal_after_cap_across_reads(self):
        for secret, secrets in (("ghp_" + "a" * 30, ()), ("личный-ключ🙂", ("личный-ключ🙂",))):
            data = secret.encode()
            result = self.run_script(
                "import os,time; os.write(1,b'x'*1500000); "
                f"s={data!r}; "
                "\nfor byte in s:\n os.write(1,bytes([byte])); time.sleep(.002)",
                cap=1000, secrets=secrets)
            self.assertEqual(result[1:], (0, False, True, True))
            self.assertNotIn(data, result[0])

    def test_long_literal_spanning_multiple_chunks_is_detected(self):
        secret = "begin-" + "z" * 150000 + "-end"
        result = self.run_script(
            "import os; os.write(1,b'prefix'); os.write(1,b'begin-'+b'z'*150000+b'-end')",
            cap=100, secrets=(secret,))
        self.assertTrue(result[4])

    def test_timeout_and_closed_output_do_not_busy_wait_or_hang(self):
        started = time.monotonic()
        result = self.run_script("import os,time; os.close(1); os.close(2); time.sleep(30)", timeout=.15)
        self.assertEqual(result[1:3], (124, True))
        self.assertLess(time.monotonic() - started, 3)

    def tree_script(self, *, wait):
        # Descendants keep inherited output handles open. The leaf only writes
        # its failure marker when the test releases it AFTER run() has returned.
        leaf = (
            "import os,time; from pathlib import Path; "
            "Path('leaf.pid').write_text(str(os.getpid())); "
            "\nwhile not Path('go').exists(): time.sleep(.01)"
            "\nPath('leaked').write_text('still running')")
        middle = (
            "import os,subprocess,sys,time; from pathlib import Path; "
            "Path('middle.pid').write_text(str(os.getpid())); "
            f"subprocess.Popen([sys.executable,'-c',{leaf!r}]); time.sleep(30)")
        return (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{middle!r}]); "
            "\nwhile not Path('leaf.pid').exists(): time.sleep(.01)"
            + ("\ntime.sleep(30)" if wait else "\nsys.exit(7)"))

    def assert_tree_stopped(self):
        self.assertTrue((self.root / "middle.pid").exists(), "middle process must actually start")
        self.assertTrue((self.root / "leaf.pid").exists(), "grandchild must actually start")
        (self.root / "go").touch()
        time.sleep(.2)
        self.assertFalse((self.root / "leaked").exists())
        if os.name == "nt":
            for name in ("middle.pid", "leaf.pid"):
                self.assert_windows_process_exited(int((self.root / name).read_text()))

    def assert_windows_process_exited(self, pid):
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # process no longer exists
            return
        try:
            self.assertEqual(api.WaitForSingleObject(handle, 2000), 0)
        finally:
            api.CloseHandle(handle)

    def test_timeout_stops_child_and_grandchild(self):
        result = self.run_script(self.tree_script(wait=True), timeout=3)
        self.assertEqual(result[1:3], (124, True))
        self.assert_tree_stopped()

    def test_exited_parent_stops_child_and_grandchild_without_pipe_hang(self):
        result = self.run_script(self.tree_script(wait=False))
        self.assertEqual(result[1:3], (7, False))
        self.assert_tree_stopped()

    @unittest.skipUnless(os.name == "posix", "POSIX setsid escape documents non-sandbox boundary")
    def test_detached_writer_cannot_hang_or_create_false_success(self):
        child = "import os,time; from pathlib import Path; Path('detached.pid').write_text(str(os.getpid())); time.sleep(10)"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
            "\nwhile not Path('detached.pid').exists(): time.sleep(.01)")
        try:
            result = self.run_script(script)
            self.assertTrue(result[3])
            self.assertTrue(result[4], "unscanned held-open pipe must fail closed")
        finally:
            pidfile = self.root / "detached.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), 9)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object test")
    def test_windows_command_already_in_job_at_first_instruction(self):
        result = self.run_script(
            "import ctypes; from ctypes import wintypes; "
            "api=ctypes.WinDLL('kernel32',use_last_error=True); "
            "api.GetCurrentProcess.restype=wintypes.HANDLE; "
            "api.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]; "
            "value=wintypes.BOOL(); "
            "assert api.IsProcessInJob(api.GetCurrentProcess(),None,ctypes.byref(value)); "
            "assert value.value; print('assigned')")
        self.assertEqual(result[1:], (0, False, False, False))
        self.assertEqual(result[0].strip(), b"assigned")

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object test")
    def test_windows_assignment_failure_never_releases_command(self):
        with patch.object(process_runner._WindowsJob, "attach", side_effect=OSError("refused")):
            result = self.run_script("from pathlib import Path; Path('unsafe').touch()")
        self.assertEqual(result[1], 127)
        self.assertFalse((self.root / "unsafe").exists())

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object test")
    def test_windows_job_does_not_allow_breakaway_children(self):
        result = self.run_script(
            "import subprocess,sys; "
            "\ntry:\n subprocess.Popen([sys.executable,'-c','pass'],creationflags=0x01000000)"
            "\nexcept OSError as error:\n assert error.winerror==5; print('breakaway denied')"
            "\nelse:\n sys.exit(9)")
        self.assertEqual(result[1:], (0, False, False, False))
        self.assertEqual(result[0].strip(), b"breakaway denied")

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object test")
    def test_windows_supervisor_ignores_cwd_import_shadowing(self):
        (self.root / "subprocess.py").write_text("raise RuntimeError('cwd injection')")
        (self.root / "sitecustomize.py").write_text("raise RuntimeError('site injection')")
        result = self.run_script("print('trusted bootstrap')")
        self.assertEqual(result[1:], (0, False, False, False))
        self.assertEqual(result[0].strip(), b"trusted bootstrap")

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object test")
    def test_windows_job_closes_when_controlling_runner_is_killed(self):
        module_path = str(Path(process_runner.__file__).resolve().parent.parent)
        script = (
            "import sys,os,re; "
            f"sys.path.insert(0,{module_path!r}); from aispa_core.process_runner import run; "
            f"run([sys.executable,'-c',{self.tree_script(wait=True)!r}],os.getcwd(),os.environ.copy(),30,"
            "max_output=100,secret_pattern=re.compile('never-match'),secrets=())")
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", script], cwd=self.root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "leaf.pid").exists() and time.monotonic() < deadline:
                self.assertIsNone(proc.poll())
                time.sleep(.02)
            self.assertTrue((self.root / "leaf.pid").exists())
            proc.kill()
            proc.wait(timeout=5)
            self.assert_tree_stopped()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
