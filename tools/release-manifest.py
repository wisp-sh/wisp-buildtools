#!/usr/bin/env python3
"""Write the acquisition manifest for a published toolchain release.

Consumers pin this manifest by SHA-256 and install each bundle with
`wisp toolchain install BUNDLE --sha256 SHA256`. Every bundle in the
release is listed with its exact size and digest; nothing is fetched or
executed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
MAX_BUNDLE = 2 * 1024**3


def fail(message: str):
    raise SystemExit(message)


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def bundle(directory: Path, archive: Path, repository: str, tag: str) -> dict:
    tool = archive.name.removesuffix(".wisp.zip")
    inventory = json.loads((directory / f"{tool}.wisp.json").read_text())
    if inventory.get("kind") != "toolchain" or inventory.get("tool") != tool or inventory.get("identity") != f"wisp:{tool}":
        fail(f"{archive.name} inventory does not describe toolchain {tool}")
    if not isinstance(inventory.get("version"), str) or not VERSION.fullmatch(inventory["version"]):
        fail(f"{archive.name} needs a stable package version")
    digest = sha256(archive)
    recorded = (directory / f"{tool}.wisp.zip.sha256").read_text().split()
    if recorded[:2] != [digest, archive.name]:
        fail(f"{archive.name} differs from its recorded SHA-256")
    size = archive.stat().st_size
    if not 0 < size <= MAX_BUNDLE:
        fail(f"{archive.name} has an invalid size")
    return {
        "tool": tool, "version": inventory["version"], "upstreamVersion": inventory.get("upstreamVersion"),
        "platform": inventory["platform"],
        "requires": sorted(str(r["name"]) for r in inventory.get("requirements", []) if r.get("kind") == "shared-tool"),
        "url": f"https://github.com/{repository}/releases/download/{tag}/{archive.name}",
        "size": size, "sha256": digest,
    }


def manifest(directory: Path, repository: str, tag: str, commit: str) -> dict:
    if not REPOSITORY.fullmatch(repository) or not TAG.fullmatch(tag) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail("invalid repository, tag or commit")
    archives = sorted(directory.glob("*.wisp.zip"))
    if not archives:
        fail("no toolchain bundles to publish")
    inventories = {archive.name.removesuffix(".wisp.zip"): json.loads((directory / archive.name.replace(".wisp.zip", ".wisp.json")).read_text()) for archive in archives}
    bundles = [bundle(directory, archive, repository, tag) for archive in archives]
    published = {(entry["tool"], entry["version"]) for entry in bundles}
    for entry in bundles:
        # Shared-tool requirements name another bundle that installs into the same prefix.
        for requirement in inventories[entry["tool"]].get("requirements", []):
            if requirement.get("kind") != "shared-tool":
                continue
            needed = (str(requirement.get("name", "")).removeprefix("wisp:"), requirement.get("version"))
            if needed not in published:
                fail(f"{entry['tool']} requires wisp:{needed[0]}@{needed[1]}, which is not in this release")
    return {"schema": 2, "platform": "linux-x86_64", "repository": repository, "tag": tag, "commit": commit, "bundles": bundles}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory holding TOOL.wisp.zip, TOOL.wisp.json and TOOL.wisp.zip.sha256 files")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = manifest(args.directory, args.repository, args.tag, args.commit)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(f"{args.output.name} {sha256(args.output)} ({len(value['bundles'])} bundles)", file=sys.stderr)


if __name__ == "__main__":
    main()
