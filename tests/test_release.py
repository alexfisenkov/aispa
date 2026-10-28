"""Мост с AISpa 0.x: обновитель пакета, сборка выпуска, старые команды и прогоны.

Запуск из корня репозитория:  python3 -m unittest discover -s tests
Сети нет: GitHub подменяет tests/_fake_github.py, всё остальное — настоящий код.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL = os.path.join(REPO, "skills", "aispa")
TOOL = os.path.join(SKILL, "tools", "aispa.py")
AP = os.path.join(SKILL, "tools", "ap.py")
FAKE = os.path.join(HERE, "_fake_github.py")
BUILD = os.path.join(REPO, "tools", "собрать-выпуск.py")
R = "alexfisenkov/aispa"
NO_JUNK = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def version_of_package(package):
    return re.search(r'VERSION = "([^"]+)"', read(os.path.join(package, "tools", "aispa_core", "__init__.py"))).group(1)


def run(args, cwd=None, env=None):
    r = subprocess.run([sys.executable, "-B"] + args, capture_output=True, text=True,
                       encoding="utf-8", cwd=cwd, env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout, r.stderr


def listing(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="aispa-release-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Versions(unittest.TestCase):
    def test_one_version_everywhere(self):
        skill = re.search(r'^\s*version:\s*"([^"]+)"', read(os.path.join(SKILL, "SKILL.md")), re.M).group(1)
        changelog = re.search(r"^## (\S+)", read(os.path.join(REPO, "CHANGELOG.md")), re.M).group(1)
        self.assertEqual(skill, version_of_package(SKILL))
        self.assertEqual(skill, changelog)

    def test_version_flag(self):
        code, out, _ = run([TOOL, "--version"])
        self.assertEqual((code, out.strip()), (0, version_of_package(SKILL)))

    def test_required_by_old_installers(self):
        # 0.x install.py грузит обновитель из коммита тега и читает RUNTIMES из пакета.
        for rel in ("updates.py", "store.py", "portable.py", "__init__.py", "project_context.py"):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, "tools", "aispa_core", rel)), rel)
        runtimes = read(os.path.join(SKILL, "tools", "aispa_core", "project_context.py"))
        for host in ("codex", "claude-code", "cursor", "openclaw", "gemini", "antigravity"):
            self.assertIn('"%s"' % host, runtimes)

    def test_template_search_skips_update_copies(self):
        text = read(os.path.join(SKILL, "phases", "0-instruments.md"))
        pattern = "*/aispa/phases/dashboard-template.html"
        self.assertIn("-path '%s'" % pattern, text)
        with tempfile.TemporaryDirectory() as tmp:
            skills = os.path.join(tmp, ".claude", "skills")
            for rel in (".aispa-backup-x1/package", "autopilot", "aispa"):
                write(os.path.join(skills, rel, "phases", "dashboard-template.html"), "x")
            out = subprocess.run(["find", "-L", skills, "-maxdepth", "6", "-path", pattern],
                                 capture_output=True, text=True).stdout.split()
            self.assertEqual(out, [os.path.join(skills, "aispa", "phases", "dashboard-template.html")])


class OldCommands(Temp):
    """Старый агент сразу после замены пакета зовёт команды 0.x — ответ ясный, без падений."""

    def test_run_commands_answer_and_touch_nothing(self):
        for args in (["init", "--slug", "x", "--title", "X", "--mode", "auto"],
                     ["--root", self.tmp, "--compact", "status"],
                     ["dashboard", "start"], ["resume"], ["batch", "--file", "steps.json"]):
            code, out, err = run([TOOL] + args, cwd=self.tmp)
            self.assertEqual(code, 2, args)
            answer = json.loads(out)
            self.assertEqual(answer["status"], "upgraded")
            self.assertEqual(answer["version"], version_of_package(SKILL))
            self.assertIn("/aispa", answer["message"])
            self.assertIsNone(answer["oldRuns"])
            self.assertTrue(err.startswith("AISpa: "))
            self.assertNotIn("Traceback", err)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_old_run_named_and_left_alone(self):
        active = os.path.join(self.tmp, ".aispa", "active.json")
        write(active, '{"run": "r1"}')
        before = listing(self.tmp)
        code, out, _ = run([TOOL, "--root", self.tmp, "status"])
        self.assertEqual(code, 2)
        answer = json.loads(out)
        self.assertEqual(answer["oldRuns"], os.path.join(self.tmp, ".aispa"))
        self.assertIn("не продолжает", answer["message"])
        self.assertEqual(listing(self.tmp), before)
        self.assertEqual(read(active), '{"run": "r1"}')

    def test_help(self):
        code, out, _ = run([TOOL])
        self.assertEqual(code, 0)
        self.assertIn("update-prepare", out)


class SkillsCli(Temp):
    def test_npx_copy_is_left_to_npx(self):
        pkg = os.path.join(self.tmp, ".agents", "skills", "aispa")
        shutil.copytree(SKILL, pkg, ignore=NO_JUNK)
        write(os.path.join(self.tmp, ".agents", ".skill-lock.json"),
              json.dumps({"version": 3, "skills": {"aispa": {"source": R}}}))
        tool = os.path.join(pkg, "tools", "aispa.py")
        code, out, _ = run([tool, "update-check"])
        self.assertEqual(code, 0)
        found = json.loads(out)
        self.assertEqual((found["status"], found["manager"], found["installAllowed"]),
                         ("skills_cli", "skills-cli", False))
        code, _, err = run([tool, "update-prepare"])
        self.assertEqual(code, 2)
        self.assertIn("npx skills update aispa", err)

    def test_without_lock_it_is_a_release_copy(self):
        pkg = os.path.join(self.tmp, ".agents", "skills", "aispa")
        shutil.copytree(SKILL, pkg, ignore=NO_JUNK)
        fixture = os.path.join(self.tmp, "offline.json")
        write(fixture, '{"offline": true}')
        code, out, _ = run([FAKE, fixture, os.path.join(pkg, "tools", "aispa.py"), "update-check"])
        self.assertEqual(code, 0)
        found = json.loads(out)
        self.assertEqual((found["status"], found["manager"], found["installAllowed"]),
                         ("offline", "release", True))


class ReleaseRoundTrip(Temp):
    """Собрать выпуск из коммита, поставить его поверх старой копии обновителем, откатить."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(SKILL, os.path.join(self.repo, "skills", "aispa"), ignore=NO_JUNK)
        os.makedirs(os.path.join(self.repo, "tools"))
        for rel in ("LICENSE", "tools/install.py", "tools/собрать-выпуск.py"):
            shutil.copy2(os.path.join(REPO, rel), os.path.join(self.repo, rel))
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "release"]):
            subprocess.run(["git", "-C", self.repo] + args, check=True, capture_output=True)
        self.commit = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], check=True,
                                     capture_output=True, text=True).stdout.strip()

    def build(self, output):
        code, out, err = run([os.path.join(self.repo, "tools", "собрать-выпуск.py"), "--output", output])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def fixture(self, dist, version, extra_rows=()):
        tag = "v" + version
        urls = {n: "https://github.com/%s/releases/download/%s/%s" % (R, tag, n)
                for n in ("aispa.zip", "aispa-update.json")}
        rows = list(extra_rows) + [{
            "draft": False, "prerelease": False, "published_at": "2026-10-29T00:00:00Z", "tag_name": tag,
            "html_url": "https://github.com/%s/releases/tag/%s" % (R, tag),
            "assets": [{"name": n, "state": "uploaded", "browser_download_url": u} for n, u in urls.items()]}]
        path = os.path.join(self.tmp, "fixture-%s.json" % version)
        write(path, json.dumps({"releases": rows, "commit": self.commit,
                                "files": {u: os.path.join(dist, n) for n, u in urls.items()}}))
        return path

    def test_build_is_deterministic_and_self_checked(self):
        one, two = os.path.join(self.tmp, "d1"), os.path.join(self.tmp, "d2")
        a, b = self.build(one), self.build(two)
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual((a["commit"], a["version"], a["published"]), (self.commit, version_of_package(SKILL), False))
        self.assertEqual(sorted(os.listdir(one)), ["aispa-update.json", "aispa.zip", "install.py"])
        manifest = json.loads(read(os.path.join(one, "aispa-update.json")))
        names = {f["path"] for f in manifest["files"]}
        self.assertTrue({"SKILL.md", "LICENSE", "tools/aispa.py", "tools/ap.py",
                         "tools/aispa_core/__init__.py", "tools/aispa_core/updates.py"} <= names)
        self.assertFalse(any("__pycache__" in n or n.endswith(".pyc") for n in names))

    def test_old_copy_updates_and_rolls_back(self):
        dist = os.path.join(self.tmp, "dist")
        built = self.build(dist)
        home = os.path.join(self.tmp, "home")
        pkg = os.path.join(home, ".claude", "skills", "aispa")
        shutil.copytree(SKILL, pkg, ignore=NO_JUNK)
        init = os.path.join(pkg, "tools", "aispa_core", "__init__.py")
        write(init, read(init).replace(built["version"], "0.4.0"))
        tool = os.path.join(pkg, "tools", "aispa.py")
        # v1.0.0 без файлов не мешает: обновитель берёт максимальный выпуск.
        bare = {"draft": False, "prerelease": False, "published_at": "2026-10-28T11:26:03Z",
                "tag_name": "v1.0.0", "html_url": "https://github.com/%s/releases/tag/v1.0.0" % R, "assets": []}
        fixture = self.fixture(dist, built["version"], [bare])

        code, out, err = run([FAKE, fixture, tool, "update-check"], cwd=home)
        self.assertEqual(code, 0, err)
        found = json.loads(out)
        self.assertEqual((found["status"], found["installAllowed"], found["release"]["version"]),
                         ("available", True, built["version"]))
        code, out, err = run([FAKE, fixture, tool, "update-prepare"], cwd=home)
        self.assertEqual(code, 0, err)
        prepared = json.loads(out)
        code, out, err = run([FAKE, fixture, tool, "update-apply", "--candidate", prepared["candidateId"]], cwd=home)
        self.assertEqual(code, 2)
        self.assertIn("подтверждение", err)
        code, out, err = run([FAKE, fixture, tool, "update-apply", "--candidate", prepared["candidateId"],
                              "--confirm"], cwd=home)
        self.assertEqual(code, 0, err)
        receipt = json.loads(out)
        self.assertEqual((receipt["status"], receipt["previousVersion"], receipt["version"]),
                         ("installed", "0.4.0", built["version"]))
        manifest = json.loads(read(os.path.join(dist, "aispa-update.json")))
        self.assertEqual(listing(pkg), sorted(f["path"] for f in manifest["files"]))
        for f in manifest["files"]:
            with open(os.path.join(pkg, f["path"]), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), f["sha256"], f["path"])

        # Сразу после замены: старые команды отвечают ясно, проверка говорит «текущая».
        self.assertEqual(run([tool, "--version"])[1].strip(), built["version"])
        code, out, _ = run([FAKE, fixture, tool, "update-check"], cwd=home)
        self.assertEqual(json.loads(out)["status"], "current")
        self.assertEqual(run([tool, "status"], cwd=home)[0], 2)

        code, out, err = run([FAKE, fixture, tool, "update-rollback", "--backup", receipt["backupId"],
                              "--confirm"], cwd=home)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "rolled_back")
        self.assertEqual(version_of_package(pkg), "0.4.0")

    def test_git_checkout_is_never_replaced(self):
        dist = os.path.join(self.tmp, "dist")
        built = self.build(dist)
        tool = os.path.join(self.repo, "skills", "aispa", "tools", "aispa.py")
        fixture = self.fixture(dist, built["version"])
        code, out, _ = run([FAKE, fixture, tool, "update-check"])
        found = json.loads(out)
        self.assertEqual((found["installAllowed"], found["installReason"]), (False, "git_checkout"))
        code, _, err = run([FAKE, fixture, tool, "update-prepare"])
        self.assertEqual(code, 2)
        self.assertIn("git_checkout", err)


