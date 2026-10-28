import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "skills/aispa/tools/aispa.py"
sys.path.insert(0, str(CLI.parent))


class WindowsCliEncodingTests(unittest.TestCase):
    def test_cli_utf8_with_legacy_pipe_encoding_and_bom_inputs(self):
        with tempfile.TemporaryDirectory(prefix="aispa cli Кириллица 🙂-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / "app.py").write_bytes(b"print('ready')\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True, capture_output=True)
            environment = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
            cli = CLI
            def call(*args):
                result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(root), "--full", *args],
                                        env=environment, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                return json.loads(result.stdout.decode("utf-8"))
            state = call("init", "--slug", "unicode", "--title", "Русский проект 🙂", "--mode", "auto", "--runtime", "codex", "--check", "balanced")
            self.assertEqual(state["title"], "Русский проект 🙂")
            cli = root / ".aispa" / state["runDirectory"] / "runtime/tools/aispa.py"
            self.assertTrue(cli.is_file(), "The following UTF-8 operations use the pinned runtime")
            inbox = root / ".aispa/inbox"; inbox.mkdir()
            brief = inbox / "brief.md"
            brief.write_text("Покажи приветствие.", encoding="utf-8-sig", newline="\n")
            call("brief", "--file", str(brief))
            requirements = inbox / "requirements.json"
            requirements.write_text(json.dumps([{"id": "R01", "text": "Приветствие", "brief": "B001",
                "quote": "Покажи приветствие.", "core": True, "acceptance": ["Виден русский текст"]}], ensure_ascii=False), encoding="utf-8-sig", newline="\n")
            call("requirements", "--file", str(requirements))
            from aispa_core import model_routes
            route = model_routes.ROUTES["judge"]["codex"]  # brief roles are judges in the route table
            judge = ("--model", route["model"], "--reasoning", route["effort"])
            analyst = call("participant", "--role", "brief-analyst", "--runtime", "codex",
                           "--native-id", "native-читатель-🙂", "--session", "unicode-session",
                           *judge)["receipt"]["actor"]
            reviewer = call("participant", "--role", "brief-reviewer", "--runtime", "codex",
                            "--native-id", "native-проверяющий-🙂", "--session", "unicode-session",
                            *judge)["receipt"]["actor"]
            from tests import alpha10_fixtures as fixtures
            source = call("brief-source")
            self.assertEqual(source["briefs"][0]["units"][0]["text"], "Покажи приветствие.")
            for action, actor in (("brief-extract", analyst), ("brief-review", reviewer)):
                report = inbox / (action + ".json")
                report.write_text(json.dumps(fixtures.source_report(call("brief-source"), actor), ensure_ascii=False),
                                  encoding="utf-8-sig", newline="\n")
                call(action, "--file", str(report))
            mapping = inbox / "brief-map.json"
            mapping.write_text(json.dumps(fixtures.source_map_report(call("brief-map-context"), reviewer), ensure_ascii=False),
                               encoding="utf-8-sig", newline="\n")
            call("brief-map", "--file", str(mapping))
            self.assertTrue(call("preparation")["brief"]["valid"])
            self.assertEqual(call("status")["requirements"][0]["text"], "Приветствие")
            self.assertIn("Покажи приветствие.", (root / ".aispa/context/brief.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
