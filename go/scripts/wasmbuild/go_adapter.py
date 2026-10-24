"""Discover ordinary local Go packages and construct generic build nodes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .graph import Dependency, Graph, Node
from .toolchain import Toolchain


TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|`[^`]*`|[A-Za-z_][A-Za-z_0-9]*|[^\s]', re.M)
SKIP_DIRS = {".git", "testdata", "node_modules"}
GOOS_NAMES = {"aix", "android", "darwin", "dragonfly", "freebsd", "illumos",
              "ios", "js", "linux", "netbsd", "openbsd", "plan9", "solaris",
              "wasip1", "windows"}
GOARCH_NAMES = {"386", "amd64", "arm", "arm64", "loong64", "mips", "mips64",
                "mips64le", "mipsle", "ppc64", "ppc64le", "riscv64", "s390x", "wasm"}
GO_ENV = {"PWD": "/build", "GOROOT": "/goroot", "GOOS": "wasip1",
          "GOARCH": "wasm", "TMPDIR": "/build"}


@dataclass
class Package:
    import_path: str
    name: str
    files: tuple[Path, ...]
    imports: set[str]


def tokens(source: str) -> list[str]:
    return [match.group() for match in TOKEN.finditer(source)
            if not match.group().startswith(("//", "/*"))]


def quoted(token: str) -> str:
    if token.startswith('"'):
        # Import paths only need common Go quoted string escapes.
        return bytes(token[1:-1], "utf-8").decode("unicode_escape")
    return token[1:-1]


def inspect(path: Path) -> tuple[str, set[str]]:
    ts = tokens(path.read_text(encoding="utf-8"))
    if not ts or ts[0] != "package" or len(ts) < 2:
        raise ValueError(f"missing package clause: {path}")
    package = ts[1]
    imports: set[str] = set()
    i = 2
    while i < len(ts):
        if ts[i] != "import":
            i += 1
            continue
        i += 1
        if i < len(ts) and ts[i] == "(":
            i += 1
            while i < len(ts) and ts[i] != ")":
                if ts[i].startswith(('"', '`')):
                    imports.add(quoted(ts[i]))
                i += 1
            if i == len(ts):
                raise ValueError(f"unterminated import group: {path}")
        else:
            while i < len(ts) and not ts[i].startswith(('"', '`')):
                i += 1
            if i == len(ts):
                raise ValueError(f"invalid import: {path}")
            imports.add(quoted(ts[i]))
        i += 1
    return package, imports


def module_prefix(root: Path, override: str | None) -> str:
    if override:
        return override.rstrip("/")
    module = root / "go.mod"
    if module.is_file():
        match = re.search(r"(?m)^module\s+(\S+)", module.read_text(encoding="utf-8"))
        if match:
            return match.group(1).strip('"`')
    return "project"


def selected(path: Path) -> bool:
    parts = path.stem.split("_")
    suffixes = parts[-2:] if parts[-1] in GOARCH_NAMES else parts[-1:]
    for suffix in suffixes:
        if suffix in GOOS_NAMES and suffix != "wasip1":
            return False
        if suffix in GOARCH_NAMES and suffix != "wasm":
            return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("//go:build "):
            if line.startswith("package "):
                break
            continue
        expression = re.findall(r"&&|\|\||!|\(|\)|[A-Za-z0-9_.]+", line[11:])
        position = 0

        def primary() -> bool:
            nonlocal position
            if position >= len(expression):
                raise ValueError(f"invalid build constraint: {path}")
            token = expression[position]
            position += 1
            if token == "!":
                return not primary()
            if token == "(":
                result = or_expression()
                if position >= len(expression) or expression[position] != ")":
                    raise ValueError(f"invalid build constraint: {path}")
                position += 1
                return result
            if token in {"&&", "||", ")"}:
                raise ValueError(f"invalid build constraint: {path}")
            if token.startswith("go1."):
                try:
                    return int(token[4:]) <= 27
                except ValueError:
                    return False
            return token in {"wasip1", "wasm", "gc"}

        def and_expression() -> bool:
            nonlocal position
            result = primary()
            while position < len(expression) and expression[position] == "&&":
                position += 1
                other = primary()
                result = result and other
            return result

        def or_expression() -> bool:
            nonlocal position
            result = and_expression()
            while position < len(expression) and expression[position] == "||":
                position += 1
                other = and_expression()
                result = result or other
            return result

        result = or_expression()
        if position != len(expression):
            raise ValueError(f"invalid build constraint: {path}")
        return result
    return True


def discover(root: Path, prefix: str) -> dict[str, Package]:
    groups: dict[Path, list[Path]] = {}
    for path in sorted(root.rglob("*.go")):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part.startswith("_") or part in SKIP_DIRS
               for part in relative.parts[:-1]):
            continue
        if path.name.startswith((".", "_")) or path.name.endswith("_test.go"):
            continue
        if not selected(path):
            continue
        groups.setdefault(path.parent, []).append(path)
    packages = {}
    for directory, files in groups.items():
        names = set()
        imports: set[str] = set()
        for file in files:
            name, found = inspect(file)
            names.add(name)
            imports |= found
        if len(names) != 1:
            raise ValueError(f"mixed Go packages in {directory}")
        relative = directory.relative_to(root)
        if relative.parts and relative.parts[0] == "vendor":
            import_path = Path(*relative.parts[1:]).as_posix()
        else:
            import_path = prefix if relative == Path(".") else prefix + "/" + relative.as_posix()
        packages[import_path] = Package(import_path, names.pop(), tuple(files), imports)
    if prefix not in packages or packages[prefix].name != "main":
        raise ValueError("project root must contain package main")
    return packages


def importcfg(mapping: dict[str, str]) -> bytes:
    return "".join(f"packagefile {name}={mapping[name]}\n" for name in sorted(mapping)).encode()


def construct(root: Path, toolchain: Toolchain, prefix: str,
              compiler_flags: tuple[str, ...] = ()) -> Graph:
    packages = discover(root, prefix)
    graph = Graph()
    visited: set[str] = set()
    active: set[str] = set()

    def add(path: str) -> None:
        if path in active:
            raise ValueError(f"Go import cycle at {path}")
        if path in visited:
            return
        active.add(path)
        package = packages[path]
        local = sorted(name for name in package.imports if name in packages)
        for name in local:
            add(name)
        unknown = package.imports - set(local) - set(toolchain.stdlib) - {"unsafe"}
        if unknown:
            raise ValueError(f"{path} imports unavailable packages: {', '.join(sorted(unknown))}")
        packagefiles = {name: toolchain.stdlib[name] for name in package.imports
                        if name in toolchain.stdlib}
        dependencies = {}
        for name in local:
            guest = f"/deps/{name}.a"
            packagefiles[name] = guest
            dependencies[guest] = Dependency(f"compile:{name}", "archive.a")
        source_inputs = {f"/src/{file.name}": file for file in package.files}
        source_inputs["/build/importcfg"] = importcfg(packagefiles)
        package_arg = "main" if path == prefix else path
        graph.add(Node(
            name=f"compile:{path}", tool="compile",
            argv=("-p", package_arg, "-pack", "-trimpath", "/src", *compiler_flags,
                  "-importcfg", "/build/importcfg", "-o", "/build/archive.a",
                  *(f"/src/{file.name}" for file in package.files)),
            environment=GO_ENV.copy(), inputs=source_inputs,
            dependencies=dependencies, outputs={"archive.a": "/build/archive.a"},
            mounts={"/src": None, "/deps": None, "/build": None,
                    "/goroot": toolchain.goroot}))
        active.remove(path)
        visited.add(path)

    add(prefix)
    packagefiles = toolchain.stdlib.copy()
    dependencies = {}
    for path in sorted(visited):
        if path == prefix:
            guest = "/build/main.a"
        else:
            guest = f"/deps/{path}.a"
            packagefiles[path] = guest
        dependencies[guest] = Dependency(f"compile:{path}", "archive.a")
    graph.add(Node(
        name="link", tool="link",
        argv=("-linkmode=internal", "-importcfg", "/build/importcfg",
              "-o", "/out/main.wasm", "/build/main.a"),
        environment=GO_ENV.copy(), inputs={"/build/importcfg": importcfg(packagefiles)},
        dependencies=dependencies, outputs={"main.wasm": "/out/main.wasm"},
        mounts={"/build": None, "/deps": None, "/out": None,
                "/goroot": toolchain.goroot}))
    return graph
