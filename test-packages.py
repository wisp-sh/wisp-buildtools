#!/usr/bin/env python3
"""Check toolchain containers, streamed inventory validation and atomic installs."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

ROOT = Path(__file__).resolve().parent
specification = importlib.util.spec_from_file_location("compiler_builds", ROOT / "build.py")
builds = importlib.util.module_from_spec(specification)
specification.loader.exec_module(builds)
import toolchain_packages as packages

CORE = b"\0asm\x01\0\0\0"
COMPONENT = b"\0asm\x0d\0\x01\0"


def fixture_tool(name, *, dependencies=()):
    return {"directory": name, "description": name, "version": "1.2.3",
            "dependencies": [], "runtimeDependencies": list(dependencies),
            "bootstrapRequirements": ["native bootstrap compiler, not needed to use this package"],
            "build": [], "test": [], "artifacts": [name + "/dist/compiler.wasm"],
            "package": {"include": [name + "/dist", name + "/compile.py"], "optional": []}}


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = {"tools": {"compiler": fixture_tool("compiler", dependencies=["shared"]),
                                   "shared": fixture_tool("shared")}}
        for name in self.document["tools"]:
            directory = self.root / name
            (directory / "dist").mkdir(parents=True)
            (directory / "dist/compiler.wasm").write_bytes(CORE)
            (directory / "compile.py").write_text("print('adapter')\n")
            (directory / "compile.py").chmod(0o755)
        # Include both Wasm encodings and a native host so size categories are useful.
        (self.root / "compiler/dist/runtime.component").write_bytes(COMPONENT)
        (self.root / "compiler/dist/native-host").write_bytes(b"\x7fELFfixture-host")
        (self.root / "compiler/dist/native-host").chmod(0o755)
        self.output = self.root / "packages"
        self.shared_record = self.package("shared")
        self.record = self.package()
        self.bundle = Path(self.record["archive"])

    def package(self, name="compiler"):
        return builds.package_tool(self.document, name, root=self.root, output_dir=self.output)

    def contents(self):
        with zipfile.ZipFile(self.bundle) as stream:
            return json.loads(stream.read("release.json")), stream.read("toolchain.tar.gz")

    def write_bundle(self, *, release=None, payload=None, raw_manifest=None, extra=(), compression=zipfile.ZIP_STORED):
        original, archive = self.contents()
        release = original if release is None else release
        payload = archive if payload is None else payload
        target = self.root / "modified.wisp.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(target, "w", compression=compression) as stream:
                stream.writestr("release.json", raw_manifest if raw_manifest is not None else json.dumps(release))
                stream.writestr("toolchain.tar.gz", payload)
                for name, data in extra:
                    stream.writestr(name, data)
        return target

    def changed_tar(self, transform):
        release, compressed = self.contents()
        output = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz") as source:
            rows = [(copy.copy(member), source.extractfile(member).read() if member.isfile() else None)
                    for member in source]
        rows = transform(rows)
        with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for member, data in rows:
                if data is not None:
                    member.size = len(data)
                archive.addfile(member, io.BytesIO(data) if data is not None else None)
        compressed = output.getvalue()
        release["archive"]["size"] = len(compressed)
        release["archive"]["sha256"] = hashlib.sha256(compressed).hexdigest()
        return self.write_bundle(release=release, payload=compressed)

    def rejected(self, bundle):
        with self.assertRaises((ValueError, zipfile.BadZipFile, tarfile.TarError)):
            packages.validate(bundle)

    def test_deterministic_stored_envelope_and_complete_file_inventory(self):
        initial = self.bundle.read_bytes()
        for path in self.root.glob("*/compile.py"):
            path.touch()
        second = self.package()
        self.assertEqual(initial, Path(second["archive"]).read_bytes())
        with zipfile.ZipFile(self.bundle) as outer:
            self.assertEqual(outer.namelist(), ["release.json", "toolchain.tar.gz"])
            self.assertTrue(all(item.compress_type == zipfile.ZIP_STORED for item in outer.infolist()))
        release = packages.read_manifest(self.bundle)
        self.assertEqual((release["schema"], release["kind"]), (4, "toolchain"))
        inventory = release["archive"]["inventory"]
        self.assertEqual(len(inventory), self.record["files"])
        for relative, metadata in inventory.items():
            original = self.root / relative
            self.assertEqual(metadata["size"], original.stat().st_size)
            self.assertEqual(metadata["sha256"], builds.digest(original))
            self.assertEqual(metadata["mode"], 0o755 if original.stat().st_mode & 0o111 else 0o644)
        packages.validate(self.bundle)

    def test_size_accounting_separates_wasm_native_and_support(self):
        release = packages.read_manifest(self.bundle)
        inventory = release["archive"]["inventory"]
        self.assertEqual(self.record["installedBytes"], sum(item["size"] for item in inventory.values()))
        self.assertEqual(self.record["wasmBytes"], len(CORE) + len(COMPONENT))
        self.assertEqual(self.record["nativeBytes"], len(b"\x7fELFfixture-host"))
        self.assertEqual(self.record["supportBytes"], len("print('adapter')\n"))
        self.assertEqual(self.record["installedBytes"],
                         self.record["wasmBytes"] + self.record["nativeBytes"] + self.record["supportBytes"])
        self.assertEqual(self.record["bytes"], self.bundle.stat().st_size)

    def test_private_evidence_and_debug_dumps_do_not_enter_public_bundle(self):
        private = ["evidence/result.json", ".private/plan.md", "benchmarks/result.json",
                   "runner.log", "macro-trace.jsonl", "frontend.time", "crash.dmp"]
        public = ["manifest.json", "SHA256SUMS", "source-lock.json", "debug/elf.a"]
        for relative in private + public:
            path = self.root / "compiler/dist" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative)
        record = self.package()
        bundle = Path(record["archive"])
        packages.validate(bundle)
        inventory = packages.read_manifest(bundle)["archive"]["inventory"]
        for relative in private:
            self.assertNotIn("compiler/dist/" + relative, inventory)
        for relative in public:
            self.assertIn("compiler/dist/" + relative, inventory)

    def test_python_bootstrap_paths_are_portable_and_keep_integrity_hashes(self):
        self.document["tools"]["python"] = fixture_tool("python")
        base = self.root / "python"
        (base / "dist/strings").mkdir(parents=True)
        (base / "compile.py").write_text("# adapter\n")
        (base / "dist/compiler.wasm").write_bytes(CORE)
        for subdir, key in [("", "native_bootstrap_tools"), ("strings/", "bootstrapOnlyNativeTools")]:
            path = base / ("dist/" + subdir + "manifest.json")
            original = {"sha256": {"compiler.wasm": builds.digest(base / "dist/compiler.wasm")},
                        key: {"wasmtime": {"path": "/home/developer/private/bin/wasmtime", "sha256": "a" * 64}}}
            path.write_text(json.dumps(original))
        record = self.package("python")
        bundle = Path(record["archive"])
        packages.validate(bundle)
        with zipfile.ZipFile(bundle) as outer, tarfile.open(fileobj=io.BytesIO(outer.read("toolchain.tar.gz")), mode="r:gz") as archive:
            for subdir, key in [("", "native_bootstrap_tools"), ("strings/", "bootstrapOnlyNativeTools")]:
                relative = "python/dist/" + subdir + "manifest.json"
                metadata = json.load(archive.extractfile("wisp-buildtools/" + relative))
                self.assertEqual(metadata[key]["wasmtime"], {"path": "wasmtime", "sha256": "a" * 64})
                self.assertIn("/home/developer/private", (self.root / relative).read_text())
                self.assertEqual(metadata["sha256"]["compiler.wasm"], builds.digest(base / "dist/compiler.wasm"))

    def test_build_machine_paths_in_binary_payloads_block_publication(self):
        path = self.root / "compiler/dist/compiler.wasm"
        original = self.bundle.read_bytes()
        # Split the path at the packer's chunk boundary to exercise streaming.
        for private in [str(Path.home()).encode() + b"/private/compiler.rs",
                        b"/Users/another-developer/private/compiler.rs"]:
            path.write_bytes(CORE + b"\0" * (1024 * 1024 - len(CORE) - 3) + private)
            with self.subTest(private=private), self.assertRaisesRegex(ValueError, "build-machine path.*path remapping"):
                self.package()
        self.assertEqual(self.bundle.read_bytes(), original)

    def test_upstream_home_collision_requires_exact_reviewed_payload(self):
        relative = "compiler/dist/compiler.wasm"
        path = self.root / relative
        public = CORE + b"/home/runner/.cargo/registry/src/public-crate/src/lib.rs\0"
        path.write_bytes(public)
        approved = {relative: hashlib.sha256(public).hexdigest()}
        with mock.patch.object(packages.Path, "home", return_value=Path("/home/runner")), \
                mock.patch.dict(packages.PUBLIC_UPSTREAM_PAYLOADS, approved, clear=True):
            record = self.package()
            packages.validate(Path(record["archive"]))
            # An updated upstream build or an appended local path needs review.
            path.write_bytes(public + b"/home/runner/private/secret.rs")
            with self.assertRaisesRegex(ValueError, "build-machine path"):
                self.package()

    def test_upstream_exception_never_allows_checkout_or_t3_paths(self):
        relative = "compiler/dist/compiler.wasm"
        path = self.root / relative
        for private in [str(self.root).encode() + b"/private.rs", b"/home/runner/.t3/projects/private.rs"]:
            payload = CORE + b"/home/runner/public.rs\0" + private
            path.write_bytes(payload)
            with self.subTest(private=private), \
                    mock.patch.object(packages.Path, "home", return_value=Path("/home/runner")), \
                    mock.patch.dict(packages.PUBLIC_UPSTREAM_PAYLOADS,
                                    {relative: hashlib.sha256(payload).hexdigest()}, clear=True):
                with self.assertRaisesRegex(ValueError, "build-machine path"):
                    self.package()

    def test_personal_home_paths_still_block_publication(self):
        path = self.root / "compiler/dist/compiler.wasm"
        for builder in ["/home/luap", "/Users/luap", "/home/runner"]:
            for home in ["/home/luap", "/Users/luap", "/home/runner"]:
                with self.subTest(builder=builder, home=home), \
                        mock.patch.object(packages.Path, "home", return_value=Path(builder)):
                    path.write_bytes(CORE + home.encode() + b"/private/compiler.rs")
                    with self.assertRaisesRegex(ValueError, "build-machine path"):
                        self.package()

    def test_composed_and_split_upstream_strings_have_narrow_exceptions(self):
        for relative, strings in packages.PUBLIC_UPSTREAM_STRINGS.items():
            path = self.root / relative
            path.parent.mkdir(parents=True)
            self.document["tools"]["compiler"]["package"]["include"].append(relative)
            for public in strings:
                # Exercise the complete string at the streaming boundary and EOF.
                for padding in [b"", b"\0" * (1024 * 1024 - 3)]:
                    with self.subTest(relative=relative, padding=len(padding)), \
                            mock.patch.object(packages.Path, "home", return_value=Path("/home/runner")):
                        path.write_bytes(CORE + padding + public)
                        self.package()
                        for private in [public[:-1] + b"/private.py\0", public + b"/home/runner/private.py\0"]:
                            path.write_bytes(CORE + padding + private)
                            with self.assertRaisesRegex(ValueError, "build-machine path"):
                                self.package()
                        path.write_bytes(CORE + public)
                # The same bytes in an unrelated file have no exception.
                unrelated = self.root / "compiler/dist/unknown.wasm"
                unrelated.write_bytes(CORE + public)
                with mock.patch.object(packages.Path, "home", return_value=Path("/home/runner")):
                    with self.assertRaisesRegex(ValueError, "build-machine path"):
                        self.package()
                unrelated.unlink()

    def test_short_inputs_are_also_checked_for_private_paths(self):
        path = self.root / "compiler/dist/compile.py"
        path.write_bytes(b"/u/leak")
        with mock.patch.object(packages.Path, "home", return_value=Path("/u")):
            with self.assertRaisesRegex(ValueError, "build-machine path"):
                self.package()

    def test_size_report_uses_bundle_names_and_host_relative_paths(self):
        host = self.root / "host"
        host.mkdir()
        (host / "engine.so").write_bytes(b"host engine")
        with mock.patch.dict(packages.os.environ, {"WISP_WASMTIME_PYTHON": str(host)}):
            report = packages.report([self.bundle, Path(self.shared_record["archive"])])
        self.assertEqual(next(p for p in report["packages"] if p["tool"] == "compiler")["archive"], self.bundle.name)
        measured = next(h for h in report["executionHosts"] if h["environment"] == "WISP_WASMTIME_PYTHON")
        self.assertEqual(measured["files"][0]["path"], "engine.so")
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_report_measures_shared_paths_and_exact_content_once(self):
        other = self.package("shared")
        bundles = [self.bundle, Path(other["archive"])]
        result = packages.report(bundles)
        inventories = [packages.read_manifest(bundle)["archive"]["inventory"] for bundle in bundles]
        unique_paths = {path: item for inventory in inventories for path, item in inventory.items()}
        unique_hashes = {item["sha256"]: item["size"] for inventory in inventories for item in inventory.values()}
        totals = result["totals"]
        self.assertEqual(totals["downloadBytes"], sum(bundle.stat().st_size for bundle in bundles))
        self.assertEqual(totals["installedBytes"], self.record["installedBytes"] + other["installedBytes"])
        self.assertEqual(totals["uniquePathInstalledBytes"], sum(item["size"] for item in unique_paths.values()))
        self.assertEqual(totals["contentDeduplicatedInstalledBytes"], sum(unique_hashes.values()))
        self.assertEqual(totals["duplicatedContentBytes"],
                         totals["installedBytes"] - totals["contentDeduplicatedInstalledBytes"])
        self.assertTrue(any(item["sha256"] == hashlib.sha256(CORE).hexdigest()
                            and len(item["paths"]) >= 2 for item in result["sharedFiles"]))
        compiler = next(item for item in result["packages"] if item["tool"] == "compiler")
        self.assertEqual(compiler["dependencies"], ["shared"])
        self.assertEqual(compiler["closureInstalledBytes"], totals["installedBytes"])

    def test_report_outputs_machine_readable_and_human_readable_sizes(self):
        output = self.root / "reports"
        packages.write_reports(packages.report([self.bundle, Path(self.shared_record["archive"])]), output)
        json_files = list(output.glob("*.json"))
        markdown_files = list(output.glob("*.md"))
        self.assertTrue(json_files)
        self.assertTrue(markdown_files)
        self.assertEqual(json.loads(json_files[0].read_text())["totals"]["installedBytes"], self.record["installedBytes"] + self.shared_record["installedBytes"])
        self.assertIn("compiler", markdown_files[0].read_text())

    def test_wrong_compressed_archive_hash_is_rejected(self):
        release, _ = self.contents()
        release["archive"]["sha256"] = "0" * 64
        self.rejected(self.write_bundle(release=release))

    def test_wrong_compressed_archive_size_is_rejected(self):
        release, _ = self.contents()
        release["archive"]["size"] += 1
        self.rejected(self.write_bundle(release=release))

    def test_truncated_zip_is_rejected(self):
        target = self.root / "truncated.wisp.zip"
        target.write_bytes(self.bundle.read_bytes()[:-20])
        self.rejected(target)

    def test_extra_outer_file_is_rejected(self):
        self.rejected(self.write_bundle(extra=[("unlisted.txt", b"unlisted")]))

    def test_duplicate_outer_file_is_rejected(self):
        self.rejected(self.write_bundle(extra=[("release.json", b"{}")]))

    def test_compressed_outer_entries_are_rejected(self):
        self.rejected(self.write_bundle(compression=zipfile.ZIP_DEFLATED))

    def test_duplicate_json_keys_are_rejected(self):
        release, _ = self.contents()
        data = json.dumps(release)
        duplicated = '{"schema":4,' + data[1:]
        target = self.write_bundle(raw_manifest=duplicated)
        with self.assertRaises(ValueError):
            packages.read_manifest(target)

    def test_nested_duplicate_json_keys_are_rejected(self):
        release, _ = self.contents()
        data = json.dumps(release)
        duplicated = data.replace('"inventory": {', '"inventory": {}, "inventory": {', 1)
        self.assertNotEqual(duplicated, data)
        self.rejected(self.write_bundle(raw_manifest=duplicated))

    def test_json_trailing_data_is_rejected(self):
        release, _ = self.contents()
        self.rejected(self.write_bundle(raw_manifest=json.dumps(release) + " {}"))

    def test_non_finite_json_number_is_rejected(self):
        release, _ = self.contents()
        data = json.dumps(release).replace('"size": ' + str(release["archive"]["size"]), '"size": NaN', 1)
        self.rejected(self.write_bundle(raw_manifest=data))

    def test_binary_payload_cannot_be_disguised_as_support_data(self):
        for relative in ["compiler/dist/native-host", "compiler/dist/compiler.wasm"]:
            with self.subTest(path=relative):
                release, _ = self.contents()
                item = release["archive"]["inventory"][relative]
                previous = "nativeBytes" if item["format"] == "native" else "wasmBytes"
                release["sizes"][previous] -= item["size"]
                release["sizes"]["supportBytes"] += item["size"]
                item["format"] = "data"
                self.rejected(self.write_bundle(release=release))

    def test_malformed_field_types_are_value_errors(self):
        for field in ["format", "role"]:
            with self.subTest(field=field):
                release, _ = self.contents()
                release["archive"]["inventory"]["compiler/compile.py"][field] = []
                with self.assertRaises(ValueError):
                    packages.read_manifest(self.write_bundle(release=release))

    def test_wrong_kind_and_schema_are_rejected(self):
        for field, value in [("schema", 3), ("kind", "application")]:
            with self.subTest(field=field):
                release, _ = self.contents()
                release[field] = value
                self.rejected(self.write_bundle(release=release))

    def test_inventory_paths_cannot_escape_prefix(self):
        for unsafe in ["../secret", "/absolute", "compiler/../secret", "compiler\\..\\secret", "compiler//file"]:
            with self.subTest(path=unsafe):
                release, _ = self.contents()
                inventory = release["archive"]["inventory"]
                inventory[unsafe] = inventory.pop(next(iter(inventory)))
                self.rejected(self.write_bundle(release=release))

    def test_tar_content_hash_tampering_is_rejected(self):
        def tamper(rows):
            member, data = rows[0]
            rows[0] = (member, bytes([data[0] ^ 1]) + data[1:])
            return rows
        self.rejected(self.changed_tar(tamper))

    def test_extra_tar_member_is_rejected_even_with_updated_archive_hash(self):
        def extra(rows):
            member = tarfile.TarInfo("wisp-buildtools/compiler/dist/unlisted")
            member.mode = 0o644
            return [*rows, (member, b"unlisted")]
        self.rejected(self.changed_tar(extra))

    def test_missing_tar_member_is_rejected(self):
        self.rejected(self.changed_tar(lambda rows: rows[1:]))

    def test_duplicate_tar_member_is_rejected(self):
        self.rejected(self.changed_tar(lambda rows: [*rows, rows[0]]))

    def test_tar_traversal_cannot_write_outside_install(self):
        def traversal(rows):
            member = tarfile.TarInfo("wisp-buildtools/../escaped")
            member.mode = 0o644
            return [*rows, (member, b"escape")]
        bundle = self.changed_tar(traversal)
        prefix = self.root / "installed"
        with self.assertRaises(ValueError):
            packages.install(bundle, prefix)
        self.assertFalse(prefix.exists())
        self.assertFalse((self.root / "escaped").exists())

    def test_symbolic_links_and_hard_links_are_rejected(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE]:
            with self.subTest(kind=kind):
                def symbolic(rows):
                    member, _ = rows[0]
                    member.type, member.size, member.linkname = kind, 0, "../../secret"
                    rows[0] = (member, None)
                    return rows
                self.rejected(self.changed_tar(symbolic))

    def test_tar_execute_bit_must_match_inventory(self):
        def mode(rows):
            member, _ = rows[0]
            member.mode = 0o644 if member.mode == 0o755 else 0o755
            return rows
        self.rejected(self.changed_tar(mode))

    def test_install_is_relocatable_and_preserves_executable_modes(self):
        prefix = self.root / "first-location"
        result = packages.install(self.bundle, prefix)
        self.assertIsInstance(result, dict)
        adapter = prefix / "compiler/compile.py"
        self.assertTrue(adapter.is_file())
        self.assertEqual(adapter.stat().st_mode & 0o777, 0o755)
        self.assertEqual((prefix / "compiler/dist/compiler.wasm").stat().st_mode & 0o777, 0o644)
        moved = self.root / "unrelated-location"
        shutil.move(prefix, moved)
        completed = subprocess.run([sys.executable, "-I", str(moved / "compiler/compile.py")],
                                   check=True, text=True, capture_output=True, cwd=self.root)
        self.assertEqual(completed.stdout, "adapter\n")
        self.assertEqual((moved / "shared/dist/compiler.wasm").read_bytes(), CORE)
        self.assertEqual(result["packages"], ["wisp:shared", "wisp:compiler"])
        self.assertEqual(result["installedBytes"], self.record["installedBytes"] + self.shared_record["installedBytes"])
        self.assertTrue((moved / "share/wisp/toolchains/shared.json").is_file())

    def test_missing_provider_prevents_installation(self):
        Path(self.shared_record["archive"]).unlink()
        prefix = self.root / "missing-provider"
        with self.assertRaisesRegex(ValueError, "missing package dependency"):
            packages.install(self.bundle, prefix)
        self.assertFalse(prefix.exists())

    def test_provider_version_mismatch_prevents_installation(self):
        provider = Path(self.shared_record["archive"])
        with zipfile.ZipFile(provider) as archive:
            doc = json.loads(archive.read("release.json"))
            payload = archive.read("toolchain.tar.gz")
        doc["version"] = "9.0.0"
        with zipfile.ZipFile(provider, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("release.json", json.dumps(doc))
            archive.writestr("toolchain.tar.gz", payload)
        prefix = self.root / "wrong-provider"
        with self.assertRaisesRegex(ValueError, "identity or version mismatch"):
            packages.install(self.bundle, prefix)
        with self.assertRaisesRegex(ValueError, "dependency version mismatch"):
            packages.report([self.bundle, provider])
        self.assertFalse(prefix.exists())

    def test_explicit_provider_can_come_from_another_directory(self):
        separate = self.root / "separate-provider"
        separate.mkdir()
        provider = separate / "shared.wisp.zip"
        Path(self.shared_record["archive"]).rename(provider)
        result = packages.install([self.bundle, provider], self.root / "separate-install")
        self.assertEqual(result["packages"], ["wisp:shared", "wisp:compiler"])

    def test_provider_damage_rolls_back_entire_installation(self):
        provider = Path(self.shared_record["archive"])
        data = bytearray(provider.read_bytes())
        with zipfile.ZipFile(provider) as archive:
            entry = archive.getinfo("toolchain.tar.gz")
            position = entry.header_offset + 30 + len(entry.filename) + 15
        data[position] ^= 1
        provider.write_bytes(data)
        prefix = self.root / "damaged-provider"
        with self.assertRaises((ValueError, zipfile.BadZipFile, OSError)):
            packages.install(self.bundle, prefix)
        self.assertFalse(prefix.exists())

    def test_shared_provider_installed_once_in_diamond_dependency_graph(self):
        for name in ("left", "right"):
            self.document["tools"][name] = fixture_tool(name, dependencies=["shared"])
            shutil.copytree(self.root / "shared", self.root / name)
            self.package(name)
        self.document["tools"]["compiler"]["runtimeDependencies"] = ["left", "right"]
        self.package()
        result = packages.install(self.bundle, self.root / "diamond")
        self.assertEqual(result["packages"], ["wisp:shared", "wisp:left", "wisp:right", "wisp:compiler"])

    def test_multiple_selected_compilers_share_one_provider(self):
        self.document["tools"]["other"] = fixture_tool("other", dependencies=["shared"])
        shutil.copytree(self.root / "shared", self.root / "other")
        other = Path(self.package("other")["archive"])
        result = packages.install([self.bundle, other], self.root / "multiple")
        self.assertEqual(result["packages"], ["wisp:shared", "wisp:compiler", "wisp:other"])

    def test_runtime_cycle_is_rejected_during_installation(self):
        self.document["tools"]["shared"]["runtimeDependencies"] = ["compiler"]
        # Write a valid individual package with a cyclic installed graph.
        files = {"shared/compile.py": self.root / "shared/compile.py",
                 "shared/dist/compiler.wasm": self.root / "shared/dist/compiler.wasm"}
        packages.pack(self.document, "shared", files, root=self.root, output_dir=self.output)
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            packages.install(self.bundle, self.root / "cycle")

    def test_portable_platform_cannot_hide_native_host(self):
        release, _ = self.contents()
        release["platform"] = "wasm32-wasi"
        self.rejected(self.write_bundle(release=release))

    def test_install_refuses_existing_prefix_without_changing_it(self):
        prefix = self.root / "existing"
        prefix.mkdir()
        (prefix / "keep").write_text("existing data")
        with self.assertRaises((ValueError, FileExistsError)):
            packages.install(self.bundle, prefix)
        self.assertEqual(list(prefix.iterdir()), [prefix / "keep"])
        self.assertEqual((prefix / "keep").read_text(), "existing data")

    def test_invalid_install_does_not_publish_prefix_or_leave_staging_files(self):
        release, _ = self.contents()
        release["archive"]["sha256"] = "0" * 64
        damaged = self.write_bundle(release=release)
        prefix = self.root / "failed-install"
        before = set(self.root.iterdir())
        with self.assertRaises(ValueError):
            packages.install(damaged, prefix)
        self.assertFalse(prefix.exists())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_mutating_source_during_archive_write_does_not_replace_published_bundle(self):
        published = self.bundle.read_bytes()
        checksum = self.bundle.with_suffix(self.bundle.suffix + ".sha256")
        published_checksum = checksum.read_bytes()
        original_addfile = tarfile.TarFile.addfile
        mutated = False
        def mutate(archive, member, fileobj=None):
            nonlocal mutated
            if member.name == "wisp-buildtools/compiler/compile.py" and not mutated:
                mutated = True
                (self.root / "compiler/compile.py").write_text("print('mutated')\n")
            return original_addfile(archive, member, fileobj)
        with mock.patch.object(tarfile.TarFile, "addfile", mutate):
            with self.assertRaises(ValueError):
                self.package()
        self.assertTrue(mutated)
        self.assertEqual(self.bundle.read_bytes(), published)
        self.assertEqual(checksum.read_bytes(), published_checksum)
        self.assertFalse(any(path.name.startswith(".compiler-") for path in self.output.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
