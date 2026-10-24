#!/usr/bin/env python3
"""Bootstrap streaming shards of a compiler artifact, never application source.

Standalone calls take the common compiler-memory.lock. A parent that already
holds that flock MUST pass lock_held=True, or --lock-held on the CLI. flock is
not recursive across independently opened descriptors. The caller retains the
lock through subsequent heavy Wasmtime compilation/validation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "shard_tool"
LOCK = Path(os.environ.get("WISP_COMPILER_MEMORY_LOCK", "/home/luap/.t3/projects/wisp-pkgs/.cache/compiler-memory.lock"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def memory_lock(lock_held=False):
    if lock_held:
        yield
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def recipe_hash():
    digest = hashlib.sha256()
    for path in [Path(__file__), TOOL / "Cargo.toml", TOOL / "Cargo.lock", *sorted((TOOL / "src").rglob("*.rs"))]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def build(*, lock_held=False):
    """Return the offline, locked Rust bootstrap executable, building if needed."""
    with memory_lock(lock_held):
        target = Path(os.environ.get("WISP_SWIFT_SHARD_TARGET", TOOL / "target")).resolve()
        binary = target / "debug/wisp-swift-shard"
        stamp = target / "wisp-shard-build.json"
        recipe = recipe_hash()
        if stamp.is_file() and binary.is_file():
            record = json.loads(stamp.read_text())
            if record.get("recipeSha256") == recipe and record.get("binarySha256") == sha(binary):
                return binary
        subprocess.run(["cargo", "build", "--offline", "--locked", "--jobs", "1", "--manifest-path", str(TOOL / "Cargo.toml")],
                       env={**os.environ, "CARGO_TARGET_DIR": str(target), "CARGO_INCREMENTAL": "0"}, check=True)
        pending = stamp.with_suffix(f".pending-{os.getpid()}")
        pending.write_text(json.dumps({"recipeSha256": recipe, "binarySha256": sha(binary)}) + "\n")
        pending.replace(stamp)
        return binary


def verify(source, out, shards=8, name="swift-frontend", *, tool_sha256=None):
    """Read and hash a prepared cache. Never builds, transforms, locks, or JITs.

    Call this from application runtime code. Pass tool_sha256 to require a
    previously approved bootstrap binary hash. The source is checked against the
    manifest; the parent separately checks its pinned upstream inventory.
    """
    source, out = Path(source).resolve(), Path(out).resolve()
    manifest = json.loads((out / "manifest.json").read_text())

    def require(condition, detail):
        if not condition:
            raise ValueError(f"invalid compiler shard cache: {detail}")

    def number(value, minimum=0):
        return type(value) is int and minimum <= value <= 0xffffffff

    def digest(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    require(number(shards, 1) and shards <= 1024, "requested shard count")
    require(isinstance(manifest, dict), "manifest object")
    require(type(manifest.get("schemaVersion")) is int and manifest["schemaVersion"] == 1
            and manifest.get("kind") == "wisp-exact-image-function-shards", "schema")
    require(manifest.get("name") == name and manifest.get("source") == str(source), "source/name")
    require(manifest.get("sourceSha256") == sha(source)
            and type(manifest.get("sourceBytes")) is int
            and manifest["sourceBytes"] == source.stat().st_size, "source hash/bytes")
    require(digest(manifest.get("toolSha256"))
            and (tool_sha256 is None or manifest["toolSha256"] == tool_sha256), "tool hash")
    require(manifest.get("nativeBootstrapOnly") is True
            and manifest.get("nativeApplicationCompilerUsed") is False, "bootstrap flags")
    for key in ["definedFunctions", "importedFunctions", "originalImports", "importedGlobals",
                "definedGlobals", "originalElementSegments", "rewrittenCrossShardCalls"]:
        require(number(manifest.get(key), 1 if key == "definedFunctions" else 0), key)
    defined = manifest["definedFunctions"]
    require(shards <= defined, "shards exceed defined function count")
    parts, state, table = manifest.get("shards"), manifest.get("state"), manifest.get("dispatchTable")
    require(isinstance(parts, list) and len(parts) == shards, "shard count")
    require(isinstance(state, dict) and isinstance(table, dict), "state/table metadata")
    require(manifest.get("stateNamespace") == "wisp-swift-state", "state namespace")
    require(isinstance(manifest.get("globalExports"), list)
            and len(manifest["globalExports"]) == manifest["definedGlobals"], "global export count")
    globals_ = [f"__wisp_global_{n}" for n in range(manifest["definedGlobals"])]
    require(manifest.get("globalExports") == globals_, "global export names/count")
    require(table.get("module") == "wisp-swift-shards" and table.get("name") == "functions"
            and table.get("element") == "funcref" and table.get("initialValue") == "null"
            and table.get("table64") is False and table.get("shared") is False
            and number(table.get("minimum")) and table["minimum"] == defined
            and number(table.get("maximum")) and table["maximum"] == defined
            and number(table.get("shardTableIndex")), "dispatch table ABI")
    require(manifest["originalImports"] >= manifest["importedFunctions"] + manifest["importedGlobals"]
            + table["shardTableIndex"] + 1, "original import counts")
    filenames = ["state.wasm", *(f"shard-{n:03}.wasm" for n in range(shards))]
    require(manifest.get("loadOrder") == ["original-primary", *filenames], "load order")
    records = [state, *parts]
    files_seen, paths_seen, cursor, total = set(), set(), 0, 0
    for n, (record, filename) in enumerate(zip(records, filenames)):
        require(isinstance(record, dict), "artifact record")
        require(record.get("file") == filename and record.get("path") == str(out / filename),
                "canonical artifact file/path")
        require(record["file"] not in files_seen and record["path"] not in paths_seen, "duplicate artifact")
        files_seen.add(record["file"]); paths_seen.add(record["path"])
        require(type(record.get("bytes")) is int and record["bytes"] > 0
                and digest(record.get("sha256")), f"artifact size/hash: {filename}")
        exports = record.get("exports")
        require(isinstance(exports, list) and all(isinstance(value, str) for value in exports)
                and len(set(exports)) == len(exports) and "memory" in exports, f"exports: {filename}")
        if n == 0:
            require(set(globals_).issubset(exports), "state global exports")
            require(not any(key in record for key in ["definedFunctionStart", "definedFunctionEndExclusive",
                                                      "definedFunctions"]), "state must not own functions")
        else:
            start, end, count = (record.get(key) for key in ["definedFunctionStart", "definedFunctionEndExclusive",
                                                           "definedFunctions"])
            require(number(start) and number(end) and number(count, 1)
                    and start == cursor and end > start and end <= defined and count == end - start,
                    f"incomplete/overlapping function range: {filename}")
            require(not set(globals_).intersection(exports), "shard exports coordinator globals")
            cursor = end
        total += record["bytes"]
    require(cursor == defined, "function ranges do not cover definedFunctions")
    require(type(manifest.get("totalModuleBytes")) is int and manifest["totalModuleBytes"] == total,
            "total module bytes")
    for record in records:
        path = Path(record["path"])
        require(not path.is_symlink() and path.is_file() and path.stat().st_size == record["bytes"]
                and sha(path) == record["sha256"], f"artifact changed: {record['file']}")
    return manifest


def ensure(source, out, shards=8, name="swift-frontend", *, lock_held=False):
    """Build or verify an immutable derived cache; return its parsed manifest.

    Existing caches with mismatched source/tool/files raise ValueError. Choose a
    new directory to derive another version. No installed distribution is edited.
    The caller must still validate pinned upstream checksums with its integrity
    routine; this generic transformer records, rather than chooses, the source.
    """
    source, out = Path(source).resolve(), Path(out).resolve()
    with memory_lock(lock_held):
        binary = build(lock_held=True)
        if not out.exists():
            subprocess.run([str(binary), "--input", str(source), "--out", str(out),
                            "--shards", str(shards), "--name", name], stdout=subprocess.DEVNULL, check=True)
        return verify(source, out, shards, name, tool_sha256=sha(binary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--name", default="swift-frontend")
    parser.add_argument("--lock-held", action="store_true")
    args = parser.parse_args()
    print(json.dumps(ensure(args.input, args.out, args.shards, args.name, lock_held=args.lock_held)))
