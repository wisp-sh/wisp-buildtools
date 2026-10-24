#!/usr/bin/env python3
"""Check adapters with support packages in separate installation payloads."""
from __future__ import annotations

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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent


class InstalledAdaptersTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.packages = {}
        for name in ("zig", "nim", "javascript", "python", "grain", "swift", "java", "kotlin", "teavm", "jvm", "llvm"):
            destination = self.root / name / "payload" / name
            destination.mkdir(parents=True)
            self.packages[name] = destination
            # Preserve script-local imports without copying compiler binaries or dist data.
            for path in (ROOT / name).glob("*.py"):
                shutil.copyfile(path, destination / path.name)
        shutil.copyfile(ROOT / "python/toolchain.lock.json", self.packages["python"] / "toolchain.lock.json")
        scripts = self.packages["python"] / "scripts"
        scripts.mkdir()
        shutil.copyfile(ROOT / "python/scripts/common.py", scripts / "common.py")
        wasm_tools = self.root / "wasm-tools/payload/wasm-tools"
        wasm_tools.mkdir(parents=True)
        shutil.copyfile(ROOT / "wasm_tools.py", wasm_tools.parent / "wasm_tools.py")
        self.environment.update({
            "WISP_LLVM_TOOLCHAIN": str(self.packages["llvm"]),
            "WISP_JVM_TOOLCHAIN": str(self.packages["jvm"]),
            "WISP_TEAVM_TOOLCHAIN": str(self.packages["teavm"]),
            "WISP_WASM_TOOLS_TOOLCHAIN": str(wasm_tools),
        })

    def run_python(self, *arguments):
        result = subprocess.run([sys.executable, "-B", *map(str, arguments)],
                                cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_help_imports_shared_helpers_without_a_source_checkout(self):
        for name, script in [("javascript", "compile.py"), ("python", "compile.py"),
                             ("grain", "component.py"), ("swift", "compile.py"),
                             ("java", "compile.py"), ("kotlin", "compile.py"),
                             ("teavm", "compile.py"), ("teavm", "component.py")]:
            with self.subTest(name=name, script=script):
                self.assertIn("usage:", self.run_python(self.packages[name] / script, "--help").stdout)

    def test_java_backend_imports_jvm_llvm_and_wasm_tools_from_separate_payloads(self):
        self.run_python("-c", f"""
import sys
sys.path.insert(0, {str(self.packages['java'].parent)!r})
import java.compile
import jvm.runner
import teavm.c_backend
import wasm_tools
assert str(jvm.runner.ROOT) == {str(self.packages['jvm'])!r}
assert str(wasm_tools.PACKAGE) == {self.environment['WISP_WASM_TOOLS_TOOLCHAIN']!r}
""")

    def test_zig_and_nim_load_configured_llvm_helper(self):
        (self.packages["llvm"] / "shared.py").write_text("def preload_args():\n    return ['--installed-llvm']\n")
        for name in ("zig", "nim"):
            with self.subTest(name=name):
                self.run_python("-c", f"""
import runpy
adapter = runpy.run_path({str(self.packages[name] / 'compile.py')!r}, run_name='installed_adapter')
assert adapter['llvm_preload_args']() == ['--installed-llvm']
""")

    def test_zig_builds_leave_installed_payload_unchanged_on_success_and_failure(self):
        package = self.packages["zig"]
        for name in ("zig.wasm", "manifest.json", "include/zig.h", "lib/std/std.zig"):
            path = package / "dist" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"installed compiler input")
        before = {str(path.relative_to(package)): path.read_bytes() if path.is_file() else None
                  for path in package.rglob("*")}
        project = self.root / "project"
        project.mkdir()
        source = project / "add.zig"
        source.write_text("export fn add(a: i32, b: i32) i32 { return a + b; }")
        spec = importlib.util.spec_from_file_location("installed_zig_adapter", package / "compile.py")
        adapter = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(adapter)
        output = self.root / "output.wasm"
        args = SimpleNamespace(source=source, project_root=project, output=output,
                               wasmtime=None, clang=None, clang_sha256=None,
                               memory_runtime=False, optimization="-O0", emit_c=None)
        scratch = self.root / "scratch"
        scratch.mkdir()

        def guest(command, **kwargs):
            mounted = Path(command[command.index("--dir") + 1].split("::")[0])
            self.assertTrue(mounted.is_relative_to(scratch))
            if str(adapter.DIST / "zig.wasm") in command:
                self.assertEqual((mounted / "lib/std/std.zig").read_bytes(), b"installed compiler input")
                (mounted / "module.c").write_text("generated C")
            else:
                (mounted / "module.wasm").write_bytes(b"\0asm\x01\0\0\0")

        with mock.patch.object(adapter, "host_runtime", return_value="wasmtime"), \
                mock.patch.object(adapter, "clang_backend", return_value=self.root / "clang.component"), \
                mock.patch.object(adapter, "clang_resource_directory", return_value=self.root / "headers"), \
                mock.patch.object(adapter, "llvm_preload_args", return_value=[]), \
                mock.patch.object(tempfile, "tempdir", str(scratch)), \
                mock.patch.object(adapter.subprocess, "run", side_effect=guest) as run:
            adapter.compile_source(args)
            self.assertEqual(output.read_bytes(), b"\0asm\x01\0\0\0")
            run.side_effect = subprocess.CalledProcessError(1, "guest")
            with self.assertRaises(subprocess.CalledProcessError):
                adapter.compile_source(args)
            self.assertEqual(output.read_bytes(), b"\0asm\x01\0\0\0")
        after = {str(path.relative_to(package)): path.read_bytes() if path.is_file() else None
                 for path in package.rglob("*")}
        self.assertEqual(after, before)
        self.assertEqual(list(scratch.iterdir()), [])


class AdapterRepackageProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "teavm"
        (self.package / "dist").mkdir(parents=True)
        inputs = {}
        for name in ("compile.py", "c_backend.py", "bootstrap.py", "src/TeaVMMain.java"):
            path = self.package / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("original " + name)
            inputs[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.manifest = self.package / "dist/manifest.json"
        self.manifest.write_text(json.dumps({"bootstrapInputs": inputs, "compiler": "original guest"}))
        spec = importlib.util.spec_from_file_location("installed_adapter_build", ROOT / "build.py")
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)

    def test_adapter_changes_keep_original_compiler_inputs_and_add_exact_provenance(self):
        original = self.manifest.read_bytes()
        (self.package / "compile.py").write_text("updated shared-package imports")
        result = self.build.adapter_repackage("teavm", self.root)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(result["bootstrapInputs"], json.loads(original)["bootstrapInputs"])
        proof = result["adapterRepackage"]
        self.assertEqual(proof["originalManifestSha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(proof["hostScripts"], {"compile.py": {
            "compilerBootstrapSha256": json.loads(original)["bootstrapInputs"]["compile.py"],
            "packageSha256": hashlib.sha256((self.package / "compile.py").read_bytes()).hexdigest(),
        }})

    def test_changed_compiler_or_build_recipe_requires_rebuild(self):
        (self.package / "compile.py").write_text("updated host adapter")
        for name in ("bootstrap.py", "src/TeaVMMain.java"):
            path = self.package / name
            original = path.read_bytes()
            path.write_text("changed compiler build input")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "compiler bootstrap input changed"):
                self.build.adapter_repackage("teavm", self.root)
            path.write_bytes(original)

    def test_unchanged_source_does_not_repackage_and_missing_provenance_fails(self):
        self.assertIsNone(self.build.adapter_repackage("teavm", self.root))
        self.manifest.write_text("{}")
        with self.assertRaisesRegex(ValueError, "bootstrap input inventory is missing"):
            self.build.adapter_repackage("teavm", self.root)


if __name__ == "__main__":
    unittest.main()
