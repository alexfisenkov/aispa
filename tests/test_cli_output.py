import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "skills/aispa/tools/aispa.py"
sys.path.insert(0, str(CLI.parent))

from aispa_core.cli_output import compact
from aispa_core.store import Journal


class CliOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-output-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "проект with spaces 🙂"
        self.root.mkdir()
        self.cli = CLI
        self.git("init", "-q")
        (self.root / "app.py").write_text("print('ready')\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def call(self, *args, compact_mode=False, expected=0):
        flags = ["--compact"] if compact_mode else []
        result = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root), *flags, *args],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def init(self, compact_mode=False):
        result = self.call("init", "--slug", "output", "--title", "Русский проект 🙂",
                           "--mode", "auto", "--runtime", "codex", compact_mode=compact_mode)
        output = json.loads(result.stdout)
        self.run = self.root / ".aispa" / output["runDirectory"]
        self.cli = self.run / "runtime/tools/aispa.py"
        self.git("add", ".gitignore", "AGENTS.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore local run")
        return output

    def full_response(self, receipt):
        path = Path(receipt["absolutePath"])
        self.assertTrue(path.is_absolute())
        self.assertTrue(path.is_relative_to(self.run))
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["sha256"])
        self.assertEqual(len(raw), receipt["bytes"])
        self.assertEqual(path, self.run / receipt["file"])
        return json.loads(raw)

    def test_default_init_status_and_mutation_contracts_stay_full(self):
        initial = self.init()
        self.assertEqual(initial["title"], "Русский проект 🙂")
        self.assertIn("tasks", initial)
        self.assertNotIn("compact", initial)
        output = json.loads(self.call("decision", "Сохранить обычный вывод").stdout)
        self.assertEqual(set(output), {"ok", "run", "runDirectory", "status", "stage", "gates",
                                      "receipt", "task", "counts", "contextIndex"})
        status = json.loads(self.call("status").stdout)
        self.assertEqual(status["decisions"][0]["text"], "Сохранить обычный вывод")
        self.assertNotIn("compact", status)
        self.assertFalse((self.run / "cli-output").exists())

    def test_compact_init_is_recoverable_and_preserves_unicode_paths(self):
        receipt = self.init(compact_mode=True)
        full = self.full_response(receipt)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["gates"], full["gates"])
        self.assertEqual(receipt["status"], "running")
        self.assertEqual(receipt["stage"], full["displayStage"])
        self.assertEqual(receipt["counts"], dict.fromkeys(("pending", "running", "blocked", "completed"), 0))
        self.assertEqual(full["title"], "Русский проект 🙂")
        self.assertIn("проект with spaces 🙂", receipt["absolutePath"])
        self.assertNotIn("compact", full)

    def test_status_receipts_are_bounded_and_never_overwrite_previous_responses(self):
        self.init()
        note = "Подробное решение владельца. " * 1500
        self.call("decision", note)
        first_process = self.call("status", compact_mode=True)
        first = json.loads(first_process.stdout)
        first_bytes = Path(first["absolutePath"]).read_bytes()
        self.assertLess(len(first_process.stdout.encode("utf-8")), 4096)
        full = self.full_response(first)
        self.assertEqual(full["decisions"][0]["text"], note)
        self.assertEqual(first["gates"], full["gates"])
        self.assertEqual(first["run"], full["run"])
        second = json.loads(self.call("status", compact_mode=True).stdout)
        self.assertNotEqual(first["absolutePath"], second["absolutePath"])
        self.assertEqual(Path(first["absolutePath"]).read_bytes(), first_bytes)
        self.full_response(second)

    def test_small_mutation_receipt_keeps_required_actor_id(self):
        self.init()
        receipt = json.loads(self.call("participant", "--role", "brief-analyst", "--runtime", "codex",
                                      "--native-id", "native-аналитик", "--session", "session",
                                      compact_mode=True).stdout)
        full = self.full_response(receipt)
        self.assertTrue(receipt["receipt"]["actor"])
        self.assertEqual(receipt["receipt"], full["receipt"])

    def test_large_receipt_is_fully_recoverable_with_task_phase_and_all_gates(self):
        self.init()
        store = Journal(self.root)
        state = store.load()
        original = {
            "ok": False, "run": state["run"], "runDirectory": "runs/" + state["run"], "status": "running",
            "stage": "build", "gates": {"coverage": True, "plan": False, "acceptance": False},
            "task": {"id": "T01", "status": "running", "phase": "critic_2", "note": "Полная причина"},
            "counts": {"pending": 2, "running": 1, "blocked": 0, "completed": 3},
            "receipt": {"failures": [{"id": i, "reason": "Подробности проверки " * 100} for i in range(100)]},
        }
        with store.lock():
            receipt = compact(store, original)
        self.assertLess(len(json.dumps(receipt, ensure_ascii=False).encode("utf-8")), 4096)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["gates"], original["gates"])
        self.assertEqual(receipt["task"]["phase"], "critic_2")
        self.assertEqual(receipt["counts"], original["counts"])
        self.assertEqual(receipt["receipt"], {"inFullResponse": True, "jsonPointer": "/receipt"})
        self.assertEqual(self.full_response(receipt), original)

    def test_failed_verify_keeps_exit_two_and_recoverable_failure_evidence(self):
        self.init()
        result = self.call("verify", "--", sys.executable, "-c",
                           "import sys; print('failure-evidence'); sys.exit(7)", compact_mode=True, expected=2)
        receipt = json.loads(result.stdout)
        full = self.full_response(receipt)
        self.assertFalse(receipt["ok"])
        self.assertFalse(full["ok"])
        self.assertEqual(receipt["gates"], full["gates"])
        store = Journal(self.root)
        state = store.load()
        self.assertEqual(state["verification"]["exitCode"], 7)
        self.assertIn("failure-evidence", store.text(state["verification"]["output"]))
        self.assertEqual(state["status"], "running")

    def test_reads_remain_full_even_with_global_compact_flag(self):
        self.init()
        brief = self.root / ".aispa/brief.md"
        brief.write_text("Сохранить каждый пункт исходного брифа.", encoding="utf-8")
        self.call("brief", "--file", str(brief))
        for command in (("context", "--kind", "coverage"), ("template", "brief-extract"),
                        ("commands",), ("memory",), ("preparation",)):
            with self.subTest(command=command):
                default = json.loads(self.call(*command).stdout)
                selected = json.loads(self.call(*command, compact_mode=True).stdout)
                self.assertEqual(selected, default)
                self.assertNotIn("compact", selected)
        self.assertFalse((self.run / "cli-output").exists())
        written = json.loads(self.call("context", "--kind", "coverage", "--write", compact_mode=True).stdout)
        self.assertNotIn("compact", written)
        packet_path = Path(written["absolutePath"])
        self.assertEqual(hashlib.sha256(packet_path.read_bytes()).hexdigest(), written["sha256"])
        self.assertEqual(json.loads(packet_path.read_text(encoding="utf-8"))["kind"], "coverage")

    def test_guard_and_flag_errors_keep_diagnostics_and_create_no_output_artifact(self):
        self.init()
        default = self.call("finish", expected=2)
        selected = self.call("finish", compact_mode=True, expected=2)
        self.assertEqual(selected.stdout, "")
        self.assertEqual(selected.stderr, default.stderr)
        self.assertFalse((self.run / "cli-output").exists())
        for arguments in (("--compact=maybe", "status"), ("status", "--compact")):
            with self.subTest(arguments=arguments):
                result = self.call(*arguments, expected=2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
        self.assertFalse((self.run / "cli-output").exists())

    def test_unavailable_compact_archive_does_not_hide_committed_success(self):
        self.init()
        # A regular file instead of a directory fails identically on all OSes.
        archive = self.run / "cli-output"
        archive.write_bytes(b"do not replace")
        result = self.call("decision", "Record this only once", compact_mode=True)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"])
        self.assertNotIn("compact", output)
        self.assertIn("полный результат", result.stderr)
        state = Journal(self.root).load()
        self.assertEqual([d["text"] for d in state["notes"]], ["Record this only once"])
        self.assertEqual(archive.read_bytes(), b"do not replace")

    def test_unavailable_compact_archive_preserves_failed_verify_and_evidence(self):
        self.init()
        (self.run / "cli-output").write_bytes(b"blocked archive")
        result = self.call("verify", "--", sys.executable, "-c",
                           "import sys; print('real failure'); sys.exit(7)", compact_mode=True, expected=2)
        output = json.loads(result.stdout)
        self.assertFalse(output["ok"])
        self.assertNotIn("compact", output)
        self.assertIn("полный результат", result.stderr)
        store = Journal(self.root)
        state = store.load()
        self.assertEqual(state["verification"]["exitCode"], 7)
        self.assertIn("real failure", store.text(state["verification"]["output"]))

    def test_draft_write_respects_runtime_pin_while_template_read_stays_available(self):
        self.init()
        before = self.call("template", "interfaces").stdout
        with (self.run / "runtime/SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nChanged runtime fixture\n")
        self.assertEqual(self.call("template", "interfaces").stdout, before)
        result = self.call("template", "worker", "--task", "T01", "--write", expected=2)
        self.assertIn("Пакет изменён", result.stderr)
        self.assertFalse((self.root / ".aispa/inbox").exists())

    def test_draft_write_respects_pending_recovery_without_consuming_events(self):
        self.init()
        store = Journal(self.root)
        with store.lock():
            state = store.load()
            state["journalRecovery"] = {"pending": True}
            store.append(state, "synthetic-recovery-fixture")
        before = (self.run / "events.jsonl").read_bytes()
        self.call("template", "interfaces")
        result = self.call("template", "worker", "--task", "T01", "--write", expected=2)
        self.assertIn("Восстановление ещё не сверено", result.stderr)
        self.assertEqual((self.run / "events.jsonl").read_bytes(), before)
        self.assertFalse((self.root / ".aispa/inbox").exists())


if __name__ == "__main__":
    unittest.main()
