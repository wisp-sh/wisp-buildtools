#!/usr/bin/env python3
"""Check the toolchain release manifest written for Wisp's library builds."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_manifest", ROOT / "tools/release-manifest.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
COMMIT = "a" * 40


def write_bundle(directory: Path, tool: str, requires: list[str] = (), data: bytes | None = None, recorded: str | None = None):
    archive = directory / f"{tool}.wisp.zip"
    archive.write_bytes(data or f"bundle {tool}".encode())
    digest = recorded or hashlib.sha256(archive.read_bytes()).hexdigest()
    (directory / f"{tool}.wisp.zip.sha256").write_text(f"{digest}  {archive.name}\n")
    requirements = [{"kind": "runtime-host", "name": "Wisp Wasmtime host", "version": "50.0.0"}]
    requirements += [{"kind": "shared-tool", "name": f"wisp:{name}", "version": "0.1.0"} for name in requires]
    (directory / f"{tool}.wisp.json").write_text(json.dumps({"kind": "toolchain", "tool": tool, "identity": f"wisp:{tool}",
        "version": "0.1.0", "upstreamVersion": "upstream", "platform": "wasm32-wasi", "requirements": requirements}))


class ReleaseManifestTests(unittest.TestCase):
    def test_lists_every_bundle_with_its_exact_size_digest_and_release_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_bundle(directory, "llvm")
            write_bundle(directory, "clang", ["llvm"])
            value = release.manifest(directory, "wisp-sh/wisp-buildtools", "toolchains-1", COMMIT)
            self.assertEqual(value["schema"], 2)
            self.assertEqual([entry["tool"] for entry in value["bundles"]], ["clang", "llvm"])
            clang = value["bundles"][0]
            self.assertEqual(clang["url"], "https://github.com/wisp-sh/wisp-buildtools/releases/download/toolchains-1/clang.wisp.zip")
            self.assertEqual(clang["sha256"], hashlib.sha256(b"bundle clang").hexdigest())
            self.assertEqual((clang["size"], clang["requires"]), (len(b"bundle clang"), ["wisp:llvm"]))

    def test_rejects_a_release_missing_a_shared_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_bundle(directory, "clang", ["llvm"])
            with self.assertRaisesRegex(SystemExit, "requires wisp:llvm"):
                release.manifest(directory, "wisp-sh/wisp-buildtools", "toolchains-1", COMMIT)

    def test_rejects_a_bundle_that_differs_from_its_recorded_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_bundle(directory, "llvm", recorded="0" * 64)
            with self.assertRaisesRegex(SystemExit, "recorded SHA-256"):
                release.manifest(directory, "wisp-sh/wisp-buildtools", "toolchains-1", COMMIT)

    def test_rejects_unsafe_tags_and_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_bundle(directory, "llvm")
            for tag, commit in [("../x", COMMIT), ("toolchains-1", "main")]:
                with self.subTest(tag=tag, commit=commit), self.assertRaises(SystemExit):
                    release.manifest(directory, "wisp-sh/wisp-buildtools", tag, commit)


if __name__ == "__main__":
    unittest.main()
