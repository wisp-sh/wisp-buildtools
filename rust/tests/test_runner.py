"""Runner policy tests with mocked processes, never compiler acceptance evidence."""

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("rust_runner", SCRIPTS / "runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class DirectiveTests(unittest.TestCase):
    def test_parses_cfg_check_cfg_and_environment_in_order(self):
        flags, environment = runner.parse_directives(
            'unrelated application stdout\n'
            'cargo:rustc-cfg=generated\n'
            'cargo::rustc-check-cfg=cfg(generated)\n'
            'cargo:rustc-cfg=mode="small"\n'
            'cargo:rustc-env=ANSWER=42\n'
            'cargo::rustc-env=WITH_EQUALS=a=b=c\n'
        )
        self.assertEqual(flags, ["--cfg", "generated", "--check-cfg", "cfg(generated)", "--cfg", 'mode="small"'])
        self.assertEqual(environment, {"ANSWER": "42", "WITH_EQUALS": "a=b=c"})

    def test_environment_directive_can_set_empty_value(self):
        self.assertEqual(runner.parse_directives("cargo:rustc-env=EMPTY=\n"), ([], {"EMPTY": ""}))

    def test_last_environment_value_wins(self):
        self.assertEqual(
            runner.parse_directives("cargo:rustc-env=ANSWER=1\ncargo:rustc-env=ANSWER=42\n"),
            ([], {"ANSWER": "42"}),
        )

    def test_rerun_directives_and_metadata_do_not_change_flags(self):
        self.assertEqual(
            runner.parse_directives(
                "cargo:rerun-if-changed=answer.txt\n"
                "cargo::rerun-if-env-changed=ANSWER\n"
                "cargo:metadata=answer=42\n"
            ),
            ([], {}),
        )

    def test_warning_is_preserved_in_stderr(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(runner.parse_directives("cargo:warning=useful detail\n"), ([], {}))
        self.assertIn("useful detail", stderr.getvalue())

    def test_error_directive_fails_build(self):
        with self.assertRaisesRegex(runner.BuildError, "build script error: rejected"):
            runner.parse_directives("cargo::error=rejected\n")

    def test_cdylib_link_arguments_are_scoped_and_preserve_one_argument(self):
        for name in ("rustc-cdylib-link-arg", "rustc-link-arg-cdylib"):
            directive = f"cargo:{name}=-zstack-size=33554432\n"
            self.assertEqual(runner.parse_directives(directive), ([], {}))
            self.assertEqual(runner.parse_directives(directive, cdylib=True),
                             (["-C", "link-arg=-zstack-size=33554432"], {}))

    def test_cdylib_link_arguments_reject_empty_and_nul(self):
        for value in ("", "bad\x00argument"):
            with self.assertRaisesRegex(runner.BuildError, "invalid cdylib link argument"):
                runner.parse_directives("cargo:rustc-cdylib-link-arg=" + value)

    def test_rejects_unknown_and_native_linker_directives(self):
        for key in ("unknown", "rustc-flags", "rustc-link-lib", "rustc-link-search", "rustc-link-arg"):
            with self.subTest(key=key), self.assertRaisesRegex(runner.BuildError, "unsupported build script directive"):
                runner.parse_directives(f"cargo:{key}=value\n")

    def test_rejects_malformed_directives(self):
        for line in ("cargo:rustc-cfg", "cargo::rustc-env", "cargo:"):
            with self.subTest(line=line), self.assertRaisesRegex(runner.BuildError, "invalid build script directive"):
                runner.parse_directives(line)

    def test_rejects_malformed_environment(self):
        for value in ("NO_EQUALS", "=value", "NAME=bad\x00value"):
            with self.subTest(value=value), self.assertRaisesRegex(runner.BuildError, "invalid build script environment"):
                runner.parse_directives("cargo:rustc-env=" + value)

    def test_does_not_parse_embedded_cargo_text(self):
        self.assertEqual(runner.parse_directives("prefix cargo:rustc-cfg=ignored\n"), ([], {}))


class MountTests(unittest.TestCase):
    def test_workspace_parent_preopen_is_not_shadowed_by_member(self):
        root = Path("/work/project")
        self.assertEqual(runner.minimal_mounts([root / "parts/util", root]), [root])

    def test_sibling_capabilities_are_preserved(self):
        paths = [Path("/work/pkg"), Path("/work/pkg-other"), Path("/work/out")]
        self.assertEqual(runner.minimal_mounts(paths), sorted(paths))

    def test_mount_normalization_does_not_add_capabilities(self):
        root = Path("/work/project")
        self.assertEqual(runner.minimal_mounts([root, root / "src/../parts", root]), [root])


class VendorChecksumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vendor = self.root / "crate"
        (self.vendor / "src").mkdir(parents=True)
        (self.vendor / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        (self.vendor / "src/lib.rs").write_text("pub fn answer() -> u32 { 42 }\n")
        self.checksums = {
            "files": {
                "Cargo.toml": runner.sha256(self.vendor / "Cargo.toml"),
                "src/lib.rs": runner.sha256(self.vendor / "src/lib.rs"),
            },
            "package": "0" * 64,
        }
        self.write_checksums()

    def write_checksums(self):
        (self.vendor / ".cargo-checksum.json").write_text(json.dumps(self.checksums))

    def test_accepts_matching_offline_vendor_files(self):
        runner.Builder.verify_vendor(self.vendor)

    def test_rejects_tampered_source(self):
        (self.vendor / "src/lib.rs").write_text("pub fn answer() -> u32 { 0 }\n")
        with self.assertRaisesRegex(runner.BuildError, "checksum mismatch"):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_missing_source(self):
        (self.vendor / "src/lib.rs").unlink()
        with self.assertRaises(runner.BuildError):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_missing_checksum_inventory(self):
        (self.vendor / ".cargo-checksum.json").unlink()
        with self.assertRaisesRegex(runner.BuildError, "vendor checksums"):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_unlisted_build_script(self):
        (self.vendor / "build.rs").write_text('fn main() { println!("cargo:rustc-cfg=injected"); }\n')
        with self.assertRaises(runner.BuildError):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_unlisted_source(self):
        (self.vendor / "src/injected.rs").write_text("pub fn injected() {}\n")
        with self.assertRaises(runner.BuildError):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_path_escape(self):
        outside = self.root / "outside.rs"
        outside.write_text("outside vendor\n")
        self.checksums["files"]["../outside.rs"] = runner.sha256(outside)
        self.write_checksums()
        with self.assertRaises(runner.BuildError):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_symlink_escape_even_with_matching_hash(self):
        outside = self.root / "outside.rs"
        outside.write_text("outside vendor\n")
        (self.vendor / "src/lib.rs").unlink()
        (self.vendor / "src/lib.rs").symlink_to(outside)
        self.checksums["files"]["src/lib.rs"] = runner.sha256(outside)
        self.write_checksums()
        with self.assertRaises(runner.BuildError):
            runner.Builder.verify_vendor(self.vendor)

    def test_rejects_malformed_checksum_maps_without_traceback(self):
        for value in ([], {}, {"files": []}, {"files": {"src/lib.rs": 42}}):
            with self.subTest(value=value):
                (self.vendor / ".cargo-checksum.json").write_text(json.dumps(value))
                with self.assertRaises(runner.BuildError):
                    runner.Builder.verify_vendor(self.vendor)


class CompilerHostTests(unittest.TestCase):
    def test_accepts_wasip3_with_linked_backend(self):
        flags = runner.compiler_runtime_flags({'compilerHost': 'wasm32-wasip3'})
        self.assertIn('-Sp3=y', flags)

    def test_rejects_other_compiler_hosts(self):
        for host in ('wasm32-wasip1-threads', 'wasm32-wasip1', None):
            with self.subTest(host=host), self.assertRaisesRegex(runner.BuildError, 'must be wasm32-wasip3'):
                runner.compiler_runtime_flags({'compilerHost':host})

    def test_rejects_external_llvm_provider(self):
        with self.assertRaisesRegex(runner.BuildError, 'must contain its LLVM backend'):
            runner.compiler_runtime_flags({'compilerHost':'wasm32-wasip3','llvmProvider':{}})


class BuilderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.prefix = self.root / "bootstrap"
        self.prefix.mkdir()
        (self.prefix / "sysroot").mkdir()
        (self.prefix / "rustc.wasm").write_bytes(b"\0asm\x01\0\0\0")
        self.project = self.root / "project"
        self.project.mkdir()
        self.manifest = self.project / "Cargo.toml"
        self.manifest.write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        self.args = SimpleNamespace(
            manifest_path=self.manifest,
            out_dir=self.root / "output",
            wasmtime=sys.executable,
            target="wasm32-wasip1",
            report=self.root / "output/report.json",
            cargo="metadata-only-cargo",
            vendor_dir=None,
            timeout=10,
            crate_type="bin",
        )
        self.toolchain = {"compilerHost":"wasm32-wasip3", "compiler": {"module": "rustc.wasm"}, "runtime": {"version": "0"}}
        self.install = patch.object(runner, "verified_install", return_value=(self.toolchain, self.prefix))
        self.install.start()
        self.addCleanup(self.install.stop)

    def test_creates_a_fresh_output_directory(self):
        builder = runner.Builder(self.args)
        self.assertTrue(builder.libs.is_dir())
        self.assertTrue(builder.temp.is_dir())

    def cdylib_fixture(self):
        self.args.crate_type = 'cdylib'
        self.args.entry_path = None
        source = self.project / 'src/lib.rs'
        source.parent.mkdir()
        source.write_text('pub struct App;\n')
        target = {'kind': ['lib'], 'name': 'fixture', 'src_path': str(source)}
        root = {'id': 'root-package', 'name': 'fixture', 'targets': [target]}
        metadata = {'packages': [root], 'resolve': {'root': root['id'], 'nodes': []}}
        return runner.Builder(self.args), source, root, metadata

    def test_cdylib_authored_library_skips_duplicate_root_rlib(self):
        builder, source, root, metadata = self.cdylib_fixture()

        def build_script(package):
            builder.script_flags[builder.unit_id(package, 'runtime')] = ['--cfg', 'from_build_script']

        def execute(compiler, argv, package, kind):
            Path(argv[argv.index('-o') + 1]).write_bytes(b'fixture wasm')

        with patch.object(builder, 'metadata', return_value=metadata), \
                patch.object(builder, 'dependency_flags', return_value=['--extern', 'dep=/stage/dep.rlib']), \
                patch.object(builder, 'compile_library') as compile_library, \
                patch.object(builder, 'build_script', side_effect=build_script) as script, \
                patch.object(builder, 'flags', return_value=[]), \
                patch.object(builder, 'execute', side_effect=execute) as compiler, \
                redirect_stderr(io.StringIO()), patch('sys.stdout', new_callable=io.StringIO):
            builder.build()
        compile_library.assert_not_called()
        script.assert_called_once_with(root)
        compiler.assert_called_once()
        argv = compiler.call_args.args[1]
        self.assertEqual(argv[0], str(source))
        self.assertEqual(argv[argv.index('--crate-name') + 1], 'fixture')
        self.assertEqual(argv[argv.index('--crate-type') + 1], 'cdylib')
        self.assertIn('dep=/stage/dep.rlib', argv)
        self.assertIn('from_build_script', argv)
        self.assertEqual(builder.report['libraryName'], 'fixture')

    def test_cdylib_explicit_wrapper_keeps_root_library_dependency(self):
        builder, source, root, metadata = self.cdylib_fixture()
        wrapper = self.project / 'entry.rs'
        wrapper.write_text('extern crate fixture;\n')
        self.args.entry_path = wrapper
        library = builder.libs / 'libfixture.rlib'

        def build_script(package):
            builder.script_flags[builder.unit_id(package, 'runtime')] = []

        def execute(compiler, argv, package, kind):
            Path(argv[argv.index('-o') + 1]).write_bytes(b'fixture wasm')

        with patch.object(builder, 'metadata', return_value=metadata), \
                patch.object(builder, 'dependency_flags', return_value=[]), \
                patch.object(builder, 'compile_library', return_value=library) as compile_library, \
                patch.object(builder, 'build_script', side_effect=build_script), \
                patch.object(builder, 'flags', return_value=[]), \
                patch.object(builder, 'execute', side_effect=execute) as compiler, \
                patch('sys.stdout', new_callable=io.StringIO):
            builder.build()
        compile_library.assert_called_once_with(root['id'])
        argv = compiler.call_args.args[1]
        self.assertEqual(argv[0], str(wrapper.resolve()))
        self.assertEqual(argv[argv.index('--crate-name') + 1], 'wisp_component')
        self.assertIn('fixture=' + str(library), argv)
        self.assertEqual(builder.report['libraryName'], 'fixture')

    def test_requires_wisp_host_even_if_package_contains_a_runtime(self):
        self.args.wasmtime = None
        bundled = self.prefix / "wasmtime-v0-x86_64-linux/wasmtime"
        bundled.parent.mkdir()
        bundled.write_bytes(b"embedded runtime is not a host")
        with patch.dict(runner.os.environ, {}, clear=True), self.assertRaisesRegex(runner.BuildError, "Wisp must provide"):
            runner.Builder(self.args)

    def test_rejects_runtime_inside_the_compiler_package(self):
        bundled = self.prefix / "wasmtime"
        bundled.write_bytes(b"embedded runtime")
        bundled.chmod(0o755)
        self.args.wasmtime = str(bundled)
        with patch.object(runner, "ROOT", self.root), self.assertRaisesRegex(runner.BuildError, "outside the Rust compiler package"):
            runner.Builder(self.args)

    def test_allows_harness_log_files_in_existing_output(self):
        self.args.out_dir.mkdir()
        (self.args.out_dir / "runner.stdout.log").write_text("")
        (self.args.out_dir / "runner.stderr.log").write_text("")
        builder = runner.Builder(self.args)
        self.assertTrue(builder.libs.is_dir())

    def test_refuses_an_existing_artifact(self):
        self.args.out_dir.mkdir()
        (self.args.out_dir / "app.wasm").write_bytes(b"\0asm\x01\0\0\0")
        with self.assertRaisesRegex(runner.BuildError, "must be fresh"):
            runner.Builder(self.args)

    def test_refuses_an_existing_library_directory(self):
        (self.args.out_dir / "lib").mkdir(parents=True)
        with self.assertRaisesRegex(runner.BuildError, "must be fresh"):
            runner.Builder(self.args)

    def test_native_cargo_is_invoked_for_locked_offline_metadata_only(self):
        builder = runner.Builder(self.args)
        metadata = {"packages": []}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(metadata), stderr="")
        with patch.object(runner.subprocess, "run", return_value=response) as run:
            self.assertEqual(builder.metadata(), metadata)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["metadata-only-cargo", "metadata"])
        self.assertIn("--locked", command)
        self.assertIn("--offline", command)
        self.assertNotIn("build", command)
        self.assertNotIn("rustc", command)

    def test_resume_rejects_changed_local_sources(self):
        builder = runner.Builder(self.args)
        metadata = {"packages": [{"manifest_path": str(self.manifest), "source": None}]}
        runner.json_write(builder.out / 'metadata.json', metadata)
        runner.json_write(builder.out / 'resume-state.json', {
            'sources': builder.source_snapshot(metadata),
            'metadataSha256': runner.sha256(builder.out / 'metadata.json')})
        runner.json_write(self.args.report, builder.report)
        self.args.resume = True
        self.args.recover_receipts = False
        self.manifest.write_text('changed manifest')
        resumed = runner.Builder(self.args)
        with self.assertRaisesRegex(runner.BuildError, 'sources or metadata changed'):
            resumed.metadata()

    def test_resume_checks_output_and_log_hashes_before_skipping_execution(self):
        builder = runner.Builder(self.args)
        package = {"id": "root-package", "name": "fixture", "manifest_path": str(self.manifest)}
        builder.packages = {package["id"]: package}
        output = builder.out / 'artifact.rlib'
        output.write_bytes(b'compiled output')
        argv = ['input.rs', '-o', str(output)]
        response = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(builder, 'environment', return_value={}), patch.object(runner.subprocess, 'run', return_value=response) as run:
            builder.execute(builder.compiler, argv, package, 'compiler')
            run.reset_mock()
            builder.replay_cursor = 0
            builder.execute(builder.compiler, argv, package, 'compiler')
            run.assert_not_called()
            output.write_bytes(b'corrupt output')
            builder.replay_cursor = 0
            builder.execute(builder.compiler, argv, package, 'compiler')
            self.assertEqual(run.call_count, 1)
            run.reset_mock()
            (builder.out / 'logs/000-compiler.stdout').write_text('corrupt log')
            builder.replay_cursor = 0
            builder.execute(builder.compiler, argv, package, 'compiler')
            self.assertEqual(run.call_count, 1)

    def test_dependency_selection_separates_normal_build_and_dev_edges(self):
        builder = runner.Builder(self.args)
        package = {"id": "root-package"}
        builder.nodes = {package["id"]: {"deps": [
            {"name": "normal", "pkg": "normal", "dep_kinds": [{"kind": None}]},
            {"name": "build", "pkg": "build", "dep_kinds": [{"kind": "build"}]},
            {"name": "dev", "pkg": "dev", "dep_kinds": [{"kind": "dev"}]},
            {"name": "both", "pkg": "both", "dep_kinds": [{"kind": None}, {"kind": "build"}]},
        ]}}
        with patch.object(builder, "compile_library", side_effect=lambda name: builder.libs / (name + ".rlib")) as compile_library:
            normal = builder.dependency_flags(package)
            self.assertEqual([call.args[0] for call in compile_library.call_args_list], ["normal", "both"])
            self.assertEqual(normal, ["--extern", f"normal={builder.libs / 'normal.rlib'}", "--extern", f"both={builder.libs / 'both.rlib'}"])
            compile_library.reset_mock()
            build = builder.dependency_flags(package, build=True)
            self.assertEqual([call.args[0] for call in compile_library.call_args_list], ["build", "both"])
            self.assertEqual(build, ["--extern", f"build={builder.libs / 'build.rlib'}", "--extern", f"both={builder.libs / 'both.rlib'}"])

    def test_native_metadata_failure_does_not_fall_back_to_native_compilation(self):
        builder = runner.Builder(self.args)
        response = subprocess.CompletedProcess([], 1, stdout="", stderr="offline crate unavailable")
        with patch.object(runner.subprocess, "run", return_value=response) as run:
            with self.assertRaisesRegex(runner.BuildError, "locked offline Cargo metadata failed"):
                builder.metadata()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], "metadata")

    def test_proc_macro_without_loader_fails_before_process_execution(self):
        builder = runner.Builder(self.args)
        package = {"id": "macro-package", "name": "fixture-macro", "targets": [{"kind": ["proc-macro"]}]}
        builder.packages = {package["id"]: package}
        with patch.object(runner.subprocess, "run") as run:
            with self.assertRaisesRegex(runner.BuildError, "Wasm macro loader"):
                builder.compile_library(package["id"])
        run.assert_not_called()

    def test_compiler_execution_uses_configured_wasmtime_and_records_actual_command(self):
        builder = runner.Builder(self.args)
        package = {"id": "root-package", "name": "fixture", "manifest_path": str(self.manifest)}
        builder.packages = {package["id"]: package}
        response = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(builder, "environment", return_value={}), patch.object(runner.subprocess, "run", return_value=response) as run:
            index = builder.execute(builder.compiler, ["input.rs"], package, "compiler")
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(Path(sys.executable).resolve()))
        self.assertEqual(command[1], "run")
        self.assertIn(str(builder.compiler), command)
        self.assertNotIn("cargo", command)
        self.assertEqual(builder.report["executions"][index]["argv"], command)
        self.assertEqual(builder.report["executions"][index]["sha256"], hashlib.sha256(builder.compiler.read_bytes()).hexdigest())
        self.assertEqual(builder.report["executions"][index]["exitCode"], 0)
        self.assertIn(str(builder.temp) + "::/", command)
        self.assertNotIn("/", [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--dir"])

    def test_wasip3_compiler_enables_component_host_and_experimental_macro_loader(self):
        self.toolchain.update(compilerHost="wasm32-wasip3", capabilities={"proceduralMacros": True})
        builder = runner.Builder(self.args)
        package = {"id": "root-package", "name": "fixture", "manifest_path": str(self.manifest)}
        builder.packages = {package["id"]: package}
        response = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(builder, "environment", return_value={}), \
                patch.object(builder, "macro_trace"), \
                patch.object(runner.subprocess, "run", return_value=response) as run:
            builder.execute(builder.compiler, ["input.rs", "-Zwasm-proc-macros"], package, "compiler")
        command = run.call_args.args[0]
        self.assertIn("-Sp3=y", command)
        self.assertIn("-Oregalloc-algorithm=single-pass", command)
        self.assertIn("-Cinlining=n", command)
        self.assertNotIn("-Sthreads=y", command)
        self.assertIn("RUSTC_BOOTSTRAP=1", command)


if __name__ == "__main__":
    unittest.main()
