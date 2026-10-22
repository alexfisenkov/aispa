#!/usr/bin/env python3
"""Local, read-only before/after dashboard preview; no project runtime is started."""
import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

ASSET = "skills/aispa/assets/dashboard.html"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AISpa — было / стало</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f4f6;color:#171719;font:14px system-ui,sans-serif}
header{padding:14px 20px;background:#fff;border-bottom:1px solid #dcdde1;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0 0 3px}p{margin:0;color:#65666b;font-size:12px}
nav{display:flex;gap:4px;border-radius:12px;padding:4px;background:#eeeef1}
button,select{font:inherit;border:1px solid transparent;border-radius:8px;padding:9px 12px;color:inherit;background:#fff;min-height:40px}
button{cursor:pointer;background:transparent}button[aria-pressed=true]{background:#171719;color:white}
button:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid #3152d3;outline-offset:2px}
label{display:flex;gap:7px;align-items:center;font-size:12px}select{border-color:#ddd;max-width:230px}
main{display:grid;grid-template-columns:1fr;gap:1px;background:#ddd}main.side{grid-template-columns:1fr 1fr}
section{min-width:0;background:#f3f4f6}section[hidden]{display:none}
.caption{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:9px 18px;background:#f8f8fa;font-size:12px}
.caption b{font-size:14px}.caption a{color:#3d4e9b;text-underline-offset:3px}
iframe{display:block;width:100%;height:calc(100dvh - 150px);min-height:500px;border:0;background:#f3f4f6}
@media(max-width:700px){header{padding:12px;gap:10px}.caption{padding:8px 12px}main.side{grid-template-columns:1fr}iframe{height:75dvh}label{flex-wrap:wrap}}
</style></head><body>
<header><div><h1>AISpa · сравнение панели</h1><p>Макет в отдельной ветке. Демо-данные · без изменения проектов и установок.</p></div>
<nav aria-label="Вариант просмотра"><button type="button" data-view="before" aria-pressed="false">Было</button><button type="button" data-view="after" aria-pressed="true">Стало</button><button type="button" data-view="side" aria-pressed="false">Рядом</button></nav>
<label>Сценарий <select id="scenario"><option value="running">Работа, проверка и ремонт</option><option value="blocked">Есть препятствие</option><option value="paused">Пауза</option><option value="stale">Проверки устарели</option><option value="delivered_with_remaining">Передано с остатками</option><option value="accepted">Принято по брифу</option></select></label>
</header><main id="views">
<section id="before" hidden><div class="caption"><b>Было · 0.3.4</b><span>@@BASE@@</span><a id="beforeLink" href="/before/dashboard.html?demo" target="_blank" rel="noopener">Открыть отдельно</a></div><iframe id="beforeFrame" title="AISpa до изменений"></iframe></section>
<section id="after"><div class="caption"><b>Стало · рабочая ветка</b><span>Не релиз</span><a id="afterLink" href="/after/dashboard.html?demo" target="_blank" rel="noopener">Открыть отдельно</a></div><iframe id="afterFrame" title="AISpa после изменений"></iframe></section>
</main><script>
const byId=id=>document.getElementById(id);
function scene(){const status=byId('scenario').value;for(const name of ['before','after']){const url='/'+name+'/dashboard.html?demo&status='+encodeURIComponent(status);byId(name+'Frame').src=url;byId(name+'Link').href=url;}}
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{const view=button.dataset.view;for(const item of document.querySelectorAll('[data-view]'))item.setAttribute('aria-pressed',String(item===button));byId('views').className=view==='side'?'side':'';byId('before').hidden=view==='after';byId('after').hidden=view==='before';});
byId('scenario').addEventListener('change',scene);scene();
</script></body></html>"""


def make_server(root, before, port=0):
    root = Path(root).resolve()
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options", before + "^{commit}"],
        text=True, stderr=subprocess.PIPE, timeout=10,
    ).strip()
    baseline = subprocess.check_output(
        ["git", "-C", str(root), "show", commit + ":" + ASSET],
        stderr=subprocess.PIPE, timeout=10,
    )
    index = PAGE.replace("@@BASE@@", html.escape(commit[:12])).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/compare.html"):
                body = index
            elif path == "/before/dashboard.html":
                body = baseline
            elif path == "/after/dashboard.html":
                # Reload candidate changes without restarting the preview.
                body = (root / ASSET).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="Exact baseline commit or ref")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server, commit = make_server(Path(__file__).resolve().parents[1], args.before, args.port)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/compare.html",
                      "beforeCommit": commit, "mode": "demo-only"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
