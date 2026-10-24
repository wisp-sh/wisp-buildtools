"""Tool agnostic DAG, content keys, output cache, and executor."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

from .runtime import RUNTIME_ABI, WasmtimeRuntime


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


@dataclass(frozen=True)
class Dependency:
    node: str
    output: str


@dataclass
class Node:
    name: str
    tool: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    inputs: dict[str, Path | bytes]
    dependencies: dict[str, Dependency]
    outputs: dict[str, str]
    mounts: dict[str, Path | None]
    capabilities: dict[str, bool] = field(default_factory=lambda: {"filesystem": True, "network": False})


class Graph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}

    def add(self, node: Node) -> None:
        if node.name in self.nodes:
            raise ValueError(f"duplicate node: {node.name}")
        self.nodes[node.name] = node

    def order(self) -> list[Node]:
        ordered: list[Node] = []
        state: dict[str, int] = {}

        def visit(name: str) -> None:
            if name not in self.nodes:
                raise ValueError(f"missing dependency: {name}")
            if state.get(name) == 1:
                raise ValueError(f"dependency cycle at {name}")
            if state.get(name) == 2:
                return
            state[name] = 1
            for dep in self.nodes[name].dependencies.values():
                visit(dep.node)
            state[name] = 2
            ordered.append(self.nodes[name])

        for name in sorted(self.nodes):
            visit(name)
        return ordered


class Engine:
    def __init__(self, runtime: WasmtimeRuntime, tools: dict[str, Path],
                 toolchain_digest: str, cache: Path):
        self.runtime = runtime
        self.tools = tools
        self.toolchain_digest = toolchain_digest
        self.cache = cache.resolve()
        self.cache.mkdir(parents=True, exist_ok=True)

    def execute(self, graph: Graph) -> tuple[dict[str, dict], dict[str, dict[str, Path]]]:
        report: dict[str, dict] = {}
        produced: dict[str, dict[str, Path]] = {}
        for node in graph.order():
            if node.tool not in self.tools:
                raise ValueError(f"unknown tool: {node.tool}")
            input_hashes = {guest: digest(value) if isinstance(value, bytes)
                            else file_digest(value) for guest, value in sorted(node.inputs.items())}
            dep_hashes = {guest: file_digest(produced[dep.node][dep.output])
                          for guest, dep in sorted(node.dependencies.items())}
            key_data = {
                "schema": 1, "runtime": RUNTIME_ABI,
                "tool": file_digest(self.tools[node.tool]),
                "toolchain": self.toolchain_digest,
                "argv": node.argv, "environment": node.environment,
                "inputs": input_hashes, "dependencies": dep_hashes,
                "outputs": node.outputs,
                "mounts": {guest: "staged" if host is None else "resource"
                           for guest, host in sorted(node.mounts.items())},
                "capabilities": node.capabilities,
            }
            key = digest(json.dumps(key_data, sort_keys=True, separators=(",", ":")).encode())
            entry = self.cache / key
            metadata = entry / "outputs.json"
            valid = False
            if metadata.is_file():
                recorded = json.loads(metadata.read_text())
                valid = set(recorded) == set(node.outputs) and all(
                    (entry / name).is_file() and file_digest(entry / name) == expected
                    for name, expected in recorded.items())
            if valid:
                report[node.name] = {"cache": "hit", "key": key, "seconds": 0,
                                     "peak_rss_kib": 0}
            else:
                started = time.perf_counter()
                with tempfile.TemporaryDirectory(prefix="wasm-node-") as work:
                    stage = Path(work)
                    mounts: dict[str, Path] = {}
                    for guest, host in node.mounts.items():
                        if host is None:
                            directory = stage / guest.lstrip("/")
                            directory.mkdir(parents=True, exist_ok=True)
                            mounts[guest] = directory
                        else:
                            mounts[guest] = host
                    def target_path(guest: str) -> Path:
                        matches = [root for root in mounts if guest.startswith(root + "/")]
                        if len(matches) != 1:
                            raise ValueError(f"input/output path lacks a unique mount: {guest}")
                        root = matches[0]
                        if node.mounts[root] is not None:
                            raise ValueError(f"cannot write to resource mount: {guest}")
                        target = mounts[root] / guest[len(root) + 1:]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        return target
                    for guest, value in node.inputs.items():
                        target = target_path(guest)
                        if isinstance(value, bytes):
                            target.write_bytes(value)
                        else:
                            shutil.copyfile(value, target)
                    for guest, dep in node.dependencies.items():
                        shutil.copyfile(produced[dep.node][dep.output], target_path(guest))
                    metrics = self.runtime.run(self.tools[node.tool], node.argv,
                                               node.environment, mounts, stage,
                                               node.capabilities)
                    new_entry = Path(tempfile.mkdtemp(prefix="cache-entry-", dir=self.cache))
                    try:
                        output_hashes = {}
                        for name, guest in node.outputs.items():
                            source = target_path(guest)
                            if not source.is_file():
                                raise RuntimeError(f"{node.name} omitted output {guest}")
                            shutil.copyfile(source, new_entry / name)
                            output_hashes[name] = file_digest(new_entry / name)
                        (new_entry / "outputs.json").write_text(json.dumps(output_hashes, sort_keys=True))
                        if entry.exists():
                            shutil.rmtree(entry)
                        new_entry.rename(entry)
                    finally:
                        if new_entry.exists():
                            shutil.rmtree(new_entry)
                report[node.name] = {"cache": "miss", "key": key, **metrics,
                                     "wall_seconds": round(time.perf_counter() - started, 3)}
            produced[node.name] = {name: entry / name for name in node.outputs}
        return report, produced
