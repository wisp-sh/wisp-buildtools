"""Reject invalid external shared-library inventories before guest execution."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "shared.py"
SPEC = importlib.util.spec_from_file_location("wisp_llvm_shared_test", HELPER)
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)
BUILD_SPEC = importlib.util.spec_from_file_location("wisp_llvm_shared_builder_test", HELPER.parent / "scripts/shared.py")
builder = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(builder)


class SharedProviderInventory(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.entries = []
        for index, name in enumerate(sorted(shared.MODULE_NAMES)):
            path = self.root / f"library-{index}.wasm"
            path.write_bytes(b"\0asm\x01\0\0\0")
            self.entries.append({"name": name, "path": path.name, "sizeBytes": path.stat().st_size,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.write()

    def write(self):
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "profile": shared.PROFILE, "coreModules": self.entries}))

    def test_all_required_modules_are_verified_and_preloaded(self):
        arguments = shared.preload_args(self.root)
        self.assertEqual(arguments[:4], ["-C", "cache=y", "-O", "opt-level=0"])
        arguments = arguments[4:]
        self.assertEqual(arguments[::2], ["--preload"] * 4)
        self.assertEqual({value.split("=", 1)[0] for value in arguments[1::2]}, shared.MODULE_NAMES)
        self.assertTrue(all(Path(value.split("=", 1)[1]).is_file() for value in arguments[1::2]))

    def test_each_complete_naming_scheme_is_preloaded_verbatim(self):
        for names in (shared.MODULE_NAMES, shared.LEGACY_MODULE_NAMES):
            for entry, name in zip(self.entries, sorted(names)):
                entry["name"] = name
            self.write()
            for helper in (shared, builder):
                with self.subTest(names=sorted(names), helper=helper.__name__):
                    arguments = helper.preload_args(self.root)
                    preloads = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--preload"]
                    self.assertEqual(len(preloads), 4)
                    self.assertEqual({value.split("=", 1)[0] for value in preloads}, names)

    def test_mixed_naming_schemes_are_rejected(self):
        self.entries[0]["name"] = self.entries[0]["name"].replace("wisp-", "vise-", 1)
        self.write()
        for helper in (shared, builder):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(ValueError, "inventory is incomplete"):
                    helper.preload_args(self.root)

    def test_unknown_module_names_are_rejected(self):
        self.entries[0]["name"] = "unknown-llvm24"
        self.write()
        for helper in (shared, builder):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(ValueError, "inventory is incomplete"):
                    helper.preload_args(self.root)

    def test_tampered_module_is_rejected(self):
        (self.root / self.entries[0]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "size mismatch|checksum mismatch"):
            shared.preload_args(self.root)

    def test_hash_changes_are_rejected_even_when_size_matches(self):
        (self.root / self.entries[0]["path"]).write_bytes(b"changed!")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            shared.preload_args(self.root)

    def test_duplicate_modules_are_rejected(self):
        self.entries.append(self.entries[0])
        self.write()
        for helper in (shared, builder):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(ValueError, "inventory is incomplete"):
                    helper.preload_args(self.root)

    def test_parent_path_is_rejected(self):
        self.entries[0]["path"] = "../outside.wasm"
        self.write()
        with self.assertRaisesRegex(ValueError, "unsafe or missing"):
            shared.preload_args(self.root)

    def test_symlink_module_is_rejected(self):
        path = self.root / self.entries[0]["path"]
        target = self.root / "target.wasm"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "unsafe or missing"):
            shared.preload_args(self.root)


if __name__ == "__main__":
    unittest.main()
