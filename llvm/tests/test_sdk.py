#!/usr/bin/env python3
"""Focused archive-format and complete-inventory checks for the SDK packager."""

import importlib.util
import json
from pathlib import Path
import tempfile
import re
import unittest

SPEC = importlib.util.spec_from_file_location("sdk", Path(__file__).resolve().parents[1] / "scripts/sdk.py")
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)


def ar_member(name, payload):
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(payload):<10}`\n".encode()
    return header + payload + (b"\n" if len(payload) % 2 else b"")


class ArchiveTests(unittest.TestCase):
    def test_checks_every_object_and_ignores_symbol_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "libLLVMCore.a"
            wasm = b"\x00asm\x01\x00\x00\x00"
            path.write_bytes(b"!<arch>\n" + ar_member("/", b"table") +
                             ar_member("one.o/", wasm) + ar_member("two.o/", wasm))
            self.assertEqual(sdk.archive_objects(path), 2)
            path.write_bytes(b"!<arch>\n" + ar_member("one.o/", wasm) + ar_member("native.o/", b"\x7fELF"))
            with self.assertRaisesRegex(ValueError, "not a Wasm object"):
                sdk.archive_objects(path)

    def test_rejects_empty_truncated_and_thin_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.a"
            for data in [b"!<arch>\n", b"!<thin>\n", b"!<arch>\ntruncated"]:
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    sdk.archive_objects(path)


class InventoryTests(unittest.TestCase):
    def fixture(self, root):
        required = ["include/llvm/IR/Module.h", "include/llvm-c/Core.h", "include/llvm/Config/llvm-config.h",
                    "include/llvm/IR/IntrinsicEnums.inc", "include/lld/Common/Driver.h", "bin/llvm-config",
                    "lib/cmake/WispLLVM/WispLLVMConfig.cmake", "licenses/llvm-LICENSE.TXT"]
        for name in required:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        library = root / "lib/libLLVMCore.a"
        library.write_bytes(b"!<arch>\n" + ar_member("one.o/", b"\x00asm\x01\x00\x00\x00"))
        sdk.write_json(root / "share/llvm/sdk.json", {
            "schema_version": 1, "id": "fixture", "host_target": "wasm32-wasip3",
            "version": "24.0.0git", "libraries": {"LLVMCore": []},
            "archive_object_counts": {"LLVMCore": 1}, "rust_compatibility": "not-verified"})
        sdk.checksum_files(root)

    def test_verifies_and_rejects_modified_or_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            self.assertEqual(sdk.verify(root)["objects"], 1)
            (root / "extra-file").write_text("extra")
            with self.assertRaisesRegex(ValueError, "file set differs"):
                sdk.verify(root)
            (root / "extra-file").unlink()
            (root / "bin/llvm-config").write_text("modified")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                sdk.verify(root)

    def test_rejects_symlink_directory_and_unsafe_checksum_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "linked-include").symlink_to("include", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                sdk.verify(root)
            (root / "linked-include").unlink()
            with (root / "SHA256SUMS").open("a") as stream:
                stream.write("0" * 64 + "  ../escape\n")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                sdk.verify(root)

    def test_nested_checksum_named_file_is_still_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "include/SHA256SUMS").write_text("extra")
            with self.assertRaisesRegex(ValueError, "file set differs"):
                sdk.verify(root)


class PatchSplitTests(unittest.TestCase):
    def test_split_preserves_every_combined_patch_hunk(self):
        patches = Path(__file__).resolve().parents[1] / "patches"
        def chunks(name):
            return [chunk for chunk in re.split(r"(?=^--- )", (patches / name).read_text(), flags=re.M) if chunk]
        original = chunks("0001-clang-wasip3-in-process.patch")
        generic = chunks("0001-llvm-wasi.patch")
        clang = chunks("0002-clang-in-process.patch")
        self.assertCountEqual(original, generic + clang)
        self.assertTrue(all("+++ b/llvm/" in chunk for chunk in generic))
        self.assertTrue(all("+++ b/clang/" in chunk for chunk in clang))


if __name__ == "__main__":
    unittest.main()
