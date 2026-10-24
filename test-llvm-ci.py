#!/usr/bin/env python3
"""Exercise shared LLVM CI archives without compiling or downloading toolchains."""
from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
specification = importlib.util.spec_from_file_location("compiler_ci_llvm", ROOT / "tools/ci-llvm.py")
llvm_ci = importlib.util.module_from_spec(specification)
specification.loader.exec_module(llvm_ci)

SDK = "llvm/artifacts/llvm-24-d297af3-wasip3-o1"
CLANG = "clang/.toolchains/build-p3/bin/clang-wasm.component"
NIM = "nim/work/backend-build/c-backend"
NATIVE_SDK = "clang/.toolchains/wasi-sdk-34.0-x86_64-linux"
METADATA = "wisp-llvm-bootstrap.json"
RECEIPT = "build/receipts/clang/build.json"
COMPONENT = b"\0asm\x0d\0\x01\0"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LLVMArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compiler-ci-llvm-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.producer = self.directory / "producer"
        self.consumer = self.directory / "consumer"
        self.archive = self.directory / "llvm-bootstrap.tar.gz"
        self.copy_inputs(ROOT, self.producer)
        self.copy_inputs(self.producer, self.consumer)
        self.prepare_outputs()
        # SDK object validation, host execution and compiler probes require real
        # toolchains. All receipt, fingerprint and archive checks remain real.
        self.semantic_validation = mock.patch.object(llvm_ci, "validate_outputs", return_value={}).start()
        self.addCleanup(mock.patch.stopall)

    def copy_inputs(self, source, destination):
        for relative in llvm_ci.fingerprint(source):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)

    def write(self, relative, data, *, executable=False):
        path = self.producer / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        path.chmod(0o755 if executable else 0o644)
        return path

    def prepare_outputs(self):
        self.write(f"{SDK}/SHA256SUMS", "fixture SDK inventory\n")
        self.write(f"{SDK}/include/llvm/IR/Module.h", "fixture LLVM header\n")
        self.write(f"{SDK}/lib/libLLVMCore.a", b"!<arch>\nfixture\n")
        self.write("llvm/dist/shared/manifest.json", '{"schema":1}\n')
        self.write("llvm/dist/shared/lib/llvm24.so.wasm", b"\0asm\x01\0\0\0")
        self.write(CLANG, COMPONENT)
        self.write("clang/include/stddef.h", "fixture resource header\n")
        self.write(NIM, COMPONENT)
        self.write("clang/.toolchains/llvm-project/LICENSE.TXT", "LLVM fixture license\n")
        self.write(f"{NATIVE_SDK}/bin/clang", "fixture native compiler\n", executable=True)
        (self.producer / NATIVE_SDK / "bin/clang++").symlink_to("clang")
        self.write(f"{NATIVE_SDK}/lib/clang/23/include/stddef.h", "fixture native SDK header\n")
        self.write("clang/.toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools", "fixture tool\n", executable=True)
        self.write(".ci/hosts/wisp-wasmtime-49.0.1/wasmtime", "fixture host\n", executable=True)
        self.write(".ci/hosts/wisp-wasmtime-49.0.1/LICENSE", "fixture host license\n")
        self.write(".ci/hosts/wisp-wasmtime-49.0.1/receipt.json", '{"schema":1}\n')
        manifest = json.loads((self.producer / "buildtools.json").read_text())
        clang = manifest["tools"]["clang"]
        receipt = {
            "schema": 1, "tool": "clang", "operation": "build", "passed": True,
            "source": clang["source"],
            "sourceInputs": {name: digest(self.producer / name) for name in clang["sourceInputs"]},
            "recipeSha256": digest(self.producer / "buildtools.json"),
            "artifacts": {name: digest(self.producer / name) for name in clang["artifacts"]},
            "validation": {"componentInterface": "wasi:cli/run@0.3.0", "smokeResult": 42,
                           "sdkInventorySha256": digest(self.producer / SDK / "SHA256SUMS")},
        }
        self.write(RECEIPT, json.dumps(receipt))

    def export(self):
        return llvm_ci.export_state(self.producer, self.archive)

    def restore(self, archive=None):
        return llvm_ci.restore_state(self.consumer, archive or self.archive)

    def rewrite_archive(self, transform):
        with tarfile.open(self.archive, "r:gz") as stream:
            members = [(copy.copy(member), stream.extractfile(member).read() if member.isfile() else None)
                       for member in stream.getmembers()]
        rewritten = self.directory / "tampered.tar.gz"
        with tarfile.open(rewritten, "w:gz") as stream:
            for member, payload in transform(members):
                stream.addfile(member, io.BytesIO(payload) if payload is not None else None)
        return rewritten

    def change_metadata(self, members, update):
        result = []
        for member, payload in members:
            if member.name == METADATA:
                metadata = json.loads(payload)
                update(metadata)
                payload = json.dumps(metadata).encode()
                member.size = len(payload)
            result.append((member, payload))
        return result

    def assert_rejected(self, archive=None):
        with self.assertRaises((ValueError, OSError, tarfile.TarError)):
            self.restore(archive)
        self.assertFalse((self.consumer / CLANG).exists(), "rejected archive installed a compiler")
        self.assertFalse((self.consumer / ".ci/llvm-bootstrap.json").exists())

    def test_round_trip_keeps_complete_payload_checksums_modes_and_internal_links(self):
        metadata = self.export()
        report = self.restore()
        self.assertEqual(report["key"], metadata["key"])
        self.assertEqual(metadata["inputs"], llvm_ci.fingerprint(self.producer))
        self.assertEqual(llvm_ci.verify_state(self.consumer)["key"], metadata["key"])
        for relative, entry in metadata["files"].items():
            with self.subTest(path=relative):
                original, restored = self.producer / relative, self.consumer / relative
                if entry["kind"] == "file":
                    self.assertEqual(restored.read_bytes(), original.read_bytes())
                    self.assertEqual(stat.S_IMODE(restored.stat().st_mode), stat.S_IMODE(original.stat().st_mode))
                elif entry["kind"] == "symlink":
                    self.assertTrue(restored.is_symlink())
                    self.assertEqual(restored.readlink(), original.readlink())
                else:
                    self.assertTrue(restored.is_dir())
        self.assertEqual((self.consumer / NATIVE_SDK / "bin/clang++").read_bytes(), b"fixture native compiler\n")
        self.assertTrue((self.consumer / ".ci/llvm-bootstrap.json").is_file())

    def test_cache_key_is_relocatable_and_changes_for_source_pins_and_recipes(self):
        expected = llvm_ci.cache_key(self.producer)
        self.assertEqual(expected, llvm_ci.cache_key(self.consumer))
        for relative in ("clang/src/clang_wasm_main.cpp", "clang/patches/0001-clang-wasip3-in-process.patch",
                         "llvm/toolchain.toml", "buildtools.json", "build.py", "tools/ci-llvm.py"):
            with self.subTest(input=relative):
                path = self.consumer / relative
                original = path.read_bytes()
                path.write_bytes(original + (b"\n" if relative == "buildtools.json" else b"\nchanged input\n"))
                try:
                    self.assertNotEqual(expected, llvm_ci.cache_key(self.consumer))
                finally:
                    path.write_bytes(original)

    def test_restore_rejects_changed_source_pin_or_recipe(self):
        self.export()
        for relative in ("clang/src/clang_wasm_main.cpp", "llvm/toolchain.toml", "build.py"):
            with self.subTest(input=relative):
                path = self.consumer / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\nchanged input\n")
                try:
                    self.assert_rejected()
                finally:
                    path.write_bytes(original)

    def test_export_rejects_stale_source_artifacts(self):
        path = self.producer / "clang/src/clang_wasm_main.cpp"
        path.write_bytes(path.read_bytes() + b"\nchanged after build\n")
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_receipt_with_wrong_source_pin(self):
        path = self.producer / RECEIPT
        receipt = json.loads(path.read_text())
        receipt["source"]["revision"] = "0" * 40
        path.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_receipt_from_another_recipe(self):
        path = self.producer / "buildtools.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_failed_build_receipt(self):
        path = self.producer / RECEIPT
        receipt = json.loads(path.read_text())
        receipt["passed"] = False
        path.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_non_component_even_with_matching_artifact_checksum(self):
        (self.producer / CLANG).write_bytes(b"\0asm\x01\0\0\0")
        path = self.producer / RECEIPT
        receipt = json.loads(path.read_text())
        receipt["artifacts"][CLANG] = digest(self.producer / CLANG)
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "Component"):
            self.export()

    def test_export_rejects_changed_sdk_inventory(self):
        (self.producer / SDK / "SHA256SUMS").write_text("different SDK inventory\n")
        with self.assertRaisesRegex(ValueError, "SDK inventory"):
            self.export()

    def test_export_rejects_incomplete_source_input_receipt(self):
        path = self.producer / RECEIPT
        receipt = json.loads(path.read_text())
        receipt["sourceInputs"].pop("nim/src/c_backend.cpp")
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "source inputs"):
            self.export()

    def test_export_rejects_missing_required_payload(self):
        (self.producer / NIM).unlink()
        with self.assertRaises((ValueError, OSError)):
            self.export()

    def test_restore_rejects_same_size_corrupted_component(self):
        self.export()
        def corrupt(members):
            return [(member, b"x" * len(data) if member.name == CLANG else data) for member, data in members]
        self.assert_rejected(self.rewrite_archive(corrupt))

    def test_restore_rejects_missing_inventory_member(self):
        self.export()
        self.assert_rejected(self.rewrite_archive(lambda members: [item for item in members if item[0].name != NIM]))

    def test_failed_restore_preserves_previously_verified_payload(self):
        self.export()
        original = self.restore()
        corrupt = self.rewrite_archive(lambda members: [
            (member, b"x" * len(data) if member.name == NIM else data) for member, data in members
        ])
        with self.assertRaises(ValueError):
            self.restore(corrupt)
        self.assertEqual(llvm_ci.verify_state(self.consumer), original)

    def test_restore_rejects_duplicate_archive_member(self):
        self.export()
        self.assert_rejected(self.rewrite_archive(lambda members: [*members, next(item for item in members if item[0].name == CLANG)]))

    def test_restore_requires_metadata_before_any_payload(self):
        self.export()
        def move_metadata(members):
            payload = next(item for item in members if item[0].name == CLANG)
            return [payload, *[item for item in members if item is not payload]]
        with self.assertRaisesRegex(ValueError, "begin with bounded metadata"):
            self.restore(self.rewrite_archive(move_metadata))

    def test_restore_rejects_oversized_member_before_metadata_without_reading_body(self):
        member = tarfile.TarInfo(CLANG)
        member.size = llvm_ci.MAX_EXPANDED + 1
        # Deliberately omit the advertised body. The parser must reject the
        # header before attempting to consume or decompress that body.
        with gzip.open(self.archive, "wb") as stream:
            stream.write(member.tobuf(format=tarfile.USTAR_FORMAT))
        with self.assertRaisesRegex(ValueError, "begin with bounded metadata"):
            self.restore()

    def test_restore_rejects_oversized_metadata_without_reading_body(self):
        member = tarfile.TarInfo(METADATA)
        member.size = llvm_ci.MAX_METADATA + 1
        with gzip.open(self.archive, "wb") as stream:
            stream.write(member.tobuf(format=tarfile.USTAR_FORMAT))
        with self.assertRaisesRegex(ValueError, "begin with bounded metadata"):
            self.restore()

    def test_restore_checks_member_size_before_consuming_its_body(self):
        self.export()
        with tarfile.open(self.archive, "r:gz") as stream:
            metadata = stream.extractfile(METADATA).read()
        entry = tarfile.TarInfo(METADATA)
        entry.size = len(metadata)
        oversized = tarfile.TarInfo(CLANG)
        oversized.size = llvm_ci.MAX_EXPANDED + 1
        with gzip.open(self.archive, "wb") as stream:
            stream.write(entry.tobuf(format=tarfile.USTAR_FORMAT))
            stream.write(metadata)
            stream.write(b"\0" * (-len(metadata) % tarfile.BLOCKSIZE))
            stream.write(oversized.tobuf(format=tarfile.USTAR_FORMAT))
        with self.assertRaisesRegex(ValueError, "member size differs from inventory"):
            self.restore()

    def test_restore_rejects_boolean_schema(self):
        self.export()
        archive = self.rewrite_archive(lambda members: self.change_metadata(
            members, lambda metadata: metadata.update(schema=True)))
        self.assert_rejected(archive)

    def test_metadata_json_rejects_nonfinite_numbers(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaisesRegex(ValueError, "invalid JSON constant"):
                    llvm_ci.read_json(('{"size":' + constant + '}').encode())

    def test_restore_rejects_path_traversal_and_absolute_members_without_writing_outside(self):
        self.export()
        outside = self.directory / "escaped"
        for name in ("../escaped", str(outside)):
            with self.subTest(path=name):
                member = tarfile.TarInfo(name)
                member.size = 6
                def add_unsafe_path(members):
                    members = self.change_metadata(members, lambda metadata: metadata["files"].update({
                        name: {"kind": "file", "size": 6, "sha256": hashlib.sha256(b"escape").hexdigest(), "mode": 0o644},
                    }))
                    return [*members, (member, b"escape")]
                archive = self.rewrite_archive(add_unsafe_path)
                self.assert_rejected(archive)
                self.assertFalse(outside.exists())

    def test_restore_rejects_external_links_even_when_claimed_by_inventory(self):
        self.export()
        relative = f"{NATIVE_SDK}/bin/clang++"
        for target in ("/tmp/external-sdk", "../../../../outside"):
            with self.subTest(target=target):
                def change_link(members):
                    members = self.change_metadata(members, lambda metadata: metadata["files"][relative].update(target=target))
                    for member, payload in members:
                        if member.name == relative:
                            member.linkname = target
                    return members
                self.assert_rejected(self.rewrite_archive(change_link))

    def test_restore_rejects_escaping_links_and_special_files(self):
        self.export()
        for kind, target in ((tarfile.SYMTYPE, "/tmp/external-sdk"),
                             (tarfile.SYMTYPE, "../../../../outside"),
                             (tarfile.LNKTYPE, CLANG), (tarfile.FIFOTYPE, "")):
            with self.subTest(kind=kind, target=target):
                member = tarfile.TarInfo(f"{NATIVE_SDK}/bin/unsafe")
                member.type, member.linkname = kind, target
                archive = self.rewrite_archive(lambda members: [*members, (member, None)])
                self.assert_rejected(archive)

    def test_export_rejects_external_sdk_symlink(self):
        link = self.producer / NATIVE_SDK / "bin/clang++"
        link.unlink()
        link.symlink_to(self.directory / "external-native-compiler")
        with self.assertRaises((ValueError, OSError)):
            self.export()

    def test_export_rejects_symlinked_payload_root(self):
        sdk = self.producer / NATIVE_SDK
        outside = self.directory / "external-sdk"
        sdk.rename(outside)
        sdk.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_payload_through_external_parent_symlink(self):
        work = self.producer / "nim/work"
        outside = self.directory / "external-work"
        work.rename(outside)
        work.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.export()

    def test_restore_rejects_staging_directory_outside_checkout(self):
        self.export()
        outside = self.directory / "external-staging"
        outside.mkdir()
        (self.consumer / ".ci").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "staging directory escapes"):
            self.restore()
        self.assertEqual(list(outside.iterdir()), [])

    def test_verify_rejects_modified_or_unlisted_restored_files(self):
        self.export()
        self.restore()
        library = self.consumer / SDK / "lib/libLLVMCore.a"
        original = library.read_bytes()
        library.write_bytes(original[::-1])
        with self.assertRaises(ValueError):
            llvm_ci.verify_state(self.consumer)
        library.write_bytes(original)
        extra = self.consumer / SDK / "lib/unlisted.a"
        extra.write_bytes(b"unexpected file\n")
        with self.assertRaises(ValueError):
            llvm_ci.verify_state(self.consumer)

    def test_compact_export_excludes_native_build_trees(self):
        self.write("clang/.toolchains/build-p3/lib/CMakeFiles/large-native-object.o", b"unused native object")
        self.write("clang/.toolchains/llvm-project/llvm/unneeded-source.cpp", b"unused LLVM source")
        metadata = self.export()
        self.assertFalse(any("CMakeFiles" in path or "unneeded-source" in path for path in metadata["files"]))
        with tarfile.open(self.archive, "r:gz") as stream:
            names = set(stream.getnames())
        self.assertIn(METADATA, names)
        self.assertIn(CLANG, names)
        self.assertIn(f"{SDK}/lib/libLLVMCore.a", names)
        self.assertFalse(any("large-native-object" in name or "unneeded-source" in name for name in names))


if __name__ == "__main__":
    unittest.main()
