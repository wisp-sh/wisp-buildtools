"""Focused checks for reproducible bootstrap inputs and pinned publication."""
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import toolchain


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="javascript bootstrap ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout with spaces"
        self.root.mkdir()
        self.cargo_home = Path(self.temporary.name) / "cargo home"
        self.sysroot = Path(self.temporary.name) / "rust installation"
        self.componentizer = toolchain.CORE_HEADER + b"componentizer"
        self.esbuild = toolchain.CORE_HEADER + b"esbuild"
        self.manifest = {"schemaVersion": 1, "bootstrapRustc": "rustc pinned", "files": {
            f".toolchains/wasm/{name}.wasm": {
                "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}
            for name, contents in (("componentize", self.componentizer), ("esbuild", self.esbuild))
        }}
        self.manifest_path = self.root / "checksums/wasm-tools.json"
        self.manifest_path.parent.mkdir()
        self.original_manifest = json.dumps(self.manifest).encode()
        self.manifest_path.write_bytes(self.original_manifest)
        self.commands = []
        self.built_componentizer = self.componentizer

    def fake_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[0] == "cargo":
            output = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / (
                "wasm32-wasip1/release/wisp-javascript-componentize.wasm")
            output.parent.mkdir(parents=True)
            output.write_bytes(self.built_componentizer)
        else:
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(self.esbuild)
            receipt = Path(command[command.index("--receipt") + 1])
            receipt.write_text("new receipt")

    def rebuild(self):
        def rustc_output(command, **kwargs):
            if command == ["rustc", "--version", "--verbose"]:
                return "rustc pinned\ncommit-hash: pinned-commit\n"
            self.assertEqual(command, ["rustc", "--print", "sysroot"])
            return str(self.sysroot) + "\n"

        with (patch.object(bootstrap, "ROOT", self.root),
              patch.object(toolchain, "MANIFEST", self.manifest_path),
              patch.object(bootstrap.shutil, "which", return_value="cargo"),
              patch.object(bootstrap.subprocess, "check_output", side_effect=rustc_output),
              patch.object(bootstrap, "run", side_effect=self.fake_run),
              patch.object(bootstrap, "runtimes")):
            bootstrap.rebuild(Namespace(offline=True, json=True, go="pinned-go"))

    def test_remaps_survive_spaces_and_replace_inherited_flags(self):
        with patch.dict(os.environ, {"CARGO_HOME": str(self.cargo_home),
                                     "RUSTFLAGS": "--cfg=untrusted",
                                     "CARGO_ENCODED_RUSTFLAGS": "--cfg=also_untrusted"}):
            self.rebuild()
        command, options = self.commands[0]
        self.assertIn("--locked", command)
        self.assertIn("--offline", command)
        env = options["env"]
        self.assertNotIn("RUSTFLAGS", env)
        self.assertEqual(env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f"), [
            f"--remap-path-prefix={self.root}=/wisp/javascript",
            f"--remap-path-prefix={self.cargo_home}/registry=/cargo/registry",
            f"--remap-path-prefix={self.sysroot}/lib/rustlib/src/rust/library=/rustc/pinned-commit/library",
        ])
        self.assertEqual((self.root / ".toolchains/wasm/componentize.wasm").read_bytes(), self.componentizer)
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_mismatch_preserves_installed_guests_receipt_and_pins(self):
        installed = {}
        for relative in (*self.manifest["files"], ".toolchains/esbuild-build.json"):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"previous installed file")
            installed[destination] = destination.read_bytes()
        # Keep the length and Wasm header correct, so the digest must reject it.
        self.built_componentizer = self.componentizer[:-1] + b"!"
        with self.assertRaisesRegex(ValueError, "checksum mismatch.*componentize.wasm"):
            self.rebuild()
        for destination, contents in installed.items():
            self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)
        self.assertEqual(list((self.root / ".toolchains").glob(".bootstrap-*")), [])


if __name__ == "__main__":
    unittest.main()
