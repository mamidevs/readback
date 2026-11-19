#!/usr/bin/env python3
"""Project routing checks using disposable Git repositories and local files only.

Run: python3 -m unittest discover -s checks -p 'test_project_registry.py' -v
The two host wrappers are exercised as processes; no provider is contacted.
"""

import copy
import fcntl
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
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_registry as pr
import publish_records as publisher


def uid():
    return str(uuid.uuid4())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_state(root):
    """Capture content, symlinks and write timestamps, excluding read access times."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        stat = path.lstat()
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path), stat.st_mtime_ns)
        elif path.is_file():
            result[relative] = ("file", file_digest(path), stat.st_mtime_ns)
        elif path.is_dir():
            result[relative] = ("dir", stat.st_mtime_ns)
    return result


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="readback-w1-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.logbook = self.base / "logbook"
        self.logbook.mkdir()
        self.registry = self.logbook / ".readback" / "projects.json"
        self.registry.parent.mkdir(mode=0o700)
        self.repo = self.base / "repositories" / "sample"
        self.git_init(self.repo)
        self.home = self.logbook / "sample-project"
        self.project = self.project_fixture(self.home, self.repo)
        pr.init_registry(self.registry)
        self.data = pr.apply_registry(
            self.registry, {"registry_version": 1, "generation": 0,
                            "projects": [self.project]}, 0)

    def git(self, path, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false",
             "-c", "user.name=Fixture Author", "-c", "user.email=fixture@example.invalid",
             "-C", str(path), *arguments],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def git_init(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.git(path, "commit", "--allow-empty", "-q", "-m", "Synthetic fixture")

    def worktree(self, name):
        path = self.base / "worktrees" / name
        path.parent.mkdir(exist_ok=True)
        self.git(self.repo, "worktree", "add", "--detach", "--quiet", str(path), "HEAD")
        return path

    def call(self, script, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *map(str, arguments)],
            capture_output=True, text=True, timeout=15,
        )

    def resolve(self, cwd, host="codex", inspect=True, legacy_fallback=False):
        return pr.resolve_project(self.registry, cwd, host,
                                  inspect_staged=inspect, logbook=self.logbook,
                                  legacy_fallback=legacy_fallback)

    @staticmethod
    def identity(path):
        stat = path.stat()
        return {"device": stat.st_dev, "inode": stat.st_ino}

    def case_alias(self, path):
        alias = path.with_name(path.name.swapcase())
        if alias == path or not alias.exists() or not os.path.samefile(path, alias):
            self.skipTest("The fixture filesystem does not expose same-file case aliases")
        self.assertNotEqual(str(path), str(alias))
        self.assertEqual(self.identity(path), self.identity(alias))
        return alias

    def project_fixture(self, home, repo):
        home.mkdir(parents=True)
        profile = {"profile_version": 1, "profile_id": "sample-v1",
                   "protocol_version": "0.7", "record_types": ["DEC", "CON", "HND", "LOG"],
                   "core_language": "en", "extension_namespace": "sample",
                   "extensions": {"sample": {}}}
        write_json(home / "profile.json", profile)
        project_uuid, home_uuid = uid(), uid()
        digest = file_digest(home / "profile.json")
        write_json(home / "project.json", {
            "project_version": 1, "project_uuid": project_uuid, "home_uuid": home_uuid,
            "protocol_version": "0.7", "profile_version": 1,
            "profile_sha256": digest, "activation_state": "STAGED"})
        common = Path(self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        return {"project_uuid": project_uuid, "slug": home.name,
                "home": str(home), "home_identity": self.identity(home), "home_uuid": home_uuid,
                "protocol_version": "0.7", "profile_version": 1, "activation_state": "STAGED",
                "profile_sha256": digest, "repositories": [{
                    "repo_uuid": uid(), "root": str(repo), "root_identity": self.identity(repo),
                    "git_common_dir": str(common), "git_common_dir_identity": self.identity(common),
                    "worktrees": []}], "retired_roots": []}

    def apply(self, candidate=None, expected=None, **kwargs):
        if candidate is None:
            candidate = copy.deepcopy(self.data)
        if expected is None:
            expected = self.data["generation"]
        self.data = pr.apply_registry(self.registry, candidate, expected, **kwargs)
        self.project = self.data["projects"][0]
        return self.data

    def activate(self, candidate=None, storage="markdown-single-writer"):
        candidate = copy.deepcopy(candidate or self.data)
        profile_path = self.home / "profile.json"
        profile = json.loads(profile_path.read_text())
        profile["extensions"]["sample"]["storage"] = storage
        write_json(profile_path, profile)
        project = candidate["projects"][0]
        project["activation_state"] = "ACTIVE"
        project["profile_sha256"] = file_digest(profile_path)
        center_path = self.home / "project.json"
        center = json.loads(center_path.read_text())
        center.update(activation_state="ACTIVE", profile_sha256=project["profile_sha256"])
        write_json(center_path, center)
        for repo in project["repositories"]:
            for root in [repo["root"]] + [tree["root"] for tree in repo["worktrees"]]:
                if root not in project["retired_roots"]:
                    write_json(Path(root) / pr.REPOSITORY_MARKER, {
                        "marker_version": 1, "project_uuid": project["project_uuid"], "repo_uuid": repo["repo_uuid"]})
        return self.apply(candidate)

    def assert_error(self, function, *arguments, code=None, **keywords):
        with self.assertRaises(pr.RegistryError) as caught:
            function(*arguments, **keywords)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_two_host_wrappers_resolve_same_repository_subdirectory_and_center(self):
        subdirectory = self.repo / "nested" / "résumé"
        subdirectory.mkdir(parents=True)
        for location in [self.repo, subdirectory, self.home]:
            outputs = []
            for host in ["codex", "claude"]:
                result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                                   "--cwd", location, "--inspect-staged", "--logbook", self.logbook)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["host"], host)
                self.assertEqual(data["project_uuid"], self.project["project_uuid"])
                self.assertEqual(data["home_uuid"], self.project["home_uuid"])
                self.assertEqual(data["home"], str(self.home))
                self.assertFalse(data["write_allowed"])
                outputs.append({key: value for key, value in data.items() if key != "host"})
            self.assertEqual(*outputs)

    def test_registered_worktree_requires_explicit_binding_for_both_hosts(self):
        worktree = self.worktree("registered")
        self.assert_error(self.resolve, worktree, code="UNREGISTERED_WORKTREE")
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["repositories"][0]["worktrees"] = [
            {"root": str(worktree), "identity": self.identity(worktree)}]
        self.apply(candidate)
        for host in ["codex", "claude"]:
            resolved = self.resolve(worktree, host)
            self.assertEqual(resolved["project_uuid"], self.project["project_uuid"])
            self.assertEqual(resolved["repo_uuid"], self.project["repositories"][0]["repo_uuid"])
        unregistered = self.worktree("unregistered")
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, unregistered, host, code="UNREGISTERED_WORKTREE")
            self.assert_error(self.resolve, unregistered, host, legacy_fallback=True,
                              code="UNREGISTERED_WORKTREE")

    def test_symlink_working_directory_alias_uses_same_identity(self):
        alias = self.base / "repository-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.resolve(alias), self.resolve(self.repo))
        center_alias = self.base / "center-alias"
        center_alias.symlink_to(self.home, target_is_directory=True)
        self.assertEqual(self.resolve(center_alias), self.resolve(self.home))

    def test_symlink_loop_cwd_returns_controlled_error_for_both_hosts(self):
        first, second = self.base / "loop-first", self.base / "loop-second"
        first.symlink_to(second)
        second.symlink_to(first)
        before = tree_state(self.base)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, first, host, code="INVALID")
            result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                               "--cwd", first, "--inspect-staged")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stderr)["status"], "INVALID")
            self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(tree_state(self.base), before)

    def test_staged_center_never_grants_write_authority(self):
        for host in ["codex", "claude"]:
            for location in [self.home, self.repo]:
                self.assert_error(self.resolve, location, host, False, code="INACTIVE")
                self.assertFalse(self.resolve(location, host)["write_allowed"])

    def test_active_repositories_and_worktree_share_home_and_preserve_context(self):
        second = self.base / "repositories" / "sample-api"
        self.git_init(second)
        worktree = self.worktree("feature")
        candidate = copy.deepcopy(self.data)
        repositories = candidate["projects"][0]["repositories"]
        repositories[0] = pr.observe_repository(self.repo, repositories[0]["repo_uuid"], [worktree])
        repositories.append(pr.observe_repository(second))
        self.activate(candidate)
        nested = worktree / "nested"
        nested.mkdir()
        original_cwd = os.getcwd()
        before = tree_state(self.base)
        for location, work_root in [(self.repo, self.repo), (second, second), (nested, worktree), (self.home, None)]:
            outputs = []
            for host in ["codex", "claude"]:
                result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                                   "--cwd", location)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["status"], "ACTIVE")
                self.assertTrue(data["write_allowed"])
                self.assertEqual(data["home"], str(self.home))
                self.assertEqual(data["profile_path"], str(self.home / "profile.json"))
                self.assertEqual(data["current_path"], str(self.home / "CURRENT.md"))
                self.assertEqual(data["log_path"], str(self.home / "LOG.md"))
                self.assertEqual(data["records_path"], str(self.home / "records"))
                self.assertEqual(data["cwd"], str(location))
                self.assertEqual(data["worktree_root"], str(work_root) if work_root else None)
                self.assertEqual(data["repo_uuid"] is None, location == self.home)
                outputs.append({key: value for key, value in data.items() if key != "host"})
            self.assertEqual(*outputs)
        self.assertEqual(os.getcwd(), original_cwd)
        self.assertEqual(tree_state(self.base), before)

    def test_active_profile_requires_explicit_supported_storage(self):
        for storage in [None, "sqlite", {}, True]:
            with self.subTest(storage=storage):
                before = self.registry.read_bytes()
                self.assert_error(self.activate, storage=storage, code="INACTIVE")
                self.assertEqual(self.registry.read_bytes(), before)

    def test_active_unknown_worktree_and_tampered_profile_fail_closed(self):
        self.activate()
        unknown = self.worktree("unknown-active")
        self.assert_error(self.resolve, unknown, inspect=False, legacy_fallback=True,
                          code="UNREGISTERED_WORKTREE")
        profile = self.home / "profile.json"
        profile.write_bytes(profile.read_bytes() + b"\n")
        self.assert_error(self.resolve, self.repo, inspect=False, legacy_fallback=True,
                          code="IDENTITY_MISMATCH")

    def test_active_marker_must_match_and_cannot_disappear(self):
        self.activate()
        path = self.repo / pr.REPOSITORY_MARKER
        original = json.loads(path.read_text())
        for key in ["project_uuid", "repo_uuid"]:
            marker = dict(original)
            marker[key] = uid()
            write_json(path, marker)
            self.assert_error(self.resolve, self.repo, legacy_fallback=True, code="IDENTITY_MISMATCH")
        path.unlink()
        self.assert_error(self.resolve, self.repo, legacy_fallback=True, code="IDENTITY_MISMATCH")

    def test_copied_marked_repository_never_falls_back_with_present_or_missing_registry(self):
        self.activate()
        copied = self.base / "copied-marked-repository"
        shutil.copytree(self.repo, copied)
        nested = copied / "nested"
        nested.mkdir()
        for registry in [self.registry, self.base / "missing" / "projects.json"]:
            for host in ["codex", "claude"]:
                self.assert_error(pr.resolve_project, registry, nested, host,
                                  legacy_fallback=True, logbook=self.logbook, code="UNREGISTERED_REPOSITORY")

    def test_active_candidate_requires_marker_before_publication(self):
        self.activate()
        before = self.registry.read_bytes()
        (self.repo / pr.REPOSITORY_MARKER).unlink()
        self.assert_error(self.apply, code="IDENTITY_MISMATCH")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_unregistered_invalid_or_symlink_marker_fails_closed(self):
        unrelated = self.base / "unrelated"
        self.git_init(unrelated)
        marker = unrelated / pr.REPOSITORY_MARKER
        marker.write_text('{"marker_version": 999}')
        self.assert_error(self.resolve, unrelated, legacy_fallback=True, code="INVALID")
        marker.unlink()
        marker.symlink_to(self.home / "project.json")
        self.assert_error(self.resolve, unrelated, legacy_fallback=True, code="INVALID")

    def test_retired_hub_rebind_preserves_uuid_and_rejects_original_and_archive(self):
        prior = self.data["projects"][0]["repositories"][0]
        archive = self.base / "recovery" / "original-hub"
        archive.parent.mkdir()
        self.repo.rename(archive)
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text("Retired; read the project home.\n")
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["repositories"][0] = pr.observe_repository(archive, prior["repo_uuid"])
        candidate["projects"][0]["retired_roots"] = [str(self.repo), str(archive)]
        self.activate(candidate)
        (self.home / "documents").mkdir()
        (self.repo / "documents").symlink_to(self.home / "documents", target_is_directory=True)
        rebound = self.data["projects"][0]["repositories"][0]
        self.assertEqual(rebound["repo_uuid"], prior["repo_uuid"])
        self.assertEqual(rebound["root_identity"], prior["root_identity"])
        self.assertEqual(rebound["git_common_dir_identity"], prior["git_common_dir_identity"])
        for host in ["codex", "claude"]:
            for location in [self.repo, archive, self.repo / "documents"]:
                result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                                   "--cwd", location, "--legacy-fallback", "--inspect-staged")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                error = json.loads(result.stderr)
                self.assertEqual(error["status"], "RETIRED")
                self.assertFalse(error["write_allowed"])
                self.assertIn(str(self.home), error["error"])
            self.assertTrue(self.resolve(self.home, host, inspect=False)["write_allowed"])

    def test_unknown_activation_states_are_rejected(self):
        for state in ["MIGRATING", "LEGACY_RECOVERY", "active", [], True]:
            with self.subTest(state=state):
                candidate = copy.deepcopy(self.data)
                candidate["projects"][0]["activation_state"] = state
                self.assert_error(pr.validate_registry, candidate, check_filesystem=False, code="INACTIVE")

    def test_readonly_staged_center_is_readable_but_ordinary_writes_are_blocked(self):
        if os.geteuid() == 0:
            self.skipTest("A root process bypasses the ordinary permission boundary")
        probe = self.home / "writable-probe"
        probe.write_text("Synthetic write before protection.\n")
        probe.unlink()
        files = [self.home / "project.json", self.home / "profile.json"]
        previous_modes = {path: path.stat().st_mode & 0o777 for path in [self.home, *files]}
        try:
            for path in files:
                path.chmod(0o400)
            self.home.chmod(0o500)
            before = tree_state(self.base)
            for host in ["codex", "claude"]:
                result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                                   "--cwd", self.home, "--inspect-staged")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                resolved = json.loads(result.stdout)
                self.assertEqual(resolved["status"], "STAGED")
                self.assertFalse(resolved["write_allowed"])
                self.assertEqual(resolved["project_uuid"], self.project["project_uuid"])
                self.assert_error(self.resolve, self.home, host, False, code="INACTIVE")
            for name in ["CURRENT.md", "LOG.md"]:
                with self.subTest(name=name):
                    with self.assertRaises(PermissionError):
                        with (self.home / name).open("x") as stream:
                            stream.write("An ordinary writer must not create this file.\n")
                    self.assertFalse((self.home / name).exists())
            for path in files:
                with self.assertRaises(PermissionError):
                    with path.open("a") as stream:
                        stream.write("An ordinary writer must not modify this file.\n")
            self.assertEqual(tree_state(self.base), before)
        finally:
            # Restore permissions before TemporaryDirectory cleanup, even after failure.
            self.home.chmod(previous_modes[self.home])
            for path in files:
                path.chmod(previous_modes[path])

    def test_resolve_success_and_failure_create_no_files_or_mutations(self):
        before = tree_state(self.base)
        self.resolve(self.repo)
        self.resolve(self.home, "claude")
        self.assert_error(self.resolve, self.repo, inspect=False)
        self.assertEqual(tree_state(self.base), before)

    def test_fallback_for_unrelated_repository_does_not_create_a_center(self):
        unrelated = self.base / "repositories" / "unrelated"
        self.git_init(unrelated)
        before = tree_state(self.base)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, unrelated, host, code="UNKNOWN_REPOSITORY")
            result = self.resolve(unrelated, host, legacy_fallback=True)
            self.assertEqual(result["status"], "LEGACY_FALLBACK")
            self.assertEqual(result["home"], str(self.logbook / "unrelated"))
        self.assertEqual(tree_state(self.base), before)
        self.assertFalse((self.logbook / "unrelated").exists())

    def test_unregistered_same_name_repository_cannot_fallback_into_reserved_home(self):
        unrelated = self.base / "elsewhere" / self.home.name
        self.git_init(unrelated)
        before = tree_state(self.base)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, unrelated, host, legacy_fallback=True)
        self.assertEqual(tree_state(self.base), before)

    def test_retired_root_and_same_name_fallback_remain_reserved(self):
        retired = self.logbook / "retired-project"
        retired.mkdir()
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["retired_roots"] = [str(retired)]
        self.apply(candidate)
        unrelated = self.base / "elsewhere" / retired.name
        self.git_init(unrelated)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, unrelated, host, legacy_fallback=True)
            self.assert_error(self.resolve, retired, host, legacy_fallback=True)

    def test_copied_center_is_not_a_new_project(self):
        copied = self.base / "copied-center"
        shutil.copytree(self.home, copied)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, copied, host)
            self.assert_error(self.resolve, copied, host, legacy_fallback=True)

    def test_missing_registry_cannot_fallback_into_an_existing_center(self):
        unrelated = self.base / "elsewhere" / self.home.name
        self.git_init(unrelated)
        self.registry.rename(self.base / "preserved-registry.json")
        before = tree_state(self.base)
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, unrelated, host)
            self.assert_error(self.resolve, unrelated, host, legacy_fallback=True)
        self.assertEqual(tree_state(self.base), before)
        self.assertFalse(self.registry.exists())

    def test_missing_registry_falls_back_only_when_explicit_without_creating_directories(self):
        missing = self.base / "absent" / "registry" / "projects.json"
        before = tree_state(self.base)
        for host in ["codex", "claude"]:
            self.assert_error(pr.resolve_project, missing, self.repo, host, code="NOT_FOUND")
            result = self.call("resolve_" + host + "_project.py", "--registry", missing,
                               "--cwd", self.repo, "--legacy-fallback", "--logbook", self.logbook)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "LEGACY_FALLBACK")
            self.assertEqual(data["home"], str(self.logbook / self.repo.name))
            self.assertIsNone(data["registry_generation"])
            self.assertFalse(data["write_allowed"])
        self.assertEqual(tree_state(self.base), before)
        self.assertFalse(missing.parent.exists())

    def test_empty_registry_fallback_preserves_repository_destination(self):
        empty = self.base / "empty-registry.json"
        pr.init_registry(empty)
        data = pr.resolve_project(empty, self.repo, "codex", legacy_fallback=True, logbook=self.logbook)
        self.assertEqual(data["home"], str(self.logbook / self.repo.name))
        self.assertEqual(data["registry_generation"], 0)
        self.assertEqual(data["status"], "LEGACY_FALLBACK")

    def test_malformed_registry_never_becomes_legacy_fallback(self):
        self.registry.write_text("{malformed json}")
        for host in ["codex", "claude"]:
            self.assert_error(self.resolve, self.repo, host, legacy_fallback=True, code="INVALID")

    def test_same_path_replaced_center_is_rejected(self):
        original = self.base / "preserved-original-center"
        self.home.rename(original)
        shutil.copytree(original, self.home)
        self.assert_error(self.resolve, self.repo)
        self.assert_error(self.resolve, self.home)

    def test_same_path_replaced_repository_is_rejected(self):
        original = self.base / "preserved-original-repo"
        self.repo.rename(original)
        shutil.copytree(original, self.repo)
        self.assert_error(self.resolve, self.repo)

    def test_same_path_replaced_common_directory_is_rejected(self):
        common = Path(self.project["repositories"][0]["git_common_dir"])
        original = self.base / "preserved-common"
        common.rename(original)
        shutil.copytree(original, common)
        self.assert_error(self.resolve, self.repo)

    def test_copied_repository_does_not_inherit_registered_identity(self):
        copied = self.base / "copied-repository"
        shutil.copytree(self.repo, copied)
        self.assert_error(self.resolve, copied, code="UNKNOWN_REPOSITORY")
        result = self.resolve(copied, legacy_fallback=True)
        self.assertEqual(result["status"], "LEGACY_FALLBACK")
        self.assertNotEqual(result.get("project_uuid"), self.project["project_uuid"])
        self.assertNotEqual(result["home"], str(self.home))

    def test_worktree_with_wrong_common_directory_is_rejected(self):
        worktree = self.worktree("registered")
        other = self.base / "repositories" / "other"
        self.git_init(other)
        candidate = copy.deepcopy(self.data)
        repo = candidate["projects"][0]["repositories"][0]
        repo["worktrees"] = [{"root": str(worktree), "identity": self.identity(worktree)}]
        common = Path(self.git(other, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        repo["git_common_dir"] = str(common)
        repo["git_common_dir_identity"] = self.identity(common)
        self.assert_error(self.apply, candidate)

    def test_rebind_moved_repository_preserves_uuid(self):
        destination = self.base / "renamed-repository"
        original_uuid = self.project["repositories"][0]["repo_uuid"]
        self.repo.rename(destination)
        candidate = copy.deepcopy(self.data)
        repo = candidate["projects"][0]["repositories"][0]
        repo["root"] = str(destination)
        repo["root_identity"] = self.identity(destination)
        common = Path(self.git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        repo["git_common_dir"] = str(common)
        repo["git_common_dir_identity"] = self.identity(common)
        self.apply(candidate)
        self.assertEqual(self.resolve(destination)["repo_uuid"], original_uuid)
        self.assertEqual(self.resolve(destination)["project_uuid"], self.project["project_uuid"])

    def test_duplicate_physical_repository_in_two_projects_is_rejected(self):
        second = self.project_fixture(self.logbook / "second-project", self.repo)
        candidate = copy.deepcopy(self.data)
        candidate["projects"].append(second)
        before = self.registry.read_bytes()
        self.assert_error(self.apply, candidate)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_samefile_case_alias_cannot_bind_the_repository_twice(self):
        alias = self.case_alias(self.repo)
        common = Path(self.project["repositories"][0]["git_common_dir"])
        common_alias = self.case_alias(common)
        candidate = copy.deepcopy(self.data)
        duplicate = copy.deepcopy(candidate["projects"][0]["repositories"][0])
        duplicate.update(repo_uuid=uid(), root=str(alias), git_common_dir=str(common_alias))
        duplicate["root_identity"] = self.identity(alias)
        duplicate["git_common_dir_identity"] = self.identity(common_alias)
        candidate["projects"][0]["repositories"].append(duplicate)
        before = self.registry.read_bytes()
        self.assert_error(self.apply, candidate, code="INVALID")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_samefile_case_alias_cannot_bind_the_center_twice(self):
        alias = self.case_alias(self.home)
        other_repo = self.base / "repositories" / "other"
        self.git_init(other_repo)
        second = self.project_fixture(self.logbook / "second-project", other_repo)
        second.update(home=str(alias), home_identity=self.identity(alias))
        candidate = copy.deepcopy(self.data)
        candidate["projects"].append(second)
        before = self.registry.read_bytes()
        self.assert_error(self.apply, candidate, code="INVALID")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_samefile_case_alias_cannot_register_git_common_directory_twice(self):
        common = Path(self.project["repositories"][0]["git_common_dir"])
        common_alias = self.case_alias(common)
        worktree = self.worktree("case-alias-common")
        worktree_git = Path(self.git(worktree, "rev-parse", "--path-format=absolute", "--git-dir")).resolve()
        # Git permits an absolute commondir. This changes only disposable fixture metadata.
        (worktree_git / "commondir").write_text(str(common_alias) + "\n")
        reported_common = Path(self.git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if str(reported_common) != str(common_alias):
            self.skipTest("This Git normalizes the common-directory case alias")
        self.assertTrue(os.path.samefile(common, reported_common))
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["repositories"].append({
            "repo_uuid": uid(), "root": str(worktree), "root_identity": self.identity(worktree),
            "git_common_dir": str(reported_common),
            "git_common_dir_identity": self.identity(reported_common), "worktrees": []})
        before = self.registry.read_bytes()
        self.assert_error(self.apply, candidate, code="INVALID")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_duplicate_repository_uuid_is_rejected_even_for_different_paths(self):
        other = self.base / "repositories" / "other"
        self.git_init(other)
        second = self.project_fixture(self.logbook / "second-project", other)
        second["repositories"][0]["repo_uuid"] = self.project["repositories"][0]["repo_uuid"]
        candidate = copy.deepcopy(self.data)
        candidate["projects"].append(second)
        self.assert_error(self.apply, candidate)

    def test_profile_byte_tampering_is_rejected(self):
        profile = self.home / "profile.json"
        profile.write_text(profile.read_text() + " ")
        self.assert_error(self.resolve, self.repo)

    def test_center_uuid_mismatch_is_rejected(self):
        path = self.home / "project.json"
        center = json.loads(path.read_text())
        center["home_uuid"] = uid()
        write_json(path, center)
        self.assert_error(self.resolve, self.repo)

    def test_readers_preserve_both_protocol_versions(self):
        profile_path = self.home / "profile.json"
        center_path = self.home / "project.json"
        for version in ("0.7", "0.8"):
            project = self.data["projects"][0]
            profile = json.loads(profile_path.read_bytes())
            profile["protocol_version"] = version
            write_json(profile_path, profile)
            project.update(protocol_version=version, profile_sha256=file_digest(profile_path))
            center = json.loads(center_path.read_bytes())
            center.update(protocol_version=version, profile_sha256=project["profile_sha256"])
            write_json(center_path, center)
            write_json(self.registry, self.data)
            files = (profile_path, center_path, self.registry)
            before = [p.read_bytes() for p in files]
            for host in ("claude", "codex"):
                self.assertEqual(self.resolve(self.repo, host)["protocol_version"], version)
            self.assertEqual([p.read_bytes() for p in files], before)
        profile["protocol_version"] = "0.7"
        write_json(profile_path, profile)
        project["profile_sha256"] = center["profile_sha256"] = file_digest(profile_path)
        write_json(center_path, center)
        write_json(self.registry, self.data)
        self.assert_error(self.resolve, self.repo, code="IDENTITY_MISMATCH")

    def test_unknown_versions_and_boolean_generations_are_rejected(self):
        mutations = [
            lambda d: d.update(registry_version=2),
            lambda d: d.update(registry_version=True),
            lambda d: d.update(generation=True),
            lambda d: d["projects"][0].update(protocol_version="9.0"),
            lambda d: d["projects"][0].update(profile_version=2),
            lambda d: d["projects"][0].update(profile_version=True),
            lambda d: d["projects"][0]["home_identity"].update(inode=True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                candidate = copy.deepcopy(self.data)
                mutate(candidate)
                self.assert_error(self.apply, candidate)

    def test_unsupported_center_and_profile_schema_are_rejected(self):
        for name, field, value in [("project.json", "project_version", 2),
                                   ("profile.json", "profile_version", 2),
                                   ("profile.json", "unexpected_core_field", "unexpected")]:
            with self.subTest(file=name, field=field):
                path = self.home / name
                original = path.read_bytes()
                data = json.loads(original)
                data[field] = value
                write_json(path, data)
                if name == "profile.json":
                    center_path = self.home / "project.json"
                    original_center = center_path.read_bytes()
                    center = json.loads(original_center)
                    digest = file_digest(path)
                    center["profile_sha256"] = digest
                    write_json(center_path, center)
                    candidate = copy.deepcopy(self.data)
                    candidate["projects"][0]["profile_sha256"] = digest
                    self.assert_error(self.apply, candidate)
                    center_path.write_bytes(original_center)
                else:
                    self.assert_error(self.resolve, self.repo)
                path.write_bytes(original)

    def test_unknown_profile_extension_namespace_is_rejected(self):
        path = self.home / "profile.json"
        profile = json.loads(path.read_text())
        profile["extensions"]["other"] = {}
        write_json(path, profile)
        digest = file_digest(path)
        center = json.loads((self.home / "project.json").read_text())
        center["profile_sha256"] = digest
        write_json(self.home / "project.json", center)
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["profile_sha256"] = digest
        self.assert_error(self.apply, candidate)

    def test_active_candidate_without_storage_is_rejected_without_changing_registry(self):
        candidate = copy.deepcopy(self.data)
        candidate["projects"][0]["activation_state"] = "ACTIVE"
        center_path = self.home / "project.json"
        center = json.loads(center_path.read_text())
        center["activation_state"] = "ACTIVE"
        write_json(center_path, center)
        before = self.registry.read_bytes()
        self.assert_error(self.apply, candidate)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_duplicate_json_keys_are_rejected(self):
        original = self.registry.read_text()
        self.registry.write_text('{"generation": 999,' + original.lstrip()[1:])
        before = self.registry.read_bytes()
        self.assert_error(self.resolve, self.repo)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_stale_generation_does_not_replace_current_registry(self):
        candidate = copy.deepcopy(self.data)
        self.apply()
        before = self.registry.read_bytes()
        self.assert_error(pr.apply_registry, self.registry, candidate, 1, code="CONFLICT")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_registry_change_waits_for_interrupted_publication_recovery(self):
        self.activate()
        request = {"operation_id": uid(), "expected_generation": self.data["generation"],
                   "expected_current_sha256": None, "records": [],
                   "log_lines": ["LOG-20261117-01 | fixture | Synthetic publication."],
                   "current": "CURRENT   fixture\n"}
        with self.assertRaises(RuntimeError):
            publisher.publish(self.registry, self.repo, "codex", request, fail_after="journal")
        before = self.registry.read_bytes()
        self.assert_error(self.apply, code="RECOVERY_REQUIRED")
        self.assertEqual(self.registry.read_bytes(), before)
        result = publisher.publish(self.registry, self.repo, "codex", request)
        self.assertEqual(result["status"], "COMMITTED")
        previous_generation = self.data["generation"]
        self.apply()
        self.assertEqual(self.data["generation"], previous_generation + 1)

    def test_invalid_publication_journals_block_registry_updates(self):
        self.activate()
        request = {"operation_id": uid(), "expected_generation": self.data["generation"],
                   "expected_current_sha256": None, "records": [],
                   "log_lines": ["LOG-20261117-01 | fixture | Synthetic publication."],
                   "current": "CURRENT   fixture\n"}
        publisher.publish(self.registry, self.repo, "codex", request)
        path = self.home / ".readback" / "publications" / (request["operation_id"] + ".json")
        original = json.loads(path.read_text())
        variants = [{}, dict(original, state="PREPARED"), dict(original, state="UNKNOWN"),
                    dict(original, schema_version=True), dict(original, schema_version=999),
                    dict(original, project_uuid=uid()), dict(original, request_sha256="0" * 64),
                    dict(original, record_hashes=[42]), dict(original, extra="unknown")]
        for variant in variants:
            with self.subTest(keys=sorted(variant)):
                write_json(path, variant)
                before = self.registry.read_bytes()
                self.assert_error(self.apply, code="RECOVERY_REQUIRED")
                self.assertEqual(self.registry.read_bytes(), before)
        path.write_text("{malformed}")
        self.assert_error(self.apply, code="RECOVERY_REQUIRED")

    def test_symlink_publication_directory_blocks_registry_updates(self):
        self.activate()
        outside = self.base / "outside-publications"
        outside.mkdir()
        (self.home / ".readback").mkdir()
        (self.home / ".readback" / "publications").symlink_to(outside, target_is_directory=True)
        before = self.registry.read_bytes()
        self.assert_error(self.apply, code="RECOVERY_REQUIRED")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_existing_registry_init_never_overwrites_data(self):
        before = self.registry.read_bytes()
        self.assert_error(pr.init_registry, self.registry)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_control_file_symlinks_are_rejected(self):
        for name in ["project.json", "profile.json"]:
            with self.subTest(name=name):
                path = self.home / name
                original = self.base / (name + ".original")
                path.rename(original)
                path.symlink_to(original)
                self.assert_error(self.resolve, self.repo)
                path.unlink()
                original.rename(path)
        original = self.base / "registry-original.json"
        self.registry.rename(original)
        self.registry.symlink_to(original)
        self.assert_error(self.resolve, self.repo)

    def test_registry_and_project_lock_paths_remain_stable(self):
        lock_paths = [self.registry.parent / "projects.lock",
                      self.registry.parent / "locks" / (self.project["project_uuid"] + ".lock")]
        original = {path: path.stat().st_ino for path in lock_paths}
        self.apply()
        self.assertEqual({path: path.stat().st_ino for path in lock_paths}, original)

    def test_contended_project_lock_returns_busy_without_mutation(self):
        lock_path = self.registry.parent / "locks" / (self.project["project_uuid"] + ".lock")
        before = self.registry.read_bytes()
        with lock_path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            self.assert_error(pr.apply_registry, self.registry, copy.deepcopy(self.data),
                              self.data["generation"], lock_timeout=0.05, code="BUSY")
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_lockfile_symlink_cannot_redirect_locking(self):
        lock_path = self.registry.parent / "projects.lock"
        preserved = self.base / "original-lock"
        lock_path.rename(preserved)
        lock_path.symlink_to(preserved)
        before = self.registry.read_bytes()
        self.assert_error(self.apply)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_two_competing_processes_allow_only_one_generation_commit(self):
        candidate = self.base / "candidate.json"
        write_json(candidate, self.data)
        command = [sys.executable, str(ROOT / "scripts" / "project_registry.py"),
                   "apply", "--registry", str(self.registry), "--candidate", str(candidate),
                   "--expected-generation", str(self.data["generation"])]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sum(code == 0 for code, _, _ in results), 1, results)
        self.assertEqual(json.loads(self.registry.read_text())["generation"], 2)
        failed = next((out + err) for code, out, err in results if code != 0)
        self.assertIn("CONFLICT", failed)

    def test_readers_never_observe_partial_json_during_registry_updates(self):
        source = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import project_registry as pr
path = pathlib.Path(sys.argv[2])
for _ in range(6):
    candidate = json.loads(path.read_text())
    pr.apply_registry(path, candidate, candidate['generation'])
print('complete')
"""
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(ROOT / "scripts"), str(self.registry)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed = []
        deadline = time.monotonic() + 15
        try:
            while process.poll() is None:
                self.assertLess(time.monotonic(), deadline)
                data = json.loads(self.registry.read_text())
                observed.append(data["generation"])
                self.assertEqual(data["projects"][0]["project_uuid"], self.project["project_uuid"])
                time.sleep(0.002)
            stdout, stderr = process.communicate(timeout=1)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertTrue(observed)
        self.assertTrue(all(1 <= generation <= 7 for generation in observed))
        self.assertEqual(json.loads(self.registry.read_text())["generation"], 7)

    def test_wrapper_rejects_host_override(self):
        for host, other in [("codex", "claude"), ("claude", "codex")]:
            result = self.call("resolve_" + host + "_project.py", "--registry", self.registry,
                               "--cwd", self.repo, "--inspect-staged", "--host", other)
            self.assertNotEqual(result.returncode, 0)


