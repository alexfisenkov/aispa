"""AISpa 1.0 over 0.4/0.5: old updaters accept it, old runs keep their pinned copies.

No network: every GitHub answer is a local file. Old code comes from the tags
`v0.4.0` and `aispa-0.5.0-engine` via `git show`/`git archive` into a temporary folder.
"""
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
import aispa_core
from aispa_core import updates

OLD_TAGS = {"v0.4.0": "0.4.0", "aispa-0.5.0-engine": "0.5.0"}
NARROW_CORE = ("__init__.py", "updates.py", "portable.py", "support_faq.py", "store.py", "project_context.py")
OLD_LINE = "это команда прогона прежней версии — запусти её закреплённой копией"
PINNED = "python3 .aispa/runs/<run>/runtime/tools/aispa.py"
GIT_USER = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")

UPDATER = r'''
import json, sys, urllib.error
from pathlib import Path
tools, package, assets_file = sys.argv[1:4]
sys.path.insert(0, tools)
from aispa_core import updates
assets = {url: Path(path).read_bytes() for url, path in json.loads(Path(assets_file).read_text()).items()}
def download(url, limit, deadline):
    if url not in assets:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return assets[url]
updates._download = download
package = Path(package)
out = {"before": updates._version(package), "check": updates.check(package)}
prepared = updates.prepare(package)
out["prepare"] = prepared
out["apply"] = updates.apply(package, prepared["candidateId"], confirmed=True)
out["after"] = updates._version(package)
out["rollback"] = updates.rollback(package, out["apply"]["backupId"], confirmed=True)
out["final"] = updates._version(package)
print(json.dumps(out, ensure_ascii=False, default=str))
'''

FAQ = r'''
import json, sys
tools, package, version = sys.argv[1:4]
sys.path.insert(0, tools)
from aispa_core import support_faq
from aispa_core.store import Invalid
try:
    print(json.dumps({"ok": True, "value": support_faq.select(package, version=version)}, ensure_ascii=False, sort_keys=True))
except Invalid as error:
    print(json.dumps({"ok": False, "value": str(error)}, ensure_ascii=False))
'''


def git(*args, cwd=ROOT):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True).stdout


def extract(tag, path, destination):
    """`git archive <tag> <path>` into destination; returns destination / path."""
    with tarfile.open(fileobj=io.BytesIO(git("archive", tag, path))) as archive:
        archive.extractall(destination, filter="data")
    return destination / path


def tree_hashes(path):
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted(path.rglob("*")) if item.is_file() and "__pycache__" not in item.parts}


def narrow_package(destination):
    """The 1.0 package as it ships after the 0.5 engine leaves: only the kept modules."""
    source = ROOT / "skills/aispa"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    destination.mkdir(parents=True)
    for name in ("SKILL.md", "LICENSE"):
        shutil.copy2(source / name, destination / name)
    for folder in ("agents", "references"):
        shutil.copytree(source / folder, destination / folder, ignore=ignore)
    (destination / "tools/aispa_core").mkdir(parents=True)
    shutil.copy2(source / "tools/aispa.py", destination / "tools/aispa.py")
    for name in NARROW_CORE:
        shutil.copy2(source / "tools/aispa_core" / name, destination / "tools/aispa_core" / name)
    return destination


def python(*args, cwd=None):
    return subprocess.run([sys.executable, "-B", *map(str, args)], capture_output=True, text=True,
                          cwd=cwd, timeout=300)


class NarrowPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-old-runs-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_version_is_one_and_matches_skill_metadata(self):
        skill = (ROOT / "skills/aispa/SKILL.md").read_text(encoding="utf-8")
        metadata = re.search(r'^metadata:\n  version: "([^"]+)"', skill, re.M)
        self.assertEqual(aispa_core.VERSION, "1.0.0")
        self.assertEqual(metadata.group(1), aispa_core.VERSION)
        result = python(ROOT / "skills/aispa/tools/aispa.py", "--version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "1.0.0"))

    def test_kept_modules_import_nothing_outside_the_narrow_core(self):
        package = narrow_package(self.root / "aispa")
        check = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import aispa_core.updates, aispa_core.support_faq, aispa_core.portable, aispa_core.store, "
                 "aispa_core.project_context; from aispa_core import support_faq\n"
                 "try:\n    support_faq.select(sys.argv[2], version='1.0.0')\n"
                 "except support_faq.Invalid:\n    pass\n"
                 "print(sorted(m for m in sys.modules if m.startswith('aispa_core')))")
        result = python("-c", check, package / "tools", package)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ast.literal_eval(result.stdout), ["aispa_core", *("aispa_core." + n[:-3] for n in sorted(NARROW_CORE)
                                                              if n != "__init__.py")])
        stub = (ROOT / "skills/aispa/tools/aispa_core/store.py").read_text(encoding="utf-8")
        self.assertEqual(sorted(re.findall(r"^(?:def|class) (\w+)", stub, re.M)), ["Invalid", "atomic_json", "digest"])

    def test_faq_selection_matches_the_tag_module(self):
        old_tools = extract("aispa-0.5.0-engine", "skills/aispa", self.root / "old") / "tools"
        driver = self.root / "faq.py"
        driver.write_text(FAQ, encoding="utf-8")
        new_tools = narrow_package(self.root / "aispa") / "tools"
        # Служебные оси 1.0 — режимы и глубина, модуль тега знает только прежние. Поэтому каждый модуль
        # читает копию пакета со своими осями, а сравнивается всё, кроме осей и хеша самого канона:
        # записи, привязки к файлам, версия.
        canon = lambda package: json.loads((package / "references/support-faq.json").read_text(encoding="utf-8"))
        axes = {"old": canon(old_tools.parent)["axes"], "new": canon(ROOT / "skills/aispa")["axes"]}
        self.assertNotEqual(axes["old"], axes["new"])
        for package, version in ((old_tools.parent, "0.5.0"), (ROOT / "skills/aispa", aispa_core.VERSION)):
            with self.subTest(package=package.parent.name, version=version):
                answers = []
                for side, tools in (("old", old_tools), ("new", new_tools)):
                    copy = self.root / side / version / "aispa"
                    shutil.copytree(package, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    value = canon(copy)
                    value["axes"] = axes[side]
                    (copy / "references/support-faq.json").write_text(
                        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    answer = python(driver, tools, copy, version)
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    result = json.loads(answer.stdout)
                    self.assertTrue(result["ok"], result["value"])
                    self.assertEqual(result["value"].pop("axes"), axes[side])
                    result["value"].pop("canonicalSha256")
                    answers.append(result["value"])
                self.assertEqual(answers[1], answers[0])

    def test_old_run_commands_point_to_the_pinned_copy(self):
        tool = narrow_package(self.root / "aispa") / "tools/aispa.py"
        for command in (["dashboard", "refresh-view", "--port", "8765"], ["dashboard", "start"], ["status"],
                        ["init", "--slug", "x", "--title", "x"], ["resume"], ["seal"], ["assess"],
                        ["batch", "--file", "steps.json"], ["--root", ".", "status"], ["--full", "page"]):
            with self.subTest(command=command):
                result = python(tool, *command, cwd=self.root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(OLD_LINE, result.stderr)
                self.assertIn(PINNED, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("usage:", result.stderr + result.stdout)

    def test_update_agents_keeps_its_json_answer(self):
        package = narrow_package(self.root / "project/.claude/skills/aispa")
        result = python(package / "tools/aispa.py", "update-agents", "--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(set(answer), {"status", "directory", "installed", "skipped", "reason", "message"})
        shipped = sorted(p.name for p in (package / "agents/claude-code").glob("aispa-*.md"))
        self.assertEqual(sorted(p.name for p in (self.root / "project/.claude/agents").glob("*.md")), shipped)


class OldUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-old-updaters-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self):
        """tools/package_update.py over a temporary commit of the current tree."""
        repo = self.root / "repo"
        repo.mkdir()
        listed = git("ls-files", "-co", "--exclude-standard", "-z", "--", "skills/aispa", "LICENSE", "tools/install.py")
        for name in filter(None, listed.decode("utf-8").split("\0")):
            source = ROOT / name
            if source.is_file() and "__pycache__" not in source.parts:
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, repo / name)
        git("init", "-q", cwd=repo)
        git("add", ".", cwd=repo)
        git("update-index", "--chmod=+x", "skills/aispa/tools/aispa.py", cwd=repo)
        git(*GIT_USER, "commit", "-qm", "candidate", cwd=repo)
        spec = importlib.util.spec_from_file_location("package_update_old_runs", ROOT / "tools/package_update.py")
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        return producer.build_release(repo, self.root / "artifact")

    def assets(self, built):
        tag = "v" + built["version"]
        prefix = f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/"
        release = {"draft": False, "prerelease": False, "published_at": "2026-10-28T00:00:00Z", "tag_name": tag,
                   "html_url": f"https://github.com/{updates.REPOSITORY}/releases/tag/{tag}",
                   "assets": [{"name": name, "state": "uploaded", "browser_download_url": prefix + name}
                              for name in ("aispa.zip", "aispa-update.json")]}
        files = {"releases.json": json.dumps([release]),
                 "ref.json": json.dumps({"ref": "refs/tags/" + tag, "object": {"type": "commit", "sha": built["commit"]}})}
        for name, text in files.items():
            (self.root / name).write_text(text, encoding="utf-8")
        table = {updates.RELEASES_URL: self.root / "releases.json", updates.RELEASES_URL + "&page=1": self.root / "releases.json",
                 f"https://api.github.com/repos/{updates.REPOSITORY}/git/ref/tags/{tag}": self.root / "ref.json",
                 prefix + "aispa.zip": built["archive"], prefix + "aispa-update.json": built["manifest"]}
        path = self.root / "assets.json"
        path.write_text(json.dumps({url: str(file) for url, file in table.items()}), encoding="utf-8")
        return path

    def test_old_updaters_install_one_point_zero_with_backup_and_rollback(self):
        built = self.candidate()
        self.assertEqual(built["version"], "1.0.0")
        assets = self.assets(built)
        driver = self.root / "updater.py"
        driver.write_text(UPDATER, encoding="utf-8")
        for tag in OLD_TAGS:
            with self.subTest(tag=tag):
                old = extract(tag, "skills/aispa", self.root / tag / "source")
                package = self.root / tag / "install/aispa"
                shutil.copytree(old, package)
                updater = self.root / tag / "updater"
                shutil.copytree(old / "tools/aispa_core", updater / "aispa_core")
                before = tree_hashes(package)
                result = python(driver, updater, package, assets)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                out = json.loads(result.stdout)
                self.assertEqual(out["check"]["status"], "available")
                self.assertTrue(out["check"]["installAllowed"])
                self.assertEqual((out["before"], out["after"], out["final"]), (OLD_TAGS[tag], "1.0.0", OLD_TAGS[tag]))
                self.assertEqual(out["apply"]["status"], "installed")
                self.assertTrue(out["apply"]["backupId"])
                self.assertEqual(tree_hashes(package), before)


class UnfinishedOldRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-old-run-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pinned_copy_answers_status_and_narrow_tool_points_to_it(self):
        engine = extract("aispa-0.5.0-engine", "skills/aispa", self.root / "old") / "tools/aispa.py"
        project = self.root / "project"
        project.mkdir()
        git("init", "-q", cwd=project)
        git(*GIT_USER, "commit", "-q", "--allow-empty", "-m", "start", cwd=project)
        init = python(engine, "--root", project, "init", "--slug", "demo", "--title", "Демо", "--mode", "auto",
                      "--runtime", "claude-code")
        self.assertEqual(init.returncode, 0, init.stderr[-2000:])
        run = json.loads((project / ".aispa/active.json").read_text(encoding="utf-8"))["run"]
        pinned = project / ".aispa/runs" / run
        journal = {name: hashlib.sha256((pinned / name).read_bytes()).hexdigest()
                   for name in ("events.jsonl", "journal-cache.json")}
        active, runtime = (project / ".aispa/active.json").read_bytes(), tree_hashes(pinned / "runtime")

        tool = narrow_package(self.root / "aispa") / "tools/aispa.py"
        for command in (["dashboard", "refresh-view", "--port", "8765"], ["status"]):
            with self.subTest(command=command):
                answer = python(tool, "--root", project, *command, cwd=project)
                self.assertNotEqual(answer.returncode, 0)
                self.assertIn(OLD_LINE, answer.stderr)

        status = python(".aispa/runs/" + run + "/runtime/tools/aispa.py", "status", cwd=project)
        self.assertEqual(status.returncode, 0, status.stderr[-2000:])
        self.assertEqual(json.loads(status.stdout)["status"], "running")
        self.assertEqual({name: hashlib.sha256((pinned / name).read_bytes()).hexdigest() for name in journal}, journal)
        self.assertEqual((project / ".aispa/active.json").read_bytes(), active)
        self.assertEqual(tree_hashes(pinned / "runtime"), runtime)
        self.assertFalse((project / ".aispa-work").exists())


if __name__ == "__main__":
    unittest.main()
