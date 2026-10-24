#!/usr/bin/env python3
"""Bootstrap the pinned Java frontend; application compilation uses the Wasm JVM."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("backend_bootstrap", ROOT.parent / "teavm/bootstrap.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify the existing distribution without rebuilding")
    args = parser.parse_args()
    if args.check:
        spec = importlib.util.spec_from_file_location("java_integrity", ROOT / "compile.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        _, manifest = helper.verified_dist()
        sources = [ROOT / "bootstrap.py", ROOT / "src/EcjMain.java", ROOT / "toolchain.lock.json"]
        expected = {str(p.relative_to(ROOT)): backend.digest(p) for p in sources}
        if manifest.get("bootstrapInputs") != expected:
            raise ValueError("Java bootstrap sources changed; rebuild the distribution")
        print("Java distribution integrity verified")
        return
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    compiler = backend.fetch(lock["compiler"], ROOT / ".cache/ecj-3.46.100.jar", args.offline)
    license_file = backend.fetch(lock["license"], ROOT / ".cache/EPL-2.0.txt", args.offline)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as directory:
        dist = Path(directory) / "dist"
        (dist / "launcher").mkdir(parents=True)
        (dist / "licenses").mkdir()
        removed = []
        with zipfile.ZipFile(compiler) as original, zipfile.ZipFile(
                dist / "ecj.jar", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for name in sorted(original.namelist()):
                # The distribution manifest verifies the transformed JAR. Keep
                # its integrity separate from upstream's signed Maven artifact.
                if name.startswith("META-INF/") and name.rsplit(".", 1)[-1].upper() in {"SF", "RSA", "DSA", "EC"}:
                    removed.append(name)
                    continue
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                output.writestr(entry, original.read(name))
            (dist / "messages.properties").write_bytes(original.read(
                "org/eclipse/jdt/internal/compiler/problem/messages.properties"))
            (dist / "licenses/ECJ-about.html").write_bytes(original.read("about.html"))
        shutil.copyfile(license_file, dist / "licenses/EPL-2.0.txt")
        subprocess.run([os.environ.get("WISP_BOOTSTRAP_JAVA", "java"), "com.sun.tools.javac.Main",
                        "-source", "17", "-target", "17", "-cp", str(compiler),
                        "-d", str(dist / "launcher"), str(ROOT / "src/EcjMain.java")], check=True)
        bootstrap_java = subprocess.check_output([os.environ.get("WISP_BOOTSTRAP_JAVA", "java"), "-version"],
                                                  stderr=subprocess.STDOUT, text=True).strip()
        inputs = [ROOT / "bootstrap.py", ROOT / "src/EcjMain.java", ROOT / "toolchain.lock.json"]
        manifest = {"schemaVersion": 1, "toolchain": "java", "version": "3.46.100",
                    "javaSourceLevel": "21", "runtimeDependencies": ["jvm", "teavm"],
                    "sourceCompilationWasmHosted": True, "nodeRequired": False,
                    "upstream": lock, "removedSignatureSidecars": removed,
                    "bootstrapJava": bootstrap_java,
                    "bootstrapInputs": {str(p.relative_to(ROOT)): backend.digest(p) for p in inputs},
                    "sha256": {str(p.relative_to(dist)): backend.digest(p)
                               for p in sorted(dist.rglob("*")) if p.is_file()}}
        (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        previous = ROOT / ".dist-previous"
        if previous.exists():
            shutil.rmtree(previous)
        if (ROOT / "dist").exists():
            (ROOT / "dist").rename(previous)
        dist.rename(ROOT / "dist")
        if previous.exists():
            shutil.rmtree(previous)
    print(f"ECJ 3.46.100 Java 21 frontend: {ROOT / 'dist'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"java bootstrap: {error}")
