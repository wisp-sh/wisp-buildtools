#!/usr/bin/env python3
"""Package and validate the pinned LLVM/WASI static SDK without rebuilding LLVM."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def fail(message: str) -> None:
    raise ValueError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_cache(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([^/#][^:=]*):[^=]+=(.*)", line)
        if match:
            result[match[1]] = match[2]
    return result


def release_flags_match(actual: str | None, expected: str) -> bool:
    """Allow source path remapping without relaxing the pinned codegen flags."""
    if actual is None:
        return False
    try:
        flags = shlex.split(actual)
    except ValueError:
        return False
    flags = [flag for flag in flags if not re.fullmatch(r"-ffile-prefix-map=.+=.+", flag)]
    return flags == shlex.split(expected)


def macros(path: Path) -> dict[str, str]:
    return dict(re.findall(r'^#define (\w+) (.+)$', path.read_text(), re.M))


def unquote(value: str) -> str:
    return json.loads(value) if value.startswith('"') else value


def archive_objects(path: Path) -> int:
    """Check every ar payload, excluding its symbol/name tables, is Wasm code."""
    data = path.read_bytes()
    if data[:8] != b"!<arch>\n":
        fail(f"not a regular static archive: {path}")
    offset, count = 8, 0
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            fail(f"malformed ar header in {path}")
        try:
            size = int(header[48:58].strip())
            name = header[:16].decode("ascii").strip()
        except (ValueError, UnicodeError):
            fail(f"malformed ar member in {path}")
        start = offset + 60
        payload = data[start:start + size]
        if len(payload) != size:
            fail(f"truncated ar member in {path}")
        if name not in {"/", "//", "/SYM64/", "__.SYMDEF", "__.SYMDEF SORTED"}:
            if name.startswith("#1/"):
                payload = payload[int(name[3:]):]
            if not payload.startswith(b"\x00asm\x01\x00\x00\x00"):
                fail(f"archive member {name} is not a Wasm object in {path}")
            count += 1
        offset = start + size + size % 2
    if offset != len(data) or count == 0:
        fail(f"empty or malformed Wasm static archive: {path}")
    return count


def target_dependencies(build: Path, names: set[str]) -> dict[str, list[str]]:
    result = {name: [] for name in names}
    files = ["llvm/LLVMExports.cmake", "lld/LLDTargets.cmake"]
    if any(name.startswith("clang") for name in names):
        files.append("clang/ClangTargets.cmake")
    for filename in files:
        text = (build / "lib/cmake" / filename).read_text()
        for name, body in re.findall(r"set_target_properties\((\w+) PROPERTIES\s+(.*?)\n\)", text, re.S):
            if name not in names:
                continue
            match = re.search(r'INTERFACE_LINK_LIBRARIES "([^"]*)"', body)
            dependencies = match[1].split(";") if match else []
            for dependency in dependencies:
                if dependency not in names and dependency not in {"rt", "dl", "m", "pthread", "z", "zstd"}:
                    fail(f"{name} requires unbuilt/unsupported SDK dependency {dependency}")
            result[name] = dependencies
    return dict(sorted(result.items()))


def components(build: Path, names: set[str]) -> dict[str, dict]:
    text = (build / "tools/llvm-config/LibraryDependencies.inc").read_text()
    raw = {}
    pattern = r'\{ "([^"]+)", (nullptr|"[^"]+"), (?:true|false), \{([^}]*)\} \}'
    for name, library, dependencies in re.findall(pattern, text):
        raw[name] = {"library": None if library == "nullptr" else library[1:-1],
                     "dependencies": re.findall(r'"([^"]+)"', dependencies)}
    if "core" not in raw or "webassembly" not in raw:
        fail("LLVM component metadata is missing core or WebAssembly")
    result = {}
    for name, component in raw.items():
        library = component["library"]
        if library in names:
            result[name] = component
    # The SDK intentionally contains the built library subset, not every CMake target.
    result["all"] = {"library": None, "dependencies": sorted(result)}
    for alias in ["webassembly", "all-targets"]:
        component = raw[alias]
        if all(dep in result for dep in component["dependencies"]):
            result[alias] = component
    for name, component in result.items():
        missing = set(component["dependencies"]) - result.keys()
        if missing:
            fail(f"LLVM component {name} requires unbuilt components: {sorted(missing)}")
    return dict(sorted(result.items()))


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        fail(f"required header directory is missing: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            fail(f"SDK input contains a symlink: {path}")
        if not path.is_file() or "CMakeFiles" in path.relative_to(source).parts:
            continue
        if path.name in {"CMakeLists.txt", "cmake_install.cmake"} or path.suffix in {".cmake", ".d"}:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def cmake_config(root: Path, metadata: dict) -> None:
    directory = root / "lib/cmake/WispLLVM"
    directory.mkdir(parents=True)
    lines = ["# Generated relocatable imports for the packaged library subset.",
             'get_filename_component(_wisp_llvm_prefix "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)',
             'set(WispLLVM_HOST_TARGET "' + metadata["host_target"] + '")',
             'set(WispLLVM_VERSION "' + metadata["version"] + '")',
             'set(WispLLVM_INCLUDE_DIR "${_wisp_llvm_prefix}/include")',
             'set(WispLLVM_LIBRARY_DIR "${_wisp_llvm_prefix}/lib")',
             'if(NOT CMAKE_CXX_COMPILER_TARGET STREQUAL WispLLVM_HOST_TARGET)',
             '  message(FATAL_ERROR "WispLLVM requires CMAKE_CXX_COMPILER_TARGET=${WispLLVM_HOST_TARGET}")',
             "endif()"]
    for name, dependencies in metadata["libraries"].items():
        lines += [f"if(NOT TARGET {name})", f"  add_library({name} STATIC IMPORTED)",
                  f"  set_target_properties({name} PROPERTIES",
                  f'    IMPORTED_LOCATION "${{_wisp_llvm_prefix}}/lib/lib{name}.a"',
                  '    INTERFACE_INCLUDE_DIRECTORIES "${_wisp_llvm_prefix}/include"',
                  '    INTERFACE_COMPILE_FEATURES "cxx_std_17"',
                  '    INTERFACE_COMPILE_OPTIONS "-fno-rtti;-fno-exceptions"',
                  '    INTERFACE_COMPILE_DEFINITIONS "_LARGEFILE_SOURCE;_FILE_OFFSET_BITS=64;__STDC_CONSTANT_MACROS;__STDC_FORMAT_MACROS;__STDC_LIMIT_MACROS"',
                  '    INTERFACE_LINK_LIBRARIES "' + ";".join(dependencies) + '")', "endif()"]
    lines += ["unset(_wisp_llvm_prefix)", ""]
    (directory / "WispLLVMConfig.cmake").write_text("\n".join(lines))


def checksum_files(root: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / "SHA256SUMS":
            lines.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}\n")
    (root / "SHA256SUMS").write_text("".join(lines))


def verify(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        fail(f"SDK is not a regular directory: {root}")
    checked = set()
    for line in (root / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        if not match:
            fail("malformed SDK checksum manifest")
        relative = Path(match[2])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in checked:
            fail(f"unsafe or duplicate SDK checksum path: {relative}")
        path = root / relative
        if path.is_symlink() or not path.is_file() or sha256(path) != match[1]:
            fail(f"SDK checksum mismatch: {relative}")
        checked.add(relative.as_posix())
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            fail(f"SDK contains a symlink: {path}")
        if path.is_file() and path != root / "SHA256SUMS":
            actual.add(path.relative_to(root).as_posix())
    if actual != checked:
        fail(f"SDK checksum file set differs: {sorted(actual ^ checked)}")
    metadata = json.loads((root / "share/llvm/sdk.json").read_text())
    if metadata.get("schema_version") != 1:
        fail("unsupported LLVM/WASI SDK schema")
    required = ["include/llvm/IR/Module.h", "include/llvm-c/Core.h", "include/llvm/Config/llvm-config.h",
                "include/llvm/IR/IntrinsicEnums.inc", "include/lld/Common/Driver.h", "bin/llvm-config",
                "lib/cmake/WispLLVM/WispLLVMConfig.cmake", "licenses/llvm-LICENSE.TXT"]
    for name in required:
        if name not in checked:
            fail(f"SDK required file is missing: {name}")
    objects = {}
    for name in metadata["libraries"]:
        path = root / "lib" / f"lib{name}.a"
        if path.relative_to(root).as_posix() not in checked:
            fail(f"SDK library is not checksummed: {name}")
        objects[name] = archive_objects(path)
    if objects != metadata["archive_object_counts"]:
        fail("SDK object inventory differs from packaged metadata")
    return {"sdk": str(root), "id": metadata["id"], "host_target": metadata["host_target"],
            "version": metadata["version"], "libraries": len(objects), "objects": sum(objects.values()),
            "files": len(checked), "rust_compatibility": metadata["rust_compatibility"]}


def package(args: argparse.Namespace) -> dict:
    manifest_path = args.manifest.resolve()
    base = manifest_path.parent
    manifest = tomllib.loads(manifest_path.read_text())
    sdk = manifest["sdk"]
    source = (args.source_dir or base / manifest["llvm"]["source_directory"]).resolve()
    build = (args.build_dir or base / sdk["build_directory"]).resolve()
    out = args.out.resolve()
    if out.exists() or out.is_symlink():
        fail(f"output already exists: {out}")
    if args.archive and (args.archive.exists() or args.archive.is_symlink()):
        fail(f"archive output already exists: {args.archive}")
    if not source.is_dir() or not build.is_dir():
        fail("LLVM source and configured build directories are required")
    if out.is_relative_to(source) or out.is_relative_to(build):
        fail("SDK output must be outside source and build input directories")
    if args.archive:
        archive_out = args.archive.resolve()
        if any(archive_out.is_relative_to(directory) for directory in [out, source, build]):
            fail("SDK archive must be outside its package, source and build directories")
    archive = base / manifest["llvm"]["archive_directory"] / manifest["llvm"]["archive"]
    source_archive_sha256 = sha256(archive)
    if source_archive_sha256 != manifest["llvm"]["archive_sha256"]:
        fail("LLVM source archive checksum differs from the pinned manifest")
    cache = read_cache(build / "CMakeCache.txt")
    expected = {"LLVM_TARGETS_TO_BUILD": ";".join(manifest["build"]["llvm_targets"]),
                "LLVM_DEFAULT_TARGET_TRIPLE": manifest["build"]["compiler_host_target"],
                "CMAKE_CXX_FLAGS_RELEASE": "-" + manifest["build"]["default_optimization"] + " -DNDEBUG",
                "CMAKE_GENERATOR": "Ninja",
                "LLVM_ENABLE_THREADS": "OFF", "LLVM_ENABLE_ASSERTIONS": "OFF",
                "BUILD_SHARED_LIBS": "OFF", "LLVM_ENABLE_RTTI": "OFF", "LLVM_ENABLE_EH": "OFF"}
    for key, value in expected.items():
        actual = cache.get(key)
        matches = release_flags_match(actual, value) if key == "CMAKE_CXX_FLAGS_RELEASE" else actual == value
        if not matches:
            fail(f"build setting {key} must be {value}, got {cache.get(key)!r}")
    rules_path = build / "CMakeFiles/rules.ninja"
    compiler_targets = set(re.findall(r" --target=([^ \n]+)", rules_path.read_text()))
    if compiler_targets != {manifest["build"]["compiler_host_target"]}:
        fail(f"build compiler targets differ from the pinned host ABI: {sorted(compiler_targets)}")
    config = macros(build / "include/llvm/Config/llvm-config.h")
    variables = macros(build / "tools/llvm-config/BuildVariables.inc")
    include_clang = not args.without_clang
    libraries = sorted(path for path in (build / "lib").glob("lib*.a")
                       if path.name.startswith(("libLLVM", "liblld")) or
                       (include_clang and path.name.startswith("libclang")))
    names = {path.stem[3:] for path in libraries}
    required = set(sdk["required_libraries"])
    if not required <= names:
        fail(f"required built static libraries are missing: {sorted(required - names)}")
    object_counts = {path.stem[3:]: archive_objects(path) for path in libraries}
    dependencies = target_dependencies(build, names)
    metadata = {"schema_version": 1, "id": sdk["id"] + ("" if include_clang else "-core"),
                "version": unquote(config["LLVM_VERSION_STRING"]),
                "llvm_revision": manifest["llvm"]["revision"],
                "host_target": manifest["build"]["compiler_host_target"],
                "configured_llvm_host_triple": unquote(config["LLVM_HOST_TRIPLE"]),
                "targets": manifest["build"]["llvm_targets"],
                "build_mode": cache["CMAKE_BUILD_TYPE"],
                "cxxflags": unquote(variables["LLVM_CXXFLAGS"]),
                "cflags": unquote(variables["LLVM_CFLAGS"]),
                "cppflags": unquote(variables["LLVM_CPPFLAGS"]),
                "ldflags": unquote(variables["LLVM_LDFLAGS"]),
                "system_libs": unquote(variables["LLVM_SYSTEM_LIBS"]),
                "assertions": False, "threads": False, "rtti": False, "exceptions": False,
                "static": True, "clang": include_clang,
                "libraries": dependencies, "components": components(build, names),
                "archive_object_counts": object_counts,
                "rust_compatibility": "not-verified",
                "notes": ["The libraries contain WebAssembly object files, not native libraries.",
                          "Use the pinned WASI SDK for its C++ runtime and wasip3 linking.",
                          "LLVM_HOST_TRIPLE in existing generated headers describes the bootstrap host; host_target records the actual archive ABI.",
                          "llvm-config is a Python build-host metadata utility and does not execute LLVM.",
                          "Only built static library targets are exported by WispLLVMConfig.cmake."]}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".llvm-sdk-", dir=out.parent) as temporary:
        stage = Path(temporary) / "sdk"
        stage.mkdir()
        for project, header_dir in [("llvm", "llvm"), ("llvm", "llvm-c"), ("lld", "lld")]:
            copy_tree(source / project / "include" / header_dir, stage / "include" / header_dir)
        copy_tree(build / "include/llvm", stage / "include/llvm")
        copy_tree(build / "tools/lld/include/lld", stage / "include/lld")
        if include_clang:
            copy_tree(source / "clang/include/clang", stage / "include/clang")
            copy_tree(build / "tools/clang/include/clang", stage / "include/clang")
        (stage / "lib").mkdir()
        for path in libraries:
            if path.is_symlink():
                fail(f"static library input must be a regular file: {path}")
            shutil.copyfile(path, stage / "lib" / path.name)
        provenance = stage / "share/llvm/provenance"
        provenance.mkdir(parents=True)
        shutil.copyfile(manifest_path, provenance / "toolchain.toml")
        shutil.copyfile(build / "CMakeCache.txt", provenance / "CMakeCache.txt")
        shutil.copyfile(build / "tools/llvm-config/BuildVariables.inc", provenance / "BuildVariables.inc")
        for patch in manifest["llvm"]["patches"]:
            shutil.copyfile(base / patch, provenance / Path(patch).name)
        for patch in sdk.get("extra_provenance", []):
            shutil.copyfile(base / patch, provenance / Path(patch).name)
        write_json(provenance / "inputs.json", {
            "source_archive": {"name": archive.name, "sha256": source_archive_sha256},
            "cmake_cache_sha256": sha256(build / "CMakeCache.txt"),
            "compiler_rules_sha256": sha256(rules_path),
            "wasi_sdk": manifest["inputs"]["wasi_sdk"],
            "source_header_roots": ["llvm/include/llvm", "llvm/include/llvm-c", "lld/include/lld"] +
                                   (["clang/include/clang"] if include_clang else [])})
        (stage / "licenses").mkdir()
        for project in ["llvm", "lld"] + (["clang"] if include_clang else []):
            shutil.copyfile(source / project / "LICENSE.TXT", stage / "licenses" / f"{project}-LICENSE.TXT")
        write_json(stage / "share/llvm/sdk.json", metadata)
        (stage / "bin").mkdir()
        shutil.copyfile(ROOT / "scripts/llvm-config.py", stage / "bin/llvm-config")
        (stage / "bin/llvm-config").chmod(0o755)
        cmake_config(stage, metadata)
        checksum_files(stage)
        verify(stage)
        stage.rename(out)
    report = verify(out)
    if args.archive:
        output_archive = args.archive.absolute()
        if output_archive.exists():
            fail(f"archive output already exists: {output_archive}")
        output_archive.parent.mkdir(parents=True, exist_ok=True)
        with output_archive.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for path in [out] + sorted(out.rglob("*")):
                        relative = Path(metadata["id"]) / path.relative_to(out)
                        info = tar.gettarinfo(str(path), arcname=relative.as_posix())
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ""
                        info.mode = 0o755 if path.is_dir() or path.parent == out / "bin" else 0o644
                        with path.open("rb") if path.is_file() else open(os.devnull, "rb") as stream:
                            tar.addfile(info, stream if path.is_file() else None)
        report["archive"] = str(output_archive)
        report["archive_sha256"] = sha256(output_archive)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package_parser = commands.add_parser("package", help="package the manifest-pinned existing build")
    package_parser.add_argument("--manifest", type=Path, default=ROOT / "toolchain.toml")
    package_parser.add_argument("--source-dir", type=Path)
    package_parser.add_argument("--build-dir", type=Path)
    package_parser.add_argument("--out", type=Path, required=True)
    package_parser.add_argument("--archive", type=Path)
    package_parser.add_argument("--without-clang", action="store_true")
    verify_parser = commands.add_parser("verify", help="verify a relocated SDK and all Wasm objects")
    verify_parser.add_argument("sdk", type=Path)
    args = parser.parse_args()
    try:
        result = package(args) if args.command == "package" else verify(args.sdk.absolute())
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"llvm-sdk: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