class LegacyReferenceTests(unittest.TestCase):
    def test_qualified_reference_round_trips_reserved_unicode_characters(self):
        source = uid()
        digest = "a" * 64
        path = "folder/naïve # note %.md"
        raw_id = "ADR-0042"
        reference = pr.make_legacy_ref(source, digest, path, raw_id)
        self.assertTrue(reference.startswith("legacy:" + source + ":" + digest + ":"))
        self.assertIn("%23", reference)
        parsed = pr.parse_legacy_ref(reference)
        self.assertEqual(parsed["source_uuid"], source)
        self.assertEqual(parsed["snapshot_sha256"], digest)
        self.assertEqual(parsed["path"], path)
        self.assertEqual(parsed["raw_id"], raw_id)

    def test_invalid_identity_digest_and_unsafe_paths_are_rejected(self):
        for source, digest, path, raw_id in [
            ("not-a-uuid", "a" * 64, "note.md", "ADR-1"),
            (uid(), "not-a-sha", "note.md", "ADR-1"),
            (uid(), "a" * 64, "../outside.md", "ADR-1"),
            (uid(), "a" * 64, "/absolute.md", "ADR-1"),
            (uid(), "a" * 64, "folder/../note.md", "ADR-1"),
            (uid(), "a" * 64, "note\x00.md", "ADR-1"),
            (uid(), "a" * 64, "note.md", ""),
        ]:
            with self.subTest(path=path, source=source):
                with self.assertRaises(pr.RegistryError):
                    pr.make_legacy_ref(source, digest, path, raw_id)

    def test_malformed_reference_is_not_treated_as_a_navigation_alias(self):
        for reference in ["legacy:broken", "legacy:" + uid() + ":" + "a" * 64 + ":note%ZZ.md#ADR-1",
                          "legacy:" + uid() + ":" + "a" * 64 + ":../note.md#ADR-1"]:
            with self.subTest(reference=reference):
                with self.assertRaises(pr.RegistryError):
                    pr.parse_legacy_ref(reference)

    def test_qualified_snapshot_resolution_never_selects_a_newer_snapshot(self):
        source = uid()
        older = pr.make_legacy_ref(source, "a" * 64, "decisions/note.md", "ADR-7")
        newer = pr.make_legacy_ref(source, "b" * 64, "decisions/note.md", "ADR-7")
        entries = [{"reference": older, "target": "older-target"},
                   {"reference": newer, "target": "newer-target"}]
        self.assertEqual(pr.resolve_legacy_ref(older, entries), "older-target")
        self.assertEqual(pr.resolve_legacy_ref(newer, entries), "newer-target")
        with self.assertRaises(pr.RegistryError) as caught:
            pr.resolve_legacy_ref("ADR-7", entries)
        self.assertEqual(caught.exception.code, "AMBIGUOUS_REFERENCE")

    def test_same_raw_label_requires_source_qualification(self):
        first, second = uid(), uid()
        entries = [
            {"reference": pr.make_legacy_ref(first, "a" * 64, "note.md", "ADR-7"), "target": "first"},
            {"reference": pr.make_legacy_ref(second, "a" * 64, "note.md", "ADR-7"), "target": "second"}]
        with self.assertRaises(pr.RegistryError):
            pr.resolve_legacy_ref("ADR-7", entries)
        self.assertEqual(pr.resolve_legacy_ref("ADR-7", entries, source_uuid=first), "first")

    def test_same_source_ambiguous_label_requires_exact_path(self):
        source = uid()
        first = pr.make_legacy_ref(source, "a" * 64, "first.md", "ADR-7")
        second = pr.make_legacy_ref(source, "a" * 64, "second.md", "ADR-7")
        entries = [{"reference": first, "target": "first"}, {"reference": second, "target": "second"}]
        with self.assertRaises(pr.RegistryError):
            pr.resolve_legacy_ref("ADR-7", entries, source_uuid=source)
        self.assertEqual(pr.resolve_legacy_ref(first, entries), "first")

    def test_duplicate_exact_reference_is_rejected_even_with_identical_target(self):
        reference = pr.make_legacy_ref(uid(), "a" * 64, "note.md", "ADR-7")
        entry = {"reference": reference, "target": "same-target"}
        with self.assertRaises(pr.RegistryError):
            pr.resolve_legacy_ref(reference, [entry, copy.deepcopy(entry)])

    def test_missing_reference_does_not_guess_by_label(self):
        source = uid()
        existing = pr.make_legacy_ref(source, "a" * 64, "note.md", "ADR-7")
        missing = pr.make_legacy_ref(source, "b" * 64, "note.md", "ADR-7")
        with self.assertRaises(pr.RegistryError) as caught:
            pr.resolve_legacy_ref(missing, [{"reference": existing, "target": "old"}])
        self.assertEqual(caught.exception.code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
