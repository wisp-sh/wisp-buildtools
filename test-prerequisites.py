#!/usr/bin/env python3
"""Check CI prerequisite staging, verified host reuse, and compiler cache setup."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


prerequisites = load_module("compiler_ci_prerequisites", ROOT / "tools/ci-prerequisites.py")
rust_builder = load_module("compiler_rust_source_builder", ROOT / "rust/scripts/build-compiler.py")


class BootstrapVersionTests(unittest.TestCase):
    def test_stock_bootstrap_cannot_drift_from_the_execution_host_version(self):
        lock = copy.deepcopy(prerequisites.LOCK)
        lock['downloads']['wasmtime']['executable'] = 'wasmtime-v0.0.0-x86_64-linux/wasmtime'
        with mock.patch.object(prerequisites, 'LOCK', lock):
            with self.assertRaisesRegex(ValueError, 'Wasmtime versions differ'):
                prerequisites.check_lock()


class RustPrerequisiteStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compiler-ci-prerequisites-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        host_recipe = self.root / 'tools/shared-hosts'
        host_recipe.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'tools/shared-hosts/wasmtime.lock.json', host_recipe / 'wasmtime.lock.json')
        self.cache = self.root / ".ci/downloads"
        self.install = self.root / ".ci/toolchains"
        self.cache.mkdir(parents=True)
        self.payload = b"Pinned SDK archive fixture\x00\x01\x02\xff\n"
        self.checksum = hashlib.sha256(self.payload).hexdigest()
        self.lock = copy.deepcopy(prerequisites.LOCK)
        self.lock["downloads"]["wasi-sdk-34"]["sha256"] = self.checksum
        spec = self.lock["downloads"]["wasi-sdk-34"]
        self.archive = self.cache / spec["url"].rsplit("/", 1)[-1]
        self.archive.write_bytes(self.payload)
        self.sdk = (self.install / "wasi-sdk-34" / spec["executable"]).parents[1]
        self.expected_archive = self.root / "rust/downloads" / self.archive.name
        self.expected_sdk = self.expected_archive.parent / self.sdk.name

        self.source_lock = json.loads((ROOT / "rust/source-lock.json").read_text())
        self.source_lock["wasi_sdk"].update(sha256=self.checksum, archive_bytes=len(self.payload))
        self.source = self.root / "rust/build/fixture-source"
        self.prepare_source()

        self.addCleanup(mock.patch.stopall)
        mock.patch.multiple(prerequisites, ROOT=self.root, CACHE=self.cache,
                            INSTALL=self.install, LOCK=self.lock).start()
        mock.patch.object(rust_builder, "ROOT", self.root / "rust").start()
        self.installer = mock.patch.object(prerequisites, "install", side_effect=self.fake_install).start()
        # Keep the real cached-download checksum check. Any network use fails.
        self.downloader = mock.patch.object(prerequisites, "download", wraps=prerequisites.download).start()
        mock.patch.object(prerequisites.urllib.request, "urlopen",
                          side_effect=AssertionError("tests must not download tools")).start()
        mock.patch.object(prerequisites.subprocess, "run").start()
        mock.patch.object(prerequisites.subprocess, "check_output",
                          return_value=str(self.root / "host-rust/bin/rustc") + "\n").start()
        mock.patch.object(prerequisites, "execution_host", side_effect=self.fake_execution_host).start()
        # Never append paths or variables to the invoking runner's environment files.
        mock.patch.object(prerequisites, "github_environment").start()

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def prepare_source(self):
        revisions = {item["name"]: item["revision"] for item in self.source_lock["sources"]}
        self.write("wisp-source-receipt.json", json.dumps({
            "sourceRevisions": revisions, "archiveChecksumsVerified": True,
        }))
        self.write("wisp-source-lock.json", json.dumps(self.source_lock))
        for name in ("Cargo.lock", "x.py", "src/bootstrap/bootstrap.py",
                     "src/bootstrap/src/core/build_steps/llvm.rs",
                     "src/bootstrap/src/core/build_steps/install.rs",
                     "src/bootstrap/src/core/build_steps/compile.rs", "src/bootstrap/src/core/builder/mod.rs",
                     "compiler/rustc_llvm/build.rs", "library/sysroot/Cargo.toml",
                     "src/llvm-project/cmake/Modules/LLVMVersion.cmake"):
            self.write(name, "locked source fixture\n")
        stage0 = "".join("dist/" + item["url"].split("/dist/", 1)[1] + "=" + item["sha256"] + "\n"
                         for item in self.source_lock.get("source_bootstrap", self.source_lock["bootstrap"])["artifacts"])
        self.write("src/stage0", stage0)
        self.write("src/version", self.source_lock["rust_version"].removesuffix("-dev") + "\n")

    def fake_install(self, name):
        executable = self.install / name / self.lock["downloads"][name]["executable"]
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"tool executable fixture\n")
        if name == "wasi-sdk-34":
            for tool in ("clang", "clang++", "wasm32-wasip1-threads-clang", "wasm32-wasip1-threads-clang++",
                         "wasm32-wasip1-clang", "wasm32-wasip1-clang++", "llvm-ar", "llvm-ranlib",
                         "ar", "ranlib", "wasm-ld"):
                (executable.parent / tool).write_bytes(b"SDK executable fixture\n")
        return executable

    def fake_execution_host(self, recipe, prefix, cache):
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "wasmtime").write_bytes(b"execution host fixture\n")
        (prefix / "receipt.json").write_text("{}\n")

    def validate(self):
        return rust_builder.validate_inputs(self.source, self.expected_sdk, self.source_lock)

    def assert_rust_staging(self, tool):
        self.assertFalse(self.expected_archive.exists())
        report = prerequisites.install_for(tool)
        self.assertEqual(self.expected_archive.read_bytes(), self.payload)
        self.assertEqual(self.expected_archive.stat().st_size, self.source_lock["wasi_sdk"]["archive_bytes"])
        self.assertEqual(prerequisites.digest(self.expected_archive), self.source_lock["wasi_sdk"]["sha256"])
        self.assertEqual(self.expected_archive.resolve(), self.archive.resolve())
        self.assertEqual(self.expected_sdk.resolve(), self.sdk.resolve())
        self.assertEqual(Path(report["environment"]["WISP_RUST_WASI_SDK"]), self.sdk)
        self.assertEqual(report["downloads"]["wasi-sdk-34"], self.checksum)
        self.assertEqual(self.validate()["wasiSdkArchiveSha256"], self.checksum)
        self.downloader.assert_called_once_with("wasi-sdk-34")
        return report

    def test_rust_stages_verified_archive_for_real_source_validation(self):
        self.assert_rust_staging("rust")
        prerequisites.subprocess.run.assert_any_call(
            ["rustup", "toolchain", "install", self.lock["rustBootstrap"]["version"],
             "--profile", "minimal", "--target", self.lock["rustBootstrap"]["target"]], check=True)

    def test_javascript_uses_its_pinned_rust_after_global_bootstrap_upgrade(self):
        manifest = json.loads((ROOT / "javascript/checksums/wasm-tools.json").read_text())
        path = self.root / "javascript/checksums/wasm-tools.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest))
        expected = manifest["bootstrapRustc"].split()[1]
        self.lock["rustBootstrap"]["version"] = "9.9.9"

        prerequisites.install_for("javascript")

        prerequisites.subprocess.run.assert_any_call(
            ["rustup", "toolchain", "install", expected, "--profile", "minimal",
             "--target", "wasm32-wasip1"], check=True)
        prerequisites.subprocess.check_output.assert_called_once_with(
            ["rustup", "which", "--toolchain", expected, "rustc"], text=True)
        _, paths = prerequisites.github_environment.call_args.args
        self.assertIn(str(self.root / "host-rust/bin"), paths)

    def test_reinstall_refreshes_stale_archive_and_sdk_symlinks(self):
        self.assert_rust_staging("rust")
        self.expected_archive.unlink()
        self.expected_archive.symlink_to(self.root / "missing-old-cache/sdk.tar.gz")
        self.expected_sdk.unlink()
        self.expected_sdk.symlink_to(self.root / "missing-old-install/sdk", target_is_directory=True)
        prerequisites.install_for("rust")
        self.assertEqual(self.expected_archive.read_bytes(), self.payload)
        self.assertEqual(self.expected_sdk.resolve(), self.sdk.resolve())
        self.assertEqual(self.validate()["wasiSdkArchiveSha256"], self.checksum)

    def test_unrelated_target_does_not_stage_rust_sdk(self):
        report = prerequisites.install_for("go")
        self.assertEqual(report["tool"], "go")
        self.assertFalse((self.root / "rust/downloads").exists())
        self.assertNotIn("WISP_RUST_WASI_SDK", report["environment"])
        self.assertNotIn("wasi-sdk-34", report["downloads"])
        self.downloader.assert_not_called()

    def test_source_validation_requires_original_archive_after_staging(self):
        self.assert_rust_staging("rust")
        self.expected_archive.unlink()
        with self.assertRaises(FileNotFoundError):
            self.validate()

    def test_source_validation_rejects_same_size_archive_corruption(self):
        self.assert_rust_staging("rust")
        self.archive.write_bytes(self.payload[::-1])
        self.assertEqual(self.expected_archive.stat().st_size, len(self.payload))
        with self.assertRaisesRegex(ValueError, "checksum/size mismatch"):
            self.validate()


class ExecutionHostCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compiler-ci-host-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        host_recipe = self.root / 'tools/shared-hosts'
        host_recipe.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'tools/shared-hosts/wasmtime.lock.json', host_recipe / 'wasmtime.lock.json')
        (self.root / ".ci").mkdir()
        self.host_commands = []
        self.check_failure = None
        self.build_failure = None
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(prerequisites, "ROOT", self.root).start()
        mock.patch.object(prerequisites, "install", side_effect=self.fake_install).start()
        mock.patch.object(prerequisites, "download", side_effect=self.fake_archive).start()
        mock.patch.object(prerequisites.urllib.request, "urlopen",
                          side_effect=AssertionError("tests must not download tools")).start()
        mock.patch.object(prerequisites.subprocess, "run", side_effect=self.run_command).start()
        mock.patch.object(prerequisites.subprocess, "check_output",
                          return_value=str(self.root / "host-rust/bin/rustc") + "\n").start()
        self.environment = mock.patch.object(prerequisites, "github_environment").start()

    def fake_install(self, name):
        executable = self.root / ".ci/toolchains" / name / prerequisites.LOCK["downloads"][name]["executable"]
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"tool fixture\n")
        return executable

    def fake_archive(self, name):
        archive = self.root / ".ci/downloads" / prerequisites.LOCK["downloads"][name]["url"].rsplit("/", 1)[-1]
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive fixture\n")
        return archive

    def run_command(self, command, *, check):
        self.assertTrue(check)
        if len(command) > 1 and Path(command[1]).name in {"build.py", "build-rust.py"}:
            self.host_commands.append(command)
            failure = self.check_failure if "--check" in command else self.build_failure
            if failure:
                raise failure(1, command)
            if "--check" not in command:
                prefix = Path(command[command.index("--prefix") + 1])
                self.write_host(prefix, {"rebuilt": True})
        return subprocess.CompletedProcess(command, 0)

    def prefix(self, tool):
        version = "50.0.0"
        return self.root / ".ci/hosts" / f"wisp-wasmtime-{version}"

    def write_host(self, prefix, receipt=None):
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "wasmtime").write_bytes(b"host fixture\n")
        (prefix / "receipt.json").write_text(json.dumps(receipt or {"cached": True}))

    def assert_host_commands(self, tool, checks):
        recipe = "build.py"
        self.assertEqual(len(self.host_commands), len(checks))
        for command, expected_check in zip(self.host_commands, checks):
            self.assertEqual(command[:4], [sys.executable, str(self.root / "tools/shared-hosts" / recipe),
                                           "--prefix", str(self.prefix(tool))])
            self.assertEqual("--check" in command, expected_check)
            self.assertEqual("--cache" in command, not expected_check)

    def test_all_shared_consumers_reuse_only_after_successful_host_check(self):
        for tool in (*prerequisites.SHARED_LLVM_CONSUMERS, *prerequisites.SHARED_RUST_CONSUMERS):
            with self.subTest(tool=tool):
                self.host_commands.clear()
                self.write_host(self.prefix(tool))
                report = prerequisites.install_for(tool)
                self.assert_host_commands(tool, [True])
                variable = "WISP_RUST_WASMTIME" if tool in prerequisites.SHARED_RUST_CONSUMERS else "WISP_WASMTIME"
                self.assertEqual(report["environment"][variable], str(self.root / 'cwasm.py'))
                self.assertEqual(report['environment']['WISP_CWASM_RUNTIME'], str(self.prefix(tool) / 'wasmtime'))
                self.assertEqual(report['environment']['WISP_BOOTSTRAP_WASMTIME'], str(self.prefix(tool) / 'wasmtime'))
                self.assertNotIn('wasmtime', report['downloads'])
                self.assertEqual(report["executionHosts"][self.prefix(tool).name], {"cached": True})

    def test_only_python_snapshot_bootstrap_installs_stock_wasmtime(self):
        python = prerequisites.install_for('python')
        stock = self.root / '.ci/toolchains/wasmtime' / prerequisites.LOCK['downloads']['wasmtime']['executable']
        self.assertEqual(python['environment']['WISP_BOOTSTRAP_WASMTIME'], str(stock))
        self.assertIn('wasmtime', python['downloads'])
        self.assertEqual(python['environment']['WISP_WASMTIME'], str(self.root / 'cwasm.py'))
        for tool in ('go', 'wasm-tools', 'assemblyscript'):
            with self.subTest(tool=tool):
                report = prerequisites.install_for(tool)
                self.assertNotIn('wasmtime', report['downloads'])
                self.assertEqual(report['environment']['WISP_BOOTSTRAP_WASMTIME'], str(self.prefix(tool) / 'wasmtime'))

    def test_missing_binary_or_receipt_builds_without_accepting_partial_cache(self):
        for tool in ("clang", "rust"):
            for missing in ("wasmtime", "receipt.json"):
                with self.subTest(tool=tool, missing=missing):
                    self.host_commands.clear()
                    self.write_host(self.prefix(tool))
                    (self.prefix(tool) / missing).unlink()
                    report = prerequisites.install_for(tool)
                    self.assert_host_commands(tool, [False])
                    self.assertEqual(report["executionHosts"][self.prefix(tool).name], {"rebuilt": True})

    def test_failed_check_rebuilds_instead_of_accepting_stale_or_corrupt_receipt(self):
        self.check_failure = subprocess.CalledProcessError
        for tool in ("clang", "rust"):
            with self.subTest(tool=tool):
                self.host_commands.clear()
                self.write_host(self.prefix(tool))
                (self.prefix(tool) / "receipt.json").write_text("corrupt cache receipt")
                report = prerequisites.install_for(tool)
                self.assert_host_commands(tool, [True, False])
                self.assertEqual(report["executionHosts"][self.prefix(tool).name], {"rebuilt": True})

    def test_failed_rebuild_propagates_and_does_not_export_invalid_host(self):
        self.check_failure = subprocess.CalledProcessError
        self.build_failure = subprocess.CalledProcessError
        for tool in ("clang", "rust"):
            with self.subTest(tool=tool):
                self.host_commands.clear()
                self.write_host(self.prefix(tool))
                with self.assertRaises(subprocess.CalledProcessError):
                    prerequisites.install_for(tool)
                self.assert_host_commands(tool, [True, False])
                self.environment.assert_not_called()
                self.assertFalse((self.root / ".ci/prerequisites.json").exists())

    def test_unselected_cached_host_is_neither_trusted_nor_read(self):
        self.write_host(self.prefix("rust"))
        (self.prefix("rust") / "receipt.json").write_text("unselected stale cache receipt")
        self.write_host(self.prefix("clang"))
        report = prerequisites.install_for("clang")
        self.assert_host_commands("clang", [True])
        self.assertEqual(report["executionHosts"], {self.prefix("clang").name: {"cached": True}})


class ClangCompilerCacheConfigurationTests(unittest.TestCase):
    def test_native_and_wasm_configurations_use_available_ccache_only(self):
        # Stub external commands so both real shell configuration paths execute
        # without downloading or compiling LLVM.
        for available in (False, True):
            with self.subTest(ccache=available), tempfile.TemporaryDirectory(prefix="clang-cache-config-") as temporary:
                root = Path(temporary) / "with spaces"
                root.mkdir()
                shutil.copy2(ROOT / "clang/build-clang-wasi.sh", root / "build-clang-wasi.sh")
                tools, commands = root / ".toolchains", root / "commands"
                commands.mkdir()
                for name in ("dirname", "mkdir", "grep", "cmp"):
                    (commands / name).symlink_to(shutil.which(name))
                (commands / "python3").symlink_to(sys.executable)

                def executable(path, body):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"#!{sys.executable}\n" + body)
                    path.chmod(0o755)

                executable(commands / "sha256sum", "import sys\nsys.stdin.read()\n")
                log = root / "cmake.jsonl"
                executable(commands / "cmake", "import json,os,sys\nwith open(os.environ['CMAKE_LOG'], 'a') as log:\n log.write(json.dumps({'args': sys.argv[1:], 'status': os.environ['NINJA_STATUS']}) + '\\n')\n")
                if available:
                    executable(commands / "ccache", "raise AssertionError('CMake stub must not compile')\n")
                executable(tools / "bin/ninja", "raise AssertionError('CMake stub must not build')\n")
                for name in ("llvm-project-d297af3.tar.gz", "wasi-sdk-34.0-x86_64-linux.tar.gz",
                             "wasm-tools-1.262.0-x86_64-linux.tar.gz", "ninja-linux.zip"):
                    (tools / name).write_bytes(b"archive fixture")
                source = tools / "llvm-project"
                (source / "llvm").mkdir(parents=True)
                driver = source / "clang/tools/driver"
                driver.mkdir(parents=True)
                (driver / "CMakeLists.txt").write_text("add_clang_tool(clang-wasm\n")
                (root / "src").mkdir()
                for path in (root / "src/clang_wasm_main.cpp", driver / "clang_wasm_main.cpp"):
                    path.write_text("driver fixture\n")
                headers = tools / "wasi-sdk-34.0-x86_64-linux/lib/clang/23/include"
                headers.mkdir(parents=True)
                (headers / "stddef.h").write_text("header fixture\n")
                executable(tools / "wasm-tools-1.262.0-x86_64-linux/wasm-tools", "pass\n")
                environment = {**os.environ, "PATH": str(commands), "CMAKE_LOG": str(log),
                               "BUILD_JOBS": "2", "COMPILER_OPT": "O1", "NINJA_STATUS": "test progress "}
                result = subprocess.run([shutil.which("bash"), str(root / "build-clang-wasi.sh")],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                invocations = [json.loads(line) for line in log.read_text().splitlines()]
                configurations = [item for item in invocations if "--fresh" in item["args"]]
                self.assertEqual(len(configurations), 2)
                for configuration in configurations:
                    launchers = [arg for arg in configuration["args"] if "COMPILER_LAUNCHER=" in arg]
                    expected = [f"-DCMAKE_C_COMPILER_LAUNCHER={commands / 'ccache'}",
                                f"-DCMAKE_CXX_COMPILER_LAUNCHER={commands / 'ccache'}"] if available else []
                    self.assertEqual(launchers, expected)
                    self.assertEqual(configuration["status"], "test progress ")
                self.assertEqual((root / "include/stddef.h").read_text(), "header fixture\n")


if __name__ == "__main__":
    unittest.main()
