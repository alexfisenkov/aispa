"""Loopback-only AISpa dashboard and exact-instance service lifecycle."""
from contextlib import contextmanager
import errno
import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import socket
from socketserver import TCPServer
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from . import VERSION, resources
from .display import project
from .dashboard_reader import DisplayJournal, SnapshotJournal
from .dashboard_snapshot import SnapshotCoordinator, calculate
from .portable import is_link
from .store import Invalid, Journal, atomic_json, digest, file_hash


MARKER = "AISpa-dashboard-service-v1"
SERVICE_DIR = "service"
META_NAME = "dashboard.json"
PORT_NAME = "last-port.json"
HOST = "127.0.0.1"
TOKEN_HEADER = "X-AISpa-Service-Token"
START_TIMEOUT = 6.0
TOOLS_DIR = Path(__file__).resolve().parents[1]
ASSET = Path(__file__).resolve().parents[2] / "assets" / "dashboard.html"
_CHILDREN = {}  # exact Popen handles in this interpreter; never discovered PIDs


class LoopbackServer(ThreadingHTTPServer):
    """Bind the known numeric loopback address without reverse DNS."""

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]

    def service_actions(self):
        binding = getattr(self, "resource_lease", None)
        if binding is not None and time.monotonic() >= self.resource_heartbeat_at:
            resources.heartbeat(binding["context"], binding["leaseId"], handle=binding["handle"])
            self.resource_heartbeat_at = time.monotonic() + 30


def _root_hash(store):
    return hashlib.sha256(str(store.root).encode("utf-8")).hexdigest()


def _service_dir(store, *, create=False):
    home = store.home / SERVICE_DIR
    if is_link(home):
        raise Invalid("Служебный каталог dashboard не должен быть ссылкой")
    if create:
        home.mkdir(parents=True, exist_ok=True)
    return home


def _path(store, name, *, create=False):
    path = _service_dir(store, create=create) / name
    if is_link(path):
        raise Invalid("Служебный файл dashboard не должен быть ссылкой")
    return path


