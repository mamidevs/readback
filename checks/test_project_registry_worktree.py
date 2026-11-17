#!/usr/bin/env python3
"""Checks for the legacy fallback name of a linked Git worktree (CON-20261118-12).

Run: python3 -B checks/test_project_registry_worktree.py LegacyWorktreeTests UnchangedTests
The resolver at the base commit is loaded from Git and run on the same fixtures.
Fixtures are real repositories in temporary folders; no provider is contacted.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_registry as pr

BASE_COMMIT = "e49f439fc310df5cc8f0eb8f56e3d40b7b586aee"
REAL_RUN = subprocess.run
GIT_PREFIX_TAIL = ["--no-optional-locks", "-c", "core.fsmonitor=false", "-C"]


def load_base():
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = REAL_RUN(["git", "-C", str(ROOT), "show", BASE_COMMIT + ":scripts/project_registry.py"],
                      capture_output=True, env=environment, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    module = types.ModuleType("project_registry_base")
    module.__file__ = "project_registry_base.py"
    exec(compile(result.stdout, "project_registry_base.py", "exec"), module.__dict__)
    return module


base_pr = load_base()


def uid():
    return str(uuid.uuid4())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def outcome(module, registry, cwd, host="codex", inspect=True, legacy=True, logbook=None):
    """The result or the error of one resolver; error classes differ per module, so compare code and text."""
    try:
        return ("ok", module.resolve_project(registry, cwd, host, inspect_staged=inspect,
                                             logbook=logbook, legacy_fallback=legacy))
    except module.RegistryError as exc:
        return ("err", exc.code, str(exc))


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="readback-p1a-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.logbook = self.base / "logbook"
        self.logbook.mkdir()
        self.registry = self.logbook / ".readback" / "projects.json"
        self.registry.parent.mkdir(mode=0o700)
        pr.init_registry(self.registry)

    def git(self, path, *arguments):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = REAL_RUN(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false", "-c", "user.name=Fixture Author",
             "-c", "user.email=fixture@example.invalid", "-C", str(path), *arguments],
            env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def repo(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.git(path, "commit", "--allow-empty", "-q", "-m", "Synthetic fixture")
        return path

    def linked(self, repo, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", "--detach", "--quiet", str(path), "HEAD")
        return path

    def separate_repo(self, main, store):
        main.parent.mkdir(parents=True, exist_ok=True)
        self.git(main.parent, "init", "-q", "--separate-git-dir", str(store), str(main))
        self.git(main, "commit", "--allow-empty", "-q", "-m", "Synthetic fixture")
        return main

    def bare_repo(self, source, path):
        self.git(source.parent, "clone", "-q", "--bare", str(source), str(path))
        return path

    def identity(self, path):
        info = Path(path).stat()
        return {"device": info.st_dev, "inode": info.st_ino}

    def project(self, home, repo, state="STAGED", retired=()):
        """Register one repository with a center at home; the registry is applied and returned."""
        home.mkdir(parents=True)
        profile = {"profile_version": 1, "profile_id": "sample-v1", "protocol_version": "0.7",
                   "record_types": ["DEC", "CON", "HND", "LOG"], "core_language": "en",
                   "extension_namespace": "sample", "extensions": {"sample": {}}}
        write_json(home / "profile.json", profile)
        digest = hashlib.sha256((home / "profile.json").read_bytes()).hexdigest()
        project_uuid, home_uuid = uid(), uid()
        write_json(home / "project.json", {
            "project_version": 1, "project_uuid": project_uuid, "home_uuid": home_uuid,
            "protocol_version": "0.7", "profile_version": 1,
            "profile_sha256": digest, "activation_state": "STAGED"})
        common = Path(self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        entry = {"project_uuid": project_uuid, "slug": "sample-project",
                 "home": str(home), "home_identity": self.identity(home), "home_uuid": home_uuid,
                 "protocol_version": "0.7", "profile_version": 1, "activation_state": state,
                 "profile_sha256": digest, "repositories": [{
                     "repo_uuid": uid(), "root": str(repo), "root_identity": self.identity(repo),
                     "git_common_dir": str(common), "git_common_dir_identity": self.identity(common),
                     "worktrees": []}], "retired_roots": [str(x) for x in retired]}
        if state == "ACTIVE":
            raise AssertionError("ACTIVE fixtures are not needed here")
        pr.apply_registry(self.registry, {"registry_version": 1, "generation": 0, "projects": [entry]}, 0)
        return entry

    def compare(self, cwd, **keywords):
        """Run the resolver at the base commit and the current one; require equal outcomes."""
        keywords.setdefault("logbook", self.logbook)
        new = outcome(pr, self.registry, cwd, **keywords)
        old = outcome(base_pr, self.registry, cwd, **keywords)
        self.assertEqual(new, old)
        return old


NAMES = ["plain-main", "dépôt principal "]  # the second has a space, a non-ASCII letter and a trailing space


class LegacyWorktreeTests(Fixtures):
    def expected(self, old, main):
        home = (self.logbook / main.name).resolve()
        return dict(old, home=str(home), log_path=str(home / "LOG.md"), current_path=str(home / "CURRENT.md"),
                    records_path=str(home / "records"))

    def test_linked_worktree_of_ordinary_repository_gets_main_name(self):
        for index, name in enumerate(NAMES):
            main = self.repo(self.base / ("r%d" % index) / name)
            linked = self.linked(main, self.base / ("w%d" % index) / "feature branch é")
            nested = linked / "src" / "nested folder"
            nested.mkdir(parents=True)
            for host in ["codex", "claude"]:
                for location in [linked, nested]:
                    old = base_pr.resolve_project(self.registry, location, host, inspect_staged=True,
                                                  logbook=self.logbook, legacy_fallback=True)
                    new = pr.resolve_project(self.registry, location, host, inspect_staged=True,
                                             logbook=self.logbook, legacy_fallback=True)
                    # The base resolver names the linked folder; the change must differ from it.
                    self.assertEqual(old["home"], str((self.logbook / linked.name).resolve()))
                    self.assertEqual(new["status"], "LEGACY_FALLBACK")
                    self.assertEqual(new["host"], host)
                    home = str((self.logbook / name).resolve())
                    self.assertEqual(new["home"], home)
                    self.assertEqual(new["log_path"], home + "/LOG.md")
                    self.assertEqual(new["current_path"], home + "/CURRENT.md")
                    self.assertEqual(new["records_path"], home + "/records")
                    self.assertEqual(new["worktree_root"], str(linked.resolve()))
                    self.assertEqual(new["worktree_root"], old["worktree_root"])
                    self.assertEqual(new["git_common_dir"], str((main / ".git").resolve()))
                    self.assertEqual(new["git_common_dir"], old["git_common_dir"])
                    self.assertEqual(new["cwd"], str(location.resolve()))
                    self.assertEqual(new, self.expected(old, main))

    def guard_fixture(self, name):
        """An unrelated registered project, a main worktree named name and its linked worktree."""
        other = self.repo(self.base / "other-repository")
        main = self.repo(self.base / "repositories" / name)
        linked = self.linked(main, self.base / "worktrees" / "linked-folder")
        return other, main, linked

    def test_destination_of_main_name_in_reserved_path_gives_reserved_path(self):
        other, main, linked = self.guard_fixture("main-reserved")
        # The center sits below the main name, so the destination is an ancestor of a reserved home.
        self.project(self.logbook / "main-reserved" / "project-home", other)
        base_main = outcome(base_pr, self.registry, main, logbook=self.logbook)
        self.assertEqual(base_main[:2], ("err", "RESERVED_PATH"))
        (linked / "sub").mkdir()
        # The linked name alone is free: the base resolver falls back without error.
        self.assertEqual(outcome(base_pr, self.registry, linked, logbook=self.logbook)[0], "ok")
        for host in ["codex", "claude"]:
            for location in [linked, linked / "sub"]:
                self.assertEqual(outcome(pr, self.registry, location, host, logbook=self.logbook), base_main)

    def test_destination_of_main_name_in_unregistered_center_gives_unregistered_center(self):
        other, main, linked = self.guard_fixture("main-center")
        write_json(self.logbook / "main-center" / "project.json", {
            "project_version": 1, "project_uuid": uid(), "home_uuid": uid(),
            "protocol_version": "0.7", "profile_version": 1,
            "profile_sha256": "0" * 64, "activation_state": "STAGED"})
        base_main = outcome(base_pr, self.registry, main, logbook=self.logbook)
        self.assertEqual(base_main[:2], ("err", "UNREGISTERED_CENTER"))
        self.assertEqual(outcome(base_pr, self.registry, linked, logbook=self.logbook)[0], "ok")
        for host in ["codex", "claude"]:
            self.assertEqual(outcome(pr, self.registry, linked, host, logbook=self.logbook), base_main)

    def test_destination_below_unregistered_center_of_a_parent_gives_unregistered_center(self):
        # The guard also looks at the ancestors of the destination.
        other, main, linked = self.guard_fixture("main-parent")
        write_json(self.logbook / "project.json", {"project_version": 1, "project_uuid": uid()})
        base_main = outcome(base_pr, self.registry, main, logbook=self.logbook)
        self.assertEqual(base_main[:2], ("err", "UNREGISTERED_CENTER"))
        self.assertEqual(outcome(pr, self.registry, linked, logbook=self.logbook), base_main)


class UnchangedTests(Fixtures):
    def test_comparison_rejects_a_difference(self):
        # On a linked worktree the two resolvers differ; the comparison must fail there.
        main = self.repo(self.base / "repositories" / "main-self-check")
        linked = self.linked(main, self.base / "worktrees" / "linked-self-check")
        with self.assertRaises(AssertionError):
            self.compare(linked)
        self.assertNotEqual(outcome(pr, self.registry, linked, logbook=self.logbook),
                            outcome(base_pr, self.registry, linked, logbook=self.logbook))

    def test_main_worktree(self):
        main = self.repo(self.base / "repositories" / "dépôt main ")
        nested = main / "a b"
        nested.mkdir()
        for host in ["codex", "claude"]:
            for location in [main, nested]:
                old = self.compare(location, host=host)
                self.assertEqual(old[0], "ok")
                self.assertEqual(old[1]["status"], "LEGACY_FALLBACK")
                self.assertEqual(old[1]["home"], str((self.logbook / "dépôt main ").resolve()))
                self.assertEqual(old[1]["worktree_root"], str(main.resolve()))

    def test_main_and_linked_worktree_of_repository_with_separate_git_directory(self):
        main = self.separate_repo(self.base / "sep" / "sep-main", self.base / "sep" / "store-dir")
        linked = self.linked(main, self.base / "sep-wt" / "sep-linked")
        listing = REAL_RUN(["git", "-C", str(main), "worktree", "list", "--porcelain"], capture_output=True,
                           text=True, check=True).stdout
        self.assertEqual(listing.splitlines()[0], "worktree " + str(self.base / "sep" / "store-dir"))
        for host in ["codex", "claude"]:
            old_main = self.compare(main, host=host)
            self.assertEqual(old_main[1]["home"], str(self.logbook / "sep-main"))
            old_linked = self.compare(linked, host=host)
            self.assertEqual(old_linked[1]["home"], str(self.logbook / "sep-linked"))
            self.assertEqual(old_linked[1]["worktree_root"], str(linked.resolve()))
            self.assertEqual(old_linked[1]["status"], "LEGACY_FALLBACK")
        self.compare(linked / ".")

    def test_folder_outside_git(self):
        folder = self.base / "outside git é"
        (folder / "nested").mkdir(parents=True)
        for host in ["codex", "claude"]:
            for location in [folder, folder / "nested"]:
                old = self.compare(location, host=host)
                self.assertEqual(old[1]["status"], "LEGACY_FALLBACK")
                self.assertEqual(old[1]["home"], str((self.logbook / location.name).resolve()))
                self.assertIsNone(old[1]["worktree_root"])

    def test_linked_worktree_of_bare_repository(self):
        source = self.repo(self.base / "source")
        bare = self.bare_repo(source, self.base / "bare-store" / "bare.git")
        linked = self.linked(bare, self.base / "bare-wt" / "bare-linked")
        for host in ["codex", "claude"]:
            old = self.compare(linked, host=host)
            self.assertEqual(old[1]["home"], str(self.logbook / "bare-linked"))
            self.assertEqual(old[1]["worktree_root"], str(linked.resolve()))
        self.compare(linked, inspect=False)

    def test_linked_worktree_of_bare_repository_with_a_dot_git_entry_in_the_bare_folder(self):
        # Only the bare mark of the first record can reject this main root: a .git entry exists there.
        source = self.repo(self.base / "source-dot")
        bare = self.bare_repo(source, self.base / "bare-dot" / "bare-dot.git")
        linked = self.linked(bare, self.base / "bare-dot-wt" / "bare-dot-linked")
        (bare / ".git").write_text("x")
        self.assertTrue(os.path.exists(bare / ".git"))
        first = REAL_RUN(["git", "-C", str(linked), "worktree", "list", "--porcelain"], capture_output=True,
                         text=True, check=True).stdout.split("\n\n")[0].splitlines()
        self.assertEqual(first, ["worktree " + str(bare), "bare"])
        old = self.compare(linked)
        self.assertEqual(old[1]["home"], str(self.logbook / "bare-dot-linked"))

    def test_main_worktree_named_like_the_linked_worktree_of_another_repository(self):
        first = self.repo(self.base / "first" / "first-main")
        self.linked(first, self.base / "wt" / "shared-name")
        second = self.repo(self.base / "second" / "shared-name")
        for host in ["codex", "claude"]:
            old = self.compare(second, host=host)
            self.assertEqual(old[1]["home"], str(self.logbook / "shared-name"))
            self.assertEqual(old[1]["worktree_root"], str(second.resolve()))

    def intercepted(self, location, mode, marker):
        """Run the new resolver with the Git call selected by marker replaced by a failure."""
        calls = []

        def run(argv, *args, **keywords):
            if marker in argv:
                calls.append((list(argv), dict(keywords)))
                if mode == "oserror":
                    raise OSError("injected")
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(argv, keywords["timeout"])
                if mode == "nonzero":
                    return subprocess.CompletedProcess(argv, 128, b"", b"fatal")
                if mode == "empty":
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                if mode == "nonzero-with-output":
                    real = REAL_RUN(argv, *args, **keywords)
                    self.assertEqual(real.returncode, 0)
                    self.assertTrue(real.stdout)
                    return subprocess.CompletedProcess(argv, 1, real.stdout, b"")
            return REAL_RUN(argv, *args, **keywords)

        with mock.patch.object(subprocess, "run", run):
            new = outcome(pr, self.registry, location, logbook=self.logbook)
        return new, calls

    def test_failure_and_timeout_of_the_git_calls_keep_the_name_of_the_linked_folder(self):
        main = self.repo(self.base / "repositories" / "main-inject")
        linked = self.linked(main, self.base / "worktrees" / "linked-inject")
        old = outcome(base_pr, self.registry, linked, logbook=self.logbook)
        self.assertEqual(old[1]["home"], str(self.logbook / "linked-inject"))
        # The injection is not vacuous: without it the new resolver names the main folder.
        untouched = outcome(pr, self.registry, linked, logbook=self.logbook)
        self.assertEqual(untouched[1]["home"], str(self.logbook / "main-inject"))
        for marker in ["worktree", "--git-dir"]:
            for mode in ["oserror", "timeout", "nonzero", "empty", "nonzero-with-output"]:
                with self.subTest(marker=marker, mode=mode):
                    new, calls = self.intercepted(linked, mode, marker)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][1]["timeout"], 5)
                    self.assertEqual(new, old)

    def test_git_calls_use_the_resolver_options_the_cleaned_environment_and_five_seconds(self):
        main = self.repo(self.base / "repositories" / "main-options")
        linked = self.linked(main, self.base / "worktrees" / "linked-options")
        seen = []

        def run(argv, *args, **keywords):
            if "--git-dir" in argv or "worktree" in argv:
                seen.append((list(argv), dict(keywords)))
            return REAL_RUN(argv, *args, **keywords)

        with mock.patch.dict(os.environ, {"GIT_WORK_TREE": "/nonexistent", "GIT_DIR": "/nonexistent",
                                          "GIT_CEILING_DIRECTORIES": "/nonexistent"}):
            with mock.patch.object(subprocess, "run", run):
                new = outcome(pr, self.registry, linked, logbook=self.logbook)
        self.assertEqual(new[1]["home"], str(self.logbook / "main-options"))
        self.assertEqual(len(seen), 2)
        expected = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        prefix = ["git"] + GIT_PREFIX_TAIL[:2] + ["core.fsmonitor=false", "-C", str(linked.resolve())]
        self.assertEqual(seen[0][0], prefix + ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"])
        self.assertEqual(seen[1][0], prefix + ["worktree", "list", "--porcelain", "-z"])
        for _, keywords in seen:
            self.assertEqual(keywords["timeout"], 5)
            self.assertEqual(keywords["env"], expected)
            self.assertFalse([k for k in keywords["env"] if k.startswith("GIT_")])
            self.assertIs(keywords["capture_output"], True)
            self.assertIs(keywords["check"], False)

    def test_main_worktree_makes_no_listing_call(self):
        main = self.repo(self.base / "repositories" / "main-calls")
        seen = []

        def run(argv, *args, **keywords):
            seen.append(list(argv))
            return REAL_RUN(argv, *args, **keywords)

        with mock.patch.object(subprocess, "run", run):
            new = outcome(pr, self.registry, main, logbook=self.logbook)
        self.assertEqual(new, outcome(base_pr, self.registry, main, logbook=self.logbook))
        self.assertFalse([a for a in seen if "worktree" in a])

    def test_folder_outside_git_makes_no_linked_worktree_call(self):
        folder = self.base / "outside-calls"
        folder.mkdir()
        seen = []

        def run(argv, *args, **keywords):
            seen.append(list(argv))
            return REAL_RUN(argv, *args, **keywords)

        with mock.patch.object(subprocess, "run", run):
            new = outcome(pr, self.registry, folder, logbook=self.logbook)
        self.assertEqual(new, outcome(base_pr, self.registry, folder, logbook=self.logbook))
        self.assertEqual(new[1]["home"], str(self.logbook / "outside-calls"))
        self.assertTrue(seen)
        self.assertFalse([a for a in seen if "--git-dir" in a or "worktree" in a])

    # One fixture per status; each gives its status at the base commit, and the resolver must equal it.
    def status_fixture_unregistered_worktree(self):
        main = self.repo(self.base / "repositories" / "registered-main")
        self.project(self.logbook / "registered-home", main)
        return self.linked(main, self.base / "worktrees" / "unregistered-linked"), {}

    def status_fixture_unknown_repository(self):
        return self.repo(self.base / "repositories" / "unknown"), {"legacy": False}

    def status_fixture_retired(self):
        main = self.repo(self.base / "repositories" / "retired-registered")
        retired = self.base / "retired-root"
        (retired / "inner").mkdir(parents=True)
        self.project(self.logbook / "retired-home", main, retired=[retired])
        return retired / "inner", {}

    def status_fixture_reserved_path(self):
        other = self.repo(self.base / "repositories" / "reserved-other")
        main = self.repo(self.base / "repositories" / "main-reserved")
        self.project(self.logbook / "main-reserved" / "project-home", other)
        return main, {}

    def status_fixture_unregistered_center(self):
        main = self.repo(self.base / "repositories" / "main-center")
        write_json(self.logbook / "main-center" / "project.json", {"project_version": 1})
        return main, {}

    def status_fixture_invalid(self):
        first, second = self.base / "loop-first", self.base / "loop-second"
        first.symlink_to(second)
        second.symlink_to(first)
        return first, {}

    def status_fixture_inactive(self):
        main = self.repo(self.base / "repositories" / "staged-main")
        self.project(self.logbook / "staged-home", main)
        return main, {"inspect": False}

    def status_fixture_identity_mismatch(self):
        main = self.repo(self.base / "repositories" / "replaced-main")
        self.project(self.logbook / "replaced-home", main)
        original = self.base / "preserved-original"
        main.rename(original)
        shutil.copytree(original, main, symlinks=True)
        return main, {}

    def status_fixture_unregistered_repository(self):
        main = self.repo(self.base / "repositories" / "marked")
        write_json(main / pr.REPOSITORY_MARKER, {"marker_version": 1, "project_uuid": uid(), "repo_uuid": uid()})
        return main, {}

    def test_every_listed_status_equals_the_base_resolver(self):
        for status in ["UNREGISTERED_WORKTREE", "UNKNOWN_REPOSITORY", "RETIRED", "RESERVED_PATH",
                       "UNREGISTERED_CENTER", "INVALID", "INACTIVE", "IDENTITY_MISMATCH",
                       "UNREGISTERED_REPOSITORY"]:
            with self.subTest(status=status):
                shutil.rmtree(self.logbook)
                self.logbook.mkdir()
                self.registry.parent.mkdir(mode=0o700)
                pr.init_registry(self.registry)
                for leftover in ["repositories", "worktrees", "retired-root", "preserved-original"]:
                    shutil.rmtree(self.base / leftover, ignore_errors=True)
                for leftover in ["loop-first", "loop-second"]:
                    (self.base / leftover).unlink(missing_ok=True)
                location, keywords = getattr(self, "status_fixture_" + status.lower())()
                old = self.compare(location, **keywords)
                self.assertEqual(old[:2], ("err", status))
                for host in ["codex", "claude"]:
                    self.assertEqual(self.compare(location, host=host, **keywords), old)


if __name__ == "__main__":
    unittest.main(failfast=True)
