#!/usr/bin/env python3
"""Record one host's clean build or compare reports from several hosts."""

import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import tempfile


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def project_digest(root: Path) -> str:
    h = hashlib.sha256()
    for file in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = file.relative_to(root).as_posix().encode()
        h.update(len(relative).to_bytes(4, "big"))
        h.update(relative)
        h.update(bytes.fromhex(sha(file)))
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs="+", type=Path, help="compare existing host reports")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--wasmtime", type=Path)
    parser.add_argument("--project", type=Path)
    args = parser.parse_args()
    if args.compare:
        reports = [json.loads(path.read_text()) for path in args.compare]
        for field in ("toolchain_archive_sha256", "source_sha256", "configuration",
                      "output_sha256"):
            values = {json.dumps(report[field], sort_keys=True) for report in reports}
            if len(values) != 1:
                raise SystemExit(f"{field} differs: {sorted(values)}")
        print(json.dumps({"hosts": [r["host"] for r in reports],
                          "output_sha256": reports[0]["output_sha256"]}, indent=2))
        return
    if not all((args.archive, args.toolchain, args.wasmtime, args.project)):
        parser.error("--archive, --toolchain, --wasmtime and --project are required")
    script = Path(__file__).resolve().parent / "build.py"
    with tempfile.TemporaryDirectory(prefix="go-repro-") as temporary:
        root = Path(temporary)
        output = root / "main.wasm"
        command = [sys.executable, str(script), "--toolchain", str(args.toolchain.resolve()),
                   "--wasmtime", str(args.wasmtime.resolve()), "--project",
                   str(args.project.resolve()), "--output", str(output),
                   "--cache", str(root / "wasmtime-cache"),
                   "--build-cache", str(root / "build-cache")]
        result = json.loads(subprocess.check_output(command, text=True))
        print(json.dumps({"host": platform.system(),
                          "toolchain_archive_sha256": sha(args.archive),
                          "source_sha256": project_digest(args.project),
                          "configuration": {"GOOS": "wasip1", "GOARCH": "wasm",
                                            "compiler_flags": []},
                          "output_sha256": result["sha256"],
                          "output_bytes": result["output_bytes"]}, indent=2))


if __name__ == "__main__":
    main()
