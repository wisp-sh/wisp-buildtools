"""Verify local go-pkg support sources without Go, downloads, or compilation."""

import hashlib
import json
from pathlib import Path


SUPPORT_ROOT = Path(__file__).resolve().parents[2]
PIN = Path(__file__).with_name("wit-runtime.lock.json")


def verified_runtime(root: Path, pin: Path = PIN) -> dict[str, bytes]:
    lock = json.loads(pin.read_text())
    if lock["module"] != "go.bytecodealliance.org/pkg" or lock["version"] != "v0.2.3":
        raise ValueError("unsupported Go WIT runtime pin")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid Go WIT runtime directory")
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink in Go WIT runtime")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("invalid Go WIT runtime source")
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != lock["files"].get(name):
            raise ValueError(f"Go WIT runtime differs from v0.2.3 pin: {name}")
        files[name] = data
    if files.keys() != lock["files"].keys():
        raise ValueError("Go WIT runtime file set differs from v0.2.3 pin")
    return files


def stage_runtime(project: Path, support: Path) -> None:
    files = verified_runtime(support)
    destination = project / "vendor/go.bytecodealliance.org/pkg"
    for directory in (project / "vendor", project / "vendor/go.bytecodealliance.org", destination):
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise ValueError("invalid vendored Go WIT runtime directory")
    if destination.exists():
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in vendored Go WIT runtime")
            if path.is_dir():
                continue
            name = path.relative_to(destination).as_posix()
            if not path.is_file() or name not in files or path.read_bytes() != files[name]:
                raise ValueError(f"vendored Go WIT runtime differs from v0.2.3 pin: {name}")
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
