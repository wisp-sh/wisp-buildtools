"""Run a declared WebAssembly tool under Wasmtime."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

RUNTIME_ABI = "wasmtime-wasip1-inherit-env=n-network=n-tcp=n-udp=n-http=n-p3=n-v1"


class WasmtimeRuntime:
    def __init__(self, executable: Path, compiled_cache: Path):
        self.executable = executable.resolve(strict=True)
        self.compiled_cache = compiled_cache.resolve()
        self.compiled_cache.mkdir(parents=True, exist_ok=True)

    def run(self, tool: Path, argv: tuple[str, ...], environment: dict[str, str],
            mounts: dict[str, Path], cwd: Path, capabilities: dict[str, bool]) -> dict:
        if capabilities.get("network", False):
            raise ValueError("network capability is unavailable in the WASIp1 executor")
        if not capabilities.get("filesystem", False) and mounts:
            raise ValueError("filesystem mounts require the filesystem capability")
        command = [str(self.executable), "run", "-S", "inherit-env=n", "-S",
                   "inherit-network=n", "-S", "tcp=n", "-S", "udp=n", "-S",
                   "http=n", "-S", "p3=n"]
        for name, value in sorted(environment.items()):
            command.extend(("--env", f"{name}={value}"))
        for guest, host in sorted(mounts.items()):
            command.extend(("--dir", f"{host.resolve()}::{guest}"))
        command.extend((str(tool.resolve()), *argv))
        # HOME only tells the Wasmtime host where to store compiled code. The
        # guest receives exactly the environment declared above.
        host_env = {"HOME": str(self.compiled_cache),
                    "XDG_CACHE_HOME": str(self.compiled_cache)}
        if os.name == "nt":
            # Windows process startup needs SystemRoot. These host-only values
            # are never inherited by the WASI guest.
            host_env["SystemRoot"] = os.environ["SystemRoot"]
            host_env["LOCALAPPDATA"] = str(self.compiled_cache)
        start = time.perf_counter()
        process = subprocess.Popen(command, env=host_env, cwd=cwd,
                                   stdin=subprocess.DEVNULL)
        if hasattr(os, "wait4"):
            _, status, usage = os.wait4(process.pid, 0)
            code = os.waitstatus_to_exitcode(status)
            peak_rss = usage.ru_maxrss
            # macOS reports bytes, Linux reports KiB.
            if sys.platform == "darwin":
                peak_rss //= 1024
        else:
            code = process.wait()
            peak_rss = None
        if code:
            raise RuntimeError(f"{tool.name} exited with status {code}")
        return {"seconds": round(time.perf_counter() - start, 3),
                "peak_rss_kib": peak_rss}