@contextmanager
def _lifecycle_lock(store):
    lock = _path(store, ".lock", create=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Invalid("Dashboard lifecycle уже выполняется") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def _read_meta(store):
    path = _path(store, META_NAME)
    if not path.exists():
        return None
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise Invalid("Dashboard metadata должен быть доступен только владельцу")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Invalid("Повреждены данные dashboard service") from exc
    if (not isinstance(value, dict) or value.get("marker") != MARKER
            or value.get("rootHash") != _root_hash(store)
            or not isinstance(value.get("instance"), str) or not value["instance"]
            or not isinstance(value.get("token"), str) or len(value["token"]) < 32
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or type(value.get("port")) is not int or not 0 < value["port"] < 65536):
        raise Invalid("Dashboard metadata не соответствует этому проекту")
    _lease_binding(store, value)
    return value


def _lease_binding(store, meta):
    """Private persisted ownership, independent of a later native host session."""
    binding = meta.get("resourceLease")
    if binding is None:
        return None
    if not isinstance(binding, dict) or not isinstance(binding.get("context"), dict) or not isinstance(binding.get("handle"), dict):
        raise Invalid("Dashboard: повреждена service lease identity")
    ctx, handle = binding["context"], binding["handle"]
    if (ctx.get("root") != str(store.root) or ctx.get("sessionKind") != "local-service"
            or ctx.get("serviceInstance") != meta["instance"] or ctx.get("owner") != "dashboard-service"
            or ctx.get("session") != "local-service:" + meta["instance"]
            or not isinstance(binding.get("leaseId"), str) or not binding["leaseId"]
            or handle.get("runtime") != "local-service" or handle.get("launchId") != meta["instance"]
            or handle.get("pid") != meta["pid"]):
        raise Invalid("Dashboard: service lease принадлежит другому экземпляру")
    return binding


def _service_finish(store, meta, *, confirmed):
    binding = _lease_binding(store, meta)
    if binding is None:
        return
    # A daemon can attest its own closed server before the parent observes exit.
    # Do not turn that exact terminal result back into an unconfirmed orphan.
    if not confirmed and _service_completed(store, meta):
        return
    resources.finish(binding["context"], binding["leaseId"], handle=binding["handle"], confirmed=confirmed)


def _service_completed(store, meta):
    binding = _lease_binding(store, meta)
    if binding is None:
        return False
    row = resources.view(binding["context"])["leases"].get(binding["leaseId"])
    return (row is not None and row.get("status") == "completed" and row.get("confirmed") is True
            and row.get("handle") == binding["handle"]
            and all(row.get(key) == binding["context"].get(key) for key in ("root", "run", "session", "owner")))


def _reuse_service(store, meta):
    binding = _lease_binding(store, meta)
    if binding is None:
        return False
    # Authenticated current health is an explicit observation of this instance.
    resources.reconcile(binding["context"], [{"leaseId": binding["leaseId"],
                        "handle": binding["handle"], "status": "running"}])
    return True


def _last_port(store):
    path = _path(store, PORT_NAME)
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return row.get("port") if (isinstance(row, dict) and row.get("marker") == MARKER
            and row.get("rootHash") == _root_hash(store)
            and type(row.get("port")) is int and 0 < row["port"] < 65536) else None


def _write_last_port(store, port):
    atomic_json(_path(store, PORT_NAME, create=True),
                {"marker": MARKER, "rootHash": _root_hash(store), "port": port})


def _url(port, suffix="dashboard.html"):
    return f"http://{HOST}:{port}/{suffix}"


def _private_request(meta, method, endpoint, timeout=.5):
    """Direct loopback control request, independent of process proxy settings."""
    connection = HTTPConnection(HOST, meta["port"], timeout=timeout)
    try:
        connection.request(method, "/" + endpoint,
                           headers={"Host": f"{HOST}:{meta['port']}", TOKEN_HEADER: meta["token"]})
        response = connection.getresponse()
        if response.status not in (200, 204):
            return None, "http-" + str(response.status)
        return (json.loads(response.read()) if response.status == 200 else {}), "ok"
    except (OSError, HTTPException, ValueError) as exc:
        return None, type(exc).__name__
    finally:
        connection.close()


def _health(store, meta):
    response, _reason = _private_request(meta, "GET", "_control/health")
    if (isinstance(response, dict) and response.get("marker") == MARKER
            and response.get("rootHash") == _root_hash(store)
            and response.get("instance") == meta["instance"]
            and response.get("pid") == meta["pid"]):
        return response
    return None


def _verified(store, meta):
    """Ownership is enough to stop our old service, never enough to reuse it."""
    return _health(store, meta) is not None


def _runtime_digest():
    package = TOOLS_DIR.parent
    return digest({path.relative_to(package).as_posix(): file_hash(path)
                   for path in sorted(package.rglob("*")) if path.is_file()
                   and "__pycache__" not in path.parts and path.suffix != ".pyc"})


def _context(state, asset, *, compatible_view=False):
    """Capture the reader package and actual asset, not a later active-run claim."""
    return {"run": state["run"], "version": state["version"], "dashboardVersion": VERSION,
            "runtimeDigest": _runtime_digest(), "pinnedRuntimeDigest": state.get("runtimeDigest"),
            "readerMode": "compatible-view" if compatible_view else "pinned",
            "assetSha256": hashlib.sha256(asset).hexdigest()}


def _expected_context(store, *, compatible_view=False, asset=None):
    reader = DisplayJournal(store.root) if compatible_view else store
    state = reader.load()
    context = _context(state, _asset_bytes(store) if asset is None else asset, compatible_view=compatible_view)
    if not compatible_view and state.get("runtimeDigest") and state["runtimeDigest"] != context["runtimeDigest"]:
        raise Invalid("Dashboard требует закреплённый runtime текущего прогона")
    return context


def _context_matches(health, expected):
    # Missing fields in a legacy health reply require an authenticated restart.
    return health is not None and all(health.get(key) == value for key, value in expected.items())


def _public_info(meta, health, expected):
    current = _context_matches(health, expected) if expected is not None else False
    return {"running": current, "url": _url(meta["port"]) if current else None,
            "port": meta["port"], "instance": meta["instance"], "pid": meta["pid"],
            "unverified": health is None, "serviceRunning": health is not None,
            "contextCurrent": current,
            **({"context": {key: health[key] for key in ("run", "version", "dashboardVersion", "runtimeDigest",
                                                        "pinnedRuntimeDigest", "readerMode", "assetSha256")
                            if key in health}} if health is not None else {})}


def _port_closed(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(.3)
        return sock.connect_ex((HOST, port)) == errno.ECONNREFUSED


def inspect(store):
    """Read service identity through authenticated loopback health, not PID."""
    meta = _read_meta(store)
    if meta is None:
        return {"running": False, "url": None, "port": _last_port(store), "instance": None, "pid": None}
    health = _health(store, meta)
    try:
        expected = _expected_context(store, compatible_view=health is not None and health.get("readerMode") == "compatible-view")
    except (Invalid, OSError, ValueError, KeyError, TypeError):
        expected = None
    return _public_info(meta, health, expected)


status = inspect


def _remove_matching(store, meta):
    current = _read_meta(store)
    if current and current["instance"] == meta["instance"] and current["token"] == meta["token"]:
        _path(store, META_NAME).unlink(missing_ok=True)
        return True
    return False


def _stop_locked(store, meta):
    if meta is None:
        return {"running": False, "stopped": False, "port": _last_port(store)}
    if not _verified(store, meta):
        child = _CHILDREN.get(meta["instance"])
        _service_finish(store, meta, confirmed=child is not None and child.poll() is not None)
        cleaned = _remove_matching(store, meta) if _port_closed(meta["port"]) else False
        return {"running": False, "stopped": False, "staleMetadataRemoved": cleaned,
                "port": meta["port"], "instance": meta["instance"]}
    response, _reason = _private_request(meta, "POST", "_control/shutdown", timeout=2)
    if response is None:
        _service_finish(store, meta, confirmed=False)
        return {"running": True, "stopped": False, "port": meta["port"], "instance": meta["instance"]}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _verified(store, meta):
        time.sleep(.05)
    if _verified(store, meta):
        _service_finish(store, meta, confirmed=False)
        return {"running": True, "stopped": False, "port": meta["port"], "instance": meta["instance"]}
    child = _CHILDREN.get(meta["instance"])
    exited = False
    if child is not None:
        try:
            child.wait(timeout=2)
            exited = True
            _CHILDREN.pop(meta["instance"], None)
        except subprocess.TimeoutExpired:
            pass
    # Across separate CLI processes, only the daemon's exact lease completion
    # attests closure; loss of health or a closed port alone does not prove exit.
    if _lease_binding(store, meta) is not None and not exited:
        deadline = time.monotonic() + 2
        while not _service_completed(store, meta) and time.monotonic() < deadline:
            time.sleep(.02)
        if not _service_completed(store, meta):
            _service_finish(store, meta, confirmed=False)
            return {"running": False, "stopped": False, "port": meta["port"], "instance": meta["instance"]}
    _service_finish(store, meta, confirmed=True)
    _remove_matching(store, meta)
    return {"running": False, "stopped": True, "port": meta["port"], "instance": meta["instance"]}


def stop(store):
    """Ask the exact authenticated service to stop; never signal a numeric PID."""
    with _lifecycle_lock(store):
        return _stop_locked(store, _read_meta(store))


def _spawn(store, port, expected):
    instance = secrets.token_hex(16)
    state = (DisplayJournal(store.root) if expected["readerMode"] == "compatible-view" else store).load()
    ctx = resources.context(store, state, owner="dashboard-service", service_instance=instance)
    lease = resources.reserve(ctx, "services") if ctx is not None else None
    token = secrets.token_urlsafe(48)
    config = {"root": str(store.root), "port": port, "instance": instance, "token": token,
              "compatibleView": expected["readerMode"] == "compatible-view"}
    try:
        child = subprocess.Popen([sys.executable, "-B", "-m", "aispa_core.dashboard_server"],
                             cwd=str(TOOLS_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             start_new_session=os.name != "nt",
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)
    except OSError:
        if lease is not None:
            resources.finish(ctx, lease["id"], outcome="launch_failed", confirmed=True)
        raise
    binding, confirmed, bound = None, False, False
    if lease is not None:
        handle = {"runtime": "local-service", "nativeId": f"popen:{os.getpid()}:{id(child)}",
                  "launchId": instance, "pid": child.pid}
        binding = {"context": ctx, "leaseId": lease["id"], "handle": handle}
    try:
        if binding is not None:
            resources.bind(ctx, lease["id"], binding["handle"])
            bound = True
            config["resourceLease"] = binding
        return _start_child(store, child, config, expected)
    finally:
        try:
            if child.poll() is None and instance not in _CHILDREN:
                try:
                    child.terminate()  # only the retained Popen handle
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            confirmed = child.poll() is not None
        finally:
            if instance not in _CHILDREN:
                try:
                    if child.stdin is not None:
                        child.stdin.close()
                    if child.stdout is not None:
                        child.stdout.close()
                finally:
                    if lease is not None:
                        resources.finish(ctx, lease["id"], handle=binding["handle"] if bound else None,
                                         outcome="completed" if bound else "launch_failed", confirmed=confirmed)


def _start_child(store, child, config, expected):
    instance = config["instance"]
    try:
        child.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode("utf-8"))
        child.stdin.close()
    except OSError:
        child.terminate()  # exact Popen handle, never a discovered PID
        child.wait(timeout=2)
        child.stdout.close()
        raise
    deadline = time.monotonic() + START_TIMEOUT
    phase = "metadata-absent"
    while time.monotonic() < deadline:
        meta = _read_meta(store)
        if meta:
            if meta["instance"] == instance and meta["pid"] == child.pid:
                response, reason = _private_request(meta, "GET", "_control/health")
                if (isinstance(response, dict) and response.get("marker") == MARKER
                        and response.get("rootHash") == _root_hash(store)
                        and response.get("instance") == instance and response.get("pid") == child.pid
                        and _context_matches(response, expected)):
                    _write_last_port(store, meta["port"])
                    child.stdout.close()  # one-shot diagnostic channel, never held by a live daemon
                    _CHILDREN[instance] = child
                    return _public_info(meta, response, expected)
                phase = "health-" + ("identity-mismatch" if reason == "ok" else reason)
            else:
                phase = "metadata-other-instance"
        if child.poll() is not None:
            break
        time.sleep(.05)
    if child.poll() is None:
        child.terminate()  # spawned child only
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()  # exact Popen handle; no numeric PID lookup
            child.wait(timeout=2)
    child_state = "exit-" + str(child.returncode)
    if child.stdout is not None:
        child_error = child.stdout.read(4096).decode("utf-8", "replace")
        child.stdout.close()
        marker = next((line for line in child_error.splitlines()
                       if line.startswith("AISPA_DASHBOARD_START_ERROR:")), None)
        if marker:
            child_state += "/" + marker.split(":", 1)[1][:80]
    meta = _read_meta(store)
    if meta and meta["instance"] == instance and _port_closed(meta["port"]):
        _remove_matching(store, meta)
    raise Invalid("Dashboard не подтвердил запуск на loopback (" + phase + ", " + child_state + ")")


def _refresh_asset(store, asset):
    """Replace only the dashboard HTML, retaining the exact prior bytes privately."""
    target = store.home / "dashboard.html"
    if is_link(target) or target.exists() and not target.is_file():
        raise Invalid("Dashboard asset должен быть обычным файлом")
    before = target.read_bytes() if target.exists() else None
    if before == asset:
        return
    if before is not None:
        folder = _service_dir(store, create=True) / "asset-backups"
        if is_link(folder):
            raise Invalid("Каталог копий dashboard не должен быть ссылкой")
        folder.mkdir(exist_ok=True)
        backup = folder / (hashlib.sha256(before).hexdigest() + ".html")
        if is_link(backup):
            raise Invalid("Копия dashboard не должна быть ссылкой")
        if backup.exists():
            if not backup.is_file() or backup.read_bytes() != before:
                raise Invalid("Конфликт резервной копии dashboard")
        else:
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
    descriptor, name = tempfile.mkstemp(prefix=".aispa-dashboard-", dir=store.home)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(asset)
            stream.flush()
            os.fsync(stream.fileno())
        if (is_link(target) or target.exists() and not target.is_file()
                or (target.read_bytes() if target.exists() else None) != before):
            raise Invalid("Dashboard asset изменился во время обновления")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def start(store, port=0, *, refresh_view=False):
    """Start at most one managed dashboard for this project and reuse its port."""
    if type(port) is not int or not 0 <= port < 65536:
        raise Invalid("Порт dashboard должен быть от 0 до 65535")
    if type(refresh_view) is not bool:
        raise Invalid("Неверный режим просмотра dashboard")
    asset = ASSET.read_bytes() if refresh_view else None
    expected = _expected_context(store, compatible_view=refresh_view, asset=asset)
    if refresh_view:
        reader = DisplayJournal(store.root)
        project(reader, reader.load())  # Validate projection before any asset/service mutation.
    with _lifecycle_lock(store):
        if refresh_view:
            _refresh_asset(store, asset)
        meta = _read_meta(store)
        health = _health(store, meta) if meta is not None else None
        state = (DisplayJournal(store.root) if refresh_view else store).load()
        managed = resources.enabled(state)
        if _context_matches(health, expected) and (not managed or _reuse_service(store, meta)):
            return _public_info(meta, health, expected)
        if health is not None and not _stop_locked(store, meta)["stopped"]:
            raise Invalid("Старый dashboard не подтвердил остановку; новый экземпляр не запущен")
        requested = port or (meta["port"] if meta else _last_port(store)) or 0
        try:
            return _spawn(store, requested, expected)
        except resources.CapacityRefused:
            raise
        except Invalid:
            if port != 0 or requested == 0 or _port_closed(requested):
                raise
            # The previous port may now be owned by an unrelated service.
            return _spawn(store, 0, expected)


def _asset_bytes(store):
    installed = store.home / "dashboard.html"
    if is_link(installed):
        raise Invalid("Dashboard asset не должен быть ссылкой")
    selected = installed if installed.is_file() else ASSET
    return selected.read_bytes()


def serve(store, *, port=0, instance=None, token=None, managed=False, announce=True, compatible_view=False,
          resource_lease=None):
    """Run the exact-path HTTP server until authenticated shutdown or signal."""
    if type(port) is not int or not 0 <= port < 65536:
        raise Invalid("Порт dashboard должен быть от 0 до 65535")
    reader_class = DisplayJournal if compatible_view else SnapshotJournal
    initial_state = reader_class(store.root).load()
    asset = _asset_bytes(store)
    loaded = _context(initial_state, asset, compatible_view=compatible_view)
    if not compatible_view and initial_state.get("runtimeDigest") and initial_state["runtimeDigest"] != loaded["runtimeDigest"]:
        raise Invalid("Dashboard требует закреплённый runtime текущего прогона")
    pinned_digest = initial_state.get("runtimeDigest")
    instance = instance or secrets.token_hex(16)
    token = token or secrets.token_urlsafe(48)
    root_hash = _root_hash(store)
    snapshots = SnapshotCoordinator(lambda: calculate(
        store.root, reader_class, loaded, pinned_digest, lambda: _asset_bytes(store), project))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return  # no token, path or private data in server logs

        def _host_ok(self):
            return self.headers.get("Host") == f"{HOST}:{self.server.server_port}"

        def _authorized(self):
            return secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), token)

        def _send(self, code, data=b"", content_type="application/octet-stream"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A disconnected client does not own or cancel the job.

        def _status_error(self, code):
            # Fixed codes only: exceptions may contain local paths or source text.
            self._send(409, json.dumps({"error": {"code": code}}).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                self._send(421)
                return
            route = urlsplit(self.path)
            generation = re.fullmatch(r"calculation=([0-9a-f]{32})", route.query) if route.query else None
            if route.fragment or route.query and not (route.path == "/status.json" and generation):
                self._send(404)
            elif route.path == "/dashboard.html":
                self._send(200, asset, "text/html; charset=utf-8")
            elif route.path == "/status.json":
                code, payload = snapshots.get(generation.group(1) if generation else None)
                self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif route.path == "/_control/health" and self._authorized():
                payload = json.dumps({"marker": MARKER, "rootHash": root_hash,
                                      "instance": instance, "pid": os.getpid(), **loaded}).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
            else:
                self._send(404)

        def do_POST(self):
            if not self._host_ok():
                self._send(421)
                return
            if self.path == "/_control/shutdown" and self._authorized():
                self._send(204)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404)

    with LoopbackServer((HOST, port), Handler) as server:
        server.resource_lease = resource_lease
        server.resource_heartbeat_at = time.monotonic() + 30
        if managed:
            metadata = {"marker": MARKER, "rootHash": root_hash, "instance": instance,
                        "token": token, "pid": os.getpid(), "port": server.server_port}
            if resource_lease is not None:
                metadata["resourceLease"] = resource_lease
                _lease_binding(store, metadata)
            atomic_json(_path(store, META_NAME, create=True), metadata)
        if announce:
            print(_url(server.server_port), flush=True)
        server.serve_forever(poll_interval=.05)
    if resource_lease is not None:
        resources.finish(resource_lease["context"], resource_lease["leaseId"],
                         handle=resource_lease["handle"], confirmed=True)
    return 0


def child_main():
    stage = "config"
    try:
        config = json.loads(sys.stdin.buffer.readline(8192))
        if not isinstance(config, dict) or not all(isinstance(config.get(key), str) and config[key]
                                                   for key in ("root", "instance", "token")):
            raise Invalid("Неверный запуск dashboard child")
        if type(config.get("compatibleView", False)) is not bool:
            raise Invalid("Неверный режим dashboard child")
        stage = "serve"
        serve(Journal(config["root"]), port=config["port"], instance=config["instance"],
              token=config["token"], managed=True, announce=False,
              compatible_view=config.get("compatibleView", False), resource_lease=config.get("resourceLease"))
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        detail = "os-" + str(exc.errno) if isinstance(exc, OSError) else type(exc).__name__
        print("AISPA_DASHBOARD_START_ERROR:" + stage + "/" + detail, file=sys.stdout)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(child_main())
