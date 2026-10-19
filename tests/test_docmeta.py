import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import docmeta
from aispa_core.store import Invalid, Journal


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class DocumentMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-docmeta-")
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.write(".gitignore", b".aispa/\nignored/\n")
        self.path = self.write("README.md", "# Проект 🎈\n\nТочное описание исходника.\n".encode())
        self.store = Journal(self.root)
        self.state = self.store.create("example", "Metadata fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True).stdout

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def register(self, **kwargs):
        return docmeta.register(self.store, self.state, "README.md", **kwargs)

    def row(self):
        return docmeta.catalog(self.store, self.state)["documents"][0]

    def test_render_is_idempotent_and_exact_body_hash_handles_crlf_quotes_unicode(self):
        body = '# Привет 🎈\r\n\r\nText with "quotes": *alias # comment\r\n'
        summary = 'Escapes "colon: quote", \\ and\nline 🎈\u0085NEL\u2028LS\u2029PS'
        rendered = docmeta.render(body, kind="document", summary=summary)
        metadata, extracted = docmeta._header(rendered)
        self.assertEqual(extracted, body)
        self.assertEqual(metadata["summary"], summary)
        self.assertEqual(metadata["bodySha256"], sha(body.encode()))
        self.assertEqual(docmeta.render(rendered, kind="document", summary=summary), rendered)
        self.assertEqual(rendered.count("\naispa:\n"), 1)
        self.assertIn("🎈", rendered)
        self.assertNotIn("\\ud83c", rendered)
        self.assertIn("\\u0085NEL\\u2028LS\\u2029PS", rendered)
        for field in ("kind", "summary", "bodySha256"):
            line = next(line for line in rendered.splitlines() if line.startswith("  " + field + ":"))
            self.assertIsInstance(json.loads(line.split(": ", 1)[1]), str)
        changed = rendered.replace("Text with", "Body with")
        refreshed = docmeta.render(changed, kind="document", summary=summary)
        self.assertEqual(docmeta._header(refreshed)[0]["bodySha256"], sha(docmeta._header(changed)[1].encode()))

    def test_register_is_read_only_reuses_exact_record_and_does_not_append_journal(self):
        source, journal = self.path.read_bytes(), (self.store.run / "events.jsonl").read_bytes()
        first = self.register()
        second = self.register()
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(list((self.store.run / "documents").iterdir())), 1)
        self.assertEqual(self.path.read_bytes(), source)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), journal)
        row = self.row()
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["currentSha256"], sha(source))
        self.assertIn("Точное описание", row["summary"])
        self.assertEqual(row["summaryOrigin"], "extractive")

    def test_semantic_summary_requires_exact_hash_and_survives_unchanged_collection(self):
        original = self.path.read_bytes()
        for kwargs in ({"summary": "Agent description"}, {"write_header": True},
                       {"summary": "Agent description", "expected_sha256": "0" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Invalid):
                self.register(**kwargs)
        receipt = self.register(summary="Agent description", expected_sha256=sha(original))
        result = docmeta.collect_sources(self.store, self.state, [{"path": "README.md", "sha256": sha(original), "excerpt": "IGNORED"}])
        self.assertEqual(result["registered"][0]["artifact"], receipt["artifact"])
        self.assertEqual(self.row()["summary"], "Agent description")
        self.assertNotIn("IGNORED", self.store.text(receipt["artifact"]))

    def test_same_length_edit_suppresses_summary_and_refresh_drops_semantic_trust(self):
        self.write("README.md", b"# One\n\nOriginal paragraph.\n")
        old = self.register(summary="Old semantic description", expected_sha256=sha(self.path.read_bytes()))
        self.path.write_bytes(b"# Two\n\nReplaced paragraph.\n")
        row = self.row()
        self.assertEqual(row["status"], "stale")
        self.assertNotIn("summary", row)
        self.assertEqual(row["artifact"], old["artifact"])
        before = self.path.read_bytes()
        result = docmeta.refresh(self.store, self.state)
        self.assertEqual(len(result["updated"]), 1)
        self.assertEqual(self.row()["summaryOrigin"], "extractive")
        self.assertIn("Replaced", self.row()["summary"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.store.run / old["artifact"]["file"]).exists())

    def test_inline_header_preserves_crlf_body_mode_and_updates_exact_file_sha(self):
        body = b"# Doc\r\n\r\nExact body.\r\n"
        self.path.write_bytes(body)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        result = self.register(write_header=True, expected_sha256=sha(body))
        self.assertTrue(result["headerWritten"])
        raw = self.path.read_bytes()
        header, extracted = docmeta._header(raw.decode())
        self.assertEqual(extracted.encode(), body)
        self.assertEqual(header["bodySha256"], sha(body))
        self.assertEqual(result["sourceSha256"], sha(raw))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)
        self.assertTrue(self.row()["header"]["bodyMatches"])
        again = self.register(write_header=True, expected_sha256=sha(raw))
        self.assertFalse(again["headerWritten"])
        self.assertTrue(again["reused"])

    def test_owned_crlf_and_mixed_header_refresh_preserves_exact_body(self):
        body = "# Windows document\n\nBody line.\n"
        rendered = docmeta.render(body, kind="document", summary="Original summary")
        for content in (rendered.replace("\n", "\r\n"),
                        rendered.replace("aispa:\n", "aispa:\r\n").replace("---\n", "---\r\n")):
            with self.subTest(content=content):
                raw = content.encode()
                self.path.write_bytes(raw)
                original_header, original_body = docmeta._header(content)
                self.assertIsNotNone(original_header)
                self.register()
                row = self.row()
                self.assertTrue(row["header"]["owned"])
                self.assertEqual(row["header"]["bodyMatches"], original_body == body)
                result = self.register(write_header=True, expected_sha256=sha(raw))
                self.assertTrue(result["headerWritten"])
                updated = self.path.read_bytes()
                header, exact_body = docmeta._header(updated.decode())
                self.assertEqual(exact_body, original_body)
                self.assertEqual(header["bodySha256"], sha(original_body.encode()))
                self.assertEqual(updated.count(b"aispa:"), 1)
                self.assertTrue(self.row()["header"]["bodyMatches"])
                self.assertFalse(self.row()["header"]["needsRefresh"])
        foreign = rendered.replace("\n---\n", "\n  extra: keep\n---\n").replace("\n", "\r\n")
        self.path.write_bytes(foreign.encode())
        self.register()
        self.assertFalse(self.row()["header"]["owned"])
        with self.assertRaises(Invalid):
            self.register(write_header=True, expected_sha256=sha(foreign.encode()))
        self.assertEqual(self.path.read_bytes(), foreign.encode())

    def test_semantic_sidecar_change_marks_owned_header_stale_until_explicit_write(self):
        initial = self.path.read_bytes()
        self.register(summary="Original semantic summary", write_header=True, expected_sha256=sha(initial))
        inline = self.path.read_bytes()
        self.register(summary="Revised semantic summary", expected_sha256=sha(inline))
        row = self.row()
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["summary"], "Revised semantic summary")
        self.assertTrue(row["header"]["bodyMatches"])
        self.assertFalse(row["header"]["summaryMatches"])
        self.assertTrue(row["header"]["needsRefresh"])
        self.assertEqual(self.path.read_bytes(), inline)
        self.register(write_header=True, expected_sha256=sha(inline))
        row = self.row()
        self.assertTrue(row["header"]["bodyMatches"])
        self.assertTrue(row["header"]["summaryMatches"])
        self.assertFalse(row["header"]["needsRefresh"])
        self.assertEqual(docmeta._header(self.path.read_bytes().decode())[0]["summary"], "Revised semantic summary")
        self.assertEqual(docmeta._header(self.path.read_bytes().decode())[1].encode(), initial)

    def test_unrelated_frontmatter_bom_and_nonmarkdown_are_sidecar_only(self):
        for raw in (b"---\nexisting: keep\n---\n# Body\n", b"---\r\nexisting: keep\r\n---\r\n# Body\r\n",
                    b"\xef\xbb\xbf# BOM document\r\n", b"---\naispa:\n  schema: 2\n---\n# Future\n"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.register()
                with self.assertRaises(Invalid):
                    self.register(write_header=True, expected_sha256=sha(raw))
                self.assertEqual(self.path.read_bytes(), raw)
        code = self.write("src/main.py", b"print('hello')\n")
        docmeta.register(self.store, self.state, "src/main.py")
        with self.assertRaises(Invalid):
            docmeta.register(self.store, self.state, "src/main.py", write_header=True, expected_sha256=sha(code.read_bytes()))
        self.assertEqual(code.read_bytes(), b"print('hello')\n")

    def test_readonly_hardlink_and_late_source_changes_cannot_be_overwritten(self):
        raw = self.path.read_bytes()
        self.path.chmod(stat.S_IREAD)
        try:
            self.register()
            with self.assertRaises(Invalid):
                self.register(write_header=True, expected_sha256=sha(raw))
        finally:
            self.path.chmod(stat.S_IREAD | stat.S_IWRITE)
        linked = self.root / "copy.md"
        os.link(self.path, linked)
        try:
            with self.assertRaises(Invalid):
                self.register(write_header=True, expected_sha256=sha(raw))
            self.assertEqual(linked.read_bytes(), raw)
        finally:
            linked.unlink()
        original = docmeta._source
        calls = 0
        def changing(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.path.write_bytes(b"Concurrent edit must survive.\n")
            return original(*args, **kwargs)
        with patch.object(docmeta, "_source", side_effect=changing), self.assertRaises(Invalid):
            self.register(write_header=True, expected_sha256=sha(raw))
        self.assertEqual(self.path.read_bytes(), b"Concurrent edit must survive.\n")
        self.assertEqual(list(self.root.glob(".aispa-document-*")), [])

    def test_paths_secret_names_ignored_files_and_links_never_read(self):
        blocked = [".env", "docs/passwords.md", "private/README.md", "ignored/README.md", "archive/README.md"]
        for name in blocked:
            self.write(name, b"PRIVATE_SENTINEL")
            self.git("add", "-f", name)
        original = os.open
        forbidden = {self.root / name for name in blocked}
        def guarded(path, *args, **kwargs):
            self.assertNotIn(Path(path), forbidden)
            return original(path, *args, **kwargs)
        with patch.object(os, "open", guarded):
            for name in blocked + ["../README.md", "C:/README.md", "x\\README.md", "NUL.md"]:
                with self.subTest(name=name), self.assertRaises(Invalid):
                    docmeta.register(self.store, self.state, name)
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "README.md").write_bytes(b"EXTERNAL_SENTINEL")
            (self.root / "linked.md").symlink_to(outside / "README.md")
            with self.assertRaises(Invalid):
                docmeta.register(self.store, self.state, "linked.md")
            (self.root / "doc").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(Invalid):
                docmeta.register(self.store, self.state, "doc/README.md")

    @unittest.skipUnless(os.name == "nt", "Native Windows junction")
    def test_windows_junction_source_cannot_be_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "README.md").write_bytes(b"EXTERNAL_SENTINEL")
            junction = self.root / "doc"
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, check=True)
            try:
                with self.assertRaises(Invalid):
                    docmeta.register(self.store, self.state, "doc/README.md")
            finally:
                junction.rmdir()

    def test_changed_to_secret_missing_or_ignored_suppresses_old_summary(self):
        self.register(summary="OLD_ADVISORY", expected_sha256=sha(self.path.read_bytes()))
        secret = "github_pat_" + "TESTVALUE0123456789ABCDEFGHIJ"
        self.path.write_bytes(("# Sensitive\n" + secret).encode())
        view = docmeta.catalog(self.store, self.state)
        self.assertEqual(view["documents"][0]["status"], "unavailable")
        self.assertNotIn("OLD_ADVISORY", json.dumps(view))
        self.assertNotIn(secret, json.dumps(view))
        self.assertEqual(docmeta.refresh(self.store, self.state)["updated"], [])
        self.path.unlink()
        self.assertEqual(self.row()["status"], "missing")
        self.write("README.md", b"# Public again\n")
        self.write(".gitignore", b".aispa/\nREADME.md\n")
        self.assertEqual(self.row()["status"], "unavailable")

    def test_tampered_artifact_is_not_reused_or_repaired_silently(self):
        receipt = self.register()
        artifact = self.store.run / receipt["artifact"]["file"]
        artifact.write_bytes(artifact.read_bytes() + b" ")
        self.assertEqual(self.row()["status"], "unavailable")
        self.assertNotIn("summary", self.row())
        with self.assertRaises(Invalid):
            self.register()
        self.assertEqual(docmeta.refresh(self.store, self.state)["updated"], [])

    def test_canonical_immutable_sources_are_cataloged_but_never_edited(self):
        raw = b"# Owner brief\n\nExact original requirement.\n"
        ref = self.store.artifact("briefs", raw, ".md")
        self.state["briefs"].append(dict(ref, id="B001"))
        path = (self.store.run / ref["file"]).relative_to(self.root).as_posix()
        receipt = docmeta.register(self.store, self.state, path)
        saved = json.loads(self.store.text(receipt["artifact"]))
        self.assertEqual(saved["sourceArtifact"], ref)
        with self.assertRaises(Invalid):
            docmeta.register(self.store, self.state, path, write_header=True, expected_sha256=sha(raw))
        self.assertEqual((self.store.run / ref["file"]).read_bytes(), raw)
        (self.store.run / ref["file"]).write_bytes(b"changed")
        self.assertEqual(docmeta.catalog(self.store, self.state)["documents"][0]["status"], "unavailable")
        arbitrary = self.write(".aispa/arbitrary.md", b"# Not canonical\n")
        with self.assertRaises(Invalid):
            docmeta.register(self.store, self.state, arbitrary.relative_to(self.root).as_posix())

    def test_bounded_catalog_reports_omissions_and_explicit_paths_can_reach_later_entries(self):
        for index in range(5):
            path = f"docs/{index}.md"
            self.write(path, ("# Title\n\n" + "long description " * 100).encode())
            docmeta.register(self.store, self.state, path)
        with patch.object(docmeta, "MAX_DOCUMENTS", 2):
            view = docmeta.catalog(self.store, self.state)
            self.assertEqual(len(view["documents"]), 2)
            self.assertEqual(view["omitted"]["count"], 3)
            selected = docmeta.catalog(self.store, self.state, paths=["docs/4.md"])
            self.assertEqual(selected["documents"][0]["status"], "fresh")
            self.assertEqual(selected["omitted"]["unselected"], 4)
        self.assertGreater(selected["documents"][0]["summaryOmittedChars"], 0)
        with patch.object(docmeta, "MAX_CATALOG_CHARS", 1):
            view = docmeta.catalog(self.store, self.state)
            self.assertEqual(view["documents"], [])
            self.assertEqual(view["omitted"]["count"], 5)

    def test_batch_operations_share_one_git_snapshot_and_refresh_later_sources(self):
        sources = []
        for index in range(5):
            name = f"docs/{index}.md"
            raw = b"# Previous\n"
            self.write(name, raw)
            sources.append({"path": name, "sha256": sha(raw)})
        from aispa_core import discovery
        with patch.object(discovery, "_names", wraps=discovery._names) as names:
            docmeta.collect_sources(self.store, self.state, sources)
            self.assertEqual(names.call_count, 1)
        for row in sources:
            self.write(row["path"], b"# Changed!\n")
        with patch.object(discovery, "_names", wraps=discovery._names) as names, patch.object(docmeta, "MAX_DOCUMENTS", 2):
            result = docmeta.refresh(self.store, self.state)
            self.assertEqual(names.call_count, 1)
            self.assertEqual(result["updatedCount"], 5)
            self.assertEqual(len(result["updated"]), 2)
            self.assertEqual(result["omitted"]["count"], 3)
        with patch.object(discovery, "_names", wraps=discovery._names) as names:
            view = docmeta.catalog(self.store, self.state)
            self.assertEqual(names.call_count, 1)
            self.assertEqual({row["status"] for row in view["documents"]}, {"fresh"})

    def test_empty_views_avoid_git_and_targeted_refresh_leaves_other_source_stale(self):
        from aispa_core import discovery
        with patch.object(discovery, "_names", side_effect=AssertionError("Unnecessary Git probe")):
            self.assertEqual(docmeta.catalog(self.store, self.state)["documents"], [])
            self.assertEqual(docmeta.refresh(self.store, self.state)["updated"], [])
            self.assertEqual(docmeta.collect_sources(self.store, self.state, [])["registered"], [])
        self.register()
        self.write("docs/other.md", b"# Other\n")
        docmeta.register(self.store, self.state, "docs/other.md")
        self.path.write_bytes(b"# Changed\n")
        self.write("docs/other.md", b"# Changed other\n")
        result = docmeta.refresh(self.store, self.state, paths=["README.md"])
        self.assertEqual(result["updatedCount"], 1)
        self.assertEqual(result["omitted"]["unselected"], 1)
        rows = {row["path"]: row for row in docmeta.catalog(self.store, self.state)["documents"]}
        self.assertEqual(rows["README.md"]["status"], "fresh")
        self.assertEqual(rows["docs/other.md"]["status"], "stale")
        with patch.object(discovery, "_names", side_effect=AssertionError("Unnecessary Git probe")):
            self.assertEqual(docmeta.catalog(self.store, self.state, paths=[])["documents"], [])
            self.assertEqual(docmeta.refresh(self.store, self.state, paths=[])["updated"], [])

    def test_collection_hash_cas_omissions_invalid_utf8_and_byte_cap(self):
        result = docmeta.collect_sources(self.store, self.state, [{"path": "README.md", "sha256": "0" * 64},
                                                               {"path": "../private", "sha256": "0" * 64}])
        self.assertEqual(result["registered"], [])
        self.assertEqual(result["omitted"]["count"], 2)
        self.assertIsNone(result["omitted"]["details"][1]["path"])
        self.path.write_bytes(b"\xff invalid UTF8")
        with self.assertRaises(Invalid):
            self.register()
        self.path.write_bytes(b"a" * (docmeta.MAX_FILE_BYTES + 1))
        with self.assertRaises(Invalid):
            self.register()
        self.path.write_bytes(b"binary\0content")
        with self.assertRaises(Invalid):
            self.register()


if __name__ == "__main__":
    unittest.main()
