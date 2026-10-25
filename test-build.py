#!/usr/bin/env python3
"""Check compiler build plans and real archive publication without compiler rebuilds."""
from __future__ import annotations

import difflib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import zipfile
import hashlib
from unittest import mock

ROOT = Path(__file__).resolve().parent
specification = importlib.util.spec_from_file_location("compiler_builds", ROOT / "build.py")
builds = importlib.util.module_from_spec(specification)
specification.loader.exec_module(builds)


class VendoredBootstrapInputTests(unittest.TestCase):
    def test_pinned_license_needs_no_network_and_rejects_modified_bytes(self):
        spec = importlib.util.spec_from_file_location('teavm_bootstrap_test', ROOT / 'teavm/bootstrap.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'licenses/ASM.txt'
            source.parent.mkdir()
            source.write_bytes(b'pinned license\n')
            item = {'source': 'licenses/ASM.txt', 'bytes': source.stat().st_size,
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
            with mock.patch.object(module, 'ROOT', root), mock.patch.object(module.urllib.request, 'urlopen',
                    side_effect=AssertionError('vendored licenses must not download')):
                self.assertEqual(module.fetch(item, root / 'cache/license', offline=True), source)
                source.write_bytes(b'pinned licensE\n')
                with self.assertRaisesRegex(ValueError, 'vendored bootstrap input checksum mismatch'):
                    module.fetch(item, root / 'cache/license')


def tool(name, *, dependencies=(), runtime_dependencies=()):
    return {"directory": name, "description": name, "version": "1.0.0",
            "dependencies": list(dependencies), "runtimeDependencies": list(runtime_dependencies),
            "bootstrapRequirements": [], "build": [], "test": [],
            "artifacts": [name + "/dist/compiler.wasm"],
            "package": {"include": [name + "/dist", name + "/compile.py"], "optional": []}}


class RustAcceptanceEvidenceTests(unittest.TestCase):
    def test_partial_acceptance_never_marks_candidate_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results.json").write_text(json.dumps({"status": "passed", "cases": [
                {"name": "scalar", "status": "passed"}]}))
            candidate = root / "candidate"
            candidate.mkdir()
            with self.assertRaisesRegex(ValueError, "all six"):
                builds.record_rust_acceptance(candidate, root)
            self.assertFalse((candidate / "runtime-adapter.json").exists())

    def test_completed_acceptance_records_identity_without_copying_private_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, results = root / "candidate", root / "results"
            results.mkdir()
            names = ["scalar", "std", "base64", "build-script", "derive", "serde"]
            (results / "results.json").write_text(json.dumps({"status": "passed", "cases": [
                {"name": name, "status": "passed"} for name in names]}))
            (results / "runner.log").write_text("private execution log")
            (results / "metadata.json").write_text('{"path":"/home/developer/private"}')
            for relative in ("scripts/runner.py", "build/bootstrap/rustc.wasm"):
                path = candidate / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())
            (candidate / "toolchain.json").write_text(json.dumps({
                "rustVersion": "1.99.1", "compilerHost": "wasm32-wasip3", "verification": {}}))
            builds.record_rust_acceptance(candidate, results)
            adapter = json.loads((candidate / "runtime-adapter.json").read_text())
            self.assertEqual(adapter["status"], "passed")
            self.assertEqual(adapter["compilerSha256"], builds.digest(candidate / "build/bootstrap/rustc.wasm"))
            self.assertEqual(adapter["receiptFiles"], {})
            self.assertEqual(adapter["acceptanceResultSha256"], builds.digest(results / "results.json"))
            self.assertFalse((candidate / "verification").exists())
            self.assertEqual((results / "runner.log").read_text(), "private execution log")
            manifest = json.loads((candidate / "toolchain.json").read_text())
            self.assertEqual(manifest["status"], "standalone-verified")
            self.assertTrue(manifest["verification"]["serdeAcceptancePassed"])


