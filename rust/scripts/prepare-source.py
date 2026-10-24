#!/usr/bin/env python3
"""Fetch and assemble pinned compiler sources without running upstream code.

The macro diff is saved for review, never applied. This prepares the baseline
Rust/LLVM port; it does not claim that the resulting compiler supports macros.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "source-lock.json"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str, *, allow_dot: bool = False) -> Path:
    path = PurePosixPath(value)
    if value == "." and allow_dot:
        return Path(".")
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in value.split("/")
    ):
        raise ValueError(f"unsafe relative path: {value!r}")
    return Path(*path.parts)


def read_lock() -> tuple[dict, bytes, bytes]:
    lock_bytes = LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    if lock.get("schema_version") != 1:
        raise ValueError("unsupported source-lock.json schema")
    for source in lock["sources"]:
        if not re.fullmatch(r"[0-9a-f]{40}", source["revision"]):
            raise ValueError(f"unresolved source revision: {source['name']}")
        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError(f"invalid source checksum: {source['name']}")
        relative_path(source["destination"], allow_dot=True)
        if not source["url"].startswith("https://"):
            raise ValueError("source downloads require HTTPS")
    if lock["sources"][0]["destination"] != ".":
        raise ValueError("first source must be the compiler source root")
    if lock["macro_reference"]["apply"] is not False:
        raise ValueError("the upstream macro reference must remain unapplied")
    patch = ROOT / relative_path(lock["macro_reference"]["path"])
    if patch.is_symlink() or not patch.is_file():
        raise ValueError(f"missing checked-in macro reference: {patch}")
    patch_bytes = patch.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != lock["macro_reference"]["sha256"]:
        raise ValueError("checked-in macro reference checksum mismatch")
    return lock, lock_bytes, patch_bytes


def cached_archive(source: dict, cache: Path, offline: bool) -> Path:
    target = cache / f"{source['sha256']}.tar.gz"
    if target.is_symlink():
        raise ValueError(f"cache entry must not be a symbolic link: {target}")
    if target.exists():
        if not target.is_file() or target.stat().st_size != source["archive_bytes"]:
            raise ValueError(f"cached archive size mismatch: {target}")
        if digest(target) != source["sha256"]:
            raise ValueError(f"cached archive checksum mismatch: {target}")
        return target
    if offline:
        raise ValueError(f"archive missing from offline cache: {target}")
    print(f"Fetching {source['name']} at {source['revision']}", file=sys.stderr)
    request = urllib.request.Request(
        source["url"], headers={"User-Agent": "wisp-rust-source-preparation/1"}
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".download-", delete=False) as out:
            temporary = Path(out.name)
            checksum = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request, timeout=60) as incoming:
                while block := incoming.read(1024 * 1024):
                    size += len(block)
                    if size > source["archive_bytes"]:
                        raise ValueError(f"download exceeds locked size: {source['name']}")
                    checksum.update(block)
                    out.write(block)
        if size != source["archive_bytes"] or checksum.hexdigest() != source["sha256"]:
            raise ValueError(f"download checksum/size mismatch: {source['name']}")
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def extract_source(archive_path: Path, source: dict, destination: Path) -> None:
    """Remove one locked archive prefix and retain only bounded data entries."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"submodule destination is not empty: {destination}")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = []
        names: set[str] = set()
        link_names: set[str] = set()
        total = 0
        for member in archive:
            parts = member.name.rstrip("/").split("/")
            if parts[0] != source["archive_root"] or any(
                part in ("", ".", "..") for part in parts
            ):
                raise ValueError(f"invalid archive path: {member.name!r}")
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("archive root must be a directory")
                continue
            name = "/".join(parts[1:])
            if name in names:
                raise ValueError(f"duplicate archive path: {name}")
            names.add(name)
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError(f"unsupported archive entry: {name}")
            if member.issym():
                link_names.add(name)
            total += member.size
            if total > source["unpacked_bytes"]:
                raise ValueError(f"archive exceeds locked unpacked size: {source['name']}")
            members.append(member.replace(name=name))
        if total != source["unpacked_bytes"]:
            raise ValueError(f"archive unpacked size mismatch: {source['name']}")
        for member in members:
            if any(str(parent) in link_names for parent in PurePosixPath(member.name).parents):
                raise ValueError(f"archive entry traverses a symbolic link: {member.name}")
        # Python's data filter rejects external links and device nodes and strips
        # privileged ownership/mode metadata. Sources may contain internal links.
        archive.extractall(destination, members=members, filter="data")


