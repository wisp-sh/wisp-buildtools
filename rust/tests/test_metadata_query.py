"""Metadata policy tests. The fake runtime does not provide build evidence.

These tests run real Cargo only to resolve an empty locked dependency graph.
The stand-in runtime answers compiler target queries and never compiles source.
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("metadata_query_runner", SCRIPTS / "runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class MetadataQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.manifest = {
            "rustVersion": "1.99.1", "compilerHost": "wasm32-wasip3",
            "llvmVersion": "24.0.0git", "compiler": {"module": "rustc.wasm"},
            "runtime": {"version": "0"},
        }
        self.prefix = self.root / "bootstrap"
        self.prefix.mkdir()
        (self.prefix / "sysroot").mkdir()
        self.compiler = self.prefix / "rustc.wasm"
        self.compiler.write_bytes(b"\0asm\x01\0\0\0")
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "Cargo.toml").write_text(
            '[package]\nname="metadata-fixture"\nversion="0.1.0"\nedition="2021"\n'
        )
        (self.project / "src/lib.rs").write_text("pub fn answer() -> u32 { 42 }\n")
        (self.project / "Cargo.lock").write_text(
            'version = 3\n\n[[package]]\nname = "metadata-fixture"\nversion = "0.1.0"\n'
        )
        self.runtime = self.root / "query-runtime"
        self.calls = self.root / "runtime-calls.jsonl"
        self.runtime.write_text(
            f"#!{Path(sys.executable).resolve()} -I\n"
            "import json, pathlib, sys\n"
            f"with pathlib.Path({str(self.calls)!r}).open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "queries = []\n"
            "for i, arg in enumerate(args):\n"
            "    if arg.startswith('--print='): queries.extend(arg.split('=', 1)[1].split(','))\n"
            "    elif arg == '--print': queries.extend(args[i + 1].split(','))\n"
            "types = [args[i + 1] for i, arg in enumerate(args) if arg == '--crate-type']\n"
            "for query in queries:\n"
            "    if query == 'file-names':\n"
            "        for kind in types or ['bin']:\n"
            "            print({'bin': '___.wasm', 'rlib': 'lib___.rlib', 'dylib': 'lib___.so', "
            "'cdylib': '___.wasm', 'staticlib': 'lib___.a', 'proc-macro': 'lib___.so'}[kind])\n"
            f"    elif query == 'sysroot': print({str(self.prefix / 'sysroot')!r})\n"
            "    elif query == 'crate-name': print('___')\n"
            "    elif query == 'split-debuginfo': print('off')\n"
            "    elif query == 'cfg':\n"
            "        print('debug_assertions\\npanic=\"abort\"\\ntarget_arch=\"wasm32\"\\n"
            "target_endian=\"little\"\\ntarget_env=\"p1\"\\ntarget_family=\"wasm\"\\n"
            "target_os=\"wasi\"\\ntarget_pointer_width=\"32\"\\ntarget_vendor=\"unknown\"')\n"
            f"    elif query == 'target-libdir': print({str(self.prefix / 'sysroot/lib/rustlib/wasm32-wasip1/lib')!r})\n"
        )
        self.runtime.chmod(0o755)
        self.context = self.root / "context.json"
        self.records = self.root / "records"
        self.records.mkdir()
        temporary = self.root / "tmp"
        temporary.mkdir()
        self.context.write_text(json.dumps({
            "manifest": self.manifest, "runtime": str(self.runtime), "compiler": str(self.compiler),
            "sysroot": str(self.prefix / "sysroot"), "temporary": str(temporary),
            "records": str(self.records),
        }))

    def test_verbose_version_uses_pinned_bootstrap_metadata(self):
        for arguments in (["-vV"], ["--version", "--verbose"], ["--verbose", "--version"]):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(runner.metadata_query(self.context, arguments), 0)
                self.assertIn("rustc 1.99.1", output.getvalue())
                self.assertIn("host: wasm32-wasip3", output.getvalue())
                self.assertIn("LLVM version: 24.0.0", output.getvalue())
        self.assertFalse(self.calls.exists())

    def test_version_query_does_not_launch_a_compiler(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runner.metadata_query(self.context, ["--version"]), 0)
        self.assertEqual(output.getvalue(), "rustc 1.99.1\n")
        self.assertFalse(self.calls.exists())

    def test_print_query_runs_pinned_wasm_module_and_records_its_digest(self):
        process = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "runner.py"), "metadata-query", str(self.context),
             "--print=cfg", "--target", "wasm32-wasip1"],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('target_arch="wasm32"', process.stdout)
        records = [json.loads(path.read_text()) for path in self.records.glob("*.json")]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["purpose"], "metadata-query")
        self.assertEqual(records[0]["program"], str(self.compiler))
        self.assertEqual(records[0]["sha256"], runner.sha256(self.compiler))
        self.assertEqual(records[0]["argv"][0], str(self.runtime))
        self.assertIn(str(self.compiler), records[0]["argv"])

    def test_rejects_source_compilation_and_unknown_print_requests(self):
        for arguments in (
            ["input.rs"], ["--version", "input.rs"], ["--print=link-args"],
            ["--print=cfg", "--print=link-args"], ["--print=cfg", "--emit", "link"],
            ["--print=cfg", "--emit=link"], ["--print=cfg", "--out-dir", "output"],
            ["--print=cfg", "--out-dir=output"], ["--print=cfg", "-o", "output.wasm"],
            ["--print=cfg", "-ooutput.wasm"], ["--print=cfg", "--test"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(runner.BuildError):
                runner.metadata_query(self.context, arguments)
        self.assertFalse(self.calls.exists())

    def make_builder(self):
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("real Cargo is required for metadata-hook tests")
        args = SimpleNamespace(
            manifest_path=self.project / "Cargo.toml", out_dir=self.root / "output",
            wasmtime=str(self.runtime), target="wasm32-wasip1", report=self.root / "output/report.json",
            cargo=cargo, vendor_dir=None, timeout=20, crate_type="rlib",
        )
        with patch.object(runner, "verified_install", return_value=(self.manifest, self.prefix)):
            return runner.Builder(args)

    def install_hook(self):
        marker = self.root / "untrusted-hook-ran"
        hook = self.root / "untrusted-compiler-hook"
        hook.write_text(
            f"#!{Path(sys.executable).resolve()} -I\n"
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('hook ran')\n"
            "raise SystemExit(71)\n"
        )
        hook.chmod(0o755)
        return hook, marker

    def write_config(self, config):
        (self.project / ".cargo").mkdir(exist_ok=True)
        (self.project / ".cargo/config.toml").write_text(config)

    def assert_real_metadata_ignores_hooks(self, *, environment=None):
        cargo_home = self.root / "cargo-home"
        cargo_home.mkdir()
        with patch.dict(os.environ, {"CARGO_HOME": str(cargo_home), **(environment or {})}):
            builder = self.make_builder()
            metadata = builder.metadata()
        self.assertEqual([package["name"] for package in metadata["packages"]], ["metadata-fixture"])
        self.assertTrue(self.calls.is_file(), "real Cargo did not ask any compiler target queries")
        self.assertTrue(builder.report["executions"])
        self.assertTrue(all(record["purpose"] == "metadata-query" for record in builder.report["executions"]))
        return builder

    def test_real_cargo_ignores_inherited_compiler_and_wrapper_environment(self):
        hook, marker = self.install_hook()
        self.assert_real_metadata_ignores_hooks(environment={name: str(hook) for name in (
            "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC",
            "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
        )})
        self.assertFalse(marker.exists())

    def test_real_cargo_ignores_project_compiler_and_wrapper_config(self):
        hook, marker = self.install_hook()
        self.write_config("[build]\n" + "".join(f"{key} = {json.dumps(str(hook))}\n" for key in (
            "rustc", "rustc-wrapper", "rustc-workspace-wrapper",
        )))
        self.assert_real_metadata_ignores_hooks()
        self.assertFalse(marker.exists())

    def test_real_cargo_ignores_forced_compiler_and_wrapper_environment(self):
        hook, marker = self.install_hook()
        self.write_config("[env]\n" + "".join(
            f"{key} = {{ value = {json.dumps(str(hook))}, force = true }}\n" for key in (
                "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC",
                "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
            )
        ))
        self.assert_real_metadata_ignores_hooks()
        self.assertFalse(marker.exists())

    def test_generated_probe_is_isolated_before_python_startup(self):
        builder = self.assert_real_metadata_ignores_hooks()
        python_path = self.root / "direct-python-path"
        python_path.mkdir()
        marker = self.root / "direct-sitecustomize-ran"
        (python_path / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize ran')\n"
        )
        python_home = self.root / "direct-python-home"
        python_home.mkdir()
        environment = dict(os.environ, PYTHONPATH=str(python_path),
                           PYTHONHOME=str(python_home), PATH=str(python_path))
        # Execute the generated shebang directly. Starting it through python -I
        # would hide a missing -I in that shebang from this regression test.
        process = subprocess.run(
            [str(builder.out / "metadata-query/rustc-query"), "--print=cfg"],
            cwd=self.project, env=environment, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('target_arch="wasm32"', process.stdout)
        self.assertFalse(marker.exists())

    def test_real_cargo_probe_ignores_forced_python_startup_and_path_environment(self):
        python_path = self.root / "python-path"
        python_path.mkdir()
        marker = self.root / "sitecustomize-ran"
        (python_path / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize ran')\n"
        )
        # PYTHONHOME deliberately points to an empty directory. An interpreter
        # without -I fails before it can execute the trusted probe's first line.
        python_home = self.root / "python-home"
        python_home.mkdir()
        self.write_config("[env]\n" + "".join(
            f"{key} = {{ value = {json.dumps(str(value))}, force = true }}\n"
            for key, value in {"PYTHONPATH": python_path, "PYTHONHOME": python_home, "PATH": python_path}.items()
        ))
        self.assert_real_metadata_ignores_hooks()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