class BinaryenConfigurationTests(unittest.TestCase):
    def test_rust_optimizer_uses_the_selected_binaryen_distribution_layout(self):
        manifest = builds.load_manifest()
        for relative_library in ("lib/x86_64-linux-gnu", "lib"):
            with self.subTest(layout=relative_library), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                optimizer = root / "binaryen/bin/wasm-opt"
                optimizer.parent.mkdir(parents=True)
                optimizer.touch()
                libraries = root / "binaryen" / relative_library
                libraries.mkdir(parents=True)
                with mock.patch.dict(builds.os.environ, {"WISP_WASM_OPT": str(optimizer)}, clear=True):
                    settings = builds.configuration(root, 2)
                plan = builds.build_plan(manifest, ["rust"], settings)
                rust = next(item for item in plan["tools"] if item["name"] == "rust")
                command = next(step["argv"] for step in rust["build"]
                               if "--wasm-opt-library-dir" in step.get("argv", []))
                selected = Path(command[command.index("--wasm-opt-library-dir") + 1])
                self.assertEqual(selected, libraries)
                self.assertTrue(selected.is_dir())

    def test_explicit_optimizer_library_directory_is_preserved(self):
        with mock.patch.dict(builds.os.environ, {"WISP_WASM_OPT_LIBRARY_DIR": "/custom/libraries"}, clear=True):
            self.assertEqual(builds.configuration(ROOT, 2)["wasm_opt_lib"], "/custom/libraries")


class BuildPlanTests(unittest.TestCase):
    def test_all_packages_have_build_test_artifacts_and_safe_paths(self):
        manifest = builds.load_manifest()
        self.assertEqual(set(manifest["tools"]),
                         {"clang", "go", "llvm", "rust", "zig", "assemblyscript", "nim", "javascript", "python", "wasm-tools", "grain", "ocaml", "goose", "swift", "llvm-swift", "jvm", "teavm", "java", "kotlin", "tsrs"})
        for name, item in manifest["tools"].items():
            self.assertEqual(item["directory"], "llvm" if name.startswith("llvm-") else name)
            for key in ("build", "test", "artifacts", "bootstrapRequirements"):
                self.assertTrue(item[key], (name, key))
            self.assertTrue((ROOT / item["directory"]).is_dir())

    def test_dependency_build_order(self):
        manifest = builds.load_manifest()
        self.assertEqual(builds.dependency_order(manifest, ["nim"]), ["clang", "llvm", "nim"])
        self.assertEqual(builds.dependency_order(manifest, ["javascript", "python"]),
                         ["wasm-tools", "javascript", "python"])

    def test_cycle_is_rejected(self):
        manifest = {"tools": {"a": tool("a", dependencies=["b"]), "b": tool("b", dependencies=["a"])}}
        with self.assertRaisesRegex(ValueError, "cycle"):
            builds.dependency_order(manifest, ["a"])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            builds.dependency_order(builds.load_manifest(), ["unknown-tool"])

    def test_plan_needs_no_compiler_artifacts_and_writes_nothing(self):
        manifest = builds.load_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = builds.configuration(root, 3)
            plan = builds.build_plan(manifest, list(manifest["tools"]), settings)
            self.assertEqual(len(plan["order"]), len(manifest["tools"]))
            self.assertEqual(len(plan["tools"]), len(manifest["tools"]))
            self.assertEqual(list(root.iterdir()), [])
            rust = next(item for item in plan["tools"] if item["name"] == "rust")
            compiler = next(step["argv"] for step in rust["build"]
                            if any(argument.endswith("/rust/scripts/build-compiler.py")
                                   for argument in step.get("argv", [])))
            self.assertIn("--jobs", compiler)

    def test_build_executes_each_dependency_once_and_passes_job_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"tools": {"shared": tool("shared"), "a": tool("a", dependencies=["shared"]),
                                  "b": tool("b", dependencies=["shared"])}}
            for name, item in manifest["tools"].items():
                program = ("from pathlib import Path; import os; "
                           f"p=Path({name!r})/'dist/compiler.wasm'; p.parent.mkdir(parents=True,exist_ok=True); "
                           "p.write_bytes(bytes.fromhex('0061736d01000000')); "
                           f"f=open('calls','a'); f.write({name!r}+' '+os.environ['BUILD_JOBS']+'\\n'); f.close()")
                item["build"] = [{"argv": ["{python}", "-c", program], "environment": {"BUILD_JOBS": "{jobs}"}}]
            builds.execute(manifest, ["a", "b"], "build", root=root, settings=builds.configuration(root, 3))
            self.assertEqual((root / "calls").read_text().splitlines(), ["shared 3", "a 3", "b 3"])
            for name in manifest["tools"]:
                receipt = json.loads((root / "build/receipts" / name / "build.json").read_text())
                self.assertTrue(receipt["passed"])

    def test_changed_source_during_build_cannot_receive_a_successful_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a/dist").mkdir(parents=True)
            (root / "a/compiler.cpp").write_text("original source\n")
            (root / "a/dist/compiler.wasm").write_bytes(bytes.fromhex("0061736d01000000"))
            item = tool("a")
            item["sourceInputs"] = ["a/compiler.cpp"]
            item["build"] = [{"argv": ["{python}", "-c", "from pathlib import Path; Path('a/compiler.cpp').write_text('changed source')"]}]
            with self.assertRaisesRegex(ValueError, "source inputs changed"):
                builds.execute({"tools": {"a": item}}, ["a"], "build", root=root,
                               settings=builds.configuration(root, 2))
            self.assertFalse((root / "build/receipts/a/build.json").exists())