def verify_stage0(source_root: Path, lock: dict) -> None:
    selected = lock.get("source_bootstrap", lock["bootstrap"])
    stage0 = (source_root / selected["source_file"]).read_text()
    if f"compiler_date={selected['date']}\n" not in stage0:
        raise ValueError("source bootstrap date does not match source lock")
    for artifact in selected["artifacts"]:
        key = "dist/" + artifact["url"].split("/dist/", 1)[1]
        if f"{key}={artifact['sha256']}\n" not in stage0:
            raise ValueError(f"bootstrap checksum not present in pinned source: {key}")
    if (source_root / "src/version").read_text().strip() != lock["rust_version"].removesuffix("-dev"):
        raise ValueError("unexpected compiler source version")


def write_recipe(source_root: Path, wasi_sdk: Path, jobs: int, lock: dict) -> None:
    patch = ROOT / lock["macro_backport"]["path"]
    command = (f"python3 -I {shlex.quote(str(ROOT / 'scripts/build-compiler.py'))}"
               f" --source {shlex.quote(str(source_root))} --wasi-sdk {shlex.quote(str(wasi_sdk))}"
               f" --jobs {jobs} --macro-patch {shlex.quote(str(patch))}")
    (source_root / "BUILD-RECIPE.md").write_text(f'''# Rust {lock['rust_version']} source build

The source archives and native bootstrap pins have been checked. Source
preparation does not apply patches or execute compiler code.

The compiler runs as a WASIp3 component. It uses the existing LLVM 24/WASIp3
SDK from `llvm/artifacts/llvm-24-d297af3-wasip3-o1` and WASI SDK 34.
The official matching Rust {lock['rust_version']} release cross-compiles rustc
at stage 0. A separate native LLVM/compiler bootstrap is unnecessary.

Apply the reviewed compiler patch, then validate or build:

```sh
patch --batch --fuzz=0 -d {shlex.quote(str(source_root))} -p1 < {shlex.quote(str(patch))}
{command} --dry-run
{command} --build
```

The reference diff in `references/` is retained for review and is not applied.
Only the final compiler and application acceptance tests establish a usable
release. See `rust/docs/source-build.md` in the buildtools repository.
''')


def main() -> None:
    if sys.version_info < (3, 12):
        raise ValueError("source preparation requires Python 3.12 or newer for safe tar extraction")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--cache", type=Path, default=ROOT / "cache/source-archives")
    parser.add_argument("--wasi-sdk", type=Path, default=ROOT / "downloads/wasi-sdk-34.0-x86_64-linux")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--offline", action="store_true", help="use verified cache entries only")
    parser.add_argument("--plan", action="store_true", help="print pinned inputs; do not fetch or extract")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        raise ValueError("--jobs must be between 1 and 64")
    lock, lock_bytes, patch_bytes = read_lock()
    if args.plan:
        print(json.dumps(lock, indent=2))
        return
    revision = lock["sources"][0]["revision"]
    out = (args.out or ROOT / f"build/source-rust-{revision[:12]}").absolute()
    if out.exists() or out.is_symlink():
        raise ValueError(f"output already exists: {out}")
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    archives = [(source, cached_archive(source, cache, args.offline)) for source in lock["sources"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    try:
        for source, archive in archives:
            print(f"Preparing {source['name']}", file=sys.stderr)
            extract_source(archive, source, stage / relative_path(source["destination"], allow_dot=True))
        verify_stage0(stage, lock)
        references = stage / "references"
        references.mkdir()
        (references / "wasm-proc-macros-wasmi.diff").write_bytes(patch_bytes)
        (stage / "wisp-source-lock.json").write_bytes(lock_bytes)
        write_recipe(stage, args.wasi_sdk.resolve(), args.jobs, lock)
        # Generated absolute paths should name the final directory, never its
        # temporary staging sibling.
        for filename in ("BUILD-RECIPE.md",):
            path = stage / filename
            path.write_text(path.read_text().replace(str(stage), str(out)))
        receipt = {
            "schemaVersion": 1,
            "sourceRoot": str(out),
            "sourceLockSha256": hashlib.sha256(lock_bytes).hexdigest(),
            "sourceRevisions": {source["name"]: source["revision"] for source in lock["sources"]},
            "archiveChecksumsVerified": True,
            "macroReferenceChecksumVerified": True,
            "macroPatchApplied": False,
            "upstreamCodeExecuted": False,
            "sourceBuildAttempted": False,
            "proceduralMacroSupport": "requires backport and compiler rebuild",
            "recipe": str(out / "BUILD-RECIPE.md"),
        }
        (stage / "wisp-source-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        os.replace(stage, out)
    except BaseException:
        shutil.rmtree(stage)
        raise
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, tarfile.TarError) as error:
        print(f"source preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
