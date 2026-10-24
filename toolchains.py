#!/usr/bin/env python3
"""Build with workspace language adapters and import their Components into Wisp."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import wasm_tools as guest_tools

ROOT = Path(__file__).resolve().parent
CORE = b"\0asm\x01\0\0\0"
COMPONENT = b"\0asm\x0d\0\x01\0"


def registry():
    document = json.loads((ROOT / "toolchains.json").read_text())
    if document.get("schema") != 1:
        raise ValueError("unsupported workspace toolchain registry schema")
    return document["toolchains"]


def artifact(path):
    with path.open("rb") as stream:
        header = stream.read(8)
    if header not in (CORE, COMPONENT):
        raise ValueError(f"not a Core Wasm module or Component: {path}")
    return {
        "format": "component" if header == COMPONENT else "core-module",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def run(command, *, cwd=None):
    subprocess.run([str(value) for value in command], cwd=cwd, check=True)


def entrypoint(config, operation):
    directory = ROOT / config["directory"]
    script = directory / config[operation]
    if not script.is_file():
        raise ValueError(f"missing {operation} entry point: {script}")
    if script.suffix == ".py":
        return directory, [sys.executable, script]
    return directory, [script]


def wasm_tools(configured=None):
    return guest_tools.module(configured)


def absent_output(value):
    path = Path(value).absolute()
    if path.exists() or path.is_symlink():
        raise ValueError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def componentize(args):
    module = Path(args.module).resolve(strict=True)
    if artifact(module)["format"] != "core-module":
        raise ValueError("componentize expects a Core Wasm module")
    output = absent_output(args.output)
    tool = wasm_tools(args.wasm_tools)
    wit = Path(args.wit).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="wisp-component-", dir=output.parent) as temp:
        embedded = Path(temp) / "embedded.wasm"
        assembled = Path(temp) / "component.wasm"
        run(guest_tools.command("component", "embed", wit, module, "--world", args.world,
                                "-o", embedded, configured=tool))
        command = ["component", "new", embedded, "-o", assembled]
        if args.preview1_adapter:
            adapter = Path(args.preview1_adapter).resolve(strict=True)
            command += ["--adapt", f"wasi_snapshot_preview1={adapter}"]
        run(guest_tools.command(*command, configured=tool))
        run(guest_tools.command("validate", assembled, configured=tool))
        if artifact(assembled)["format"] != "component":
            raise ValueError("component assembler did not produce a Component")
        os.replace(assembled, output)
    print(json.dumps({"output": str(output), **artifact(output)}, sort_keys=True))


def copy_wit(source, destination):
    destination.mkdir()
    if source.is_file():
        shutil.copyfile(source, destination / "world.wit")
        return
    found = False
    for path in sorted(source.rglob("*.wit")):
        if not path.resolve().is_relative_to(source):
            raise ValueError(f"WIT input escapes its directory: {path}")
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        found = True
    if not found:
        raise ValueError("WIT directory contains no .wit files")


def wisp_project(args):
    component = Path(args.component).resolve(strict=True)
    details = artifact(component)
    if details["format"] != "component":
        raise ValueError("wisp-project requires a Component; run componentize on Core Wasm first")
    wit = Path(args.wit).resolve(strict=True) if args.wit else None
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.name):
        raise ValueError("package name must be namespace:name in lowercase kebab case")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", args.version):
        raise ValueError("version must be a semantic version such as 0.1.0")
    output = absent_output(args.output)
    quote = json.dumps
    with tempfile.TemporaryDirectory(prefix="wisp-project-", dir=output.parent) as temp:
        staging = Path(temp) / "project"
        staging.mkdir()
        shutil.copyfile(component, staging / "component.wasm")
        if wit:
            if not args.world:
                raise ValueError("--world is required with an authored --wit input")
            copy_wit(wit, staging / "wit")
            world = args.world
        else:
            # Extract the real imports/exports. Only the generated wrapper package
            # receives the release identity; nested interface identities stay intact.
            extracted = subprocess.check_output(guest_tools.command(
                "component", "wit", component, configured=args.wasm_tools
            ), text=True)
            public, replacements = re.subn(
                r"\A\s*package\s+[^;]+;",
                f"package {args.name}@{args.version};", extracted, count=1
            )
            if replacements != 1:
                raise ValueError("cannot identify the extracted Component's wrapper package")
            selected = re.search(r"(?m)^world\s+([a-zA-Z0-9_-]+)\s*\{", public)
            if not selected:
                raise ValueError("cannot identify the extracted Component's world")
            world = args.world or selected.group(1)
            (staging / "wit").mkdir()
            (staging / "wit/world.wit").write_text(public)
        config = (
            f"[package]\nname = {quote(args.name)}\nversion = {quote(args.version)}\n\n"
            '[build]\nwit = "wit"\nroot = "application"\n\n'
            f"[build.units.application]\nworld = {quote(world)}\n"
            f'component = {{ path = "component.wasm", sha256 = "{details["sha256"]}" }}\n'
        )
        # Parse the staged contract before publishing a project. This checks
        # full SemVer syntax, authored identity, dependencies and world selection.
        tool = wasm_tools(args.wasm_tools)
        canonical = subprocess.check_output(guest_tools.command(
            "component", "wit", staging / "wit", configured=tool
        ), text=True)
        declaration = re.match(r"\s*package\s+([^;]+);", canonical)
        if not declaration or declaration.group(1) != f"{args.name}@{args.version}":
            raise ValueError("authored WIT package identity/version must match --name and --version")
        run(guest_tools.command("component", "embed", staging / "wit", "--world", world,
             "--only-custom", "-o", Path(temp) / "contract.wasm", configured=tool))
        (staging / "wisp.toml").write_text(config)
        os.replace(staging, output)
    print(f"Created {output / 'wisp.toml'}")
    print(f"wisp build {output} --component-only --out <new-output-directory>")


def main():
    definitions = registry()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list adapters and their entry points")
    listing.add_argument("--json", action="store_true")
    for operation in ("bootstrap", "verify"):
        command = commands.add_parser(operation)
        command.add_argument("language", choices=[*definitions, "all"])
    build = commands.add_parser("build")
    build.add_argument("language", choices=definitions)
    build.add_argument("source")
    build.add_argument("-o", "--output", required=True)
    assembly = commands.add_parser("componentize", help="assemble a Core Wasm module with WIT")
    assembly.add_argument("module")
    assembly.add_argument("--wit", required=True)
    assembly.add_argument("--world", required=True)
    assembly.add_argument("-o", "--output", required=True)
    assembly.add_argument("--wasm-tools")
    assembly.add_argument("--preview1-adapter")
    project = commands.add_parser("wisp-project", help="create a Wisp prebuilt Component project")
    project.add_argument("component")
    project.add_argument("--wit", help="authored WIT; omitted to extract the Component's exact contract")
    project.add_argument("--world", help="authored world, or extracted world override")
    project.add_argument("--wasm-tools")
    project.add_argument("--name", required=True, help="package identity matching the authored WIT")
    project.add_argument("--version", default="0.1.0")
    project.add_argument("-o", "--output", required=True)
    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]
    if args.command in ("list", "componentize", "wisp-project") and extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    if args.command == "list":
        items = [{"language": name, "directory": config["directory"],
                  "entrypoints": {key: (ROOT / config["directory"] / config[key]).is_file()
                                  for key in ("build", "bootstrap", "verify")}}
                 for name, config in definitions.items()]
        if args.json:
            print(json.dumps(items, indent=2))
        else:
            for item in items:
                available = ", ".join(key for key, present in item["entrypoints"].items() if present)
                print(f'{item["language"]}: {item["directory"]}, entry points: {available or "pending"}')
    elif args.command == "componentize":
        componentize(args)
    elif args.command == "wisp-project":
        wisp_project(args)
    elif args.command == "build":
        source = Path(args.source).resolve(strict=True)
        output = absent_output(args.output)
        directory, command = entrypoint(definitions[args.language], "build")
        # Adapters publish atomically and may write adjacent WIT/build receipts.
        # Preserve their requested output path and all those companion files.
        run([*command, source, "-o", output, *extra])
        details = artifact(output)
        print(json.dumps({"language": args.language, "output": str(output), **details}, sort_keys=True))
    else:
        if args.command == "bootstrap":
            run([sys.executable, ROOT / "wasm-tools/bootstrap.py"])
        selected = definitions.values() if args.language == "all" else [definitions[args.language]]
        visited = set()
        for config in selected:
            if config["directory"] in visited:
                continue
            visited.add(config["directory"])
            directory, command = entrypoint(config, args.command)
            run([*command, *extra])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 1)
    except (OSError, ValueError, KeyError) as error:
        print(f"toolchains: {error}", file=sys.stderr)
        sys.exit(1)
