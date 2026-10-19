import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import cleanroom
from aispa_core.store import Invalid, Journal


VENV_PYTHON = Path(".venv") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class CleanroomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa клон space-🙂-")
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".aispa/\n.env\n.venv/\nnode_modules/\n__pycache__/\nbuild/\n", encoding="utf-8", newline="\n")
        (self.root / "app.py").write_text("print('committed application')\n", encoding="utf-8", newline="\n")
        self.commit()
        self.store = Journal(self.root)
        self.state = self.store.create("clean", "Clean clone")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, root=None):
        return subprocess.run(["git", "-C", str(root or self.root), *args],
                              check=True, capture_output=True).stdout

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def prepare(self):
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        return Path(self.state["cleanroom"]["path"])

    def execute(self, script="print('checked')", timeout=5):
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "-c", script], timeout)
        return self.state["cleanrun"]

    def test_clone_is_real_distinct_exact_head_without_ignored_files_or_origin(self):
        (self.root / ".env").write_text("PRIVATE_VALUE=never-copy", encoding="utf-8", newline="\n")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/fake-package").write_text("fake local dependency", encoding="utf-8", newline="\n")
        self.git("remote", "add", "origin", "https://user:private-value@example.invalid/private.git")
        clone = self.prepare()
        self.assertNotEqual(clone, self.root)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD", root=clone))
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), self.git("rev-parse", "HEAD^{tree}", root=clone))
        self.assertFalse((clone / "app.py").samefile(self.root / "app.py"))
        self.assertFalse((clone / ".env").exists())
        self.assertFalse((clone / "node_modules").exists())
        self.assertFalse((clone / ".aispa").exists())
        self.assertEqual(self.git("remote", root=clone), b"")
        self.assertFalse((clone / ".git/objects/info/alternates").exists())
        for path in (clone / ".git/objects").rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(self.state["cleanroom"]["sourceCode"], self.store.code())
        self.assertTrue(cleanroom.current(self.store, self.state))
        self.assertFalse(cleanroom.valid(self.store, self.state))
        snapshot = self.store.run / self.state["cleanroom"]["snapshot"]["file"]
        self.assertTrue(snapshot.is_file())
        self.assertFalse(snapshot.is_relative_to(clone))

    def test_old_history_secret_is_not_transferred(self):
        # The current committed tree is safe; depth=1 must not bring old .env back.
        old = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "app.py").write_text("print('new application')\n", encoding="utf-8", newline="\n")
        self.commit()
        clone = self.prepare()
        result = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", old], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-list", "--count", "HEAD", root=clone), b"1\n")

    def test_dirty_tracked_missing_and_unignored_files_refused(self):
        for action in ("edit", "missing", "untracked"):
            with self.subTest(action=action):
                original = (self.root / "app.py").read_text(encoding="utf-8")
                if action == "edit":
                    (self.root / "app.py").write_text("uncommitted", encoding="utf-8", newline="\n")
                elif action == "missing":
                    (self.root / "app.py").unlink()
                else:
                    (self.root / "missing from commit.py").write_text("needed", encoding="utf-8", newline="\n")
                with self.assertRaises(Invalid):
                    self.prepare()
                (self.root / "app.py").write_text(original, encoding="utf-8", newline="\n")
                (self.root / "missing from commit.py").unlink(missing_ok=True)

    def test_assume_unchanged_cannot_hide_source_edit(self):
        self.git("update-index", "--assume-unchanged", "app.py")
        (self.root / "app.py").write_text("secret hidden edit", encoding="utf-8", newline="\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        with self.assertRaises(Invalid):
            self.prepare()

    def test_source_must_be_git_root_and_service_dir_ignored(self):
        sub = self.root / "sub"
        sub.mkdir()
        store = Journal(sub)
        state = store.create("sub", "Nested")
        with self.assertRaises(Invalid):
            cleanroom.prepare(store, state)
        (self.root / ".gitignore").write_text(".env\n", encoding="utf-8", newline="\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove ignore")
        with self.assertRaises(Invalid):
            self.prepare()

    def test_tracked_secrets_symlinks_and_submodules_are_refused(self):
        for name, content in ((".env", "TOKEN=private"), ("id_ed25519", "key"),
                              ("config.txt", "ghp_" + "a" * 30)):
            with self.subTest(name=name):
                (self.root / name).write_text(content, encoding="utf-8", newline="\n")
                self.git("add", "-f", name)
                self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "bad fixture")
                with self.assertRaises(Invalid):
                    self.prepare()
                self.git("rm", name)
                self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove fixture")
        (self.root / "link").symlink_to("app.py")
        self.commit()
        with self.assertRaises(Invalid):
            self.prepare()
        self.git("rm", "link")
        self.commit()
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.git("rev-parse", "HEAD").decode().strip() + ",submodule")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "submodule")
        with self.assertRaises(Invalid):
            self.prepare()

    def test_no_credentials_or_user_configs_in_command_environment(self):
        clone = self.prepare()
        with patch.dict(os.environ, {"AISPA_TEST_TOKEN": "private-sentinel-12345", "SSH_AUTH_SOCK": "/private/ssh-agent",
                                     "PYTHONPATH": "/private/modules", "NODE_PATH": "/private/node-modules",
                                     "GIT_ASKPASS": "/private/helper"}):
            receipt = self.execute("import json, os; print(json.dumps(dict(os.environ), sort_keys=True))")
        output = self.store.text(receipt["output"])
        for value in ("private-sentinel", "SSH_AUTH_SOCK", "PYTHONPATH", "NODE_PATH", "GIT_ASKPASS"):
            self.assertNotIn(value, output)
        self.assertNotEqual(json.loads(output)["HOME"], str(Path.home()))
        self.assertNotEqual(json.loads(output)["HOME"], str(clone))
        self.assertTrue(cleanroom.valid(self.store, self.state))

    def test_unicode_output_survives_clean_environment(self):
        self.prepare()
        receipt = self.execute("print('Проверка в чистом клоне 🙂')")
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        self.assertEqual(self.store.text(receipt["output"]).strip(), "Проверка в чистом клоне 🙂")

    def test_source_autocrlf_setting_does_not_change_clone_bytes(self):
        self.git("config", "core.autocrlf", "true")
        clone = self.prepare()
        self.assertEqual((clone / "app.py").read_bytes(), (self.root / "app.py").read_bytes())
        self.assertTrue(cleanroom.current(self.store, self.state))

    @unittest.skipUnless(os.name == "nt", "Windows SystemRoot and filemode contract")
    def test_windows_environment_preserves_system_root_without_host_home(self):
        clone = self.prepare()
        receipt = self.execute("import json, os; print(json.dumps(dict(os.environ)))")
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        environment = {key.upper(): value for key, value in json.loads(self.store.text(receipt["output"])).items()}
        self.assertEqual(Path(environment["SYSTEMROOT"]), Path(os.environ["SystemRoot"]))
        self.assertNotEqual(Path(environment["USERPROFILE"]), Path(os.environ["USERPROFILE"]))
        self.assertEqual(self.git("config", "--get", "core.filemode", root=clone).strip(), b"false")

    def test_ignored_install_and_build_outputs_are_allowed_but_local_dependency_absent(self):
        clone = self.prepare()
        receipt = self.execute("from pathlib import Path; assert not Path('node_modules').exists(); "
                               "Path('node_modules').mkdir(); Path('node_modules/built').write_text('installed'); "
                               "Path('build').mkdir(); Path('build/result').write_text('built')")
        self.assertTrue(receipt["passed"])
        self.assertTrue(cleanroom.valid(self.store, self.state))
        self.assertTrue((clone / "node_modules/built").exists())
        self.assertFalse((self.root / "node_modules").exists())

    def test_actual_dependency_install_from_hash_lock_in_clone(self):
        # A tiny committed wheel lets pip perform a real install without network.
        (self.root / ".gitignore").write_text((self.root / ".gitignore").read_text(encoding="utf-8") + ".venv/\n", encoding="utf-8", newline="\n")
        vendor = self.root / "vendor"
        vendor.mkdir()
        wheel = vendor / "aispa_fixture-0.0.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("aispa_fixture.py", "VALUE = 'installed from committed lock'\n")
            archive.writestr("aispa_fixture-0.0.1.dist-info/METADATA",
                             "Metadata-Version: 2.1\nName: aispa-fixture\nVersion: 0.0.1\n")
            archive.writestr("aispa_fixture-0.0.1.dist-info/WHEEL",
                             "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("aispa_fixture-0.0.1.dist-info/RECORD", "")
        sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (self.root / "requirements.lock").write_text("./vendor/" + wheel.name + " --hash=sha256:" + sha + "\n", encoding="utf-8", newline="\n")
        self.commit()
        clone = self.prepare()
        receipt = self.execute(
            "import os, subprocess, venv; from pathlib import Path; venv.EnvBuilder(with_pip=True).create('.venv'); "
            "python = str(Path('.venv', 'Scripts/python.exe' if os.name == 'nt' else 'bin/python').resolve()); "
            "subprocess.run([python, '-m', 'pip', 'install', '--no-index', '--require-hashes', "
            "'-r', 'requirements.lock'], check=True); "
            "subprocess.run([python, '-c', \"import aispa_fixture; "
            "assert aispa_fixture.VALUE == 'installed from committed lock'\"], check=True)", timeout=60)
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        self.assertTrue(cleanroom.valid(self.store, self.state))
        self.assertTrue((clone / VENV_PYTHON).is_file())
        self.assertFalse((self.root / ".venv").exists())
        self.state["cleanrun"] = cleanroom.execute(
            self.store, self.state, [str(VENV_PYTHON), "-c", "import aispa_fixture; print(aispa_fixture.VALUE)"], 10)
        self.assertTrue(self.state["cleanrun"]["passed"], self.store.text(self.state["cleanrun"]["output"]))
        self.assertTrue(cleanroom.valid(self.store, self.state))

    def test_failed_setup_removes_only_its_owned_directory(self):
        sentinel = self.store.home / "cleanrooms/preexisting"
        sentinel.mkdir(parents=True)
        (sentinel / "keep").write_text("user data", encoding="utf-8", newline="\n")
        real_git = cleanroom._git

        def fail_clone(root, *args, **kwargs):
            if "clone" in args:
                raise Invalid("fixture failure")
            return real_git(root, *args, **kwargs)

        with patch.object(cleanroom, "_git", side_effect=fail_clone), self.assertRaises(Invalid):
            self.prepare()
        self.assertEqual((sentinel / "keep").read_text(encoding="utf-8"), "user data")
        self.assertEqual(list(sentinel.parent.iterdir()), [sentinel])

    def test_failed_command_and_timeout_cannot_pass(self):
        self.prepare()
        failed = self.execute("import sys; print('failed check'); sys.exit(7)")
        self.assertEqual(failed["exitCode"], 7)
        self.assertFalse(cleanroom.valid(self.store, self.state))
        timed = self.execute("import time; print('waiting', flush=True); time.sleep(5)", timeout=0.1)
        self.assertEqual(timed["exitCode"], 124)
        self.assertTrue(timed["timedOut"])
        self.assertFalse(cleanroom.valid(self.store, self.state))

    def test_timeout_terminates_process_tree_children(self):
        clone = self.prepare()
        script = ("import subprocess, sys, time; from pathlib import Path; Path('build').mkdir(); "
                  "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; "
                  "time.sleep(.7); Path('build/leaked-child').write_text('bad')\"]); time.sleep(10)")
        self.execute(script, timeout=0.15)
        time.sleep(0.8)
        self.assertFalse((clone / "build/leaked-child").exists())

    def test_successful_parent_does_not_leave_background_children(self):
        clone = self.prepare()
        script = ("import subprocess, sys; from pathlib import Path; Path('build').mkdir(); "
                  "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; "
                  "time.sleep(1); Path('build/leaked-child').write_text('bad')\"])")
        receipt = self.execute(script)
        self.assertTrue(receipt["passed"], self.store.text(receipt["output"]))
        time.sleep(1.1)
        self.assertFalse((clone / "build/leaked-child").exists())

    def test_success_stales_on_commit_clone_output_and_evidence_mutations(self):
        clone = self.prepare()
        receipt = self.execute()
        self.assertTrue(cleanroom.valid(self.store, self.state))
        evidence = self.store.run / receipt["output"]["file"]
        before = evidence.read_bytes()
        evidence.write_bytes(b"rewritten")
        self.assertFalse(cleanroom.valid(self.store, self.state))
        evidence.write_bytes(before)
        (clone / "app.py").write_text("changed clone", encoding="utf-8", newline="\n")
        self.assertFalse(cleanroom.valid(self.store, self.state))
        self.git("checkout", "--", "app.py", root=clone)
        self.assertTrue(cleanroom.valid(self.store, self.state))
        (clone / "unexpected.txt").write_text("unignored artifact", encoding="utf-8", newline="\n")
        self.assertFalse(cleanroom.valid(self.store, self.state))
        (clone / "unexpected.txt").unlink()
        (self.root / "app.py").write_text("print('new commit')\n", encoding="utf-8", newline="\n")
        self.commit()
        self.assertFalse(cleanroom.current(self.store, self.state))
        self.assertFalse(cleanroom.valid(self.store, self.state))

    def test_command_that_changes_tracked_or_unignored_clone_files_fails(self):
        self.prepare()
        receipt = self.execute("from pathlib import Path; Path('generated.txt').write_text('not ignored')")
        self.assertEqual(receipt["exitCode"], 0)
        self.assertTrue(receipt["changedCode"])
        self.assertFalse(receipt["passed"])

    def test_source_file_arguments_and_code_path_literals_are_rejected(self):
        self.prepare()
        script = "from pathlib import Path; Path(%r).write_text('source changed')" % str(self.root / "app.py")
        shell_command = (["cmd.exe", "/d", "/c", 'type "' + str(self.root / "app.py") + '"']
                         if os.name == "nt" else ["/bin/sh", "-c", "cat " + repr(str(self.root / "app.py"))])
        for command in ([sys.executable, str(self.root / "app.py")], [sys.executable, "-c", script], shell_command,
                        ["cc", "-I" + str(self.root), "app.c"],
                        [sys.executable, "../../../../app.py"]):
            with self.subTest(command=command), self.assertRaises(Invalid):
                cleanroom.execute(self.store, self.state, command, 5)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "print('committed application')\n")

    def test_programmatic_source_mutation_is_still_detected_after_execution(self):
        self.prepare()
        # Deliberately computed paths demonstrate why this is not a sandbox.
        encoded = str(self.root / "app.py").encode().hex()
        receipt = self.execute("from pathlib import Path; Path(bytes.fromhex(%r).decode()).write_text('changed')" % encoded)
        self.assertTrue(receipt["changedSource"])
        self.assertFalse(cleanroom.valid(self.store, self.state))

    def test_source_venv_interpreter_is_rejected(self):
        source_python = self.root / VENV_PYTHON
        source_python.parent.mkdir(parents=True)
        shutil.copyfile(sys.executable, source_python)
        self.prepare()
        for executable in (str(source_python), str(Path("../../../..") / VENV_PYTHON)):
            with self.subTest(executable=executable), self.assertRaises(Invalid):
                cleanroom.execute(self.store, self.state, [executable, "-c", "print('source venv')"], 5)

    def test_source_venv_symlink_alias_is_rejected(self):
        source_python = self.root / VENV_PYTHON
        source_python.parent.mkdir(parents=True)
        source_python.symlink_to(sys.executable)
        clone = self.prepare()
        # An ignored clone-local alias must not tunnel through the source venv.
        (clone / VENV_PYTHON).parent.mkdir(parents=True)
        (clone / VENV_PYTHON).symlink_to(source_python)
        with self.assertRaises(Invalid):
            cleanroom.execute(self.store, self.state, [str(VENV_PYTHON), "-c", "print('source venv')"], 5)

    def test_sanitized_path_does_not_use_source_tools_and_resolved_alias_is_rejected(self):
        source_tool = self.root / VENV_PYTHON.parent / ("aispa-fixture-tool.exe" if os.name == "nt" else "aispa-fixture-tool")
        source_tool.parent.mkdir(parents=True)
        source_tool.symlink_to(sys.executable)
        self.prepare()
        with patch.dict(os.environ, {"PATH": str(source_tool.parent)}):
            result = cleanroom.execute(self.store, self.state, [source_tool.name, "-c", "print('bad')"], 5)
        self.assertEqual(result["exitCode"], 127)
        with tempfile.TemporaryDirectory(prefix="aispa-external-bin-") as external:
            (Path(external) / source_tool.name).symlink_to(source_tool)
            with patch.object(cleanroom, "SAFE_PATH", external + os.pathsep + cleanroom.SAFE_PATH), self.assertRaises(Invalid):
                cleanroom.execute(self.store, self.state, [source_tool.name, "-c", "print('bad')"], 5)

    def test_receipts_and_snapshot_cannot_be_modified_or_rebound(self):
        self.prepare()
        receipt = self.execute()
        receipt["command"] = ["different-command"]
        self.assertFalse(cleanroom.valid(self.store, self.state))
        snapshot = self.store.run / self.state["cleanroom"]["snapshot"]["file"]
        snapshot.write_text("{}", encoding="utf-8", newline="\n")
        self.assertFalse(cleanroom.current(self.store, self.state))

    def test_git_metadata_and_parent_symlink_tampering_is_rejected(self):
        clone = self.prepare()
        config = clone / ".git/config"
        config.write_text(config.read_text(encoding="utf-8") + "[remote \"origin\"]\nurl=/private/origin\n", encoding="utf-8", newline="\n")
        self.assertFalse(cleanroom.current(self.store, self.state))
        config.write_bytes(cleanroom._CONFIG)
        self.assertTrue(cleanroom.current(self.store, self.state))
        (clone / ".git/objects/info/alternates").write_text(str(self.root / ".git/objects"), encoding="utf-8", newline="\n")
        self.assertFalse(cleanroom.current(self.store, self.state))

    def test_command_output_bounded_and_secrets_withheld(self):
        self.prepare()
        receipt = self.execute("print('x' * 1500000)")
        self.assertTrue(receipt["truncated"])
        self.assertLess(len(self.store.text(receipt["output"])), cleanroom.MAX_OUTPUT + 200)
        # A token beyond the retained prefix must still be detected while draining.
        receipt = self.execute("print('x' * 1100000); print('ghp_' + 'a' * 30)")
        self.assertTrue(receipt["redacted"])
        self.assertFalse(receipt["passed"])
        self.assertNotIn("ghp_", self.store.text(receipt["output"]))

    def test_invalid_commands_and_timeouts_are_refused(self):
        self.prepare()
        for command, timeout in (("echo check", 1), ([], 1), (["echo"], 0), (["echo"], float("inf")),
                                 (["echo", "ghp_" + "a" * 30], 1)):
            with self.subTest(command=command, timeout=timeout), self.assertRaises(Invalid):
                cleanroom.execute(self.store, self.state, command, timeout)

    def test_missing_executable_fails_with_evidence(self):
        self.prepare()
        receipt = cleanroom.execute(self.store, self.state, ["/nonexistent/aispa-command"], 1)
        self.assertEqual(receipt["exitCode"], 127)
        self.assertFalse(receipt["passed"])
        self.assertTrue((self.store.run / receipt["output"]["file"]).is_file())


if __name__ == "__main__":
    unittest.main()
