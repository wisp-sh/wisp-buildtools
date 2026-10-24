"""Report validator tests. These never claim a Rust compilation succeeded."""

import hashlib
import importlib.util
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("acceptance.py")
SPEC = importlib.util.spec_from_file_location("rust_acceptance", MODULE_PATH)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class ReportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / "output"
        self.out.mkdir()
        self.manifest = self.root / "Cargo.toml"
        self.manifest.write_text('[package]\nname="unit-only"\nversion="0.1.0"\n')
        self.compiler = self.root / "compiler.wasm"
        self.compiler.write_bytes(b"\0asm\x01\0\0\0")
        self.artifact = self.out / "application.wasm"
        self.artifact.write_bytes(b"\0asm\x01\0\0\0")
        self.digest = hashlib.sha256(self.compiler.read_bytes()).hexdigest()
        self.runtime = Path(shutil.which("true")).resolve()
        self.case = {"dependencies": [], "buildScripts": [], "proceduralMacros": []}
        self.report = {
            "schemaVersion": 1,
            "manifestPath": str(self.manifest),
            "target": "wasm32-wasip1",
            "compiler": {"host": "wasm", "path": str(self.compiler), "sha256": self.digest},
            "artifact": {"path": "application.wasm", "sha256": self.digest},
            "executions": [{
                "kind": "compiler",
                "program": str(self.compiler),
                "sha256": self.digest,
                "argv": [str(self.runtime), "run", str(self.compiler)],
                "cwd": str(self.root),
                "exitCode": 0,
            }],
            "dependencies": [],
            "buildScripts": [],
            "proceduralMacros": [],
        }

    def validate(self):
        return acceptance.validate_report(self.report, self.case, self.manifest, self.out, self.runtime)

    def test_validates_report_structure_without_executing_a_compiler(self):
        self.assertEqual(self.validate(), self.artifact)

    def test_rejects_native_compiler_even_if_hash_matches(self):
        self.compiler.write_bytes(b"\x7fELFnot-a-wasm-compiler")
        self.report["compiler"]["sha256"] = acceptance.sha256(self.compiler)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not a core Wasm"):
            self.validate()

    def test_rejects_mismatched_compiler_hash(self):
        self.report["compiler"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(acceptance.AcceptanceError, "SHA-256"):
            self.validate()

    def test_rejects_native_compiler_command(self):
        self.report["executions"][0]["argv"][0] = str(Path(shutil.which("python3")).resolve())
        with self.assertRaisesRegex(acceptance.AcceptanceError, "configured Wasmtime"):
            self.validate()

    def test_rejects_command_without_compiler_program(self):
        self.report["executions"][0]["argv"] = [str(self.runtime), "--version"]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "does not name"):
            self.validate()

    def test_rejects_failed_compiler_execution(self):
        self.report["executions"][0]["exitCode"] = 1
        with self.assertRaisesRegex(acceptance.AcceptanceError, "did not exit"):
            self.validate()

    def test_rejects_artifact_outside_evidence_directory(self):
        self.report["artifact"]["path"] = str(self.compiler)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "inside its output"):
            self.validate()

    def test_rejects_wrong_manifest(self):
        self.report["manifestPath"] = str(self.root / "another-project/Cargo.toml")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "different source manifest"):
            self.validate()

    def test_requires_pinned_vendored_dependency(self):
        self.case["dependencies"] = [{"name": "serde", "version": "1.0.228"}]
        self.report["dependencies"] = [{"name": "serde", "version": "1.0.228", "source": "native-cache"}]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "missing vendored dependency"):
            self.validate()

    def test_requires_executed_build_script(self):
        self.case["buildScripts"] = ["acceptance-build-script"]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "missing Wasm execution"):
            self.validate()

    def test_rejects_build_script_pointing_to_compiler_execution(self):
        self.report["buildScripts"] = [{
            "package": "acceptance-build-script", "program": str(self.compiler),
            "sha256": self.digest, "execution": 0,
        }]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "did not execute through Wasmtime"):
            self.validate()

    def test_accepts_in_process_macro_only_inside_wasm_compiler(self):
        self.install_macro_trace()
        self.assertEqual(self.validate(), self.artifact)
        self.report["executions"][1]["kind"] = "build-script"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "must run inside the Wasm compiler"):
            self.validate()

    def install_macro_trace(self):
        self.case["proceduralMacros"] = ["acceptance-derive"]
        macro = self.out / "derive.wasm"
        macro.write_bytes(b"\0asm\x01\0\0\0")
        self.trace = self.out / "macro.jsonl"
        self.events = [
            {"schemaVersion": 1, "module": str(macro), "runtime": "wasmi", "index": 0,
             "name": "Answer", "kind": "derive", "macroTarget": "wasm32-unknown-unknown",
             "event": event, "status": status}
            for event, status in (("load", "succeeded"), ("invoke", "started"), ("invoke", "succeeded"))]
        self.trace.write_text("".join(json.dumps(event) + "\n" for event in self.events))
        compilation = copy.deepcopy(self.report["executions"][0])
        compilation["argv"] += ["--crate-type", "proc-macro", "--target", "wasm32-unknown-unknown",
                                "-Zwasm-proc-macros", "-o", str(macro)]
        invocation = copy.deepcopy(self.report["executions"][0])
        invocation["argv"] += ["--env", "WISP_WASM_PROC_MACRO_LOG=" + str(self.trace)]
        self.report["executions"] = [compilation, invocation]
        record = {"package": "acceptance-derive", "program": str(macro),
                  "sha256": self.digest, "execution": 1, "compilation": 0,
                  "loader": "compiler-wasm-runtime", "runtime": "wasmi",
                  "macroIndex": 0, "macroName": "Answer", "macroKind": "derive",
                  "trace": str(self.trace), "traceSha256": acceptance.sha256(self.trace)}
        self.report["proceduralMacros"] = [record]
        return record

    def test_rejects_macro_presence_without_completed_interpreter_invocation(self):
        record = self.install_macro_trace()
        for events in (self.events[:1], self.events[:2], self.events[1:]):
            self.trace.write_text("".join(json.dumps(event) + "\n" for event in events))
            record["traceSha256"] = acceptance.sha256(self.trace)
            with self.subTest(events=events), self.assertRaisesRegex(acceptance.AcceptanceError, "invocation|trace"):
                self.validate()

    def test_rejects_tampered_trace_or_wrong_compiler_trace_path(self):
        record = self.install_macro_trace()
        self.trace.write_text(self.trace.read_text() + "\n")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "trace must match"):
            self.validate()
        record["traceSha256"] = acceptance.sha256(self.trace)
        self.report["executions"][1]["argv"] = [str(self.runtime), "run", str(self.compiler)]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attached to its compiler"):
            self.validate()

    def test_rejects_unbuilt_macro_or_wrong_target_compilation(self):
        record = self.install_macro_trace()
        record["compilation"] = 1
        with self.assertRaisesRegex(acceptance.AcceptanceError, "earlier compilation"):
            self.validate()
        record["compilation"] = 0
        command = self.report["executions"][0]["argv"]
        command[command.index("--target") + 1] = "wasm32-wasip1"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "reported Wasm target"):
            self.validate()

    def test_rejects_trace_for_a_different_macro_declaration(self):
        record = self.install_macro_trace()
        record["macroName"] = "NeverInvoked"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "no completed invocation"):
            self.validate()

    def test_requires_each_declared_macro_kind_to_have_a_completed_invocation(self):
        self.install_macro_trace()
        self.case["macroInvocations"] = [{"package": "acceptance-derive", "name": "Answer", "kind": "function"}]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "missing completed function macro"):
            self.validate()
        self.case["macroInvocations"][0]["kind"] = "derive"
        self.assertEqual(self.validate(), self.artifact)

    def test_harness_forwards_selected_runtime_to_external_candidate_runner(self):
        catalog = {"target": "wasm32-wasip1", "pendingWispFlows": [], "cases": [
            {"name": "fixture", "project": "", "expectedStdout": "42\n", **self.case}]}
        catalog_path = self.root / "cases.json"
        catalog_path.write_text(json.dumps(catalog))
        args = SimpleNamespace(runner=None, runner_command=json.dumps([sys.executable, "-I", "/candidate/runner.py"]),
                               wasmtime=str(self.runtime), vendor_dir=None, out=self.root / "candidate-results",
                               cases=None, list=False, timeout=10)
        observed = []

        def run(command, **kwargs):
            observed.append(command)
            if "build" in command:
                self.assertEqual(command[command.index("--wasmtime") + 1], str(self.runtime))
                output = Path(command[command.index("--out-dir") + 1])
                (output / "application.wasm").write_bytes(b"\0asm\x01\0\0\0")
                report = copy.deepcopy(self.report)
                report["artifact"] = {"path": "application.wasm", "sha256": self.digest}
                Path(command[command.index("--report") + 1]).write_text(json.dumps(report))
                return subprocess.CompletedProcess(command, 0)
            return subprocess.CompletedProcess(command, 0, stdout=b"42\n", stderr=b"")

        with patch.object(acceptance, "parse_args", return_value=args), patch.object(acceptance, "CATALOG", catalog_path), \
                patch.object(acceptance, "ROOT", self.root), patch.object(acceptance.subprocess, "run", side_effect=run):
            self.assertEqual(acceptance.main(), 0)
        self.assertEqual(len(observed), 2)

    def test_rejects_native_macro_artifact(self):
        macro = self.root / "macro.so"
        macro.write_bytes(b"\x7fELFnative-macro")
        self.report["proceduralMacros"] = [{
            "package": "acceptance-derive", "program": str(macro),
            "sha256": acceptance.sha256(macro), "execution": 0, "loader": "compiler-wasm-runtime",
        }]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not a core Wasm"):
            self.validate()

    def test_does_not_allow_boolean_execution_index(self):
        self.report["proceduralMacros"] = [{
            "package": "acceptance-derive", "program": str(self.artifact),
            "sha256": self.digest, "execution": False, "loader": "compiler-wasm-runtime",
        }]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "no execution record"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
