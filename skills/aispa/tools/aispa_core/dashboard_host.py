"""Mandatory host UI requests, never evidence that a browser was opened.

The CLI can start a verified loopback service. Only the native host can open a
visible embedded browser and check the rendered page; these actions stay pending
in its returned instructions. This module neither launches a process nor opens UI.
"""
from .store import Invalid


def start_request():
    return [{"action": "start-and-open-dashboard", "runner": "pinned-aispa-cli",
             "cliArguments": ["dashboard", "start"], "surface": "embedded",
             "visible": True, "verification": "required"}]


def open_request(service):
    """Carry only the exact public URL from a successfully verified service."""
    port = service.get("port")
    url = service.get("url")
    if (service.get("running") is not True or service.get("unverified", False) is not False
            or type(port) is not int or not 0 < port < 65536
            or url != f"http://127.0.0.1:{port}/dashboard.html"):
        raise Invalid("Dashboard: нет подтверждённого loopback URL для открытия в среде агента")
    return [{"action": "open-dashboard", "url": url, "surface": "embedded",
             "visible": True, "verification": "required"}]
