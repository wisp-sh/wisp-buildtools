#!/usr/bin/env python3
"""Exercise change-aware compiler selection against real Git histories."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "tools/ci-select.py"
specification = importlib.util.spec_from_file_location("compiler_ci_selection", SCRIPT)
selection = importlib.util.module_from_spec(specification)
specification.loader.exec_module(selection)


def tool(name, *, directory=None, dependencies=(), runtime_dependencies=(), source_inputs=(), includes=(), optional=()):
    directory = directory or name
    return {
        "directory": directory,
        "description": name,
        "version": "1.0.0",
        "sourceInputs": list(source_inputs),
        "dependencies": list(dependencies),
        "runtimeDependencies": list(runtime_dependencies),
        "bootstrapRequirements": [],
        "build": [],
        "test": [],
        "artifacts": [f"{directory}/dist/compiler.wasm"],
        "package": {
            "include": [f"{directory}/README.md", f"{directory}/src", *includes],
            "optional": list(optional),
        },
    }


class Repository:
    def __init__(self, root):
        self.root = root
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GITHUB_"))}
        self.environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Compiler CI fixture")
        self.git("config", "user.email", "compiler-ci@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)

    def git(self, *arguments):
        process = subprocess.run(["git", *arguments], cwd=self.root, env=self.environment,
                                 text=True, capture_output=True, check=True)
        return process.stdout.strip()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def manifest(self, tools, **global_fields):
        value = {"schema": 1, "description": "Compiler CI fixture", **global_fields, "tools": tools}
        self.write("buildtools.json", json.dumps(value, indent=2) + "\n")
        return value

    def profiles(self, tools):
        value = {
            "schema": 1,
            "platform": "wasm32-wasi",
            "packageVersion": "1.0.0",
            "tools": {name: {"include": item["package"]["include"], "optional": [], "exclude": []}
                      for name, item in tools.items()},
        }
        self.write("package-profiles.json", json.dumps(value, indent=2) + "\n")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


class GitFixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="compiler-ci-history-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Repository(self.root)
        self.tools = {
            "core": tool("core"),
            "frontend": tool("frontend", dependencies=["core"]),
            "app": tool("app", runtime_dependencies=["frontend"]),
            "independent": tool("independent"),
        }
        self.repo.manifest(self.tools)
        self.repo.profiles(self.tools)
        for item in self.tools.values():
            self.repo.write(f"{item['directory']}/src/compiler.py", "initial compiler\n")
        self.repo.write("README.md", "Compiler workspace\n")
        self.repo.write("LICENSE", "Fixture license\n")
        self.repo.write("docs/ci.md", "Compiler CI documentation\n")
        self.base = self.repo.commit("Initial compiler definitions")

    def select(self, base, head="HEAD", *, merge_base=False):
        report = selection.select_toolchains(self.root, base=base, head=head, merge_base=merge_base)
        self.assertIsInstance(report, dict)
        for field in ("toolchains", "reasons", "changedFiles", "base", "head"):
            self.assertIn(field, report)
        self.assertIsInstance(report["toolchains"], list)
        self.assertEqual(len(report["toolchains"]), len(set(report["toolchains"])))
        self.assertIsInstance(report["changedFiles"], list)
        self.assertTrue(all(isinstance(path, str) for path in report["changedFiles"]))
        self.assertIsInstance(report["head"], str)
        for name in report["toolchains"]:
            self.assertIn(name, report["reasons"])
            reasons = report["reasons"][name]
            self.assertIsInstance(reasons, list)
            self.assertTrue(reasons, f"selected compiler {name} has no explanation")
            self.assertTrue(all(isinstance(reason, str) and reason.strip() for reason in reasons))
        return report

    def assert_tools(self, report, expected):
        self.assertEqual(set(report["toolchains"]), set(expected), report)

    def event_cli(self, name, payload, *, tested_sha=None):
        with tempfile.TemporaryDirectory(prefix="compiler-ci-event-") as directory:
            event = Path(directory) / "event.json"
            outputs = Path(directory) / "outputs"
            event.write_text(json.dumps(payload))
            environment = dict(self.repo.environment, GITHUB_EVENT_NAME=name,
                               GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(outputs))
            if tested_sha is not None:
                environment["GITHUB_SHA"] = tested_sha
            process = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)],
                                     cwd=self.root, env=environment, text=True, capture_output=True, check=True)
            report = json.loads(process.stdout)
            fields = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
            self.assertEqual(json.loads(fields["toolchains"]), report["toolchains"])
            self.assertEqual(fields["has-toolchains"], "true" if report["toolchains"] else "false")
            return report


class SelectionTests(GitFixtureCase):
    def test_push_compares_all_commits_since_before_sha(self):
        self.repo.write("independent/src/compiler.py", "changed in first pushed commit\n")
        self.repo.commit("Change independent compiler first")
        self.repo.write("core/src/compiler.py", "changed in second pushed commit\n")
        head = self.repo.commit("Change common compiler later")
        report = self.select(self.base, head)
        self.assert_tools(report, self.tools)
        self.assertEqual(set(report["changedFiles"]), {"core/src/compiler.py", "independent/src/compiler.py"})

    def test_pull_request_merge_base_excludes_changes_only_on_base_branch(self):
        self.repo.git("branch", "feature", self.base)
        self.repo.write("independent/src/compiler.py", "base-only advancement\n")
        base_tip = self.repo.commit("Advance main independently")
        self.repo.git("checkout", "-q", "feature")
        self.repo.write("core/src/compiler.py", "pull request change\n")
        feature = self.repo.commit("Change core on feature")
        report = self.select(base_tip, feature, merge_base=True)
        self.assert_tools(report, {"core", "frontend", "app"})
        self.assertEqual(report["changedFiles"], ["core/src/compiler.py"])
        self.assertEqual(report["base"], self.base)
        self.assert_tools(self.select(base_tip, feature), self.tools)

    def test_tested_merge_commit_uses_merged_manifest_consumers(self):
        self.repo.git("branch", "feature", self.base)
        self.tools["new-consumer"] = tool("new-consumer", runtime_dependencies=["core"])
        self.repo.manifest(self.tools)
        self.repo.write("independent/src/compiler.py", "base-only change\n")
        base_tip = self.repo.commit("Main adds a consumer before merge")
        self.repo.git("checkout", "-q", "feature")
        self.repo.write("core/src/compiler.py", "feature change\n")
        self.repo.commit("Feature changes the provider")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "--no-ff", "--no-edit", "feature")
        merged = self.repo.git("rev-parse", "HEAD")
        report = self.select(base_tip, merged, merge_base=True)
        self.assert_tools(report, {"core", "frontend", "app", "new-consumer"})
        self.assertEqual(report["changedFiles"], ["core/src/compiler.py"])

    def test_rename_between_toolchains_keeps_deleted_and_added_paths(self):
        before = "core/src/name with space\nand newline.rs"
        after = "independent/src/name with space\nand newline.rs"
        self.repo.write(before, "same source\n")
        base = self.repo.commit("Add unusually named source")
        (self.root / before).rename(self.root / after)
        head = self.repo.commit("Move source between compiler packages")
        report = self.select(base, head)
        self.assert_tools(report, self.tools)
        self.assertEqual(set(report["changedFiles"]), {before, after})

    def test_deleted_external_source_input_still_selects_owner_and_consumers(self):
        path = "shared/old adapter.c"
        self.tools["core"]["sourceInputs"] = [path]
        self.repo.manifest(self.tools)
        self.repo.write(path, "old adapter\n")
        base = self.repo.commit("Track external compiler source")
        (self.root / path).unlink()
        report = self.select(base, self.repo.commit("Remove external source"))
        self.assert_tools(report, {"core", "frontend", "app"})
        self.assertEqual(report["changedFiles"], [path])

    def test_initial_missing_and_all_zero_base_build_every_current_tool(self):
        for base in (None, "missing-base", "0" * 40):
            with self.subTest(base=base):
                self.assert_tools(self.select(base), self.tools)

    def test_same_revision_has_no_work(self):
        report = self.select(self.base, self.base)
        self.assert_tools(report, [])
        self.assertEqual(report["changedFiles"], [])

    def test_shallow_history_falls_back_to_all_instead_of_dropping_compilers(self):
        self.repo.write("core/src/compiler.py", "new compiler\n")
        head = self.repo.commit("Advance history before shallow checkout")
        with tempfile.TemporaryDirectory(prefix="compiler-ci-shallow-") as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            shallow = Repository(checkout)
            shallow.git("fetch", "-q", "--depth=1", self.root.as_uri(), head)
            shallow.git("checkout", "-q", "--detach", "FETCH_HEAD")
            report = selection.select_toolchains(checkout, base=self.base, head=head)
            self.assert_tools(report, self.tools)

    def test_one_build_definition_change_does_not_rebuild_unrelated_tools(self):
        self.tools["frontend"]["version"] = "1.0.1"
        self.repo.manifest(self.tools)
        report = self.select(self.base, self.repo.commit("Update one compiler version"))
        self.assert_tools(report, {"frontend", "app"})
        self.assertEqual(report["changedFiles"], ["buildtools.json"])

    def test_build_manifest_key_and_entry_reordering_does_not_trigger_builds(self):
        manifest = json.loads((self.root / "buildtools.json").read_text())
        manifest["tools"] = {name: dict(reversed(list(item.items())))
                             for name, item in reversed(list(manifest["tools"].items()))}
        self.repo.write("buildtools.json", json.dumps(dict(reversed(list(manifest.items()))), separators=(",", ":")) + "\n")
        report = self.select(self.base, self.repo.commit("Reformat manifest without semantic changes"))
        self.assert_tools(report, [])
        self.assertEqual(report["changedFiles"], ["buildtools.json"])

    def test_new_tool_selects_itself_and_new_consumers_only(self):
        self.tools["new-provider"] = tool("new-provider")
        self.tools["new-consumer"] = tool("new-consumer", runtime_dependencies=["new-provider"])
        self.repo.manifest(self.tools)
        report = self.select(self.base, self.repo.commit("Add a provider and its consumer"))
        self.assert_tools(report, {"new-provider", "new-consumer"})

    def test_removed_tool_is_not_in_matrix_but_old_consumers_are(self):
        del self.tools["core"]
        self.tools["frontend"]["dependencies"] = []
        self.repo.manifest(self.tools)
        report = self.select(self.base, self.repo.commit("Remove provider and detach its consumer"))
        self.assert_tools(report, {"frontend", "app"})
        self.assertNotIn("core", report["toolchains"])

    def test_both_old_and_new_dependency_edges_reach_consumers(self):
        self.tools["app"]["runtimeDependencies"] = ["core"]
        self.tools["frontend"]["dependencies"] = []
        self.repo.manifest(self.tools)
        head = self.repo.commit("Move provider relationships")
        report = self.select(self.base, head)
        self.assert_tools(report, {"frontend", "app"})
        # Changes to the provider must still reach the removed edge in a push
        # that contains both the relationship edit and source update.
        self.repo.write("core/src/compiler.py", "changed provider\n")
        report = self.select(self.base, self.repo.commit("Change provider after dependency movement"))
        self.assert_tools(report, {"core", "frontend", "app"})

    def test_union_of_old_and_new_dependencies_can_cycle_without_hanging(self):
        self.tools = {"a": tool("a", dependencies=["b"]), "b": tool("b"), "unrelated": tool("unrelated")}
        self.repo.manifest(self.tools)
        base = self.repo.commit("A depends on B")
        self.tools["a"]["dependencies"] = []
        self.tools["b"]["runtimeDependencies"] = ["a"]
        self.repo.manifest(self.tools)
        self.repo.write("b/src/compiler.py", "changed provider\n")
        report = self.select(base, self.repo.commit("Reverse dependency in new revision"))
        self.assert_tools(report, {"a", "b"})

    def test_directory_matching_respects_component_boundaries(self):
        self.tools = {"a": tool("a"), "a-extra": tool("a-extra"), "other": tool("other")}
        self.repo.manifest(self.tools)
        self.repo.write("a-extra/src/compiler.py", "initial\n")
        base = self.repo.commit("Add similar compiler directory names")
        self.repo.write("a-extra/src/compiler.py", "changed\n")
        report = self.select(base, self.repo.commit("Update only longer directory name"))
        self.assert_tools(report, {"a-extra"})

    def test_packaged_docs_override_default_documentation_noop(self):
        self.tools["core"]["package"]["include"].append("docs/core-api.md")
        self.tools["independent"]["package"]["optional"].append("licenses/extra-license.txt")
        self.repo.manifest(self.tools)
        self.repo.write("docs/core-api.md", "initial docs\n")
        self.repo.write("licenses/extra-license.txt", "initial license\n")
        base = self.repo.commit("Declare packaged docs outside tool directories")
        self.repo.write("docs/core-api.md", "changed docs\n")
        report = self.select(base, self.repo.commit("Change shipped API documentation"))
        self.assert_tools(report, {"core", "frontend", "app"})
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.write("licenses/extra-license.txt", "changed license\n")
        report = self.select(base, self.repo.commit("Change optional shipped notice"))
        self.assert_tools(report, {"independent"})

    def test_external_packaging_and_directory_source_inputs_are_observed(self):
        self.tools["core"]["sourceInputs"] = ["shared/codegen"]
        self.tools["independent"]["package"]["include"].append("shared/packaged.json")
        self.repo.manifest(self.tools)
        self.repo.write("shared/codegen/schema.json", "{}\n")
        self.repo.write("shared/packaged.json", "{}\n")
        base = self.repo.commit("Declare external build and package inputs")
        self.repo.write("shared/codegen/schema.json", '{"changed":true}\n')
        report = self.select(base, self.repo.commit("Change file below external source directory"))
        self.assert_tools(report, {"core", "frontend", "app"})
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.write("shared/packaged.json", '{"changed":true}\n')
        report = self.select(base, self.repo.commit("Change external packaged file"))
        self.assert_tools(report, {"independent"})

    def test_shared_configuration_and_ci_code_build_everything(self):
        paths = ["build.py", "new-root-helper.py", "license-helper.py", "tools/ci-select.py", "tools/ci-prerequisites.py", ".github/workflows/ci.yml", "unowned/helpers.py"]
        for path in paths:
            with self.subTest(path=path):
                base = self.repo.git("rev-parse", "HEAD")
                self.repo.write(path, "shared CI or build implementation\n")
                self.assert_tools(self.select(base, self.repo.commit(f"Change {path}")), self.tools)
        self.tools["core"]["package"]["include"].append("wasm_tools.py")
        self.repo.manifest(self.tools)
        base = self.repo.commit("Declare a packaged root helper")
        self.repo.write("wasm_tools.py", "shared root helper still affects everything\n")
        self.assert_tools(self.select(base, self.repo.commit("Change packaged shared helper")), self.tools)
        base = self.repo.git("rev-parse", "HEAD")
        profiles = json.loads((self.root / "package-profiles.json").read_text())
        profiles["tools"]["core"]["exclude"].append("core/debug.log")
        self.repo.write("package-profiles.json", json.dumps(profiles) + "\n")
        self.assert_tools(self.select(base, self.repo.commit("Change shared packaging configuration")), self.tools)

    def test_global_build_manifest_fields_build_everything(self):
        for field, value in (("description", "Changed workspace contract"), ("future-global-field", {"strict": True})):
            with self.subTest(field=field):
                base = self.repo.git("rev-parse", "HEAD")
                manifest = json.loads((self.root / "buildtools.json").read_text())
                manifest[field] = value
                self.repo.write("buildtools.json", json.dumps(manifest) + "\n")
                self.assert_tools(self.select(base, self.repo.commit(f"Change global {field}")), self.tools)

    def test_unknown_future_manifest_schema_is_rejected_explicitly(self):
        self.repo.manifest(self.tools, schema=2)
        head = self.repo.commit("Require an unsupported manifest schema")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.select(self.base, head)

    def test_docs_and_licenses_do_not_schedule_compiler_builds(self):
        for path in ("README.md", "docs/ci.md", "docs/helper.py", "LICENSE", "LICENSE.txt", "COPYING"):
            self.repo.write(path, "documentation or license change\n")
        report = self.select(self.base, self.repo.commit("Update only docs and licenses"))
        self.assert_tools(report, [])

    def test_selection_uses_revision_files_and_ignores_dirty_worktree_manifest(self):
        self.repo.write("core/src/compiler.py", "committed compiler change\n")
        head = self.repo.commit("Change committed provider")
        # A dirty checkout must not redefine the CI target graph for that SHA.
        self.repo.manifest({"wrong": tool("wrong")})
        report = self.select(self.base, head)
        self.assert_tools(report, {"core", "frontend", "app"})

    def test_push_event_cli_uses_before_and_after_for_entire_pushed_range(self):
        self.repo.write("independent/src/compiler.py", "first pushed commit\n")
        self.repo.commit("Push independent compiler update first")
        self.repo.write("core/src/compiler.py", "second pushed commit\n")
        head = self.repo.commit("Push shared compiler update second")
        # Event revisions must win over whichever revision is checked out.
        self.repo.git("checkout", "-q", "--detach", self.base)
        report = self.event_cli("push", {"before": self.base, "after": head})
        self.assert_tools(report, self.tools)
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["head"], head)
        self.assertEqual(set(report["changedFiles"]), {"core/src/compiler.py", "independent/src/compiler.py"})

    def test_pull_request_event_cli_selects_against_tested_merge_manifest(self):
        self.repo.git("branch", "feature", self.base)
        self.tools["new-consumer"] = tool("new-consumer", runtime_dependencies=["core"])
        self.repo.manifest(self.tools)
        self.repo.write("independent/src/compiler.py", "base-only advancement\n")
        base_tip = self.repo.commit("Main adds consumer before pull request test")
        self.repo.git("checkout", "-q", "feature")
        self.repo.write("core/src/compiler.py", "pull request provider change\n")
        feature_tip = self.repo.commit("Pull request changes shared compiler")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "--no-ff", "--no-edit", "feature")
        merged = self.repo.git("rev-parse", "HEAD")
        event = {"pull_request": {"base": {"sha": base_tip}, "head": {"sha": feature_tip}}}
        report = self.event_cli("pull_request", event, tested_sha=merged)
        self.assert_tools(report, {"core", "frontend", "app", "new-consumer"})
        self.assertEqual(report["base"], base_tip)
        self.assertEqual(report["head"], merged)
        self.assertEqual(report["changedFiles"], ["core/src/compiler.py"])

    def test_github_cli_appends_json_matrix_and_boolean_for_changed_and_docs_only_ranges(self):
        for path, expected in (("core/src/compiler.py", {"core", "frontend", "app"}), ("docs/ci.md", set())):
            with self.subTest(path=path):
                base = self.repo.git("rev-parse", "HEAD")
                self.repo.write(path, "CLI selection change\n")
                head = self.repo.commit(f"Change {path} for CLI")
                outputs = self.root / "github-outputs"
                outputs.write_text("preserved=value\n")
                environment = dict(self.repo.environment, GITHUB_OUTPUT=str(outputs))
                process = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--base", base, "--head", head],
                                         cwd=self.root, env=environment, text=True, capture_output=True, check=True)
                report = json.loads(process.stdout)
                self.assert_tools(report, expected)
                fields = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
                self.assertEqual(fields["preserved"], "value")
                self.assertEqual(set(json.loads(fields["toolchains"])), expected)
                self.assertEqual(fields["has-toolchains"], "true" if expected else "false")
                outputs.unlink()


class MatrixPartitionTests(unittest.TestCase):
    def outputs(self, tools):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            output.write_text("existing=value\n")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                selection.write_github_outputs(tools)
            fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(fields["existing"], "value")
        return fields

    def test_empty_selection_skips_all_build_groups(self):
        fields = self.outputs([])
        for name in ("toolchains", "llvm-toolchains", "rust-toolchains", "other-toolchains"):
            self.assertEqual(json.loads(fields[name]), [])
            self.assertEqual(fields["has-" + name], "false")

    def test_go_only_does_not_wait_for_an_llvm_bootstrap(self):
        fields = self.outputs(["go"])
        self.assertEqual(fields["has-llvm-toolchains"], "false")
        self.assertEqual(fields["has-rust-toolchains"], "false")
        self.assertEqual(json.loads(fields["other-toolchains"]), ["go"])

    def test_zig_only_still_schedules_shared_backend(self):
        fields = self.outputs(["zig"])
        self.assertEqual(fields["has-llvm-toolchains"], "true")
        self.assertEqual(json.loads(fields["llvm-toolchains"]), ["zig"])
        self.assertEqual(fields["has-rust-toolchains"], "false")
        self.assertEqual(fields["has-other-toolchains"], "false")

    def test_rust_only_schedules_rust_family_without_other_build_groups(self):
        fields = self.outputs(["rust"])
        self.assertEqual(fields["has-rust-toolchains"], "true")
        self.assertEqual(json.loads(fields["rust-toolchains"]), ["rust"])
        self.assertEqual(fields["has-llvm-toolchains"], "false")
        self.assertEqual(fields["has-other-toolchains"], "false")

    def test_rust_backend_only_still_schedules_rust_family_build(self):
        fields = self.outputs(["llvm-rust"])
        self.assertEqual(fields["has-rust-toolchains"], "true")
        self.assertEqual(json.loads(fields["rust-toolchains"]), ["llvm-rust"])
        self.assertEqual(fields["has-llvm-toolchains"], "false")
        self.assertEqual(fields["has-other-toolchains"], "false")

    def test_both_rust_targets_share_one_group_and_preserve_selected_order(self):
        selected = ["llvm-rust", "rust"]
        fields = self.outputs(selected)
        self.assertEqual(json.loads(fields["toolchains"]), selected)
        self.assertEqual(json.loads(fields["rust-toolchains"]), selected)
        self.assertEqual(fields["has-rust-toolchains"], "true")
        self.assertEqual(fields["has-llvm-toolchains"], "false")
        self.assertEqual(fields["has-other-toolchains"], "false")

    def test_full_manifest_partitions_without_losing_targets(self):
        tools = list(json.loads((ROOT / "buildtools.json").read_text())["tools"])
        fields = self.outputs(tools)
        llvm = json.loads(fields["llvm-toolchains"])
        rust = json.loads(fields["rust-toolchains"])
        others = json.loads(fields["other-toolchains"])
        self.assertEqual(set(llvm), {"clang", "llvm", "zig", "nim", "ocaml", "goose", "teavm", "java", "kotlin"})
        self.assertEqual(set(rust), {"rust"})
        for index, group in enumerate((llvm, rust, others)):
            self.assertEqual(group, [name for name in tools if name in group])
            for other in (llvm, rust, others)[index + 1:]:
                self.assertFalse(set(group) & set(other))
        self.assertEqual(sorted(llvm + rust + others), sorted(tools))
        self.assertEqual(fields["has-llvm-toolchains"], "true")
        self.assertEqual(fields["has-rust-toolchains"], "true")
        self.assertEqual(fields["has-other-toolchains"], "true")
        self.assertEqual(json.loads(fields["toolchains"]), tools)


class RealManifestTests(GitFixtureCase):
    def setUp(self):
        super().setUp()
        self.tools = json.loads((ROOT / "buildtools.json").read_text())["tools"]
        self.repo.write("buildtools.json", (ROOT / "buildtools.json").read_text())
        self.repo.write("package-profiles.json", (ROOT / "package-profiles.json").read_text())
        self.base = self.repo.commit("Install real compiler manifests")

    def test_real_manifest_profiles_follow_shared_build_and_runtime_dependencies(self):
        cases = [
            ("clang/src/clang_wasm_main.cpp", {"clang", "llvm", "nim", "zig", "rust", "ocaml", "goose", "teavm", "java", "kotlin"}),
            ("nim/src/c_backend.cpp", {"clang", "llvm", "nim", "zig", "rust", "ocaml", "goose", "teavm", "java", "kotlin"}),
            ("zig/compile.py", {"zig"}),
            ("rust/scripts/runner.py", {"rust"}),
            ("swift/src/adapter.swift", {"swift", "llvm-swift"}),
            ("llvm/toolchain.toml", {"clang", "llvm", "nim", "zig", "rust", "swift", "llvm-swift", "ocaml", "goose", "teavm", "java", "kotlin"}),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                base = self.repo.git("rev-parse", "HEAD")
                self.repo.write(path, "compiler or shared provider change\n")
                self.assert_tools(self.select(base, self.repo.commit(f"Change real input {path}")), expected)

    def test_real_manifest_docs_only_is_noop_but_packaged_readme_is_not(self):
        self.repo.write("README.md", "workspace documentation\n")
        self.repo.write("docs/ci.md", "CI documentation\n")
        report = self.select(self.base, self.repo.commit("Change real workspace docs"))
        self.assert_tools(report, [])
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.write("clang/README.md", "changed bundled compiler documentation\n")
        self.assert_tools(self.select(base, self.repo.commit("Change a packaged tool README")), {"clang", "llvm", "nim", "zig", "rust", "ocaml", "goose", "teavm", "java", "kotlin"})

    def test_real_rust_source_change_selects_only_one_rust_family_job(self):
        self.repo.write("rust/scripts/runner.py", "changed Rust compiler runner\n")
        head = self.repo.commit("Change Rust source without touching shared LLVM files")
        report = self.select(self.base, head)
        self.assertEqual(report["toolchains"], ["rust"])
        self.assertEqual(report["changedFiles"], ["rust/scripts/runner.py"])
        outputs = self.root / "github-outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}):
            selection.write_github_outputs(report["toolchains"])
        fields = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
        self.assertEqual(json.loads(fields["rust-toolchains"]), ["rust"])
        self.assertEqual(fields["has-rust-toolchains"], "true")
        self.assertEqual(fields["has-llvm-toolchains"], "false")
        self.assertEqual(fields["has-other-toolchains"], "false")


if __name__ == "__main__":
    unittest.main()
