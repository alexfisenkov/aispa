"""Bounded command capture and process-lifetime cleanup; not a sandbox.

POSIX commands run in a new process group. On Windows an isolated, fixed Python
supervisor waits for a one-byte release before starting the requested command.
The supervisor is assigned to a kill-on-close Job Object before that release.
No breakaway limits are enabled, and the job handle is never inheritable.

Windows uses CPython's retained ``Popen._handle`` (not a PID lookup), and fails
closed if it is unavailable. Python 3.11+ on Windows 10/11 is the supported
Windows runtime. Native Windows tests are required to verify this branch.
"""
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid

from . import resources


_CHUNK = 65536
_POLL = 0.02
_DRAIN_TIMEOUT = 1.0
_START_FAILED = b"AISpa: command could not start.\n"


class _LaunchUncertain(OSError):
    """The bootstrap failed without confirmed cleanup of its exact child."""

# -I excludes cwd/PYTHONPATH/user site; -S also disables system sitecustomize
# and .pth code. The original argv is passed as arguments, never interpolated
# into this code or evaluated by a shell. The child's stdin is still DEVNULL.
_SUPERVISOR = """import subprocess, sys
if sys.stdin.buffer.read(1) != b'1':
    sys.exit(125)
sys.stdin.close()
try:
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, shell=False)
except OSError:
    sys.stdout.buffer.write(b'AISpa: command could not start.\\n')
    sys.exit(127)
sys.exit(child.wait())
"""


class _PosixGroup:
    def __init__(self, proc):
        self.proc = proc

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may report EPERM when only the exited parent remains.
            if self.proc.poll() is None:
                raise


class _WindowsJob:
    """Own exactly one unnamed, non-inheritable Windows Job Object handle."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        # Layouts: winnt.h JOBOBJECT_{BASIC,EXTENDED}_LIMIT_INFORMATION and
        # IO_COUNTERS. SIZE_T/ULONG_PTR are pointer-sized on both Win32/Win64.
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, proc):
        handle = getattr(proc, "_handle", None)
        if handle is None:
            raise OSError("Windows runner requires CPython's retained process handle")
        if not self.api.AssignProcessToJobObject(self.handle, int(handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            self.handle = None


class _PipeReader:
    """Read only available bytes, including Windows anonymous pipes.

One reader owns the pipe. PeekNamedPipe avoids using select/selectors on a
Windows pipe and avoids a blocked background thread if a pipe stays open.
"""

    def __init__(self, stream):
        self.fd = stream.fileno()
        self.selector = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            self.ctypes = ctypes
            self.handle = msvcrt.get_osfhandle(self.fd)
            self.available = wintypes.DWORD()
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            self.api.PeekNamedPipe.argtypes = [
                wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            self.api.PeekNamedPipe.restype = wintypes.BOOL
        else:
            self.selector = selectors.DefaultSelector()
            self.selector.register(stream, selectors.EVENT_READ)

    def read(self):
        if self.selector is not None:
            if not self.selector.select(_POLL):
                return None
            return os.read(self.fd, _CHUNK)
        if not self.api.PeekNamedPipe(self.handle, None, 0, None,
                                     self.ctypes.byref(self.available), None):
            error = self.ctypes.get_last_error()
            if error == 109:  # ERROR_BROKEN_PIPE: all writers have closed.
                return b""
            raise self.ctypes.WinError(error)
        if self.available.value:
            return os.read(self.fd, min(self.available.value, _CHUNK))
        time.sleep(_POLL)
        return None

    def close(self):
        if self.selector is not None:
            self.selector.close()


def _start(command, cwd, env):
    options = dict(cwd=cwd, env=env, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, shell=False, bufsize=0)
    if os.name != "nt":
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                start_new_session=True, **options)
        return proc, _PosixGroup(proc)
    job, proc = _WindowsJob(), None
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _SUPERVISOR, *command],
                                stdin=subprocess.PIPE, **options)
        job.attach(proc)
        # Only this release permits the trusted supervisor to execute argv.
        proc.stdin.write(b"1")
        proc.stdin.close()
        return proc, job
    except BaseException:
        try:
            try:
                job.close()
            finally:
                if proc is not None:
                    # Also handles assignment failure: the blocked supervisor was
                    # never in the job, but no requested command could have started.
                    try:
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait(timeout=5)
                    finally:
                        proc.stdin.close()
                        proc.stdout.close()
        except BaseException as cleanup_error:
            raise _LaunchUncertain("Windows bootstrap cleanup was not confirmed") from cleanup_error
        raise


def run(command, cwd, env, timeout, *, max_output, secret_pattern, secrets, resource_context=None,
        on_admitted=None):
    """Return (output, exit_code, timed_out, truncated, redacted).

