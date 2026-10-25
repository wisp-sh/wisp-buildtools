"""Protect source-build completion and candidate publication boundaries."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-compiler.py"
spec = importlib.util.spec_from_file_location("wisp_source_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class SourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wisp-source-build-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.out, self.sdk = [self.root / name for name in ("source", "output", "sdk")]
        for directory in (self.source, self.out, self.sdk, self.root / "scripts", self.root / "licenses"):
            directory.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(builder, "ROOT", self.root).start()
        patch.object(builder, "macro_notices", return_value=(b"fixture notices\n", [])).start()
        self.lock = {"schema_version": 1, "bootstrap": {"host": "x86_64-unknown-linux-gnu"},
                     "compiler_host_target": "wasm32-wasip3",
                     "application_targets": ["wasm32-wasip1", "wasm32-unknown-unknown"]}
        (self.root / "source-lock.json").write_text(json.dumps(self.lock))
        (self.root / "scripts/runner.py").write_text("# candidate runner fixture\n")
        (self.source / "Cargo.lock").write_text("version = 3\n")
        (self.out / "config.toml").write_text("# completed-build configuration fixture\n")
        (self.out / "build-driver.py").write_text("# original build driver fixture\n")
        (self.out / "source-lock.json").write_bytes((self.root / "source-lock.json").read_bytes())
        compiler = self.out / "install/bin/rustc.wasm"
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(builder.COMPONENT_WASM)
        self.compiler = compiler
        for target in [self.lock["compiler_host_target"], *self.lock["application_targets"]]:
            directory = self.out / "install/lib/rustlib" / target / "lib"
            directory.mkdir(parents=True)
            for name in ("libstd-fixture.rlib", "libproc_macro-fixture.rlib"):
                (directory / name).write_bytes(b"sysroot fixture")
        manifest = {"schemaVersion": 1, "rustRepository": "https://example.invalid/rust",
                    "rustRevision": "rust-revision", "llvmRevision": "llvm-revision",
                    "compilerHost": self.lock["compiler_host_target"], "applicationTarget": "wasm32-wasip1",
                    "runtime": {"version": "50.0.0"}, "licenses": {}}
        (self.root / "toolchain.json").write_text(json.dumps(manifest))
        self.report = {"schemaVersion": 1, "mode": "build", "status": "build-passed",
                       "sourceRoot": str(self.source), "compilerBuildCompleted": True,
                       "compilerHost": self.lock["compiler_host_target"], "applicationTargets": self.lock["application_targets"],
                       "compiler": {"path": str(compiler), "sha256": builder.digest(compiler)},
                       "compilerProbe": {"exitCode": 0},
                       "buildDriverSha256": builder.digest(self.out / "build-driver.py"),
                       "sourceLockSha256": builder.digest(self.out / "source-lock.json"),
                       "configSha256": builder.digest(self.out / "config.toml"),
                       "runnerSha256": builder.digest(self.root / "scripts/runner.py"),
                       "inputs": {"sourceRevisions": {"rust": "rust-revision", "llvm": "llvm-revision"},
                                  "sourceInputs": {"Cargo.lock": builder.digest(self.source / "Cargo.lock")}}}
        self.save_report()

    def save_report(self):
        (self.out / "build-report.json").write_text(json.dumps(self.report))

    def package(self):
        builder.package_candidate(self.source, self.out, self.root / "candidate", True)

    def test_plan_records_explicit_and_environment_selected_compiler_cache(self):
        # Planning must wire ccache into upstream LLVM configuration and retain
        # its provenance without executing bootstrap or any cache executable.
        for selection in ("disabled", "argument", "environment"):
            with self.subTest(selection=selection):
                arguments = [str(SCRIPT), "--plan", "--source", str(self.source),
                             "--out", str(self.out), "--wasi-sdk", str(self.sdk)]
                for name in ("ninja", "cmake", "cc", "cxx"):
                    arguments += ["--" + name, sys.executable]
                environment = {"WISP_RUST_CCACHE": ""}
                if selection == "argument":
                    arguments += ["--ccache", sys.executable]
                elif selection == "environment":
                    environment["WISP_RUST_CCACHE"] = sys.executable
                with patch.dict(builder.os.environ, environment), patch.object(sys, "argv", arguments), \
                        patch.object(builder, "validate_inputs", return_value={}), \
                        patch.object(builder.subprocess, "run", side_effect=AssertionError("plan executed a tool")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(builder.main(), 0)
                configuration = builder.tomllib.loads((self.out / "config.toml").read_text())
                report = json.loads((self.out / "build-plan.json").read_text())
                if selection == "disabled":
                    self.assertNotIn("ccache", configuration["llvm"])
                    self.assertNotIn("ccache", report["nativeTools"])
                else:
                    executable = Path(sys.executable).resolve()
                    self.assertEqual(configuration["llvm"]["ccache"], str(executable))
                    self.assertEqual(report["nativeTools"]["ccache"],
                                     {"path": str(executable), "sha256": builder.digest(executable)})

    def test_missing_selected_compiler_cache_fails_before_source_validation(self):
        arguments = [str(SCRIPT), "--plan", "--source", str(self.source),
                     "--out", str(self.out), "--wasi-sdk", str(self.sdk),
                     "--ccache", str(self.root / "missing-cache")]
        for name in ("ninja", "cmake", "cc", "cxx"):
            arguments += ["--" + name, sys.executable]
        with patch.object(sys, "argv", arguments), patch.object(builder, "validate_inputs") as validate:
            with self.assertRaisesRegex(ValueError, "missing executable"):
                builder.main()
            validate.assert_not_called()

    def test_completed_candidate_is_separate_and_keeps_acceptance_unverified(self):
        self.package()
        candidate = self.root / "candidate"
        manifest = json.loads((candidate / "toolchain.json").read_text())
        self.assertEqual(manifest["status"], "candidate-unverified")
        self.assertFalse(manifest["verification"]["firstUsableRelease"])
        self.assertFalse(manifest["verification"]["serdeAcceptancePassed"])
        self.assertEqual(manifest["runtime"]["provider"], "wisp")
        self.assertFalse(any((candidate / "build/bootstrap").glob("wasmtime-*")))
        self.assertEqual((candidate / "scripts/runner.py").read_bytes(), (self.root / "scripts/runner.py").read_bytes())
        self.assertTrue((candidate / "build/bootstrap/SHA256SUMS.json").is_file())
        self.assertFalse((self.root / "build/bootstrap").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.package()

    def prepare_build_outputs(self):
        objects = self.out / "obj"
        compiler = objects / self.lock["compiler_host_target"] / "stage2/bin/rustc.wasm"
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(self.compiler.read_bytes())
        sysroot = objects / self.lock["bootstrap"]["host"] / "stage1/lib/rustlib"
        for target in [self.lock["compiler_host_target"], *self.lock["application_targets"]]:
            source = self.out / "install/lib/rustlib" / target / "lib"
            builder.shutil.copytree(source, sysroot / target / "lib")
        return compiler, sysroot

    def test_staging_uses_wasm_stage2_and_complete_stage1_sysroots_without_native_stage2(self):
        compiler, sysroot = self.prepare_build_outputs()
        # Obsolete libraries from earlier builds must not survive staging.
        stale = self.out / "install/lib/rustlib/obsolete/lib/libstd-stale.rlib"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"obsolete")
        builder.stage_build_outputs(self.out, self.lock)
        self.assertEqual(self.compiler.read_bytes(), compiler.read_bytes())
        targets = [self.lock["compiler_host_target"], *self.lock["application_targets"]]
        builder.check_install(self.out / "install", targets)
        self.assertFalse(stale.exists())
        for target in targets:
            for library in (sysroot / target / "lib").iterdir():
                self.assertEqual((self.out / "install/lib/rustlib" / target / "lib" / library.name).read_bytes(),
                                 library.read_bytes())
        self.assertFalse((self.out / "install/lib/rustlib" / self.lock["bootstrap"]["host"]).exists())
        self.assertFalse((self.out / "obj" / self.lock["bootstrap"]["host"] / "stage2").exists())

    def test_incomplete_or_invalid_build_outputs_preserve_previous_install(self):
        compiler, sysroot = self.prepare_build_outputs()
        before = {str(path.relative_to(self.out / "install")): path.read_bytes()
                  for path in (self.out / "install").rglob("*") if path.is_file()}
        compiler.write_bytes(b"native executable")
        with self.assertRaisesRegex(ValueError, "not a Wasm Component"):
            builder.stage_build_outputs(self.out, self.lock)
        compiler.write_bytes(self.compiler.read_bytes())
        (sysroot / self.lock["application_targets"][1] / "lib/libproc_macro-fixture.rlib").unlink()
        with self.assertRaisesRegex(ValueError, "missing std or proc_macro"):
            builder.stage_build_outputs(self.out, self.lock)
        after = {str(path.relative_to(self.out / "install")): path.read_bytes()
                 for path in (self.out / "install").rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_source_alias_cannot_reuse_another_source_root_receipt(self):
        alias = self.root / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.report["sourceRoot"] = str(alias)
        self.save_report()
        with self.assertRaisesRegex(ValueError, "installed-compiler probe"):
            self.package()
        self.assertFalse((self.root / "candidate").exists())

    def test_different_source_directory_cannot_use_an_old_build_receipt(self):
        other = self.root / "different-source"
        other.mkdir()
        self.report["sourceRoot"] = str(other)
        self.save_report()
        with self.assertRaisesRegex(ValueError, "installed-compiler probe"):
            self.package()
        self.assertFalse((self.root / "candidate").exists())

    def test_missing_or_incomplete_build_report_cannot_publish_a_candidate(self):
        (self.out / "build-report.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.package()
        for field, value in [("mode", "dry-run"), ("status", "failed"), ("compilerBuildCompleted", "true")]:
            report = dict(self.report)
            report[field] = value
            (self.out / "build-report.json").write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "completed source build"):
                self.package()
        self.assertFalse((self.root / "candidate").exists())

    def test_changed_executable_inputs_cannot_be_packaged(self):
        for path in (self.compiler, self.root / "scripts/runner.py", self.out / "config.toml", self.source / "Cargo.lock"):
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                self.package()
            path.write_bytes(original)
        self.assertFalse((self.root / "candidate").exists())

    def test_missing_runner_or_compiler_cannot_be_packaged(self):
        for path in (self.compiler, self.root / "scripts/runner.py"):
            original = path.read_bytes()
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                self.package()
            path.write_bytes(original)
        self.assertFalse((self.root / "candidate").exists())

    def test_plan_and_dry_run_preserve_the_completed_build_receipt(self):
        receipt = (self.out / "build-report.json").read_bytes()
        snapshots = {name: (self.out / name).read_bytes() for name in ("build-driver.py", "source-lock.json")}
        tool = self.root / "native-tool"
        tool.write_bytes(b"native tool fixture")
        macro_patch = self.root / "macro.patch"
        macro_patch.write_text("diff --git a/Cargo.lock b/Cargo.lock\n")
        self.lock["macro_backport"] = {"sha256": builder.digest(macro_patch),
                                      "patched_cargo_lock_sha256": builder.digest(self.source / "Cargo.lock"),
                                      "macro_target": "wasm32-unknown-unknown"}
        (self.root / "source-lock.json").write_text(json.dumps(self.lock))

        def run(argv, **kwargs):
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(builder.COMPONENT_WASM)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(builder, "validate_inputs", return_value=self.report["inputs"]), \
             patch.object(builder, "executable", return_value=tool), \
             patch.object(builder, "check_patch"), patch.object(builder.subprocess, "run", side_effect=run):
            for mode in ("--plan", "--dry-run"):
                argv = [str(SCRIPT), mode, "--source", str(self.source), "--out", str(self.out),
                        "--wasi-sdk", str(self.sdk), "--macro-patch", str(macro_patch)]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(builder.main(), 0)
                self.assertEqual((self.out / "build-report.json").read_bytes(), receipt)
                for name, content in snapshots.items():
                    self.assertEqual((self.out / name).read_bytes(), content)
        self.assertEqual(json.loads((self.out / "dry-run-report.json").read_text())["status"], "dry-run-passed")
        for name in ("build-plan.json", "dry-run-report.json"):
            report = json.loads((self.out / name).read_text())
            command = report["command"]
            self.assertEqual(command[command.index("--stage") + 1], "1")
            self.assertEqual(command[3], "build")
            self.assertEqual(command[-3:-1] if "--dry-run" in command else command[-2:],
                             ["compiler/rustc", "library"])
            self.assertEqual(report["bootstrapStage"], 1)
            self.assertEqual(report["compilerStage"], 2)

    def test_actual_compiler_size_is_checked_before_reading_the_module(self):
        with self.compiler.open("r+b") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "256 MiB"):
            self.package()
        self.assertFalse((self.root / "candidate").exists())

    def test_macro_patch_and_dependency_lock_must_match_the_source_lock(self):
        macro_patch = self.root / "macro.patch"
        macro_patch.write_text("fixture patch")
        self.lock["macro_backport"] = {"sha256": builder.digest(macro_patch),
                                      "patched_cargo_lock_sha256": builder.digest(self.source / "Cargo.lock"),
                                      "macro_target": "wasm32-unknown-unknown"}
        builder.check_macro_pins(self.source, macro_patch, self.lock)
        macro_patch.write_text("changed patch")
        with self.assertRaisesRegex(ValueError, "patch checksum"):
            builder.check_macro_pins(self.source, macro_patch, self.lock)
        macro_patch.write_text("fixture patch")
        (self.source / "Cargo.lock").write_text("changed dependency lock")
        with self.assertRaisesRegex(ValueError, "Cargo.lock checksum"):
            builder.check_macro_pins(self.source, macro_patch, self.lock)

    def test_optimizer_is_skipped_when_raw_compiler_already_fits(self):
        with patch.object(builder.subprocess, "run", side_effect=AssertionError("unexpected optimizer")):
            result = builder.optimize_large_compiler(self.compiler, self.out, None, {})
        self.assertFalse(result["applied"])
        self.assertEqual(result["inputSha256"], builder.digest(self.compiler))

    def test_build_patch_pins_both_the_patch_and_every_changed_source(self):
        header = self.source / "header.h"
        header.write_text("#include <cstddef>\n#include <cstdint>\n")
        directory = self.root / "patches"
        directory.mkdir()
        build_patch = directory / "header.patch"
        build_patch.write_text("diff --git a/header.h b/header.h\n--- a/header.h\n+++ b/header.h\n@@ -1 +1,2 @@\n #include <cstddef>\n+#include <cstdint>\n")
        self.lock["build_patches"] = [{"path": "patches/header.patch", "sha256": builder.digest(build_patch),
                                       "source_files": {"header.h": builder.digest(header)}}]
        records = builder.check_build_patches(self.source, self.lock)
        self.assertTrue(records[0]["reverseApplyCheckPassed"])
        self.assertEqual(records[0]["sourceFiles"], {"header.h": builder.digest(header)})
        header.write_text(header.read_text() + "// changed\n")
        with self.assertRaisesRegex(ValueError, "source hashes"):
            builder.check_build_patches(self.source, self.lock)
        build_patch.write_text(build_patch.read_text() + "changed\n")
        with self.assertRaisesRegex(ValueError, "patch checksum"):
            builder.check_build_patches(self.source, self.lock)


class Wasip3BuildTests(unittest.TestCase):
    def test_optimizer_preserves_import_encoding_supported_by_wasmtime50(self):
        native = os.environ.get("WISP_WASM_OPT") or shutil.which("wasm-opt")
        if not native:
            candidate = builder.ROOT.parent / ".ci/toolchains/binaryen/binaryen-version_133/bin/wasm-opt"
            if candidate.is_file():
                native = str(candidate)
        if not native:
            self.skipTest("native Binaryen is required for the import-encoding regression")
        optimizer = Path(shutil.which(native) or native).resolve()
        # Two exported imports from one module trigger Binaryen 133's compact
        # import writer when all proposals are enabled. Our Wasmtime 50 profile
        # keeps compact imports disabled.
        imports = bytes.fromhex("02110203656e760166000003656e7601670000")
        module = (builder.CORE_WASM + bytes.fromhex("010401600000") + imports
                  + bytes.fromhex("0709020166000001670001"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            compiler = out / "compiler.wasm"
            compiler.write_bytes(module)
            library = optimizer.parent.parent / "lib"
            if (library / "x86_64-linux-gnu").is_dir():
                library /= "x86_64-linux-gnu"
            profile = {"path": str(optimizer), "sha256": builder.digest(optimizer),
                       "libraryDirectory": str(library), "libraries": {},
                       "memoryLimitMiB": 1024, "timeoutSeconds": 30}
            builder.optimize_large_compiler(compiler, out, profile, {}, force=True, flags=())
            self.assertIn(imports, compiler.read_bytes())

    def test_component_optimizer_preserves_other_modules_and_outer_sections(self):
        def section(kind, content):
            self.assertLess(len(content), 128)
            return bytes([kind, len(content)]) + content

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            compiler = out / "compiler.wasm"
            auxiliary = section(1, builder.CORE_WASM)
            before = section(0, b"\x04namebefore") + auxiliary
            after = section(0, b"\x04nameafter")
            main = builder.CORE_WASM + section(0, b"\x04namemain")
            replacement = builder.CORE_WASM + section(0, b"\x04namenew")
            original = builder.COMPONENT_WASM + before + section(1, main) + after
            compiler.write_bytes(original)

            passes = []
            def optimize(core, output, profile, environment, *, force, flags):
                self.assertTrue(force)
                self.assertEqual(core.read_bytes(), replacement if passes else main)
                passes.append(flags)
                core.write_bytes(replacement)
                return {"applied": True}

            with patch.object(builder, "optimize_large_compiler", side_effect=optimize):
                receipt = builder.optimize_compiler_component(compiler, out, {"path": "optimizer"}, {})
            self.assertEqual(compiler.read_bytes(), builder.COMPONENT_WASM + before + section(1, replacement) + after)
            self.assertEqual(Path(receipt["rawCompiler"]).read_bytes(), original)
            self.assertEqual(receipt["outputSha256"], builder.digest(compiler))
            self.assertEqual(passes, [("--duplicate-function-elimination", "--remove-unused-module-elements", "--vacuum"), ("-O1",)])

    def test_component_optimizer_rejects_truncated_sections_without_changing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            compiler = out / "compiler.wasm"
            original = builder.COMPONENT_WASM + b"\x01\x40truncated"
            compiler.write_bytes(original)
            with self.assertRaisesRegex(ValueError, "truncated"):
                builder.optimize_compiler_component(compiler, out, {"path": "optimizer"}, {})
            self.assertEqual(compiler.read_bytes(), original)

    def test_cross_build_preserves_native_compiler_metadata_identity(self):
        lock = {"external_llvm": {"version": "24.0.0git"}, "rust_version": "1.99.1",
                "bootstrap": {"host": "native", "compiler_description": "58cf8f9c3 2026-10-13"},
                "compiler_host_target": "wasm32-wasip3",
                "application_targets": ["wasm32-wasip1", "wasm32-unknown-unknown"]}
        native = {name: Path("native") / name for name in ("rustc", "cargo", "llvm-config", "cc", "cxx")}
        config = builder.tomllib.loads(builder.configuration(Path("source"), Path("out"), Path("sdk"), native, 2, lock))
        version = lock["rust_version"] + " (" + config["build"]["description"] + ")"
        self.assertEqual(version, "1.99.1 (58cf8f9c3 2026-10-13)")
        self.assertTrue(config["build"]["local-rebuild"])
        self.assertFalse(config["rust"]["debug-assertions"])
        self.assertTrue(config["rust"]["remap-debuginfo"])
        self.assertEqual(config["target"]["wasm32-wasip3"]["llvm-config"], "native/llvm-config")
        for target in [lock['compiler_host_target'], *lock['application_targets']]:
            flags = config['target'][target]['rustflags']
            self.assertTrue(any(flag.endswith('=/rustc/source') for flag in flags), target)
            self.assertTrue(any(flag.endswith('=/build-user') for flag in flags), target)

    def test_source_and_output_remaps_remain_single_flags_with_spaces(self):
        lock = {"external_llvm": {}, "rust_version": "1.99.1",
                "bootstrap": {"host": "native", "compiler_description": "test"},
                "compiler_host_target": "wasm32-wasip3",
                "application_targets": ["wasm32-unknown-unknown"]}
        lock['external_llvm'] = {'version': '24'}
        native = {name: Path("native") / name for name in ("rustc", "cargo", "llvm-config", "cc", "cxx")}
        source, out = Path('source with spaces'), Path('output with spaces')
        config = builder.tomllib.loads(builder.configuration(source, out, Path('sdk'), native, 1, lock))
        flags = config['target']['wasm32-unknown-unknown']['rustflags']
        self.assertIn(f'--remap-path-prefix={source.resolve()}=/rustc/source', flags)
        self.assertIn(f'--remap-path-prefix={out.resolve()}=/wisp-buildtools/rust/build', flags)

    def test_component_install_and_stage0_sysroots(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            lock = {"compiler_host_target": "wasm32-wasip3", "compiler_stage": 1,
                    "bootstrap_stage": 0, "bootstrap": {"host": "native"},
                    "application_targets": ["wasm32-wasip1", "wasm32-unknown-unknown"]}
            compiler = out / "obj/native/stage1-rustc/wasm32-wasip3/release/rustc-main.wasm"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(builder.COMPONENT_WASM)
            targets = [lock["compiler_host_target"], *lock["application_targets"]]
            for target in targets:
                libraries = out / "obj/native/stage0-sysroot/lib/rustlib" / target / "lib"
                libraries.mkdir(parents=True)
                for name in ("std", "proc_macro"):
                    (libraries / f"lib{name}-fixture.rlib").write_bytes(target.encode())
            builder.stage_build_outputs(out, lock)
            installed = builder.check_install(out / "install", targets)
            self.assertEqual(installed.read_bytes(), builder.COMPONENT_WASM)
            installed.write_bytes(builder.CORE_WASM)
            with self.assertRaisesRegex(ValueError, "format"):
                builder.check_install(out / "install", targets)

    def test_external_sdk_rejects_wrong_host_and_changed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            selected = {"revision": "pinned", "version": "24.0.0git",
                        "host_target": "wasm32-wasip3", "metadata": "sdk.json",
                        "inventory": "SHA256SUMS"}
            metadata = {"llvm_revision": "pinned", "version": "24.0.0git",
                        "host_target": "wasm32-wasip3"}
            (sdk / "sdk.json").write_text(json.dumps(metadata))
            archive = sdk / "libLLVMCore.a"
            archive.write_bytes(b"verified SDK library")
            (sdk / "SHA256SUMS").write_text(builder.digest(archive) + "  libLLVMCore.a\n")
            self.assertEqual(builder.validate_llvm_sdk(sdk, selected)["verifiedFiles"], 1)
            archive.write_bytes(b"changed library")
            with self.assertRaisesRegex(ValueError, "checksum"):
                builder.validate_llvm_sdk(sdk, selected)
            metadata["host_target"] = "wasm32-wasip1-threads"
            (sdk / "sdk.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "host ABI"):
                builder.validate_llvm_sdk(sdk, selected)


if __name__ == "__main__":
    unittest.main()
