"""Wisp toolchain bundles and measured compiler-package sizes.

Compiler packages use the schema-4 toolchain kind, separate from application
Components. Only file I/O, hashing and archive compression run in this host.
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import unicodedata
import zipfile
import zlib

MAX_COMPRESSED = 2 * 1024**3
MAX_EXPANDED = 4 * 1024**3
MAX_FILES = 50000
MAX_MANIFEST = 16 * 1024**2
FORMATS = {"wasm-component", "wasm-core", "wasm-archive", "native", "host-script", "data"}
ROLES = {"compiler", "stdlib", "headers", "runtime", "adapter", "licenses", "metadata", "sdk", "documentation"}
ROOT_FIELDS = {"schema", "kind", "identity", "version", "tool", "upstreamVersion", "platform", "archive",
               "entrypoints", "requirements", "sizes", "bootstrapRequirements"}

# These published upstream payloads contain public build locations or path
# examples. On GitHub Actions /home/runner also happens to be our builder's HOME.
# Bind that collision to the complete, reviewed upstream bytes, never a directory
# or a manifest supplied by the build. Repository and T3 paths remain forbidden.
# Upstream upgrades must review these locations and update the matching hashes.
PUBLIC_UPSTREAM_PAYLOADS = {
    # Unchanged Zig source tests contain the public example /home/absolute/path.rc.
    "zig/dist/lib/compiler/resinator/cli.zig": "009a7f9fa74436786c758bc232e94acda73d0a5cf160f03c36b4a0c76aea8f2a",
    # Unchanged upstream path tests include /home/andy and /home/foo examples.
    "zig/dist/lib/std/fs/path.zig": "fa05488f142bf45ee10cf6f5a75ba35c34fa99ff6fa8770f8b9fbe0db28ed91e",
    "wasm-tools/dist/wasm-tools.wasm": "3e7ec6561150ce3b885626033dff6162eabb5fc8226023f1f1afc0689de0db49",
    "assemblyscript/dist/binaryen.wasm": "265109799c6a9c7f27b961b213d6aef300550be8e8210c29a5709b4fd9b11605",
    "grain/dist/grainc.js": "0a85cf0120aafd873e41eb901a18090453010f8f819074b77c14e1c359cafd1e",
    "grain/dist/stdlib/path.gr": "f658c337dde0f242d09d9047f15a4e7f100922ac6dba229021bb6d26100b924d",
    "javascript/.toolchains/runtimes/runtime-sync.wasm": "25703f010b80efb806f9c35daf9ee6524ad60320c9781b0bae4e234264f8dac5",
    "javascript/.toolchains/runtimes/runtime-opt-size-sync.wasm": "51bced91769e0a3cb8bc288d230675b03fca8f97b42723b9f9f1702e6903b6f7",
    # Unchanged Temurin 23+37 image; XMLEntityManager contains /space/home/stax test paths.
    "jvm/dist/jdk/lib/modules": "8a476f72d26967ecdd3b02bede4a13306afd56cec2691a184e2c9f9eb4cd3452",
    "swift/dist/sysroot/swift/lib/swift_static/clang/lib/wasip1/libclang_rt.builtins-wasm32.a": "31ba1b372f2b7244c09424d0f33c51d1493675ace11d15dbc60152c9fb7c82b7",
    "swift/dist/sysroot/swift/lib/swift_static/clang/lib/wasip1/libclang_rt.profile-wasm32.a": "bd9b5e65bc147cac106df2ef963bcf1924d12f7aa8430c610ad0e5a64943bbc6",
    "swift/dist/sysroot/swift/lib/swift_static/wasi/_Differentiation.swiftmodule/wasm32-unknown-wasip1.swiftmodule": "1e0aa705fa1b3fa8afd1373cdbefab810a5ba9ee778a85bb022f13614be2e30c",
    "swift/dist/sysroot/swift/lib/swift_static/wasi/libswiftCore.a": "33bbd4b3c761429a410b22c77ad9de74855df580610610e1b400b48d24442043",
    "swift/dist/sysroot/swift/lib/swift_static/wasi/libswift_Differentiation.a": "3056d7f387105caea4c5491e6a8c6c066c23871b8419d90e47af064f1743258c",
    "swift/dist/sysroot/swift/lib/swift_static/wasi/libswift_StringProcessing.a": "658aa48a2bce11bc729751c6fd66cca48cc40b15302c63accb4079c261d64a0c",
    "swift/dist/sysroot/wasi-sysroot/lib/wasm32-wasip1/libc++abi.a": "070ae97463a7a8ca9df6660bd6a31ee592fa09a2194d3e6a14c468c4b09ba331",
    "swift/dist/sysroot/wasi-sysroot/lib/wasm32-wasip1/libxml2.a": "29be63c1526c33a3bc511e084ac0a9991782234c2b48a331716d44abc140be08",
}
# These upstream strings survive composition or splitting of pinned inputs.
# Match complete NUL-terminated values, not arbitrary paths below those roots.
PUBLIC_UPSTREAM_STRINGS = {
    "python/dist/template.component.wasm": (
        b"/home/runner/work/componentize-py/componentize-py/cpython/builddir/wasi/install\0",
    ),
    "swift/dist/bin/swift-frontend-primary.wasm": (
        b"/home/runner/work/swift-toolchain-wasm/swift-toolchain-wasm/src/swift/lib/Demangling/Demangler.cpp\0",
    ),
}


def public_upstream_strings(relative):
    values = PUBLIC_UPSTREAM_STRINGS.get(relative, ())
    if (relative == 'python/dist/inference.component.wasm'
            or re.fullmatch(r'python/dist/contracts/[a-f0-9]{64}/template.component.wasm', relative)):
        values += PUBLIC_UPSTREAM_STRINGS['python/dist/template.component.wasm']
    # Pinned Rust stdlib metadata includes this complete env::join_paths
    # documentation example. The trailing byte is rustc's string delimiter.
    if re.fullmatch(r'rust/dist/compiler/build/bootstrap/sysroot/lib/rustlib/'
                    r'wasm32-(?:unknown-unknown|wasip1|wasip3)/lib/libstd-[0-9a-f]+\.(?:rmeta|rlib)', relative):
        values += (b'/home/xyz/bin"));\xc1',)
    return values


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


def strict_json(data: bytes):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out
    def invalid(value):
        raise ValueError(f"invalid JSON constant: {value}")
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"unexpected {label} fields")


def safe_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 240 or value.startswith("/")
            or "\\" in value or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError(f"unsafe package path: {value!r}")
    return value


def uint(value, label, maximum=MAX_EXPANDED):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("invalid SHA-256")


def text(value, maximum, label):
    if (not isinstance(value, str) or len(value.encode()) > maximum
            or any(unicodedata.category(c) == "Cc" for c in value)):
        raise ValueError(f"invalid {label}")
    return value


def load_profiles(root: Path) -> dict:
    path = root / "package-profiles.json"
    return strict_json(path.read_bytes()) if path.is_file() else {"schema": 1, "packageVersion": "0.1.0", "tools": {}}


def profile(root: Path, name: str) -> dict:
    return load_profiles(root).get("tools", {}).get(name, {})


def file_format(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(8)
    return header_format(header, path)


def header_format(header: bytes, path: Path) -> str:
    if header == b"\0asm\x0d\0\x01\0":
        return "wasm-component"
    if header == b"\0asm\x01\0\0\0":
        return "wasm-core"
    if header == b"!<arch>\n":
        return "wasm-archive"
    if header.startswith(b"\x7fELF") or header[:2] == b"MZ":
        return "native"
    if path.suffix in {".py", ".sh"} or header.startswith(b"#!"):
        return "host-script"
    return "data"


def file_role(relative: str, kind: str, rules: list) -> str:
    for rule in rules:
        if fnmatch.fnmatchcase(relative, rule["pattern"]):
            return rule["role"]
    name = Path(relative).name.lower()
    if "license" in name or "copyright" in name or "notice" in name:
        return "licenses"
    if "/include/" in relative or Path(relative).suffix in {".h", ".hpp"}:
        return "headers"
    if "/lib/" in relative or "/sysroot" in relative:
        return "stdlib"
    if kind == "native":
        return "runtime"
    if kind.startswith("wasm-"):
        return "compiler"
    if kind == "host-script":
        return "adapter"
    if name.endswith(".md"):
        return "documentation"
    return "metadata"


def inventory_sizes(inventory: dict) -> dict:
    result = dict.fromkeys(["installedBytes", "wasmBytes", "nativeBytes", "supportBytes"], 0)
    for item in inventory.values():
        size = item["size"]
        result["installedBytes"] += size
        key = "wasmBytes" if item["format"].startswith("wasm-") else "nativeBytes" if item["format"] == "native" else "supportBytes"
        result[key] += size
    return result


def check_manifest(doc: dict) -> dict:
    exact(doc, ROOT_FIELDS, "toolchain release")
    if type(doc["schema"]) is not int or doc["schema"] != 4 or doc["kind"] != "toolchain":
        raise ValueError("unsupported toolchain release")
    if not re.fullmatch("[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*", text(doc["tool"], 64, "tool name")):
        raise ValueError("invalid tool name")
    if doc["identity"] != "wisp:" + doc["tool"]:
        raise ValueError("toolchain identity differs from tool")
    match = re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", text(doc["version"], 128, "package version"))
    if not match or (match[1] and any(x.isdigit() and len(x) > 1 and x[0] == "0" for x in match[1].split("."))):
        raise ValueError("invalid package version")
    if doc["platform"] not in {"wasm32-wasi", "linux-x86_64"} or not text(doc["upstreamVersion"], 256, "upstream version"):
        raise ValueError("invalid toolchain platform or upstream version")
    archive = doc["archive"]
    exact(archive, {"file", "size", "sha256", "inventory"}, "toolchain archive")
    if archive["file"] != "toolchain.tar.gz":
        raise ValueError("unexpected toolchain archive filename")
    uint(archive["size"], "archive size", MAX_COMPRESSED)
    sha(archive["sha256"])
    inventory = archive["inventory"]
    if not isinstance(inventory, dict) or not 0 < len(inventory) <= MAX_FILES:
        raise ValueError("toolchain inventory size limit")
    for path, item in inventory.items():
        safe_path(path)
        safe_path("wisp-buildtools/" + path)
        exact(item, {"size", "sha256", "mode", "format", "role"}, "file inventory")
        uint(item["size"], "file size", MAX_COMPRESSED)
        sha(item["sha256"])
        if type(item["mode"]) is not int or item["mode"] not in {0o644, 0o755}:
            raise ValueError("invalid executable mode")
        if (not isinstance(item["format"], str) or item["format"] not in FORMATS
                or not isinstance(item["role"], str) or item["role"] not in ROLES):
            raise ValueError("invalid file format or role")
        if any("/".join(path.split("/")[:index]) in inventory for index in range(1, len(path.split("/")))):
            raise ValueError("file and parent path collision")
    expected = inventory_sizes(inventory)
    exact(doc["sizes"], expected.keys(), "toolchain sizes")
    if any(type(doc["sizes"][key]) is not int or doc["sizes"][key] != value for key, value in expected.items()):
        raise ValueError("toolchain size accounting mismatch")
    if expected["installedBytes"] > MAX_EXPANDED:
        raise ValueError("installed size limit")
    if doc["platform"] == "wasm32-wasi" and expected["nativeBytes"]:
        raise ValueError("portable Wasm toolchain contains native binaries")
    if not isinstance(doc["entrypoints"], list) or len(doc["entrypoints"]) > 64:
        raise ValueError("entrypoint limit")
    seen = set()
    for entry in doc["entrypoints"]:
        exact(entry, {"name", "path", "format"}, "entrypoint")
        if not text(entry["name"], 128, "entrypoint name") or entry["name"] in seen:
            raise ValueError("invalid or duplicate entrypoint")
        seen.add(entry["name"])
        if (not isinstance(entry["path"], str) or entry["path"] not in inventory
                or entry["format"] != inventory[entry["path"]]["format"]):
            raise ValueError("entrypoint absent or format mismatch")
    if not isinstance(doc["requirements"], list) or len(doc["requirements"]) > 64:
        raise ValueError("requirements limit")
    names = set()
    for item in doc["requirements"]:
        exact(item, {"name", "kind", "version", "included", "note"}, "requirement")
        if (not isinstance(item["kind"], str) or item["kind"] not in {"runtime-host", "orchestrator", "shared-tool", "system-library"}
                or type(item["included"]) is not bool or any(not isinstance(item[key], str) for key in ("name", "version", "note"))):
            raise ValueError("invalid runtime requirement")
        if not text(item["name"], 128, "requirement name") or item["name"] in names:
            raise ValueError("duplicate or invalid requirement name")
        names.add(item["name"])
        text(item["version"], 256, "requirement version")
        text(item["note"], 4096, "requirement note")
    if not isinstance(doc["bootstrapRequirements"], list) or len(doc["bootstrapRequirements"]) > 64:
        raise ValueError("invalid bootstrap requirements")
    for value in doc["bootstrapRequirements"]:
        text(value, 4096, "bootstrap requirement")
    return doc


class HashReader:
    def __init__(self, stream):
        self.stream, self.hash = stream, hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        self.count += len(data)
        return data


def pack(document: dict, name: str, files: dict[str, Path], *, root: Path, output_dir: Path,
         native_sources=None) -> dict:
    native_sources = native_sources or {}
    profiles = load_profiles(root)
    item = document["tools"][name]
    config = profiles.get("tools", {}).get(name, {})
    inventory = {}
    home_prefix = str(Path.home()).encode() + b"/"
    # Also catch personal paths carried over from another builder or OS.
    home_paths = re.compile(re.escape(home_prefix) + rb"|/(?:home|Users)/[^/\x00\s]{1,255}/")
    root_prefix = str(root.resolve()).encode() + b"/"
    overlap = max(len(home_prefix), len(root_prefix), len(b".t3/projects/"), len(b"/Users/") + 255 + 1) - 1
    for relative, path in files.items():
        safe_path(relative)
        public_strings = (public_upstream_strings(relative)
                          + public_upstream_strings(native_sources.get(relative, '')))
        lookahead = max([overlap, *(len(value) - 1 for value in public_strings)])

        def private_home(data, limit):
            return any(match.start() < limit and not any(data.startswith(value, match.start()) for value in public_strings)
                       for match in home_paths.finditer(data))

        with path.open("rb") as source:
            attributes = os.fstat(source.fileno())
            header = source.read(8)
            checksum = hashlib.sha256()
            count = 0
            previous = b""
            contains_home = False
            chunk = header
            while chunk:
                scanned = previous + chunk
                if root_prefix in scanned or b".t3/projects/" in scanned:
                    raise ValueError(f"package input contains a build-machine path: {relative}; rebuild with path remapping")
                # Defer the tail until enough bytes arrive to distinguish an
                # approved whole string from a private path at a chunk boundary.
                contains_home |= private_home(scanned, len(scanned) - lookahead)
                previous = scanned[-lookahead:]
                checksum.update(chunk)
                count += len(chunk)
                chunk = source.read(1024 * 1024)
            contains_home |= private_home(previous, len(previous))
            if count != attributes.st_size:
                raise ValueError(f"package input changed during inventory: {relative}")
            derived_public = False
            if relative in native_sources:
                original = native_sources[relative]
                expected = PUBLIC_UPSTREAM_PAYLOADS.get(original)
                derived_public = bool(expected and original in files and digest(files[original]) == expected)
            if contains_home and PUBLIC_UPSTREAM_PAYLOADS.get(relative) != checksum.hexdigest() and not derived_public:
                raise ValueError(f"package input contains a build-machine path: {relative}; rebuild with path remapping")
        kind = header_format(header, path)
        original = native_sources.get(relative)
        role = (file_role(original, file_format(files[original]), config.get('roles', []))
                if original is not None else file_role(relative, kind, config.get('roles', [])))
        inventory[relative] = {"size": count, "sha256": checksum.hexdigest(),
                               "mode": 0o755 if attributes.st_mode & 0o111 else 0o644,
                               "format": kind, "role": role}
    # Stage outside the destination: failure leaves neither a partial bundle nor
    # a destination directory that could be mistaken for a successful package.
    with tempfile.TemporaryDirectory(prefix="wisp-toolchain-package-") as temporary:
        stage = Path(temporary)
        payload = stage / "toolchain.tar.gz"
        with payload.open("wb") as output:
            with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0, compresslevel=6) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
                    for relative, path in sorted(files.items()):
                        spec = inventory[relative]
                        entry = tarfile.TarInfo("wisp-buildtools/" + relative)
                        safe_path(entry.name)
                        entry.size, entry.mode = spec["size"], spec["mode"]
                        entry.uid = entry.gid = entry.mtime = 0
                        entry.uname = entry.gname = ""
                        with path.open("rb") as source:
                            reader = HashReader(source)
                            archive.addfile(entry, reader)
                            extra = source.read(1)
                            if extra or reader.count != spec["size"] or reader.hash.hexdigest() != spec["sha256"]:
                                raise ValueError(f"package input changed during packaging: {relative}")
        sizes = inventory_sizes(inventory)
        requirements = list(config.get("requirements", []))
        for dependency in item.get("runtimeDependencies", []):
            identity = "wisp:" + dependency
            if any(value["name"] == identity for value in requirements):
                raise ValueError(f"package dependency is generated from runtimeDependencies: {identity}")
            requirements.append({"name": identity, "kind": "shared-tool",
                                 "version": profiles.get("packageVersion", "0.1.0"), "included": False,
                                 "note": "Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle."})
        doc = {"schema": 4, "kind": "toolchain", "identity": "wisp:" + name,
               "version": profiles.get("packageVersion", "0.1.0"), "tool": name,
               "upstreamVersion": item["version"], "platform": ("linux-x86_64" if native_sources else profiles.get("platform", "linux-x86_64" if sizes["nativeBytes"] else "wasm32-wasi")),
               "archive": {"file": "toolchain.tar.gz", "size": payload.stat().st_size,
                           "sha256": digest(payload), "inventory": inventory},
               "entrypoints": config.get("entrypoints", []), "requirements": requirements,
               "sizes": sizes, "bootstrapRequirements": item.get("bootstrapRequirements", [])}
        check_manifest(doc)
        manifest = canonical(doc)
        if len(manifest) > MAX_MANIFEST:
            raise ValueError("toolchain manifest size limit")
        bundle = stage / f"{name}.wisp.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for filename in ["release.json", "toolchain.tar.gz"]:
                info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                if filename == "release.json":
                    archive.writestr(info, manifest)
                else:
                    with archive.open(info, "w") as output, payload.open("rb") as source:
                        shutil.copyfileobj(source, output, 1024 * 1024)
        if bundle.stat().st_size > MAX_COMPRESSED:
            raise ValueError("toolchain bundle size limit")
        checksum = digest(bundle)
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / bundle.name
        # Copy to the destination filesystem before atomic publication.
        with tempfile.NamedTemporaryFile(prefix="." + bundle.name, dir=output_dir, delete=False) as pending:
            pending_path = Path(pending.name)
            try:
                with bundle.open("rb") as source:
                    shutil.copyfileobj(source, pending, 1024 * 1024)
                pending.close()
                pending_path.chmod(0o644)
                pending_path.replace(destination)
            finally:
                pending_path.unlink(missing_ok=True)
        destination.with_suffix(".zip.sha256").write_text(f"{checksum}  {destination.name}\n")
        destination.with_suffix(".json").write_bytes(manifest)
    return {"tool": name, "archive": str(destination), "sha256": checksum,
            "bytes": destination.stat().st_size, "files": len(inventory), **sizes}


def open_bundle(path: Path):
    if path.stat().st_size > MAX_COMPRESSED:
        raise ValueError("toolchain bundle size limit")
    archive = zipfile.ZipFile(path)
    try:
        entries = archive.infolist()
        if [x.filename for x in entries] != ["release.json", "toolchain.tar.gz"] or archive.comment:
            raise ValueError("unexpected toolchain ZIP entries")
        position = 0
        with path.open("rb") as source:
            for entry in entries:
                if (entry.header_offset != position or entry.compress_type != zipfile.ZIP_STORED
                        or entry.flag_bits or entry.extra or entry.comment or entry.file_size != entry.compress_size):
                    raise ValueError("unsupported toolchain ZIP framing")
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in {0, stat.S_IFREG} or entry.external_attr & 0x10:
                    raise ValueError("ZIP symlink, directory or special file")
                source.seek(position)
                header = source.read(30)
                if len(header) != 30:
                    raise ValueError("truncated ZIP header")
                fields = struct.unpack("<4s5H3I2H", header)
                if fields[0] != b"PK\x03\x04" or fields[2] or fields[3] or fields[-1]:
                    raise ValueError("unsupported local ZIP header")
                if source.read(fields[-2]) != entry.filename.encode() or fields[6:9] != (entry.CRC, entry.compress_size, entry.file_size):
                    raise ValueError("ZIP local and central headers differ")
                position += 30 + fields[-2] + entry.file_size
            if position != archive.start_dir:
                raise ValueError("ZIP payload gap or overlap")
            source.seek(-22, io.SEEK_END)
            end = struct.unpack("<4s4H2IH", source.read(22))
            if end[0] != b"PK\x05\x06" or end[1:5] != (0, 0, 2, 2) or end[-1] or end[6] != position:
                raise ValueError("unsupported ZIP directory framing")
            if end[5] + position + 22 != path.stat().st_size:
                raise ValueError("trailing ZIP data")
        if entries[0].file_size > MAX_MANIFEST:
            raise ValueError("toolchain manifest size limit")
        doc = check_manifest(strict_json(archive.read("release.json")))
        if entries[1].file_size != doc["archive"]["size"]:
            raise ValueError("archive size mismatch")
        return archive, doc
    except BaseException:
        archive.close()
        raise


def read_manifest(bundle: Path) -> dict:
    archive, doc = open_bundle(Path(bundle))
    archive.close()
    return doc


class SingleGzip(io.RawIOBase):
    """Bound a single gzip member, including its trailer and CRC."""
    def __init__(self, source):
        self.source, self.decoder = source, zlib.decompressobj(31)
        self.pending = b""
        self.finished, self.expanded = False, 0

    def readable(self):
        return True

    def readinto(self, output):
        while not self.pending and not self.finished:
            chunk = self.source.read(64 * 1024)
            if not chunk:
                if not self.decoder.eof:
                    raise ValueError("truncated gzip payload")
                self.finished = True
                break
            self.pending = self.decoder.decompress(chunk)
            self.expanded += len(self.pending)
            if self.expanded > MAX_EXPANDED:
                raise ValueError("expanded payload size limit")
            if self.decoder.eof:
                if self.decoder.unused_data or self.source.read(1):
                    raise ValueError("trailing or concatenated gzip data")
                self.finished = True
        count = min(len(output), len(self.pending))
        output[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


def consume_payload(archive, doc, prefix=None, *, shared_files=()):
    with archive.open("toolchain.tar.gz") as source:
        compressed = HashReader(source)
        with io.BufferedReader(SingleGzip(compressed), buffer_size=64 * 1024) as stream:
            # Read USTAR directly so PAX/GNU extensions and nonzero trailing tar
            # bytes cannot hide outside the inventory or tarfile's iterator.
            seen, header = set(), stream.read(512)
            while header != b"\0" * 512:
                if len(header) != 512:
                    raise ValueError("truncated tar header")
                entry = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
                if not entry.isfile() or entry.pax_headers or header[156:157] not in {b"0", b"\0"}:
                    raise ValueError("non-regular tar entry or symlink")
                if (header[257:265] != b"ustar\x0000" or entry.uid or entry.gid or entry.mtime
                        or entry.uname or entry.gname or entry.linkname):
                    raise ValueError("noncanonical USTAR header")
                if not entry.name.startswith("wisp-buildtools/"):
                    raise ValueError("unexpected tar root")
                relative = safe_path(entry.name[len("wisp-buildtools/"):])
                if relative in seen or relative not in doc["archive"]["inventory"]:
                    raise ValueError("duplicate or undeclared tar file")
                seen.add(relative)
                spec = doc["archive"]["inventory"][relative]
                if entry.size != spec["size"] or entry.mode != spec["mode"]:
                    raise ValueError("file size or mode mismatch")
                target = None
                if prefix is not None:
                    path = prefix / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if relative not in shared_files or not path.exists():
                        target = path.open("xb")
                checksum, remaining = hashlib.sha256(), entry.size
                beginning = b""
                try:
                    while remaining:
                        data = stream.read(min(64 * 1024, remaining))
                        if not data:
                            raise ValueError("truncated file payload")
                        if not beginning:
                            beginning = data[:8]
                        checksum.update(data)
                        remaining -= len(data)
                        if target:
                            target.write(data)
                finally:
                    if target:
                        target.close()
                if checksum.hexdigest() != spec["sha256"]:
                    raise ValueError(f"file checksum mismatch: {relative}")
                if header_format(beginning, Path(relative)) != spec["format"]:
                    raise ValueError("declared binary format mismatch")
                if prefix is not None:
                    (prefix / relative).chmod(spec["mode"])
                padding = stream.read((-entry.size) % 512)
                if any(padding):
                    raise ValueError("nonzero tar file padding")
                header = stream.read(512)
            if stream.read(512) != b"\0" * 512:
                raise ValueError("invalid tar termination")
            while data := stream.read(64 * 1024):
                if any(data):
                    raise ValueError("nonzero trailing tar data")
            if stream.raw.expanded % 512:
                raise ValueError("unaligned tar termination")
            if seen != set(doc["archive"]["inventory"]):
                raise ValueError("missing inventoried payload file")
            if compressed.count != doc["archive"]["size"] or compressed.hash.hexdigest() != doc["archive"]["sha256"]:
                raise ValueError("archive checksum mismatch")


def validate(bundle: Path) -> dict:
    archive, doc = open_bundle(Path(bundle))
    with archive:
        consume_payload(archive, doc)
    return {"valid": True, "schema": 4, "kind": "toolchain", "identity": doc["identity"],
            "tool": doc["tool"], "version": doc["version"], "files": len(doc["archive"]["inventory"]), **doc["sizes"]}


def package_dependencies(doc: dict) -> list[dict]:
    """Wisp package requirements are resolved locally, never downloaded."""
    return [item for item in doc["requirements"]
            if item["kind"] == "shared-tool" and item["name"].startswith("wisp:") and not item["included"]]


def resolve_packages(bundles: list[Path]) -> list[tuple[Path, dict]]:
    resolved, active, ordered = {}, set(), []
    provided = {}
    for path in bundles:
        doc = read_manifest(path)
        previous = provided.get(doc["identity"])
        if previous and previous[1] != doc:
            raise ValueError(f"incompatible package providers: {doc['identity']}")
        provided[doc["identity"]] = (path, doc)

    def visit(path: Path, required: dict | None = None):
        doc = read_manifest(path)
        identity = doc["identity"]
        if required and (identity != required["name"] or doc["version"] != required["version"]):
            raise ValueError(f"package dependency identity or version mismatch: {required['name']}")
        if identity in active:
            raise ValueError(f"package dependency cycle: {identity}")
        if identity in resolved:
            if resolved[identity] != doc:
                raise ValueError(f"incompatible package providers: {identity}")
            return
        active.add(identity)
        for requirement in package_dependencies(doc):
            name = requirement["name"][len("wisp:"):]
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
                raise ValueError("invalid Wisp package dependency name")
            explicit = provided.get(requirement["name"])
            dependency = explicit[0] if explicit else path.parent / f"{name}.wisp.zip"
            if not dependency.is_file():
                raise ValueError(f"missing package dependency {requirement['name']}: {dependency}")
            visit(dependency, requirement)
        active.remove(identity)
        resolved[identity] = doc
        ordered.append((path, doc))

    for bundle in bundles:
        visit(Path(bundle))
    return ordered


def install(bundle: Path | list[Path], prefix: Path) -> dict:
    prefix = Path(prefix).absolute()
    if prefix.exists() or prefix.is_symlink():
        raise ValueError(f"installation prefix already exists: {prefix}")
    selected = bundle if isinstance(bundle, list) else [bundle]
    graph = resolve_packages([Path(path) for path in selected])
    inventory, shared_files = {}, set()
    for _, doc in graph:
        for relative, spec in doc["archive"]["inventory"].items():
            # Every native tool package carries the same root launcher. Share
            # only that file, with identical bytes, mode, and format required.
            shared = relative == "cwasm.py" and inventory.get(relative) == spec
            if relative.startswith("share/wisp/toolchains/") or (relative in inventory and not shared):
                raise ValueError(f"package file ownership collision: {relative}")
            if shared:
                shared_files.add(relative)
            inventory[relative] = spec
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wisp-toolchain-", dir=prefix.parent) as work:
        stage = Path(work) / "installation"
        stage.mkdir()
        receipts = stage / "share/wisp/toolchains"
        receipts.mkdir(parents=True)
        for path, expected in graph:
            archive, doc = open_bundle(path)
            with archive:
                if doc != expected:
                    raise ValueError("package changed during dependency resolution")
                consume_payload(archive, doc, stage, shared_files=shared_files)
            (receipts / f"{doc['tool']}.json").write_bytes(canonical(doc))
        if prefix.exists() or prefix.is_symlink():
            raise ValueError("installation prefix appeared during verification")
        stage.rename(prefix)
    return {"installed": True, "prefix": str(prefix),
            "packages": [doc["identity"] for _, doc in graph], **inventory_sizes(inventory)}


def report(bundle_paths: list[Path]) -> dict:
    packages, contents, paths, identities = [], {}, {}, set()
    for bundle in bundle_paths:
        doc = read_manifest(bundle)
        if doc["identity"] in identities:
            raise ValueError(f"duplicate package provider in report: {doc['identity']}")
        identities.add(doc["identity"])
        inventory = doc["archive"]["inventory"]
        roles = {}
        for relative, spec in inventory.items():
            roles[spec["role"]] = roles.get(spec["role"], 0) + spec["size"]
            existing = paths.get(relative)
            if existing and existing["sha256"] != spec["sha256"]:
                raise ValueError(f"incompatible packages provide different bytes at {relative}")
            paths[relative] = spec
            item = contents.setdefault(spec["sha256"], {"sha256": spec["sha256"], "size": spec["size"], "packages": set(), "paths": set()})
            item["packages"].add(doc["tool"])
            item["paths"].add(relative)
        packages.append({"tool": doc["tool"], "identity": doc["identity"], "version": doc["version"],
                         "upstreamVersion": doc["upstreamVersion"], "archive": bundle.name, "sha256": digest(bundle),
                         "downloadBytes": bundle.stat().st_size, **doc["sizes"],
                         "dependencies": [item["name"][len("wisp:"):] for item in package_dependencies(doc)],
                         "dependencyVersions": {item["name"][len("wisp:"):]: item["version"] for item in package_dependencies(doc)},
                         "files": len(inventory), "byRoleBytes": roles,
                         "topFiles": [{"path": p, **s} for p, s in sorted(inventory.items(), key=lambda pair: pair[1]["size"], reverse=True)[:8]],
                         "requirements": doc["requirements"], "bootstrapRequirements": doc["bootstrapRequirements"],
                         "filesInventory": inventory})
    by_name = {p["tool"]: p for p in packages}
    for package in packages:
        names, active = set(), set()
        def add(name):
            if name in active:
                raise ValueError(f"package dependency cycle: {name}")
            if name in names:
                return
            if name not in by_name:
                raise ValueError(f"size report is missing package dependency: {name}")
            active.add(name)
            for dependency in by_name[name]["dependencies"]:
                if dependency in by_name and by_name[dependency]["version"] != by_name[name]["dependencyVersions"][dependency]:
                    raise ValueError(f"package dependency version mismatch: {dependency}")
                add(dependency)
            active.remove(name)
            names.add(name)
        add(package["tool"])
        package["closurePackages"] = sorted(names)
        package["closureDownloadBytes"] = sum(by_name[name]["downloadBytes"] for name in names)
        package["closureInstalledBytes"] = sum(by_name[name]["installedBytes"] for name in names)
    installed = sum(p["installedBytes"] for p in packages)
    unique = sum(s["size"] for s in contents.values())
    hosts = []
    for variable, name in (("WISP_WASMTIME", "Wisp Wasmtime host"),
                           ("WISP_RUST_WASMTIME", "Rust Wasmtime host"),
                           ("WISP_WASMTIME_PYTHON", "Wasmtime Python host")):
        configured = os.environ.get(variable)
        if not configured:
            continue
        path = Path(configured)
        candidates = sorted(path.rglob("*")) if variable.endswith("PYTHON") and path.is_dir() else [path]
        measured = [{"path": file.relative_to(path).as_posix() if path.is_dir() else file.name,
                     "sizeBytes": file.stat().st_size, "sha256": digest(file)}
                    for file in candidates if file.is_file() and not file.is_symlink()
                    and "__pycache__" not in file.parts and file.suffix not in {".pyc", ".pyo"}]
        if measured:
            hosts.append({"name": name, "environment": variable, "bytes": sum(file["sizeBytes"] for file in measured),
                          "scope": "Python binding files and engine" if variable.endswith("PYTHON") else "Executable only",
                          "files": measured})
    return {"schema": 1, "packageFormat": {"schema": 4, "kind": "toolchain", "extension": ".wisp.zip"},
            "measurement": "Each bundle contains its own files. Required Wisp packages are installed once alongside consumers. Installed bytes exclude generated share/wisp/toolchains receipts, filesystem allocation and RAM. Download bytes are complete bundles. Support covers file formats other than Wasm and native binaries, including JavaScript and JVM compiler code, JDK data, library sources and headers. External Wisp execution hosts are excluded from payload totals.",
            "executionHosts": hosts, "packages": packages, "totals": {"downloadBytes": sum(p["downloadBytes"] for p in packages),
            "installedBytes": installed, "uniquePathInstalledBytes": sum(s["size"] for s in paths.values()),
            "contentDeduplicatedInstalledBytes": unique, "duplicatedContentBytes": installed - unique},
            "sharedFiles": [{**s, "packages": sorted(s["packages"]), "paths": sorted(s["paths"])}
                            for s in sorted(contents.values(), key=lambda x: x["size"], reverse=True) if len(s["packages"]) > 1]}


def mib(value):
    return f"{value / 1024**2:.2f}"


def write_reports(data: dict, output_dir: Path) -> None:
    from size_report import render_html

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sizes.json").write_bytes(canonical(data))
    rows = ["# Toolchain package sizes", "", data["measurement"], "",
            "| Tool | Download MiB | Installed MiB | Wasm MiB | Native MiB | Support MiB | Required packages | Complete download MiB |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |"]
    for p in data["packages"]:
        rows.append("| " + p["tool"] + " | " + " | ".join(mib(p[k]) for k in ["downloadBytes", "installedBytes", "wasmBytes", "nativeBytes", "supportBytes"]) + " | " + (", ".join(p["dependencies"]) or "none") + " | " + mib(p["closureDownloadBytes"]) + " |")
    rows.extend(["", "## Runtime requirements", ""])
    for p in data["packages"]:
        items = [f"{r['name']} {r['version']}: {'bundled' if r['included'] else 'external'}. {r['note']}" for r in p["requirements"]]
        rows.append(f"- **{p['tool']}**: " + " ".join(items))
    if data.get("executionHosts"):
        rows.extend(["", "## Wisp execution hosts", "", "Measured once outside compiler packages. These sizes exclude system libraries and caches.", "",
                     "| Host | MiB | Measurement |", "| --- | ---: | --- |"])
        rows.extend(f"| {host['name']} | {mib(host['bytes'])} | {host['scope']} |" for host in data["executionHosts"])
    rows.extend(["", "## Shared payload", "", f"Installing every package once totals {mib(data['totals']['installedBytes'])} MiB. Exact file-content union is {mib(data['totals']['contentDeduplicatedInstalledBytes'])} MiB. LLVM, Clang and wasm-tools dependencies are separate packages; consumers do not embed their files.", ""])
    for item in data["sharedFiles"][:10]:
        rows.append(f"- {mib(item['size'])} MiB shared by {', '.join(item['packages'])}: `{item['paths'][0]}`.")
    (output_dir / "sizes.md").write_text("\n".join(rows) + "\n")
    (output_dir / "sizes.html").write_text(render_html(data))