The caller validates argv, timeout, environment and output limit. Output is
bounded by max_output plus short status notices. All received bytes are scanned
even after that limit, with an overlap for patterns and complete literal secrets.
The caller must withhold the returned bytes when redacted is true.

POSIX cleanup covers the process group, not deliberate setsid()/external service
escapes. Windows covers descendants created normally via CreateProcess; jobs
are lifetime management, not a security boundary against external services.
"""
    lease = resources.reserve(resource_context, "testProcesses") if resource_context is not None else None
    if lease is not None and on_admitted is not None:
        try:
            on_admitted("testProcesses")
        except BaseException:
            # No process exists yet. In particular, a persistence OSError is
            # not an executable-start failure and must reach the caller.
            resources.finish(resource_context, lease["id"], outcome="launch_failed", confirmed=True)
            raise
    bound, handle = False, None
    cleanup = {"confirmed": False, "uncertain": False}
    try:
        try:
            proc, owner = _start(command, cwd, env)
        except BaseException as error:
            if lease is not None:
                resources.finish(resource_context, lease["id"], outcome="launch_failed",
                                 confirmed=isinstance(error, OSError) and not isinstance(error, _LaunchUncertain))
            if isinstance(error, OSError) and not isinstance(error, _LaunchUncertain):
                return _START_FAILED, 127, False, False, False
            raise
        # This identity names the retained Popen object in this controller and
        # this launch. It must never be reconstructed as permission to kill PID.
        handle = {"runtime": "local-process", "nativeId": f"popen:{os.getpid()}:{id(proc)}",
                  "launchId": uuid.uuid4().hex, "pid": proc.pid,
                  "containment": "windows-job" if os.name == "nt" else "posix-process-group"}
        try:
            if lease is not None:
                resources.bind(resource_context, lease["id"], handle)
                bound = True
        except BaseException:
            try:
                owner.close()
                proc.wait(timeout=5)
                cleanup["confirmed"] = True
            finally:
                proc.stdout.close()
            raise
        return _capture(proc, owner, timeout, max_output=max_output,
                        secret_pattern=secret_pattern, secrets=secrets, cleanup=cleanup,
                        heartbeat=(lambda: resources.heartbeat(resource_context, lease["id"], handle=handle))
                        if lease is not None else None)
    finally:
        if lease is not None and handle is not None:
            resources.finish(resource_context, lease["id"], handle=handle if bound else None,
                             outcome="completed" if bound else "launch_failed",
                             confirmed=cleanup["confirmed"] and not cleanup["uncertain"])


def _capture(proc, owner, timeout, *, max_output, secret_pattern, secrets, cleanup, heartbeat=None):
    """Capture while retaining both the exact process and containment owner."""
    stopped, reader = False, None
    try:
        captured, tail = bytearray(), b""
        tail_size = max([4096] + [len(s.encode("utf-8")) for s in secrets])
        truncated = timed_out = redacted = eof = False
        deadline = time.monotonic() + timeout
        heartbeat_at = time.monotonic() + 30
        drain_deadline = None
        reader = _PipeReader(proc.stdout)
        while not eof or proc.poll() is None:
            now, rc = time.monotonic(), proc.poll()
            if heartbeat is not None and now >= heartbeat_at:
                heartbeat()
                heartbeat_at = now + 30
            if not stopped and (rc is not None or now >= deadline):
                timed_out = rc is None
                owner.close()
                stopped, drain_deadline = True, time.monotonic() + _DRAIN_TIMEOUT
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                # Unread bytes must never turn into a successful verification.
                truncated, redacted = True, True
                cleanup["uncertain"] = True
                break
            if eof:
                time.sleep(_POLL)
                continue
            chunk = reader.read()
            if chunk is None:
                continue
            if not chunk:
                eof = True
                continue
            combined = tail + chunk
            text = combined.decode("utf-8", "replace")
            redacted = redacted or bool(secret_pattern.search(text)) or any(s in text for s in secrets)
            tail = combined[-tail_size:]
            keep = min(len(chunk), max_output - len(captured))
            captured.extend(chunk[:keep])
            truncated = truncated or keep < len(chunk)
        rc = proc.wait(timeout=5)
        if timed_out:
            captured.extend(b"\nAISpa: timeout; process tree terminated.\n")
            rc = 124
        if truncated:
            captured.extend(("\nAISpa: output truncated at %d bytes.\n" % max_output).encode())
        return bytes(captured), rc, timed_out, truncated, redacted
    except BaseException:
        cleanup["uncertain"] = True
        raise
    finally:
        try:
            if not stopped:
                owner.close()
            proc.wait(timeout=5)
            cleanup["confirmed"] = not cleanup["uncertain"]
        finally:
            if reader is not None:
                reader.close()
            proc.stdout.close()
