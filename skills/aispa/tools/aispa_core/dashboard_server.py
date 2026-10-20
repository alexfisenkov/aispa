"""Loopback-only AISpa dashboard and exact-instance service lifecycle."""
from contextlib import contextmanager
import errno
import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
from socketserver import TCPServer
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .display import project
from .portable import is_link
from .store import Invalid, Journal, atomic_json


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
    return value


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


def _verified(store, meta):
    response, _reason = _private_request(meta, "GET", "_control/health")
    return bool(isinstance(response, dict) and response.get("marker") == MARKER
                and response.get("rootHash") == _root_hash(store)
                and response.get("instance") == meta["instance"]
                and response.get("pid") == meta["pid"])


def _port_closed(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(.3)
        return sock.connect_ex((HOST, port)) == errno.ECONNREFUSED


def inspect(store):
    """Read service identity through authenticated loopback health, not PID."""
    meta = _read_meta(store)
    if meta is None:
        return {"running": False, "url": None, "port": _last_port(store), "instance": None, "pid": None}
    running = _verified(store, meta)
    return {"running": running, "url": _url(meta["port"]) if running else None,
            "port": meta["port"], "instance": meta["instance"], "pid": meta["pid"],
            "unverified": not running}


status = inspect


def _remove_matching(store, meta):
    current = _read_meta(store)
    if current and current["instance"] == meta["instance"] and current["token"] == meta["token"]:
        _path(store, META_NAME).unlink(missing_ok=True)
        return True
    return False


def stop(store):
    """Ask the exact authenticated service to stop; never signal a numeric PID."""
    with _lifecycle_lock(store):
        meta = _read_meta(store)
        if meta is None:
            return {"running": False, "stopped": False, "port": _last_port(store)}
        if not _verified(store, meta):
            cleaned = _remove_matching(store, meta) if _port_closed(meta["port"]) else False
            return {"running": False, "stopped": False, "staleMetadataRemoved": cleaned,
                    "port": meta["port"], "instance": meta["instance"]}
        response, _reason = _private_request(meta, "POST", "_control/shutdown", timeout=2)
        if response is None:
            return {"running": True, "stopped": False, "port": meta["port"], "instance": meta["instance"]}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _verified(store, meta):
            time.sleep(.05)
        if _verified(store, meta):
            return {"running": True, "stopped": False, "port": meta["port"], "instance": meta["instance"]}
        child = _CHILDREN.pop(meta["instance"], None)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        _remove_matching(store, meta)
        return {"running": False, "stopped": True, "port": meta["port"], "instance": meta["instance"]}


def _spawn(store, port):
    instance = secrets.token_hex(16)
    token = secrets.token_urlsafe(48)
    config = {"root": str(store.root), "port": port, "instance": instance, "token": token}
    child = subprocess.Popen([sys.executable, "-B", "-m", "aispa_core.dashboard_server"],
                             cwd=str(TOOLS_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             start_new_session=os.name != "nt",
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)
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
                        and response.get("instance") == instance and response.get("pid") == child.pid):
                    _write_last_port(store, meta["port"])
                    child.stdout.close()  # one-shot diagnostic channel, never held by a live daemon
                    _CHILDREN[instance] = child
                    return inspect(store)
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


def start(store, port=0):
    """Start at most one managed dashboard for this project and reuse its port."""
    if type(port) is not int or not 0 <= port < 65536:
        raise Invalid("Порт dashboard должен быть от 0 до 65535")
    store.load()  # a dashboard belongs to an initialized project
    with _lifecycle_lock(store):
        live = inspect(store)
        if live["running"]:
            return live
        requested = port or live["port"] or 0
        try:
            return _spawn(store, requested)
        except Invalid:
            if port != 0 or requested == 0 or _port_closed(requested):
                raise
            # The previous port may now be owned by an unrelated service.
            return _spawn(store, 0)


def _asset_bytes(store):
    installed = store.home / "dashboard.html"
    if is_link(installed):
        raise Invalid("Dashboard asset не должен быть ссылкой")
    selected = installed if installed.is_file() else ASSET
    return selected.read_bytes()


def serve(store, *, port=0, instance=None, token=None, managed=False, announce=True):
    """Run the exact-path HTTP server until authenticated shutdown or signal."""
    if type(port) is not int or not 0 <= port < 65536:
        raise Invalid("Порт dashboard должен быть от 0 до 65535")
    store.load()
    asset = _asset_bytes(store)
    instance = instance or secrets.token_hex(16)
    token = token or secrets.token_urlsafe(48)
    root_hash = _root_hash(store)

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
                self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                self._send(421)
                return
            route = urlsplit(self.path)
            if route.query or route.fragment:
                self._send(404)
            elif route.path == "/dashboard.html":
                self._send(200, asset, "text/html; charset=utf-8")
            elif route.path == "/status.json":
                try:
                    reader = Journal(store.root)
                    payload = json.dumps(project(reader, reader.load()), ensure_ascii=False).encode("utf-8")
                except (Invalid, OSError, ValueError, KeyError, TypeError):
                    self._send(409)
                    return
                self._send(200, payload, "application/json; charset=utf-8")
            elif route.path == "/_control/health" and self._authorized():
                payload = json.dumps({"marker": MARKER, "rootHash": root_hash,
                                      "instance": instance, "pid": os.getpid()}).encode("utf-8")
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
        if managed:
            metadata = {"marker": MARKER, "rootHash": root_hash, "instance": instance,
                        "token": token, "pid": os.getpid(), "port": server.server_port}
            atomic_json(_path(store, META_NAME, create=True), metadata)
        if announce:
            print(_url(server.server_port), flush=True)
        server.serve_forever(poll_interval=.05)
    return 0


def child_main():
    stage = "config"
    try:
        config = json.loads(sys.stdin.buffer.readline(8192))
        if not isinstance(config, dict) or not all(isinstance(config.get(key), str) and config[key]
                                                   for key in ("root", "instance", "token")):
            raise Invalid("Неверный запуск dashboard child")
        stage = "serve"
        serve(Journal(config["root"]), port=config["port"], instance=config["instance"],
              token=config["token"], managed=True, announce=False)
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        detail = "os-" + str(exc.errno) if isinstance(exc, OSError) else type(exc).__name__
        print("AISPA_DASHBOARD_START_ERROR:" + stage + "/" + detail, file=sys.stdout)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(child_main())
