"""Mandatory host UI requests, never evidence that a browser was opened.

The CLI can start a verified loopback service. Only the native host can open a
visible embedded browser and check the rendered page; these actions stay pending
in its returned instructions. This module neither launches a process nor opens UI.
"""
from .store import Invalid


def _resource_contract():
    return {"kind": "browsers", "scope": "project", "identityPurpose": "browser",
            "prerequisites": ["real-native-session", "allowed-browser-capability"],
            "identityAction": {"cliArguments": ["host-context", "--purpose", "browser", "--file", "<host-evidence.json>"],
                               "instruction": "Adopt the actual source-bound native runtime/session and browser capability before reserving. Other unknown capabilities remain unknown."},
            "steps": ["reserve", "launch", "bind", "finish"],
            "instruction": "The native host reserves browsers before opening the real surface, binds its exact native handle, and confirms completion only after verified close. A failed or uncertain close retains the lease. Reuse an already owned live surface with its existing lease. These instructions do not open a browser."}


def start_request(*, managed=False):
    return [{"action": "start-and-open-dashboard", "runner": "pinned-aispa-cli",
             "cliArguments": ["dashboard", "start"], "surface": "embedded",
             "visible": True, "verification": "required",
             **({"resourceContract": _resource_contract()} if managed else {})}]


def open_request(service, *, managed=False):
    """Carry only the exact public URL from a successfully verified service."""
    port = service.get("port")
    url = service.get("url")
    if (service.get("running") is not True or service.get("unverified", False) is not False
            or type(port) is not int or not 0 < port < 65536
            or url != f"http://127.0.0.1:{port}/dashboard.html"):
        raise Invalid("Dashboard: нет подтверждённого loopback URL для открытия в среде агента")
    return [{"action": "open-dashboard", "url": url, "surface": "embedded",
             "visible": True, "verification": "required",
             **({"resourceContract": _resource_contract()} if managed else {})}]
