#!/usr/bin/env python3
"""Offline behavioral checks for the generated shared-project document index."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_index as pi


class ProjectIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="readback-index-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.docs = self.home / "documents"
        self.docs.mkdir()
        (self.home / "records").mkdir()
        self.write("documents/api.md", "# API decisions\n\napi-service owns this endpoint.\n## Authentication\nToken lifetime decision.\n")
        self.write("documents/web.md", "# Web decisions\nweb-ui renders this screen.\n")
        self.write("documents/shared.md", "# Shared policy\nAll components follow this policy.\n")
        self.write("records/HND-20260101-01.md", "# Partial integration handoff\napi-service and web-ui integration.\n")
        self.roots = ["documents", "records"]
        self.repos = ["api-service", "web-ui"]

    def write(self, path, text):
        full = self.home / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        return full

    def build(self, **kwargs):
        return pi.build(self.home, self.roots, self.repos, **kwargs)

    def paths(self, result):
        return {entry["path"] for entry in result["results"]}

    def test_deterministic_catalog_and_overview(self):
        result = self.build()
        self.assertEqual(result["documents"], 4)
        catalog = (self.home / ".readback/project-index.json").read_bytes()
        overview = (self.home / "PROJECT-INDEX.md").read_bytes()
        self.build()
        self.assertEqual(catalog, (self.home / ".readback/project-index.json").read_bytes())
        self.assertEqual(overview, (self.home / "PROJECT-INDEX.md").read_bytes())
        self.assertLess(len(overview.splitlines()), 60)
        self.assertEqual(pi.check(self.home)["status"], "FRESH")

    def test_paths_hashes_headings_and_exact_repo_mentions(self):
        self.build()
        result = pi.query(self.home, repo="api-service")
        entry = next(entry for entry in result["results"] if entry["path"] == "documents/api.md")
        self.assertEqual(entry["sha256"], hashlib.sha256((self.docs / "api.md").read_bytes()).hexdigest())
        self.assertEqual(entry["title"], "API decisions")
        self.assertEqual(entry["line"], 1)
        self.assertEqual(entry["headings"][1], {"line": 4, "text": "Authentication"})
        self.assertEqual(entry["repo_mentions"], ["api-service"])
        self.assertEqual(result["authority"], "discovery_only")
        self.assertIn("discovery hints", result["scope_note"])

    def test_repo_query_includes_unclassified_and_cross_repo(self):
        self.build()
        paths = self.paths(pi.query(self.home, repo="api-service"))
        self.assertEqual(paths, {"documents/api.md", "documents/shared.md", "documents/web.md", "records/HND-20260101-01.md"})
        self.assertIn("documents/shared.md", self.paths(pi.query(self.home, repo="web-ui")))
        self.assertIn("documents/api.md", self.paths(pi.query(self.home, repo="web-ui")))

    def test_repo_name_boundaries_do_not_infer_ownership(self):
        self.write("documents/boundaries.md", "# Examples\nx-api-service-v2 and API-SERVICE are different text.\n")
        self.build()
        entry = next(entry for entry in pi.query(self.home)["results"] if entry["path"].endswith("boundaries.md"))
        self.assertEqual(entry["repo_mentions"], [])
        self.assertEqual(entry["scope_hint"], "shared_or_unclassified")

    def test_text_query_returns_actual_match_line(self):
        self.build()
        result = pi.query(self.home, repo="api-service", text="LIFETIME")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["match_line"], 5)
        self.assertEqual(pi.query(self.home, text="unknown phrase")["total"], 0)

    def test_path_query_and_result_limit_are_explicit(self):
        self.build()
        result = pi.query(self.home, limit=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(pi.query(self.home, text="documents/api.md")["total"], 1)

    def test_repo_mentions_rank_without_filtering_and_counts_show_omissions(self):
        self.build()
        first = pi.query(self.home, repo="api-service", limit=1)
        self.assertEqual(first["results"][0]["path"], "documents/api.md")
        self.assertEqual(first["class_counts"], {
            "requested_repo_mentions": {"total": 2, "returned": 1},
            "no_repo_mentions": {"total": 1, "returned": 0},
            "other_repo_mentions": {"total": 1, "returned": 0},
        })
        self.assertEqual(first["next_offset"], 1)
        second = pi.query(self.home, repo="api-service", limit=3, offset=1)
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(self.paths(first) | self.paths(second), self.paths(pi.query(self.home)))
        self.assertIsNone(second["next_offset"])
        with self.assertRaises(pi.IndexError):
            pi.query(self.home, offset=-1)

    def test_stale_query_uses_current_documents_without_writing_cache(self):
        self.build()
        catalog_path = self.home / ".readback/project-index.json"
        overview_path = self.home / "PROJECT-INDEX.md"
        before = (catalog_path.read_bytes(), overview_path.read_bytes(), catalog_path.stat().st_mtime_ns)
        self.write("documents/new.md", "# Newly discovered policy\nApplies to every repository.\n")
        result = pi.query(self.home, repo="web-ui", text="Newly discovered")
        self.assertEqual(result["status"], "FRESH")
        self.assertTrue(result["stale_cache"])
        self.assertEqual(self.paths(result), {"documents/new.md"})
        self.assertEqual(before, (catalog_path.read_bytes(), overview_path.read_bytes(), catalog_path.stat().st_mtime_ns))

    def test_stale_after_add_change_and_delete(self):
        for mutation, expected in [(lambda: self.write("documents/new.md", "# New\n"), "added"),
                                   (lambda: self.write("documents/api.md", "# Changed\n"), "changed"),
                                   (lambda: (self.docs / "api.md").unlink(), "removed")]:
            self.build()
            mutation()
            result = pi.query(self.home, repo="web-ui")
            self.assertEqual(result["status"], "FRESH")
            self.assertTrue(result["stale_cache"])
            self.assertTrue(result["cache_changes"][expected])
            self.assertIn("results", result)
            self.assertEqual(pi.check(self.home)["status"], "STALE")

    def test_added_excluded_child_is_reported_without_reading(self):
        self.build()
        self.write("documents/new.py", "raise RuntimeError('never execute')")
        self.assertEqual(pi.check(self.home)["status"], "STALE")
        result = self.build()
        self.assertIn({"path": "documents/new.py", "reason": "unsupported_extension"}, result["skipped"])

    def test_hidden_outputs_code_credentials_and_archive_are_not_read(self):
        fixtures = {
            "documents/.hidden.md": "never read", "documents/.git/config": "never read",
            "documents/outputs/report.md": "never read", "documents/archive/old.md": "never read",
            "documents/passwords.json": "never read", "documents/credentials/data.md": "never read",
            "documents/evidence.json": "never read", "documents/script.py": "never read", "documents/node_modules/readme.md": "never read",
        }
        for path, text in fixtures.items():
            self.write(path, text)
        opened = []
        original = pi.read_regular
        def observed(parent, name, max_bytes):
            opened.append(name)
            return original(parent, name, max_bytes)
        with mock.patch.object(pi, "read_regular", side_effect=observed):
            result = self.build()
        self.assertEqual(result["documents"], 4)
        self.assertEqual(result["skipped_count"], len(fixtures))
        for forbidden in [".hidden.md", "config", "report.md", "old.md", "passwords.json", "data.md", "evidence.json", "script.py", "readme.md"]:
            self.assertNotIn(forbidden, opened)

    def test_symlink_file_and_directory_are_skipped_without_traversal(self):
        external = self.base / "outside"
        external.mkdir()
        (external / "private.md").write_text("# Never read\n")
        (self.docs / "linked").symlink_to(external, target_is_directory=True)
        (self.docs / "linked.md").symlink_to(external / "private.md")
        result = self.build()
        self.assertEqual(result["documents"], 4)
        self.assertEqual([entry["reason"] for entry in result["skipped"]], ["symlink", "symlink"])

    def test_symlink_root_home_and_ancestor_are_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError):
            pi.build(alias, self.roots, self.repos)
        linked = self.home / "linked"
        linked.symlink_to(self.docs, target_is_directory=True)
        with self.assertRaises(OSError):
            pi.build(self.home, ["linked"], self.repos)
        with self.assertRaises(OSError):
            pi.build(self.base / "alias" / "documents", ["nested"], self.repos)

    def test_symlink_output_directory_does_not_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / ".readback").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.build()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_output_files_are_not_overwritten(self):
        outside = self.base / "outside.md"
        outside.write_text("original")
        (self.home / "PROJECT-INDEX.md").symlink_to(outside)
        with self.assertRaises(pi.IndexError):
            self.build()
        self.assertEqual(outside.read_text(), "original")
        self.assertFalse((self.home / ".readback/project-index.json").exists())
        (self.home / "PROJECT-INDEX.md").unlink()
        (self.home / ".readback/project-index.json").symlink_to(outside)
        with self.assertRaises(pi.IndexError):
            self.build()
        self.assertEqual(outside.read_text(), "original")

    def test_hardlinked_input_is_skipped_and_output_is_rejected(self):
        os.link(self.docs / "api.md", self.docs / "duplicate.md")
        result = self.build()
        self.assertEqual(result["documents"], 3)
        self.assertEqual(sum(entry["reason"] == "hardlink" for entry in result["skipped"]), 2)
        os.link(self.home / "PROJECT-INDEX.md", self.base / "duplicate-overview.md")
        with self.assertRaises(pi.IndexError):
            self.build()

    def test_oversized_binary_and_invalid_encoding_are_skipped(self):
        self.write("documents/large.md", "x" * 1025)
        (self.docs / "binary.md").write_bytes(b"hello\0world")
        (self.docs / "invalid.md").write_bytes(b"\xff\xfe")
        result = self.build(max_bytes=1024)
        self.assertEqual(result["documents"], 4)
        self.assertEqual({entry["reason"] for entry in result["skipped"]}, {"size_limit", "binary", "non_utf8"})

    def test_invalid_roots_and_repo_names_fail(self):
        for roots in [["../outside"], [str(self.docs)], ["documents/../records"], ["documents", "documents/nested"],
                      ["documents", "documents"], [".git"], ["outputs"], ["credentials"], ["documents//nested"]]:
            with self.subTest(roots=roots), self.assertRaises(pi.IndexError):
                pi.build(self.home, roots, self.repos)
        for repos in [["bad/name"], ["api-service", "api-service"], [""], ["../outside"]]:
            with self.subTest(repos=repos), self.assertRaises(pi.IndexError):
                pi.build(self.home, self.roots, repos)

    def test_tampered_catalog_cannot_supply_false_hints(self):
        self.build()
        path = self.home / ".readback/project-index.json"
        catalog = json.loads(path.read_text())
        catalog["entries"][0]["title"] = "Fabricated title"
        path.write_bytes(pi.canonical_bytes(catalog))
        fresh = pi.query(self.home)
        self.assertTrue(fresh["stale_cache"])
        self.assertNotIn("Fabricated title", [entry["title"] for entry in fresh["results"]])
        catalog["config"]["roots"] = ["../outside"]
        path.write_bytes(pi.canonical_bytes(catalog))
        with self.assertRaises(pi.IndexError):
            pi.query(self.home)

    def test_duplicate_keys_and_unsupported_versions_fail(self):
        self.build()
        path = self.home / ".readback/project-index.json"
        catalog = path.read_text()
        path.write_text(catalog.replace('"index_version": 1,', '"index_version": 1, "index_version": 1,'))
        with self.assertRaises(pi.IndexError):
            pi.query(self.home)
        path.write_text(catalog.replace('"index_version": 1,', '"index_version": 99,'))
        with self.assertRaises(pi.IndexError):
            pi.query(self.home)

    def test_unicode_paths_and_markdown_heading_escape(self):
        self.write("documents/café [sample].md", "# Brackets [demo] | sample\nShared example.\n")
        self.build()
        result = pi.query(self.home, text="Shared example")
        self.assertEqual(result["results"][0]["path"], "documents/café [sample].md")
        overview = (self.home / "PROJECT-INDEX.md").read_text()
        self.assertIn("caf%C3%A9%20%5Bsample%5D.md", overview)
        self.assertIn("\\[demo\\] \\|", overview)

    def test_cli_success_staleness_and_invalid_input_codes(self):
        script = ROOT / "scripts/project_index.py"
        def run(*args):
            return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, timeout=15)
        built = run("build", "--home", self.home, "--roots", *self.roots, "--repos", *self.repos)
        self.assertEqual(built.returncode, 0, built.stderr)
        checked = run("check", "--home", self.home)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        queried = run("query", "--home", self.home, "--repo", "api-service", "--text", "Authentication")
        self.assertEqual(queried.returncode, 0, queried.stderr)
        self.assertEqual(json.loads(queried.stdout)["total"], 1)
        self.write("documents/new.md", "# New\n")
        stale = run("query", "--home", self.home)
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertTrue(json.loads(stale.stdout)["stale_cache"])
        stale_check = run("check", "--home", self.home)
        self.assertEqual(stale_check.returncode, 2, stale_check.stderr)
        self.assertEqual(json.loads(stale_check.stdout)["status"], "STALE")
        invalid = run("query", "--home", self.home, "--repo", "unknown")
        self.assertEqual(invalid.returncode, 3)
        self.assertEqual(json.loads(invalid.stderr)["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
