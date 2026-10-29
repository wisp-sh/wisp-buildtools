"""Run the binding splicer in Wasm and snapshot using the approved Wizer host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from toolchain import ROOT, digest, run_guest, wasmtime_path
from starlingmonkey_bootstrap import check, DEST


def initialization_environment(metadata, *, clocks=False):
    if metadata.get("schemaVersion") != 1:
        raise ValueError("unsupported StarlingMonkey binding metadata")
    exports, imports = metadata["exports"], metadata["imports"]
    env = {"SOURCE_NAME": "source.mjs", "DEBUG": "", "FEATURE_CLOCKS": "1" if clocks else "",
           "EXPORT_CNT": str(len(exports)), "IMPORT_CNT": str(len(imports))}
    for index, function in enumerate(exports):
        prefix = f"EXPORT{index}_"
        env[prefix + "NAME"] = function["name"]
        env[prefix + "ARGS"] = ("*" if function["paramptr"] else "") + ",".join(function["params"])
        env[prefix + "RET"] = ("*" if function["retptr"] else "") + (function["ret"] or "")
        env[prefix + "RETSIZE"] = str(function["retsize"])
    for index, (_, name, count) in enumerate(imports):
        env[f"IMPORT{index}_NAME"] = name
        env[f"IMPORT{index}_ARGCNT"] = str(count)
    return env


def compile_component(work, wit, world, *, stub_wasi, timeout, wasm_tool, adapter):
    manifest = check()
    # Pure worlds retain the same explicit stubbing option as QuickJS. Without
    # it, retain stdio, clocks and random; HTTP needs a separate host contract.
    features = [] if stub_wasi else ["stdio", "clocks", "random"]
    wit_args = ["--wit-path", "/wit/" + wit.name if wit.is_file() else "/wit"]
    if world:
        wit_args += ["--world-name", world]
    feature_args = [item for feature in features for item in ("--features", feature)]
    mounts = [(work, "/work"), (work / "wit-inputs", "/wit"), (DEST, "/engine")]
    run_guest([DEST / "splicer.wasm", "splice-bindings", "--input", "/engine/runtime.wasm",
               "--out-dir", "/work", *wit_args, *feature_args], mounts=mounts, timeout=timeout)
    metadata = json.loads((work / "bindings.json").read_text())
    env = initialization_environment(metadata, clocks="clocks" in features)
    wizer = Path(os.environ.get("WISP_WIZER_TOOLCHAIN", ROOT.parent / "wizer"))
    command = [sys.executable, str(wizer / "snapshot.py"), str(work / "component.wasm"),
               "-o", str(work / "snapshot.wasm"), "--init-func", "componentize.wizer",
               "--allow-wasi", "--mapdir", "/::" + str(work), "--timeout", str(int(timeout)),
               "--stdin", str(work / "initialize.txt")]
    for name, value in sorted(env.items()):
        command += ["--env", name + "=" + value]
    (work / "initialize.txt").write_text("--initializer-script-path /initializer.js --strip-path-prefix / source.mjs")
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout + 45)
    if result.returncode:
        raise ValueError("StarlingMonkey initialization failed:\n" + (result.stderr or result.stdout))
    receipt = json.loads((work / "snapshot.wasm.build.json").read_text())
    # check_init reports binding/export initialization errors that do not trap
    # during snapshotting. Execute it before publishing any application output.
    checked = subprocess.run([str(wasmtime_path()), "run", "-Ccache=n", "-Cparallel-compilation=n",
                              "-Oopt-level=0", "-Wunknown-imports-trap=y", "--invoke", "check_init",
                              str(work / "snapshot.wasm")], text=True, capture_output=True, timeout=timeout,
                             env={**os.environ, "RAYON_NUM_THREADS": "1"})
    if checked.returncode or checked.stdout.strip() != "0":
        raise ValueError("StarlingMonkey initialization failed: " + checked.stdout + checked.stderr)
    run_guest([DEST / "splicer.wasm", "stub-wasi", "--input", "/work/snapshot.wasm",
               "--output", "/work/linked.wasm", *wit_args, *feature_args], mounts=mounts, timeout=timeout)
    wasm_tool("component", "new", work / "linked.wasm", "--adapt",
              "wasi_snapshot_preview1=" + str(adapter), "-o", work / "candidate.wasm")
    return {"package": manifest, "snapshot": receipt, "bindingsSha256": digest(work / "bindings.json"),
            "initializerSha256": digest(work / "initializer.js"), "features": features}
