#!/usr/bin/env python3
"""Compile AssemblyScript using the upstream Wasm compiler and backend."""

import argparse
import sys
from src.runtime import AssemblyScript, wasmtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-O", "--optimize", type=int, choices=range(4), default=0)
    parser.add_argument(
        "--runtime", choices=["stub", "minimal", "incremental"], default="incremental"
    )
    args = parser.parse_args()
    try:
        data = AssemblyScript().compile(
            args.source, args.output, args.optimize, args.runtime
        )
        print(f"{args.output}: {len(data)} bytes")
    except (
        OSError,
        ValueError,
        RuntimeError,
        wasmtime.WasmtimeError,
        wasmtime.Trap,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
