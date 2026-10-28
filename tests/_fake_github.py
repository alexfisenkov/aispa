"""Запускает tools/aispa.py пакета с подменённым GitHub — для тестов, без сети.

    python3 tests/_fake_github.py <fixture.json> <package>/tools/aispa.py <аргументы…>

fixture.json: {"releases": [строки /releases], "files": {url: путь к файлу},
"commit": "<sha тега>"} или {"offline": true}. Подменяются три сетевые функции
обновителя; всё остальное — настоящий код пакета.
"""
import json
from pathlib import Path
import runpy
import sys
import urllib.error

fixture = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tool = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(tool.parent))
from aispa_core import updates  # noqa: E402


def releases(_deadline):
    if fixture.get("offline"):
        raise urllib.error.URLError("нет сети (тест)")
    return fixture["releases"]


def download(url, limit, _deadline):
    data = Path(fixture["files"][url]).read_bytes()
    if len(data) > limit:
        raise updates.Invalid("Ресурс превышает допустимый размер")
    return data


updates._published_releases = releases
updates._download = download
updates._source_commit = lambda _tag, _deadline: fixture["commit"]
sys.argv = [str(tool)] + sys.argv[3:]
runpy.run_path(str(tool), run_name="__main__")
