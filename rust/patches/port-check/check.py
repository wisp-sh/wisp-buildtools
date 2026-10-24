#!/usr/bin/env python3
"""Check the proc_macro bridge and Wasmi port without building LLVM/rustc.

This compiles development fixtures with native stage0. It is not application
compilation acceptance, a rustc macro integration test, or a Serde substitute.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, env: dict[str, str], cwd: Path) -> str:
    process = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=600)
    if process.returncode:
        raise RuntimeError(f"development port check failed: {command!r}\n{process.stdout}\n{process.stderr}")
    return process.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--wasmtime", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--patch", type=Path, default=HERE.parent / "0001-rust-1.83-wasm-proc-macro-bridge.patch")
    args = parser.parse_args()
    cargo = str(Path(args.cargo).resolve()) if "/" in args.cargo else args.cargo
    source, rustc, runtime, out = (path.resolve() for path in (args.source, args.rustc, args.wasmtime, args.out_dir))
    patch = args.patch.resolve()
    patch_digest = digest(patch)
    verify = ["patch", "--batch", "--fuzz=0", "--reverse", "--dry-run", "-p1", "-i", str(patch), "--directory", str(source)]
    subprocess.run(verify, check=True, capture_output=True, text=True, timeout=60)
    if out.exists() and any(out.iterdir()):
        parser.error("--out-dir must be fresh")
    out.mkdir(parents=True, exist_ok=True)
    (out / "src").mkdir()
    for name in ("Cargo.toml", "Cargo.lock", "macro_guest.rs"):
        shutil.copyfile(HERE / name, out / name)
    host = (HERE / "host.rs").read_text().replace("@SOURCE@", str(source))
    (out / "src/main.rs").write_text(host)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER")}
    environment.update(RUSTC_BOOTSTRAP="1", RUSTC=str(rustc), CARGO_PROFILE_DEV_DEBUG="0",
                       CARGO_TARGET_DIR=str(out / "target"))
    compiler_version = run([str(rustc), "--version"], env=environment, cwd=out).strip()
    bridge = source / "library/proc_macro/src/lib.rs"
    libraries = {}
    for name, target in (("native", None), ("guest", "wasm32-unknown-unknown"), ("host", "wasm32-wasip1")):
        library = out / f"libwisp_proc_macro_port.{name}.rlib"
        command = [str(rustc), "--crate-name", "wisp_proc_macro_port", "--crate-type", "rlib", "--edition", "2021"]
        if target:
            command += ["--target", target]
        run([*command, str(bridge), "-o", str(library)], env=environment, cwd=out)
        libraries[name] = library
    guest = out / "macro_guest.wasm"
    run([str(rustc), "--crate-name", "wisp_macro_guest", "--crate-type", "cdylib", "--edition", "2021",
         "--target", "wasm32-unknown-unknown", "--extern", "wisp_proc_macro_port=" + str(libraries["guest"]),
         str(out / "macro_guest.rs"), "-o", str(guest)], env=environment, cwd=out)
    outputs = {}
    for name, target in (("native", None), ("wasm", "wasm32-wasip1")):
        build_environment = dict(environment)
        build_environment["CARGO_ENCODED_RUSTFLAGS"] = "--extern\x1fwisp_proc_macro_port=" + str(libraries["native" if target is None else "host"])
        command = [cargo, "build", "--manifest-path", str(out / "Cargo.toml"), "--locked", "--jobs", "2"]
        if target:
            command += ["--target", target]
        run(command, env=build_environment, cwd=out)
        trace = out / (name + "-trace.jsonl")
        if target:
            program = out / "target" / target / "debug/wisp-rust-macro-port-check.wasm"
            command = [str(runtime), "run", "-Ccache=y", "--dir", str(out), "--env", "WISP_WASM_PROC_MACRO_LOG=" + str(trace),
                       str(program), str(guest)]
        else:
            program = out / "target/debug/wisp-rust-macro-port-check"
            command = [str(program), str(guest)]
            build_environment["WISP_WASM_PROC_MACRO_LOG"] = str(trace)
        outputs[name] = run(command, env=build_environment, cwd=out).strip()
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        invoked = {record["name"] for record in records if record["event"] == "invoke" and record["status"] == "succeeded"}
        if len(records) != 9 or invoked != {"identity", "identity_attr", "Identity"}:
            raise RuntimeError("interpreter trace does not prove all three macro clients ran")
    subprocess.run(verify, check=True, capture_output=True, text=True, timeout=60)
    if digest(patch) != patch_digest:
        raise RuntimeError("backport patch changed during the proof; rerun with the final patch")
    report = {"patchSha256": patch_digest, "kind": "development-bridge-proof", "applicationAcceptance": False, "compilerVersion": compiler_version,
              "guestSha256": digest(guest), "sourceLoaderSha256": digest(source / "compiler/rustc_metadata/src/wasm_proc_macro.rs"),
              "outputs": outputs}
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
