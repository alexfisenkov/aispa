"""Private standalone historical snapshot; never a claim about current code."""
import json
import re
from pathlib import Path

from .contracts import require
from .display import project
from .store import timestamp


def archive(store, state):
    template = Path(__file__).resolve().parents[2] / "assets" / "dashboard.html"
    html = template.read_text(encoding="utf-8")
    # Offline archives use the declared system-font fallbacks, without even
    # optional font/preconnect requests to external providers.
    html = re.sub(r'<link\b[^>]*href="https://fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*',
                  "", html)
    marker = '<script id="aispa-app">'
    require(html.count(marker) == 1, "Неизвестная версия шаблона панели")
    value = {**project(store, state), "archive": True, "archivedAt": timestamp()}
    # No HTML/script termination even when an original brief contains markup.
    data = json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = html.replace(marker, "<script>window.AISPA_ARCHIVE=" + data + ";</script>\n" + marker)
    ref = store.artifact("dashboard-archives", html.encode(), ".html")
    return {**ref, "absolutePath": str(store.run / ref["file"]), "archive": True,
            "archivedAt": value["archivedAt"], "private": True}
