"""Small synthetic SDK fixtures. No compiler, downloads, or real SDK extraction."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

import target_sdk as sdk


class TargetSdkTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "work"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="target-sdk-test-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "sdk.tar.gz"
        self.output = self.root / "target"
        self.notices = self.root / "notices"
        self.notices.mkdir()
        receipts = {}
        for name in sdk.LICENSE_NAMES:
            path = self.notices / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture license\n")
            receipts[name] = {"sha256": sdk.digest(path), "url": "https://example.invalid/pinned-license"}
        (self.notices / "receipts.json").write_text(json.dumps(receipts))
        self.entries = [(name, b"fixture\n", tarfile.REGTYPE) for name in sorted(sdk.REQUIRED)]
        # None of these bootstrap binaries, alternate targets or EH/LTO runtimes
        # can enter an application compiler package.
        self.entries += [(name, b"excluded\n", tarfile.REGTYPE) for name in [
            "bin/clang++", "bin/wasm-ld", "share/wasi-sysroot/include/wasm32-wasip3/stdlib.h",
            "share/wasi-sysroot/include/wasm32-wasip1/eh/c++/v1/vector",
            "share/wasi-sysroot/lib/wasm32-wasip1/eh/libc++.a",
            "share/wasi-sysroot/lib/wasm32-wasip1/llvm-lto/libc.a",
            "share/wasi-sysroot/lib/wasm32-wasip1/libc.so"]]

    def write(self):
        with tarfile.open(self.archive, "w:gz") as tar:
            for name, data, kind in self.entries:
                member = tarfile.TarInfo("sdk/" + name)
                member.type = kind
                if kind == tarfile.SYMTYPE:
                    member.linkname = "/etc/passwd"
                else:
                    member.size = len(data)
                tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
        return {"archive": self.archive.name, "version": "34.0", "url": "https://example.invalid/sdk",
                "sha256": sdk.digest(self.archive)}

    def test_packages_target_only_and_preserves_driver_resource_lookup(self):
        pin = self.write()
        manifest = sdk.package(self.archive, self.output, pin, self.notices)
        licenses = {"licenses/" + name for name in sdk.LICENSE_NAMES} | {"licenses/receipts.json"}
        self.assertEqual(set(manifest["files"]), sdk.REQUIRED | licenses)
        self.assertEqual(sdk.verify(self.output, pin), manifest)
        sysroot = self.output / manifest["sysroot"]
        self.assertEqual((sysroot / "../../lib/clang/23/include/stddef.h").resolve(),
                         (self.output / manifest["resourceDir"] / "include/stddef.h").resolve())
        self.assertTrue((sysroot / "../../" / manifest["builtins"]).is_file())
        self.assertFalse((self.output / "bin").exists())
        # A repeat verifies and reuses the package, even without the bootstrap archive.
        self.archive.unlink()
        self.assertEqual(sdk.package(self.archive, self.output, pin, self.notices), manifest)

    def test_archive_pin_and_notice_tampering_fail_before_publication(self):
        pin = self.write()
        bad = dict(pin, sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "archive checksum"):
            sdk.package(self.archive, self.output, bad, self.notices)
        (self.notices / "libcxx-LICENSE.TXT").write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "license checksum"):
            sdk.package(self.archive, self.output, pin, self.notices)
        self.assertFalse(self.output.exists())

    def test_missing_symlink_duplicate_and_traversal_inputs_fail_atomically(self):
        original = list(self.entries)
        for extra in [[], [(original[0][0], b"duplicate", tarfile.REGTYPE)],
                      [(f"{sdk.RESOURCE}/include/link", b"", tarfile.SYMTYPE)],
                      [(f"{sdk.RESOURCE}/include/../../escape", b"unsafe", tarfile.REGTYPE)]]:
            with self.subTest(extra=extra):
                self.entries = (original[1:] if not extra else original + extra)
                pin = self.write()
                with self.assertRaises(ValueError):
                    sdk.package(self.archive, self.output, pin, self.notices)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".target-sdk-*")))

    def test_modified_extra_and_symlink_packaged_files_rejected(self):
        pin = self.write()
        manifest = sdk.package(self.archive, self.output, pin, self.notices)
        name = sorted(sdk.REQUIRED)[0]
        path = self.output / name
        original = path.read_bytes()
        for change in ["modified", "extra", "symlink"]:
            with self.subTest(change=change):
                extra = self.output / "extra"
                if change == "modified":
                    path.write_bytes(b"changed\n")
                elif change == "extra":
                    extra.write_text("extra")
                else:
                    path.unlink()
                    path.symlink_to(self.notices / "libcxx-LICENSE.TXT")
                with self.assertRaises(ValueError):
                    sdk.verify(self.output, pin)
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(original)
                extra.unlink(missing_ok=True)
        self.assertEqual(sdk.verify(self.output, pin), manifest)

    def test_required_license_must_exist_even_when_inventory_is_rewritten(self):
        pin = self.write()
        manifest = sdk.package(self.archive, self.output, pin, self.notices)
        (self.output / "licenses/libcxxabi-LICENSE.TXT").unlink()
        manifest["files"] = sdk.inventory(self.output)
        (self.output / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "license checksum"):
            sdk.verify(self.output, pin)


if __name__ == "__main__":
    unittest.main()
