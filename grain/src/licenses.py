#!/usr/bin/env python3
"""Collect checksum-pinned notices for libraries in upstream Grain's compiler.

These are data-only bootstrap inputs. They are copied to the package; none of
the upstream projects or package scripts are executed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

# Versions are those resolved by Grain 0.7.2's compiler/esy.lock/index.json.
# Related runtime and generated-code libraries retain their own notices.
GITHUB = [
    ("OCaml-5.2.0", "ocaml/ocaml", "5.2.0", "LICENSE", 27179,
     "65f47d3bf5a921011ac7fa30f23d54f9dffbe7683921eeb116d5ce8dc26ff0c1"),
    ("js_of_ocaml-6.0.1", "ocsigen/js_of_ocaml", "6.0.1", "LICENSE", 27823,
     "01992ab570302874fc161b3db261e77af8453ab71a9f553f403c464b73668ae2"),
    ("Binaryen-124", "WebAssembly/binaryen", "version_124", "LICENSE", 11535,
     "64c0d02491e16eced74826440ecd2bcf7722d37fa586faf457b2494293afbffe"),
    ("binaryen.ml-0.34.0", "grain-lang/binaryen.ml", "v0.34.0", "LICENSE", 10173,
     "a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9"),
    ("Cmdliner-1.3.0", "dbuenzli/cmdliner", "v1.3.0", "LICENSE.md", 742,
     "deb8445a08c3a4fe46462e3b58f863782476d45bc762ca386a4c74bc01ef02c8"),
    ("Dune-build-info-3.20.2", "ocaml/dune", "3.20.2", "LICENSE.md", 1111,
     "7a9971a9f7b19a18568a7a2758573a989830947969506f5a59b657bbfc17abca"),
    ("Yojson-2.1.1", "ocaml-community/yojson", "2.1.1", "LICENSE.md", 1504,
     "cc8efe2cc2af7b49bbf61f9ab99e15a7cfec5272367423e478da628cd9450ab7"),
    ("Sexplib-v0.17.0", "janestreet/sexplib", "v0.17.0", "LICENSE.md", 1126,
     "169e74f8409c6ad52676a16aacc0d8fb4cfa8738032af834c8a1b5305b4bcba9"),
    ("Sexplib0-v0.17.0", "janestreet/sexplib0", "v0.17.0", "LICENSE.md", 1126,
     "169e74f8409c6ad52676a16aacc0d8fb4cfa8738032af834c8a1b5305b4bcba9"),
    ("Parsexp-v0.17.0", "janestreet/parsexp", "v0.17.0", "LICENSE.md", 1126,
     "32c30525f61e707562867e232749a0ebd70ad9e6af989fe0aa0b10ec83addbed"),
    ("Base-v0.17.3", "janestreet/base", "v0.17.3", "LICENSE.md", 1126,
     "32c30525f61e707562867e232749a0ebd70ad9e6af989fe0aa0b10ec83addbed"),
    ("OCaml-intrinsics-v0.17.1", "janestreet/ocaml_intrinsics_kernel", "v0.17.1", "LICENSE.md", 1126,
     "1a28723500e43570896146ec235d4b11f70987ab4ef665a273b60207ebc73eea"),
    ("ppx_sexp_conv-v0.17.0", "janestreet/ppx_sexp_conv", "v0.17.0", "LICENSE.md", 1126,
     "83894c3960cc372e21c99892557109a8327b41c721b8e1124d80cde3ca590d07"),
    ("ppx_deriving_yojson-3.9.1", "ocaml-ppx/ppx_deriving_yojson", "v3.9.1", "LICENSE.txt", 1088,
     "b6dd9979cfea20886b55d1cd956b9bdb40c205e10c525f4550b46e641175dee9"),
    ("ppx_deriving-5.2.1", "ocaml-ppx/ppx_deriving", "v5.2.1", "LICENSE.txt", 1088,
     "30692750dc39d5026a367f95c05f68853c58cf560051076c49e911438386fc48"),
    ("OCamlgraph-2.2.0", "backtracking/ocamlgraph", "2.2.0", "LICENSE", 27534,
     "e9684ff81666d63a1746f6b3f0d904db100d092fb4f15032ea4fdc0b89762e4d"),
    ("Sedlex-3.7", "ocaml-community/sedlex", "v3.7", "LICENSE", 1096,
     "5a8ddcf4cd0373cf5d9da94ffdadb78ce8e66b454fa5a6bf6f844cdb224e2de9"),
    ("Reason-native-fp-fs-utf8", "reasonml/reason-native", "20b1997b6451d9715dfdbeec86a9d274c7430ed8", "LICENSE", 1086,
     "52412d7bc7ce4157ea628bbaacb8829e0a9cb3c58f57f99176126bc8cf2bfc85"),
    ("Num-1.5", "ocaml/num", "v1.5", "LICENSE", 27256,
     "fbf788740f750a077bb91a2afc8faf5a822e7177403abba424a019d038412968"),
]

INPUTS = [
    {"name": name, "url": f"https://raw.githubusercontent.com/{repository}/{revision}/{path}",
     "size": size, "sha256": sha256}
    for name, repository, revision, path, size, sha256 in GITHUB
] + [
    {"name": "Menhir-20240715",
     "url": "https://gitlab.inria.fr/fpottier/menhir/-/raw/20240715/LICENSE",
     "size": 45483, "sha256": "9c97527b92b577647880abd2a5eaf5bf0f3b3df1bd09d9f93fc4e103eb1d30d5"},
]


def verify(data: bytes, pin: dict) -> None:
    if len(data) != pin["size"] or hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError(f"license input does not match pinned SHA-256: {pin['name']}")


def obtain(pin: dict, offline: bool) -> tuple[dict, bytes]:
    cache = ROOT / ".cache" / "licenses" / (pin["name"] + ".txt")
    if cache.is_file():
        data = cache.read_bytes()
        verify(data, pin)
        return pin, data
    if offline:
        raise ValueError(f"offline bootstrap needs the cached {pin['name']} license")
    request = urllib.request.Request(pin["url"], headers={"User-Agent": "wisp-buildtools"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(pin["size"] + 1)
    verify(data, pin)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".license-", dir=cache.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(data)
    try:
        os.replace(temporary, cache)
    finally:
        temporary.unlink(missing_ok=True)
    return pin, data


def main(destination: Path | None = None, *, offline: bool = False) -> list[dict]:
    destination = destination or ROOT / "dist" / "licenses"
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        for pin, data in executor.map(lambda value: obtain(value, offline), INPUTS):
            filename = pin["name"] + ".txt"
            (destination / filename).write_bytes(data)
            records.append({"path": filename, **pin})
    (destination / "THIRD-PARTY.json").write_text(json.dumps({
        "schemaVersion": 1,
        "scope": "Notices for upstream Grain compiler libraries, including related runtime libraries and generated-code support",
        "grainVersion": "0.7.2",
        "sourceLock": "https://github.com/grain-lang/grain/blob/grain-v0.7.2/compiler/esy.lock/index.json",
        "files": records,
    }, indent=2) + "\n")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    records = main(arguments.output, offline=arguments.offline)
    print(f"Collected {len(records)} verified upstream license notices")
