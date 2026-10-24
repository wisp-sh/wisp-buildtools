"""Keep compiler-build provenance separate from later packaging changes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wisp_packaging_fixture", ROOT / "tests/test_source_build.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builder = fixture.builder


class PackageProvenanceTests(unittest.TestCase):
    setUp = fixture.SourceBuildTests.setUp
    save_report = fixture.SourceBuildTests.save_report
    package = fixture.SourceBuildTests.package

    def snapshot_report(self):
        return {"mode": "build", "buildDriverSha256": builder.digest(Path(builder.__file__)),
                "sourceLockSha256": builder.digest(self.root / "source-lock.json")}

    def test_snapshot_helper_preserves_exact_current_driver_and_lock(self):
        report = self.snapshot_report()
        builder.snapshot_build_provenance(self.out, report, self.root / "source-lock.json")
        self.assertEqual((self.out / "build-driver.py").read_bytes(), Path(builder.__file__).read_bytes())
        self.assertEqual((self.out / "source-lock.json").read_bytes(), (self.root / "source-lock.json").read_bytes())
        self.assertEqual(builder.digest(self.out / "build-driver.py"), report["buildDriverSha256"])
        self.assertEqual(builder.digest(self.out / "source-lock.json"), report["sourceLockSha256"])

    def test_snapshot_helper_refuses_stale_inputs_and_nonbuild_modes_without_clobbering(self):
        snapshots = {name: (self.out / name).read_bytes() for name in ("build-driver.py", "source-lock.json")}
        for field in ("buildDriverSha256", "sourceLockSha256"):
            report = self.snapshot_report()
            report[field] = "0" * 64
            with self.assertRaisesRegex(ValueError, "input changed before snapshot"):
                builder.snapshot_build_provenance(self.out, report, self.root / "source-lock.json")
            for name, content in snapshots.items():
                self.assertEqual((self.out / name).read_bytes(), content)
        for mode in ("plan", "dry-run"):
            report = self.snapshot_report()
            report["mode"] = mode
            with self.assertRaisesRegex(ValueError, "only a real build"):
                builder.snapshot_build_provenance(self.out, report, self.root / "source-lock.json")
            for name, content in snapshots.items():
                self.assertEqual((self.out / name).read_bytes(), content)

    def test_real_build_mode_archives_inputs_before_any_upstream_execution(self):
        tool = self.root / "native-tool"
        tool.write_bytes(b"native tool fixture")
        macro_patch = self.root / "macro.patch"
        macro_patch.write_text("diff --git a/Cargo.lock b/Cargo.lock\n")
        self.lock["macro_backport"] = {"sha256": builder.digest(macro_patch),
                                      "patched_cargo_lock_sha256": builder.digest(self.source / "Cargo.lock"),
                                      "macro_target": "wasm32-unknown-unknown"}
        (self.root / "source-lock.json").write_text(json.dumps(self.lock))
        calls = []

        def run(argv, **kwargs):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "wasm-opt version 133\n", "")
            report = json.loads((self.out / "build-report.json").read_text())
            self.assertEqual(report["mode"], "build")
            self.assertEqual(builder.digest(self.out / "build-driver.py"), report["buildDriverSha256"])
            self.assertEqual(builder.digest(self.out / "source-lock.json"), report["sourceLockSha256"])
            self.assertEqual((self.out / "build-driver.py").read_bytes(), Path(builder.__file__).read_bytes())
            calls.append(argv)
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(builder.COMPONENT_WASM)
                return subprocess.CompletedProcess(argv, 0, "", "")
            # Stop at the mocked compiler build rather than compiling anything.
            return subprocess.CompletedProcess(argv, 1, "", "")

        with patch.object(builder, "validate_inputs", return_value=self.report["inputs"]), \
             patch.object(builder, "executable", return_value=tool), \
             patch.object(builder, "check_patch"), patch.object(builder.subprocess, "run", side_effect=run):
            argv = [str(fixture.SCRIPT), "--build", "--source", str(self.source), "--out", str(self.out),
                    "--wasi-sdk", str(self.sdk), "--macro-patch", str(macro_patch), "--wasm-opt", str(tool)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(builder.main(), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads((self.out / "build-report.json").read_text())["status"], "failed")

    def test_candidate_ships_exact_report_and_original_build_inputs(self):
        # Deliberate noncanonical formatting catches accidental reserialization.
        report_bytes = (json.dumps(self.report, indent=1) + "\n\n").encode()
        (self.out / "build-report.json").write_bytes(report_bytes)
        self.package()
        candidate = self.root / "candidate"
        manifest = json.loads((candidate / "toolchain.json").read_text())
        provenance = json.loads((candidate / "source-build.json").read_text())
        self.assertEqual((candidate / "build-report.json").read_bytes(), report_bytes)
        self.assertEqual(builder.digest(candidate / "build-report.json"),
                         manifest["compiler"]["sourceBuildReportSha256"])
        self.assertEqual(provenance["buildReportSha256"], builder.digest(candidate / "build-report.json"))
        for name, field in (("build-driver.py", "buildDriverSha256"), ("source-lock.json", "sourceLockSha256")):
            self.assertEqual((candidate / name).read_bytes(), (self.out / name).read_bytes())
            self.assertEqual(builder.digest(candidate / name), self.report[field])
            self.assertEqual(provenance["buildProvenance"][name]["sha256"], self.report[field])
        self.assertEqual(provenance["buildReport"], self.report)
        self.assertEqual(provenance["packagingDriverSha256"], builder.digest(Path(builder.__file__)))
        self.assertNotEqual(provenance["packagingDriverSha256"], self.report["buildDriverSha256"])

    def test_missing_or_changed_archived_inputs_cannot_be_packaged(self):
        for name in ("build-driver.py", "source-lock.json"):
            path = self.out / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(ValueError, "provenance missing or changed"):
                self.package()
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "provenance missing or changed"):
                self.package()
            path.write_bytes(original)
        self.assertFalse((self.root / "candidate").exists())

    def test_report_must_identify_each_archived_input(self):
        for field in ("buildDriverSha256", "sourceLockSha256"):
            original = self.report.pop(field)
            self.save_report()
            with self.assertRaisesRegex(ValueError, "provenance missing or changed"):
                self.package()
            self.report[field] = original
        self.assertFalse((self.root / "candidate").exists())



if __name__ == "__main__":
    unittest.main()