class ApNotes(Temp):
    """ap.py: строка ↑ для копии из выпуска и честная строка про .aispa/ при init."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "-C", self.tmp, "init", "-q"], check=True)
        self.a = os.path.join(self.tmp, ".aispa-work")
        os.makedirs(self.a)
        shutil.copy(os.path.join(SKILL, "phases", "dashboard-template.html"), os.path.join(self.a, "dashboard.html"))
        shutil.copy(AP, os.path.join(self.a, "ap.py"))

    def init(self, skill_dir, *extra, env=None):
        args = [os.path.join(self.a, "ap.py"), "init", "--slug", "bot", "--title", "Бот",
                "--skill-dir", skill_dir, "--no-serve"] + list(extra)
        code, out, err = run(args, cwd=self.tmp, env=env)
        self.assertEqual(code, 0, out + err)
        return out

    def test_old_run_note(self):
        write(os.path.join(self.tmp, ".aispa", "active.json"), "{}")
        out = self.init(SKILL, env={"AISPA_NO_UPDATE_CHECK": "1"})
        self.assertIn("! в проекте есть .aispa/ — прогон AISpa 0.x", out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, ".aispa")), ["active.json"])

    def test_no_old_run_no_note(self):
        self.assertNotIn(".aispa/", self.init(SKILL, env={"AISPA_NO_UPDATE_CHECK": "1"}))

    def test_release_copy_gets_release_line(self):
        skill = os.path.join(self.tmp, "skill")
        shutil.copytree(SKILL, skill, ignore=NO_JUNK)
        answer = {"status": "available", "manager": "release", "installAllowed": True,
                  "installedVersion": "1.0.1",
                  "release": {"version": "1.0.2", "url": "https://github.com/%s/releases/tag/v1.0.2" % R}}
        write(os.path.join(skill, "tools", "aispa.py"), "import json\nprint(json.dumps(%r))\n" % answer)
        env = dict(os.environ)
        env.pop("AISPA_NO_UPDATE_CHECK", None)
        r = subprocess.run([sys.executable, "-B", os.path.join(self.a, "ap.py"), "init", "--slug", "bot",
                            "--skill-dir", skill, "--no-serve", "--check-update"],
                           capture_output=True, text=True, encoding="utf-8", cwd=self.tmp, env=env)
        self.assertIn("↑ вышла версия AISpa 1.0.2 (у тебя 1.0.1): обновлю после сборки, "
                      "если скажешь «обнови AISpa»", r.stdout)


if __name__ == "__main__":
    unittest.main()
