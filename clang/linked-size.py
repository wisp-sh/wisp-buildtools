#!/usr/bin/env python3
"""Summarize actual linked bytes attributed to archive members in wasm-ld maps."""
import collections
import json
import re
import sys
from pathlib import Path

ROW = re.compile(r"^\s*[0-9a-f-]+\s+[0-9a-f]+\s+([0-9a-f]+)\s+\S*?\s*(lib/lib[^ (]+\.a)\(([^)]+)\):")


def summarize(path):
    libraries = collections.Counter()
    members = collections.Counter()
    for line in Path(path).open():
        match = ROW.match(line)
        if match:
            size = int(match.group(1), 16)
            library, member = match.group(2, 3)
            libraries[library] += size
            members[f"{library}({member})"] += size
    return {"attributed_bytes": sum(libraries.values()),
            "libraries": dict(libraries.most_common()),
            "members": dict(members.most_common())}


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: linked-size.py ORIGINAL_MAP DRIVERLESS_MAP OUTPUT_JSON")
    old, new = summarize(sys.argv[1]), summarize(sys.argv[2])
    difference = {k: old["libraries"].get(k, 0) - new["libraries"].get(k, 0)
                  for k in old["libraries"].keys() | new["libraries"].keys()}
    report = {"original": old, "driverless": new,
              "removed_bytes_by_library": dict(sorted(difference.items(),
                                                     key=lambda item: -item[1]))}
    Path(sys.argv[3]).write_text(json.dumps(report, indent=2) + "\n")
    for name, data in [("original", old), ("driverless", new)]:
        print(name, data["attributed_bytes"])
        for library, size in list(data["libraries"].items())[:20]:
            print(f"  {library}: {size}")
    print("largest reductions")
    for library, size in list(report["removed_bytes_by_library"].items())[:20]:
        print(f"  {library}: {size}")


if __name__ == "__main__":
    main()
