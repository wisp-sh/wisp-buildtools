"""Package a prepared release runtime and its checksum-pinned manifest."""

import argparse
import gzip
import hashlib
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_format import fail, json_bytes, strict_json, MAX_EXPANDED, MAX_RUNTIME, validate_catalog


def package(prepared, destination, environment, url):
    prepared = prepared.resolve()
    for name in ("wisp", "wisp-worker"):
        if not (prepared / "bin" / name).is_file() or not os.access(prepared / "bin" / name, os.X_OK):
            fail(f"prepared runtime is missing executable bin/{name}")
    if destination.resolve().is_relative_to(prepared):
        fail("write the runtime archive outside its prepared directory")
    paths = []
    expanded = 0
    for path in sorted(prepared.rglob("*")):
        if path.is_symlink():
            if not path.resolve().is_relative_to(prepared) or path.is_dir():
                fail("runtime symlinks must refer to files inside the prepared directory; copy linked directories first")
        if path.is_dir():
            paths.append(path)
            continue
        if not path.is_file():
            fail("runtime contains a non-regular file")
        expanded += path.stat().st_size
        if expanded > MAX_EXPANDED or len(paths) >= 200000:
            fail("runtime exceeds expanded limits")
        paths.append(path)
    if any(not (prepared / relative).exists() for relative in environment.values()):
        fail("runtime environment points to a missing file or directory")
    # Validation before archive creation catches unsupported env keys and paths.
    validate_catalog({"schema": 1, "platform": "linux-x86_64", "runtime": {"url": url, "size": 1, "sha256": "0" * 64}, "cli": {"path": "bin/wisp"}, "env": environment})
    with destination.open("xb") as output, gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=6) as compressed, tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            entry = tarfile.TarInfo(path.relative_to(prepared).as_posix())
            if path.is_dir():
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                archive.addfile(entry)
            elif path.is_symlink():
                entry.type = tarfile.SYMTYPE
                entry.linkname = os.readlink(path)
                archive.addfile(entry)
            else:
                entry.size = path.stat().st_size
                entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                with path.open("rb") as source:
                    archive.addfile(entry, source)
    if destination.stat().st_size > MAX_RUNTIME:
        destination.unlink()
        fail("compressed runtime exceeds the release asset limit")
    digest = hashlib.sha256()
    with destination.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return validate_catalog({"schema": 1, "platform": "linux-x86_64", "runtime": {"url": url, "size": destination.stat().st_size, "sha256": digest.hexdigest()}, "cli": {"path": "bin/wisp"}, "env": environment})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--env", required=True, type=Path, help="JSON object mapping runtime env names to relative paths")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--url", required=True, help="Immutable public HTTPS URL where this exact archive will be published")
    args = parser.parse_args()
    environment = strict_json(args.env.read_bytes())
    if not isinstance(environment, dict):
        fail("runtime environment must be a JSON object")
    catalog = package(args.prepared, args.out, environment, args.url)
    args.catalog.write_bytes(json_bytes(catalog))
    print(f"Packaged runtime {catalog['runtime']['sha256']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
