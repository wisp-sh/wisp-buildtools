import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wasmbuild.wit_runtime import SUPPORT_ROOT, stage_runtime, verified_runtime


class WitRuntimeTests(unittest.TestCase):
    def test_staging_keeps_existing_pin_and_rejects_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            stage_runtime(project, SUPPORT_ROOT / "wit-runtime")
            destination = project / "vendor/go.bytecodealliance.org/pkg"
            self.assertEqual(verified_runtime(destination), verified_runtime(SUPPORT_ROOT / "wit-runtime"))
            stage_runtime(project, SUPPORT_ROOT / "wit-runtime")
            file = destination / "wit/runtime/runtime.go"
            file.write_text("package runtime\n")
            with self.assertRaisesRegex(ValueError, "vendored Go WIT runtime differs"):
                stage_runtime(project, SUPPORT_ROOT / "wit-runtime")
            self.assertEqual(file.read_text(), "package runtime\n")

    def test_pinned_offline_sources_include_sync_runtime_and_types(self):
        files = verified_runtime(SUPPORT_ROOT / "wit-runtime")
        self.assertIn("wit/runtime/runtime.go", files)
        self.assertIn("wit/types/option.go", files)
        self.assertIn("wit/types/tuple.go", files)
        self.assertIn("LICENSE", files)
        self.assertIn(b"adapter_monotonic_clock_set_paused", files["wit/runtime/runtime.go"])

    def test_modified_missing_extra_and_symlink_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            shutil.copytree(SUPPORT_ROOT / "wit-runtime", root)
            file = root / "wit/types/option.go"
            original = file.read_bytes()
            file.write_bytes(original + b"\n// changed\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verified_runtime(root)
            file.unlink()
            with self.assertRaisesRegex(ValueError, "file set differs"):
                verified_runtime(root)
            file.write_bytes(original)
            extra = root / "extra.go"
            extra.write_text("package runtime\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verified_runtime(root)
            extra.unlink()
            file.unlink()
            file.symlink_to(SUPPORT_ROOT / "wit-runtime/wit/types/option.go")
            with self.assertRaisesRegex(ValueError, "symlink"):
                verified_runtime(root)