@unittest.skipUnless(shutil.which("patch"), "Rust source preparation requires patch")
class RustSourcePatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "rust/build/source-rust-0123456789ab"
        self.source.mkdir(parents=True)
        self.before = {"compiler/macros.rs": "fn bridge() { old_bridge(); }\n",
                       "Cargo.lock": 'version = 3\nchecksum = "old-interpreter"\n',
                       "src/llvm-project/SmallVector.h": "#include <cstddef>\n#include <cstdlib>\n"}
        self.macro_after = {"compiler/macros.rs": "fn bridge() { wasm_bridge(); }\n",
                            "Cargo.lock": 'version = 3\nchecksum = "pinned-wasmi"\n',
                            "compiler/wasm_proc_macro.rs": "fn wasm_bridge() {}\n"}
        self.build_after = {"src/llvm-project/SmallVector.h":
                            "#include <cstddef>\n#include <cstdint>\n#include <cstdlib>\n"}
        self.write_source(self.before)
        self.lock = {"sources": [{"revision": "0123456789abcdef"}],
                     "macro_backport": self.make_patch("macro.patch", self.macro_after),
                     "build_patches": [self.make_patch("smallvector.patch", self.build_after)]}
        self.lock_path = self.root / "rust/source-lock.json"
        self.lock_path.write_text(json.dumps(self.lock))

    def write_source(self, files):
        for name, contents in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)

    def make_patch(self, filename, after):
        path = self.root / "rust/patches" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join("".join(difflib.unified_diff(
            self.before.get(name, "").splitlines(keepends=True), contents.splitlines(keepends=True),
            fromfile="a/" + name if name in self.before else "/dev/null", tofile="b/" + name))
            for name, contents in after.items()))
        return {"path": "patches/" + filename, "sha256": builds.digest(path)}

    def prepare(self):
        builds.action("rust-source", root=self.root, settings=builds.configuration(self.root, 2))

    def snapshot(self):
        return {path.relative_to(self.source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.source.rglob("*") if path.is_file()}

    def assert_fully_patched(self):
        expected = {**self.before, **self.macro_after, **self.build_after}
        actual = {path.relative_to(self.source).as_posix(): path.read_text()
                  for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected)

    def test_fresh_sources_apply_macro_and_build_patches(self):
        self.prepare()
        self.assert_fully_patched()

    def test_fully_patched_sources_are_reused_without_writes(self):
        self.write_source({**self.macro_after, **self.build_after})
        before = self.snapshot()
        self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assert_fully_patched()

    def test_applied_macro_still_allows_pending_build_patch(self):
        self.write_source(self.macro_after)
        macro_before = {name: self.snapshot()[name] for name in self.macro_after}
        self.prepare()
        self.assert_fully_patched()
        self.assertEqual({name: self.snapshot()[name] for name in self.macro_after}, macro_before)

    def test_applied_build_patch_does_not_skip_fresh_macro_patch(self):
        self.write_source(self.build_after)
        self.prepare()
        self.assert_fully_patched()

    def test_partially_applied_macro_is_rejected_without_writing_hunks(self):
        self.write_source({"Cargo.lock": self.macro_after["Cargo.lock"]})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "cannot apply compiler patch cleanly"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_divergent_macro_is_rejected_without_writing_earlier_hunks(self):
        self.write_source({"Cargo.lock": "unrelated edits to the lockfile\n"})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "cannot apply compiler patch cleanly"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_all_patch_checksums_are_checked_before_source_changes(self):
        patch = self.root / "rust" / self.lock["build_patches"][0]["path"]
        patch.write_text(patch.read_text() + "tampered patch\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "compiler patch checksum mismatch"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = {"tools": {"compiler": tool("compiler", runtime_dependencies=["shared"]),
                                   "shared": tool("shared")}}
        for name in self.manifest["tools"]:
            directory = self.root / name
            (directory / "dist").mkdir(parents=True)
            (directory / "dist/compiler.wasm").write_bytes(bytes.fromhex("0061736d01000000"))
            (directory / "compile.py").write_text("print('adapter')\n")
        self.output = self.root / "archives"

    def package(self):
        return builds.package_tool(self.manifest, "compiler", root=self.root, output_dir=self.output)

    def test_archive_is_deterministic_and_declares_external_runtime_dependency(self):
        first = self.package()
        (self.root / "compiler/compile.py").touch()
        second = self.package()
        self.assertEqual(first["sha256"], second["sha256"])
        archive = Path(first["archive"])
        self.assertEqual(archive.stat().st_mode & 0o777, 0o644)
        self.assertEqual(archive.name, "compiler.wisp.zip")
        self.assertEqual(archive.with_suffix(archive.suffix + ".sha256").read_text(),
                         first["sha256"] + "  compiler.wisp.zip\n")
        with zipfile.ZipFile(archive) as outer:
            self.assertEqual(set(outer.namelist()), {"release.json", "toolchain.tar.gz"})
            self.assertTrue(all(entry.compress_type == zipfile.ZIP_STORED for entry in outer.infolist()))
            release = json.loads(outer.read("release.json"))
            self.assertEqual((release["schema"], release["kind"]), (4, "toolchain"))
            inventory = release["archive"]["inventory"]
            self.assertEqual(release["requirements"][0]["name"], "wisp:shared")
            self.assertFalse(release["requirements"][0]["included"])
            self.assertEqual(inventory["compiler/compile.py"]["sha256"],
                             builds.digest(self.root / "compiler/compile.py"))
            with tarfile.open(fileobj=io.BytesIO(outer.read("toolchain.tar.gz")), mode="r:gz") as stream:
                names = stream.getnames()
                self.assertNotIn("wisp-buildtools/shared/dist/compiler.wasm", names)
                self.assertNotIn("wisp-buildtools/PACKAGE.json", names)
                self.assertNotIn("wisp-buildtools/SHA256SUMS", names)
                self.assertTrue(all(member.mtime == member.uid == member.gid == 0 for member in stream))

    def test_packaging_consumer_does_not_require_provider_build_tree(self):
        (self.root / "shared/dist/compiler.wasm").unlink()
        result = self.package()
        self.assertTrue(Path(result["archive"]).is_file())

    def test_native_compiler_cannot_be_packaged_as_wasm(self):
        (self.root / "compiler/dist/compiler.wasm").write_bytes(b"\x7fELFnative")
        with self.assertRaisesRegex(ValueError, "not Wasm"):
            self.package()

    def test_symlink_input_cannot_escape_archive(self):
        (self.root / "compiler/dist/leak").symlink_to(self.root / "shared/compile.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.package()

    def test_package_excludes_bytecode_and_does_not_collect_source_caches(self):
        (self.root / "compiler/dist/__pycache__").mkdir()
        (self.root / "compiler/dist/__pycache__/cached.pyc").write_bytes(b"cached")
        (self.root / "compiler/.sources").mkdir()
        (self.root / "compiler/.sources/big-download").write_bytes(b"not a package input")
        (self.root / "compiler/dist/.work").mkdir()
        (self.root / "compiler/dist/.work/active-compiler-cache").write_bytes(b"temporary compiler output")
        result = self.package()
        with zipfile.ZipFile(result["archive"]) as outer:
            with tarfile.open(fileobj=io.BytesIO(outer.read("toolchain.tar.gz")), mode="r:gz") as stream:
                self.assertFalse(any("__pycache__" in name or ".sources" in name or ".work" in name
                                     for name in stream.getnames()))

    def test_runtime_dependency_cycle_is_rejected(self):
        self.manifest["tools"]["shared"]["runtimeDependencies"] = ["compiler"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.package()

    def test_unsafe_inventory_path_is_rejected(self):
        inventory = self.root / "checksums.json"
        inventory.write_text(json.dumps({"../secret": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "safe repository-relative"):
            builds.verify_checksum_list(self.root, inventory)

    def test_changed_pinned_guest_cannot_be_republished_with_a_new_inventory(self):
        directory = self.root / "wasm-tools"
        (directory / "dist").mkdir(parents=True)
        (directory / "dist/wasm-tools.wasm").write_bytes(bytes.fromhex("0061736d01000000"))
        (directory / "toolchain.lock.json").write_text(json.dumps({"module_sha256": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "compiler package checksum mismatch"):
            builds.verify_package_inputs("wasm-tools", self.root)


class ZigBackendTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("zig_bootstrap_under_test", ROOT / "zig/bootstrap.py")
        self.zig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.zig)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "zig").mkdir()
        (self.root / "clang").mkdir()
        self.backend = self.root / "clang/compiler.component"
        self.backend.write_bytes(bytes.fromhex("0061736d0d000100"))
        (self.root / "clang/compiler.cpp").write_text("compiler source\n")
        self.clang = {"source": {"repository": "https://example.test/llvm", "revision": "expected-revision"},
                    "sourceInputs": ["clang/compiler.cpp"]}
        self.recipe = self.root / "buildtools.json"
        self.recipe.write_text(json.dumps({"tools": {"clang": self.clang}}))
        lock = {"clang_wasm": {"sha256": "0" * 64, "llvm_revision": "expected-revision", "source_build": {
            "receipt_schema": 1, "recipe": "../buildtools.json", "artifact": "clang/compiler.component",
            "required_interface": "wasi:cli/run@0.3.0", "required_smoke_result": 42}}}
        self.root_patch = mock.patch.object(self.zig, "ROOT", self.root / "zig")
        self.lock_patch = mock.patch.object(self.zig, "LOCK", lock)
        self.root_patch.start()
        self.lock_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.lock_patch.stop)
        self.receipt = self.root / "receipt.json"
        self.record = {"schema": 1, "tool": "clang", "operation": "build", "passed": True,
                       "recipeSha256": builds.digest(self.recipe), "source": self.clang["source"],
                       "sourceInputs": {"clang/compiler.cpp": builds.digest(self.root / "clang/compiler.cpp")},
                       "artifacts": {"clang/compiler.component": builds.digest(self.backend)},
                       "validation": {"componentInterface": "wasi:cli/run@0.3.0", "smokeResult": 42}}
        self.write_receipt()

    def write_receipt(self):
        self.receipt.write_text(json.dumps(self.record))

    def test_default_backend_still_requires_the_original_pinned_hash(self):
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            self.zig.backend_provenance(self.backend)

    def test_explicit_verified_source_build_accepts_its_recorded_artifact(self):
        proof = self.zig.backend_provenance(self.backend, self.receipt)
        self.assertEqual(proof["kind"], "verified-source-build")
        self.assertEqual(proof["sha256"], builds.digest(self.backend))

    def test_source_change_invalidates_the_receipt(self):
        (self.root / "clang/compiler.cpp").write_text("changed compiler source\n")
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            self.zig.backend_provenance(self.backend, self.receipt)

    def test_missing_component_smoke_check_invalidates_the_receipt(self):
        self.record["validation"] = {}
        self.write_receipt()
        with self.assertRaisesRegex(RuntimeError, "lacks the required Component"):
            self.zig.backend_provenance(self.backend, self.receipt)

    def test_changed_backend_invalidates_the_receipt(self):
        self.backend.write_bytes(self.backend.read_bytes() + b"changed")
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            self.zig.backend_provenance(self.backend, self.receipt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
