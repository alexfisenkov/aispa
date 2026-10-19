import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "skills/aispa/tools/aispa.py"


class WindowsCliEncodingTests(unittest.TestCase):
    def test_cli_utf8_with_legacy_pipe_encoding_and_bom_inputs(self):
        with tempfile.TemporaryDirectory(prefix="aispa cli Кириллица 🙂-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / "app.py").write_bytes(b"print('ready')\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True, capture_output=True)
            environment = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
            def call(*args):
                result = subprocess.run([sys.executable, "-B", str(CLI), "--root", str(root), *args],
                                        env=environment, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                return json.loads(result.stdout.decode("utf-8"))
            state = call("init", "--slug", "unicode", "--title", "Русский проект 🙂", "--mode", "auto", "--runtime", "codex")
            self.assertEqual(state["title"], "Русский проект 🙂")
            inbox = root / ".aispa/inbox"; inbox.mkdir()
            brief = inbox / "brief.md"
            brief.write_text("Покажи приветствие.", encoding="utf-8-sig", newline="\n")
            call("brief", "--file", str(brief))
            requirements = inbox / "requirements.json"
            requirements.write_text(json.dumps([{"id": "R01", "text": "Приветствие", "brief": "B001",
                "quote": "Покажи приветствие.", "core": True, "acceptance": ["Виден русский текст"]}], ensure_ascii=False), encoding="utf-8-sig", newline="\n")
            call("requirements", "--file", str(requirements))
            self.assertEqual(call("status")["requirements"][0]["text"], "Приветствие")
            self.assertIn("Покажи приветствие.", (root / ".aispa/context/brief.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
