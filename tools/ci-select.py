#!/usr/bin/env python3
"""Select compiler CI jobs from changed inputs and their consumers."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
LLVM_TOOLCHAINS = frozenset({"clang", "llvm", "zig", "nim", "ocaml", "goose", "teavm", "java", "kotlin"})
RUST_TOOLCHAINS = frozenset({"rust", "llvm-rust"})


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def revision(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def manifest_at(root: Path, ref: str) -> dict:
    manifest = json.loads(git(root, "show", ref + ":buildtools.json"))
    tools = manifest.get("tools")
    if manifest.get("schema") != 1 or not isinstance(tools, dict) or not tools:
        raise ValueError("unsupported or empty buildtools manifest")
    if any(not re.fullmatch(r"[a-z0-9-]+", name) for name in tools):
        raise ValueError("invalid toolchain name in buildtools manifest")
    return manifest


def matches(path: str, pattern: str) -> bool:
    prefix = pattern.rstrip("/")
    return path == prefix or path.startswith(prefix + "/") or fnmatch.fnmatchcase(path, pattern)


def inputs(item: dict) -> list[str]:
    package = item.get("package", {})
    return [item["directory"], *item.get("sourceInputs", []), *item.get("artifacts", []),
            *package.get("include", []), *package.get("optional", [])]


def documentation(path: str) -> bool:
    # Tool directories and explicit package inputs are checked before this rule.
    if path.startswith("docs/"):
        return True
    suffix = Path(path).suffix.lower()
    return "/" not in path and (suffix in {".md", ".rst"}
                                or (suffix in {"", ".txt"}
                                    and path.upper().startswith(("LICENSE", "COPYING", "NOTICE"))))


def select_toolchains(root: Path, *, base: str | None, head: str = "HEAD",
                      merge_base: bool = False) -> dict:
    head = revision(root, head)
    current = manifest_at(root, head)
    tools = current["tools"]
    report = {"schema": 1, "base": None, "head": head, "changedFiles": [],
              "toolchains": [], "reasons": {}}

    def all_tools(reason: str) -> dict:
        report["toolchains"] = list(tools)
        report["reasons"] = {name: [reason] for name in tools}
        return report

    if not base or set(base) == {"0"}:
        return all_tools("No previous revision; build every manifest target")
    try:
        base = revision(root, base)
        if merge_base:
            base = git(root, "merge-base", base, head).decode().strip()
        report["base"] = base
        previous = manifest_at(root, base)
        paths = [os.fsdecode(path) for path in
                 git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--").split(b"\0") if path]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return all_tools("Previous revision or manifest unavailable; build every manifest target")
    report["changedFiles"] = paths
    old_tools = previous["tools"]
    reasons: dict[str, list[str]] = {}

    def mark(name: str, reason: str) -> None:
        if reason not in reasons.setdefault(name, []):
            reasons[name].append(reason)

    if {key: value for key, value in previous.items() if key != "tools"} != {
            key: value for key, value in current.items() if key != "tools"}:
        return all_tools("Shared buildtools manifest settings changed")
    for name in sorted(old_tools.keys() | tools.keys()):
        if old_tools.get(name) != tools.get(name):
            mark(name, "Toolchain manifest entry changed")

    for path in paths:
        if path == "buildtools.json":
            continue  # Compare parsed entries, so formatting alone does not rebuild compilers.
        # These files affect the runner or selector even if a package also includes them.
        if path.startswith((".github/workflows/", "tools/")) or ("/" not in path and not documentation(path)):
            return all_tools("Shared CI or bootstrap input changed: " + path)
        owners = set()
        for manifest in (previous, current):
            for name, item in manifest["tools"].items():
                if any(matches(path, pattern) for pattern in inputs(item)):
                    owners.add(name)
        if owners:
            for name in sorted(owners):
                mark(name, "Input changed: " + path)
        elif not documentation(path):
            # Unknown files are conservative. New shared helpers must not silently skip CI.
            return all_tools("Shared or unclassified input changed: " + path)

    # Build and runtime edges can form cycles together, e.g. clang <-> llvm.
    # Reach consumers to a fixed point and retain old edges for removed inputs/tools.
    pending = list(reasons)
    while pending:
        dependency = pending.pop()
        for manifest in (previous, current):
            for name, item in manifest["tools"].items():
                if dependency in [*item.get("dependencies", []), *item.get("runtimeDependencies", [])]:
                    first = name not in reasons
                    mark(name, "Depends on changed toolchain: " + dependency)
                    if first:
                        pending.append(name)
    report["toolchains"] = [name for name in tools if name in reasons]
    report["reasons"] = {name: reasons[name] for name in report["toolchains"]}
    return report


def event_revisions(event_name: str, event: dict) -> tuple[str | None, str, bool]:
    if event_name == "push":
        return event.get("before"), event.get("after") or "HEAD", False
    if event_name == "pull_request":
        # Checkout tests the merge commit. Compare it with the PR base so unrelated
        # base-branch changes do not count as changes introduced by this PR.
        return event["pull_request"]["base"]["sha"], os.environ.get("GITHUB_SHA") or "HEAD", True
    return None, "HEAD", False


def write_github_outputs(selected: list[str]) -> None:
    groups = {"toolchains": selected,
              "llvm-toolchains": [name for name in selected if name in LLVM_TOOLCHAINS],
              "rust-toolchains": [name for name in selected if name in RUST_TOOLCHAINS],
              "other-toolchains": [name for name in selected
                                   if name not in LLVM_TOOLCHAINS | RUST_TOOLCHAINS]}
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            for name, tools in groups.items():
                output.write(name + "=" + json.dumps(tools) + "\n")
                output.write("has-" + name + "=" + str(bool(tools)).lower() + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", help="previous push revision or pull request base")
    parser.add_argument("--head", help="revision being tested, defaults to HEAD")
    parser.add_argument("--merge-base", action="store_true", help="compare from the common ancestor")
    args = parser.parse_args()
    base, head, merge_base = args.base, args.head or "HEAD", args.merge_base
    if args.base is None and args.head is None and os.environ.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        base, head, merge_base = event_revisions(os.environ.get("GITHUB_EVENT_NAME", ""), event)
    report = select_toolchains(args.root, base=base, head=head, merge_base=merge_base)
    selected = report["toolchains"]
    write_github_outputs(selected)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write("## Compiler build selection\n\n")
            summary.write("Selected targets: " + (", ".join("`" + name + "`" for name in selected)
                                                   or "none; compiler inputs did not change") + ".\n\n")
            summary.write("Input changes and selection reasons are in `build-selection.json` in the build plan artifact.\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
