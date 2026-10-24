#!/usr/bin/env python3
"""Transfer verified LLVM bootstrap outputs between isolated compiler CI jobs."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SDK = "llvm/artifacts/llvm-24-d297af3-wasip3-o1"
NATIVE_SDK = "clang/.toolchains/wasi-sdk-34.0-x86_64-linux"
WASM_TOOLS = "clang/.toolchains/wasm-tools-1.262.0-x86_64-linux"
HOST = ".ci/hosts/wisp-wasmtime-49.0.1"
PAYLOAD_PATHS = (
    SDK, "llvm/dist/shared", "clang/.toolchains/build-p3/bin/clang-wasm.component",
    "clang/include", "nim/work/backend-build/c-backend", "build/receipts/clang/build.json",
    "clang/.toolchains/llvm-project/LICENSE.TXT", NATIVE_SDK, WASM_TOOLS, HOST,
)
INPUT_PATHS = (
    "buildtools.json", "build.py", "package-profiles.json", "tools/ci-llvm.py",
    "tools/ci-prerequisites.py", "tools/ci-tools.lock.json", "llvm/toolchain.toml",
)
INPUT_ROOTS = ("llvm/scripts", "llvm/patches", "llvm/licenses/sdk", "tools/shared-hosts", ".github/workflows")
ARCHIVE_METADATA = "wisp-llvm-bootstrap.json"
STATE_METADATA = ".ci/llvm-bootstrap.json"
KIND = "wisp-llvm-bootstrap"
MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 4 * 1024**3
MAX_METADATA = 16 * 1024**2
MAX_FILES = 60000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def read_json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("invalid JSON constant: " + value)
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def safe_path(value: str) -> str:
    require(isinstance(value, str) and value and len(value) <= 1024
            and "\\" not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "unsafe state path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(part not in {"", ".", ".."} for part in value.split("/")),
            "unsafe state path: " + value)
    return value


def owner(path: str) -> str:
    safe_path(path)
    for prefix in PAYLOAD_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    raise ValueError("path outside LLVM state: " + path)


def fingerprint(root: Path) -> dict[str, str]:
    root = Path(root).resolve()
    manifest = read_json((root / "buildtools.json").read_bytes())
    paths = set(INPUT_PATHS) | set(manifest["tools"]["clang"]["sourceInputs"])
    for relative in INPUT_ROOTS:
        directory = root / relative
        require(directory.is_dir() and not directory.is_symlink(), "missing fingerprint directory: " + relative)
        paths.update(path.relative_to(root).as_posix() for path in directory.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"})
    result = {}
    for relative in sorted(paths):
        safe_path(relative)
        path = root / relative
        require(path.is_file() and path.resolve().is_relative_to(root),
                "missing or unsafe fingerprint input: " + relative)
        result[relative] = digest(path)
    return result


def key_for(inputs: dict[str, str]) -> str:
    release = platform.freedesktop_os_release()
    abi = {"system": platform.system(), "machine": platform.machine(), "libc": platform.libc_ver(),
           "distribution": {key: release.get(key) for key in ("ID", "VERSION_ID")}}
    require(abi["system"] == "Linux" and abi["machine"] == "x86_64", "LLVM CI state requires Linux x86_64")
    return "llvm-bootstrap-v1-" + hashlib.sha256(canonical({"inputs": inputs, "abi": abi})).hexdigest()


def cache_key(root: Path) -> str:
    return key_for(fingerprint(root))


def validate_receipt(root: Path) -> dict:
    """Bind restored compiler bytes to the current recipe and source inputs."""
    root = Path(root)
    recipe_path = root / "buildtools.json"
    recipe = read_json(recipe_path.read_bytes())["tools"]["clang"]
    receipt = read_json((root / "build/receipts/clang/build.json").read_bytes())
    require(receipt.get("schema") == 1 and receipt.get("tool") == "clang"
            and receipt.get("operation") == "build" and receipt.get("passed") is True,
            "missing successful Clang build receipt")
    require(receipt.get("recipeSha256") == digest(recipe_path), "Clang build recipe changed")
    require(receipt.get("source") == recipe["source"], "Clang source revision changed")
    expected = {path: digest(root / safe_path(path)) for path in recipe["sourceInputs"]}
    require(receipt.get("sourceInputs") == expected, "Clang source inputs changed")
    artifacts = {path: digest(root / safe_path(path)) for path in recipe["artifacts"]}
    require(receipt.get("artifacts") == artifacts, "Clang artifact checksum mismatch")
    compiler = root / "clang/.toolchains/build-p3/bin/clang-wasm.component"
    with compiler.open("rb") as stream:
        require(stream.read(8) == b"\0asm\x0d\0\x01\0", "Clang artifact is not a Component")
    validation = receipt.get("validation", {})
    require(validation.get("componentInterface") == "wasi:cli/run@0.3.0"
            and validation.get("smokeResult") == 42, "Clang receipt lacks successful ABI/source smoke")
    require(validation.get("sdkInventorySha256") == digest(root / SDK / "SHA256SUMS"),
            "Clang receipt selects a different LLVM SDK inventory")
    return receipt


def load_helper(path: Path, name: str):
    # Inspection must never add bytecode files to authenticated directories.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        specification = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


def validate_outputs(root: Path) -> dict:
    """Check SDK, split-module and native-tool contracts without rebuilding tools."""
    root = Path(root)
    recipe = read_json((root / "buildtools.json").read_bytes())["tools"]["clang"]
    sdk = load_helper(root / "llvm/scripts/sdk.py", "ci_llvm_sdk").verify(root / SDK)
    metadata = read_json((root / SDK / "share/llvm/sdk.json").read_bytes())
    require(metadata.get("host_target") == "wasm32-wasip3"
            and metadata.get("llvm_revision") == recipe["source"]["revision"], "LLVM SDK ABI or revision mismatch")
    shared = load_helper(root / "llvm/shared.py", "ci_llvm_shared").manifest(root / "llvm/dist/shared")
    require(shared.get("llvmRevision") == recipe["source"]["revision"]
            and shared.get("hostTarget") == "wasm32-wasip3"
            and shared.get("sdkChecksumManifest") == digest(root / SDK / "SHA256SUMS"),
            "shared LLVM ABI, source revision or SDK mismatch")
    shared_sources = ("clang/src/clang_wasm_main.cpp", "nim/src/c_backend.cpp", "llvm/scripts/shared.py", "llvm/shared.py")
    require(shared.get("sourceInputs") == {path: digest(root / path) for path in shared_sources},
            "shared LLVM source inputs changed")
    for name, relative in (("clang", "clang/.toolchains/build-p3/bin/clang-wasm.component"),
                           ("nim", "nim/work/backend-build/c-backend")):
        require(shared["consumers"][name]["sha256"] == digest(root / relative),
                name + " backend differs from shared LLVM provider")
    lock = read_json((root / "tools/ci-tools.lock.json").read_bytes())
    require(digest(root / WASM_TOOLS / "wasm-tools") == lock["downloads"]["wasm-tools"]["executableSha256"],
            "native wasm-tools checksum mismatch")
    for relative in (NATIVE_SDK + "/bin/clang", NATIVE_SDK + "/bin/clang++",
                     NATIVE_SDK + "/bin/wasm-ld", NATIVE_SDK + "/bin/llvm-nm", WASM_TOOLS + "/wasm-tools"):
        require(os.access(root / relative, os.X_OK), "native bootstrap executable missing: " + relative)
    host = root / HOST
    receipt = read_json((host / "receipt.json").read_bytes())
    host_lock = read_json((root / "tools/shared-hosts/wasmtime.lock.json").read_bytes())
    require(receipt.get("owner") == "wisp" and receipt.get("source") == host_lock
            and receipt.get("patchSha256") == digest(root / "tools/shared-hosts/component-preload.patch")
            and receipt.get("binarySha256") == digest(host / "wasmtime")
            and os.access(host / "wasmtime", os.X_OK), "Wisp host receipt or binary mismatch")
    require((host / "LICENSE").is_file(), "Wisp host license missing")
    return {"sdk": sdk["id"], "llvmRevision": recipe["source"]["revision"], "hostSha256": receipt["binarySha256"]}


def validate_links(files: dict) -> None:
    for relative, entry in files.items():
        # No member may write through a symlinked parent.
        parents = PurePosixPath(relative).parents
        require(not any(files.get(str(parent), {}).get("kind") == "symlink" for parent in parents),
                "state member traverses a symlink: " + relative)
        if entry["kind"] != "symlink":
            continue
        current, visited = relative, set()
        while files[current]["kind"] == "symlink":
            require(current not in visited, "cyclic state symlink: " + relative)
            visited.add(current)
            target = files[current]["target"]
            require(isinstance(target, str) and target and "\\" not in target
                    and not PurePosixPath(target).is_absolute()
                    and not any(ord(c) < 32 or ord(c) == 127 for c in target), "unsafe state symlink")
            current = posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
            require(owner(current) == owner(relative) and current in files,
                    "state symlink escapes its payload: " + relative)


def inventory(root: Path) -> dict:
    root = Path(root).resolve()
    files = {}
    for prefix in PAYLOAD_PATHS:
        source = root / prefix
        require(source.exists() and not source.is_symlink() and source.resolve().is_relative_to(root),
                "missing or unsafe state payload: " + prefix)
        paths = [source]
        if source.is_dir():
            for directory, directories, filenames in os.walk(source, followlinks=False):
                paths.extend(Path(directory) / name for name in [*directories, *filenames])
        for path in paths:
            relative = path.relative_to(root).as_posix()
            owner(relative)
            attributes = path.lstat()
            if stat.S_ISLNK(attributes.st_mode):
                entry = {"kind": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(attributes.st_mode):
                entry = {"kind": "directory", "mode": 0o755}
            else:
                require(stat.S_ISREG(attributes.st_mode), "unsupported state file: " + relative)
                entry = {"kind": "file", "mode": 0o755 if attributes.st_mode & 0o111 else 0o644,
                         "size": attributes.st_size, "sha256": digest(path)}
            files[relative] = entry
    require(len(files) <= MAX_FILES and sum(v.get("size", 0) for v in files.values()) <= MAX_EXPANDED,
            "LLVM state exceeds inventory bounds")
    validate_links(files)
    return dict(sorted(files.items()))


def validate_metadata(root: Path, metadata: dict) -> None:
    require(isinstance(metadata, dict) and set(metadata) == {"schema", "kind", "key", "inputs", "files"}
            and type(metadata["schema"]) is int and metadata["schema"] == 1
            and metadata["kind"] == KIND, "unsupported LLVM state metadata")
    inputs = fingerprint(root)
    require(metadata["inputs"] == inputs and metadata["key"] == key_for(inputs), "LLVM state fingerprint changed")
    files = metadata["files"]
    require(isinstance(files, dict) and 0 < len(files) <= MAX_FILES, "invalid LLVM state file inventory")
    total = 0
    for relative, entry in files.items():
        owner(relative)
        require(isinstance(entry, dict), "invalid LLVM state entry")
        kind = entry.get("kind")
        fields = {"file": {"kind", "mode", "size", "sha256"}, "directory": {"kind", "mode"},
                  "symlink": {"kind", "target"}}
        require(kind in fields and set(entry) == fields[kind], "invalid LLVM state entry fields")
        if kind != "symlink":
            require(type(entry["mode"]) is int and entry["mode"] in (0o644, 0o755), "invalid LLVM state mode")
        if kind == "file":
            require(type(entry["size"]) is int and 0 <= entry["size"] <= MAX_EXPANDED
                    and isinstance(entry["sha256"], str) and re.fullmatch("[0-9a-f]{64}", entry["sha256"]),
                    "invalid LLVM state file size or checksum")
            total += entry["size"]
    require(total <= MAX_EXPANDED and all(path in files for path in PAYLOAD_PATHS), "incomplete or oversized LLVM state")
    validate_links(files)


def write_metadata(root: Path, metadata: dict) -> None:
    destination = root / STATE_METADATA
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".llvm-state-", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical(metadata))
    temporary.replace(destination)


def verify_state(root: Path) -> dict:
    root = Path(root).resolve()
    path = root / STATE_METADATA
    require(path.is_file() and path.stat().st_size <= MAX_METADATA, "LLVM state inventory missing or oversized")
    metadata = read_json(path.read_bytes())
    validate_metadata(root, metadata)
    require(inventory(root) == metadata["files"], "LLVM state file inventory or checksum mismatch")
    validate_receipt(root)
    validate_outputs(root)
    return metadata


def export_state(root: Path, archive: Path) -> dict:
    root, archive = Path(root).resolve(), Path(archive).absolute()
    inputs = fingerprint(root)
    validate_receipt(root)
    validate_outputs(root)
    metadata = {"schema": 1, "kind": KIND, "key": key_for(inputs), "inputs": inputs, "files": inventory(root)}
    encoded = canonical(metadata)
    require(len(encoded) <= MAX_METADATA, "LLVM state metadata exceeds limit")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".llvm-export-", dir=archive.parent, delete=False) as temporary:
        path = Path(temporary.name)
    try:
        with path.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=1) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as bundle:
                import io
                entry = tarfile.TarInfo(ARCHIVE_METADATA)
                entry.size, entry.mode = len(encoded), 0o644
                bundle.addfile(entry, io.BytesIO(encoded))
                for relative, specification in metadata["files"].items():
                    entry = tarfile.TarInfo(relative)
                    entry.mode = specification.get("mode", 0o777)
                    if specification["kind"] == "directory":
                        entry.type = tarfile.DIRTYPE
                        bundle.addfile(entry)
                    elif specification["kind"] == "symlink":
                        entry.type, entry.linkname = tarfile.SYMTYPE, specification["target"]
                        bundle.addfile(entry)
                    else:
                        entry.size = specification["size"]
                        with (root / relative).open("rb") as stream:
                            bundle.addfile(entry, stream)
        require(path.stat().st_size <= MAX_ARCHIVE, "LLVM state archive exceeds limit")
        require(fingerprint(root) == inputs and inventory(root) == metadata["files"], "LLVM state changed during export")
        path.replace(archive)
        write_metadata(root, metadata)
    finally:
        path.unlink(missing_ok=True)
    return metadata


def restore_state(root: Path, archive: Path) -> dict:
    root, archive = Path(root).resolve(), Path(archive).resolve()
    require(archive.is_file() and archive.stat().st_size <= MAX_ARCHIVE, "LLVM state archive missing or oversized")
    (root / ".ci").mkdir(parents=True, exist_ok=True)
    require((root / ".ci").resolve().is_relative_to(root), "LLVM staging directory escapes checkout")
    with tempfile.TemporaryDirectory(prefix=".llvm-restore-", dir=root / ".ci") as directory:
        stage = Path(directory)
        members = {}
        with tarfile.open(archive, mode="r:gz") as bundle:
            first = bundle.next()
            require(first is not None and first.name == ARCHIVE_METADATA and first.isfile()
                    and 0 <= first.size <= MAX_METADATA, "LLVM archive must begin with bounded metadata")
            metadata = read_json(bundle.extractfile(first).read())
            validate_metadata(root, metadata)
            members[ARCHIVE_METADATA] = first
            for member in bundle:
                if member is first:
                    continue
                relative = safe_path(member.name.rstrip("/") if member.isdir() else member.name)
                require(relative not in members and len(members) <= MAX_FILES, "duplicate or excessive LLVM archive members")
                require(member.isfile() or member.isdir() or member.issym(), "unsupported LLVM archive member")
                owner(relative)
                require(relative in metadata["files"], "LLVM archive member is outside its inventory")
                expected = metadata["files"][relative]
                require(0 <= member.size <= MAX_EXPANDED
                        and member.size == expected.get("size", 0), "LLVM archive member size differs from inventory")
                members[relative] = member
            require(metadata is not None and set(members) == set(metadata["files"]) | {ARCHIVE_METADATA},
                    "LLVM archive file set differs from inventory")
            for relative, entry in metadata["files"].items():
                member = members[relative]
                require((entry["kind"] == "file" and member.isfile() and member.size == entry["size"])
                        or (entry["kind"] == "directory" and member.isdir())
                        or (entry["kind"] == "symlink" and member.issym() and member.linkname == entry["target"]),
                        "LLVM archive member differs from inventory: " + relative)
                require(member.mode == entry.get("mode", 0o777), "LLVM archive mode differs from inventory")
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    destination.mkdir(exist_ok=True)
                    destination.chmod(entry["mode"])
                elif member.issym():
                    destination.symlink_to(entry["target"])
                else:
                    with bundle.extractfile(member) as incoming, destination.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    destination.chmod(entry["mode"])
                    require(digest(destination) == entry["sha256"], "LLVM archive checksum mismatch: " + relative)
        # Validate against source files from this checkout before replacing any outputs.
        for relative in metadata["inputs"]:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        validate_receipt(stage)
        validate_outputs(stage)
        require(inventory(stage) == metadata["files"], "restored LLVM inventory mismatch")
        require(fingerprint(root) == metadata["inputs"], "LLVM source inputs changed during restore")
        installed, backups = [], []
        try:
            for relative in PAYLOAD_PATHS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                require(destination.parent.resolve().is_relative_to(root), "LLVM destination escapes checkout")
                if destination.exists() or destination.is_symlink():
                    backup = stage / "backup" / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    destination.replace(backup)
                    backups.append((destination, backup))
                (stage / relative).replace(destination)
                installed.append(destination)
            write_metadata(root, metadata)
        except BaseException:
            for destination in reversed(installed):
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            for destination, backup in reversed(backups):
                backup.replace(destination)
            raise
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("key", "export", "restore", "verify"):
        child = commands.add_parser(name)
        child.add_argument("--root", type=Path, default=ROOT)
        if name in {"export", "restore"}:
            child.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "key":
        print(cache_key(args.root))
        return
    if args.command == "export":
        metadata = export_state(args.root, args.archive)
    elif args.command == "restore":
        metadata = restore_state(args.root, args.archive)
    else:
        metadata = verify_state(args.root)
    print(json.dumps({"key": metadata["key"], "files": len(metadata["files"]), "verified": True}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        sys.exit("ci-llvm: " + str(error))
