"""Read and verify the packaged Go toolchain without invoking host Go."""

from __future__ import annotations

from pathlib import Path
import tomllib

from .graph import file_digest


class Toolchain:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.manifest = tomllib.loads((self.root / "manifest.toml").read_text(encoding="utf-8"))
        if self.manifest["host"] != {"kind": "wasip1", "arch": "wasm32"}:
            raise ValueError("toolchain host must be wasip1/wasm32")
        if self.manifest["capabilities"] != {"filesystem": True, "network": False}:
            raise ValueError("unsupported toolchain capabilities")
        checksum_path = self.root / "SHA256SUMS"
        listed = {}
        for line in checksum_path.read_text(encoding="utf-8").splitlines():
            expected, relative = line.split("  ", 1)
            file = (self.root / relative).resolve(strict=True)
            if not file.is_relative_to(self.root) or file_digest(file) != expected:
                raise ValueError(f"toolchain checksum mismatch: {relative}")
            listed[relative] = expected
        actual = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                  if path.is_file()}
        if actual != set(listed) | {"SHA256SUMS"}:
            raise ValueError("toolchain file set differs from SHA256SUMS")
        self.digest = file_digest(checksum_path)
        self.tools = {}
        for name, details in self.manifest["tools"].items():
            relative = details["path"]
            if relative not in listed:
                raise ValueError(f"unlisted tool: {relative}")
            self.tools[name] = self.root / relative
        self.goroot = (self.root / self.manifest["resources"]["goroot"]).resolve(strict=True)
        if not self.goroot.is_relative_to(self.root):
            raise ValueError("GOROOT resource escapes the toolchain")
        archive_root = self.goroot / "pkg/wasip1_wasm"
        self.stdlib = {path.relative_to(archive_root).as_posix()[:-2]:
                       f"/goroot/pkg/wasip1_wasm/{path.relative_to(archive_root).as_posix()}"
                       for path in archive_root.rglob("*.a")}
        if len(self.stdlib) != self.manifest["package"]["stdlib_archive_count"]:
            raise ValueError("standard-library archive count mismatch")
