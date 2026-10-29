"""Runtime archive format shared by release packaging and acquisition."""

import argparse
import os
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hashlib
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


def fail(message):
    raise ValueError(message)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda value: fail("invalid JSON constant"))


def https_url(value):
    if not isinstance(value, str):
        fail("expected an HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        fail("expected HTTPS without credentials or fragment")
    return value


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        https_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


HTTP = build_opener(HTTPSRedirect())

MAX_RUNTIME = 2 * 1024 * 1024 * 1024
MAX_EXPANDED = 24 * 1024 * 1024 * 1024


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or set(catalog) != {"schema", "platform", "runtime", "cli", "env"}:
        fail("invalid CI catalog fields")
    if type(catalog["schema"]) is not int or catalog["schema"] != 1 or catalog["platform"] != "linux-x86_64":
        fail("unsupported CI runtime platform")
    runtime = catalog["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"url", "sha256", "size"}:
        fail("invalid CI runtime descriptor")
    https_url(runtime["url"])
    if not re.fullmatch(r"[0-9a-f]{64}", runtime["sha256"]):
        fail("invalid CI runtime digest")
    if type(runtime["size"]) is not int or not 0 < runtime["size"] <= MAX_RUNTIME:
        fail("invalid CI runtime size")
    if catalog["cli"] != {"path": "bin/wisp"} or not isinstance(catalog["env"], dict):
        fail("runtime CLI must be bin/wisp")
    for name, value in catalog["env"].items():
        if not re.fullmatch(r"WISP_[A-Z0-9_]+|CARGO_HOME|CARGO_TARGET_DIR|GOCACHE|GOMODCACHE", name):
            fail("unsupported runtime environment variable")
        relative(value)
    return catalog


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        fail("invalid runtime path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..", "") for part in value.split("/")):
        fail("runtime paths must be canonical and relative")
    return path


def extract_runtime(archive, destination):
    # Node's npm/JCO launchers use relative file symlinks. Admit those links,
    # but create them only after all regular-file writes have finished.
    total = 0
    seen = set()
    regular = set()
    links = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            path = relative(member.name.rstrip("/"))
            if str(path) in seen or not (member.isfile() or member.isdir() or member.issym()):
                fail("duplicate or unsupported runtime entry")
            seen.add(str(path))
            total += member.size
            if total > MAX_EXPANDED or len(seen) > 200000:
                fail("expanded runtime exceeds limits")
            if member.issym():
                link = member.linkname
                if not link or link.startswith("/") or "\\" in link or any(ord(c) < 32 for c in link):
                    fail("invalid runtime file symlink")
                target_path = posixpath.normpath(posixpath.join(str(path.parent), link))
                relative(target_path)
                links[str(path)] = (link, target_path)
                continue
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            regular.add(str(path))
    for path, (_, target_path) in links.items():
        visited = {path}
        while target_path in links:
            if target_path in visited:
                fail("runtime file symlinks form a cycle")
            visited.add(target_path)
            target_path = links[target_path][1]
        if target_path not in regular:
            fail("runtime symlinks must resolve to archived regular files")
        if any(str(parent) in links for parent in PurePosixPath(path).parents):
            fail("runtime symlink parents cannot be links")
    for path, (link, _) in links.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)



def download(url, expected, destination, *, size=None, limit=MAX_RUNTIME):
    """Verify a release asset, including HTTPS-only GitHub asset redirects."""
    https_url(url)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        fail("expected a lowercase SHA-256")
    digest = hashlib.sha256()
    total = 0
    with HTTP.open(Request(url), timeout=120) as response, Path(destination).open("xb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit or (size is not None and total > size):
                fail("download exceeds declared size")
            digest.update(chunk)
            output.write(chunk)
    if not total or (size is not None and total != size) or digest.hexdigest() != expected:
        fail("download size or SHA-256 mismatch")
    return Path(destination)
