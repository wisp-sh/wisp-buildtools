#!/usr/bin/env python3
"""Stage the pinned local Go WIT runtime into a project without fetching modules."""

import argparse
from pathlib import Path

from wasmbuild.wit_runtime import SUPPORT_ROOT, stage_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=SUPPORT_ROOT / "wit-runtime")
    args = parser.parse_args()
    stage_runtime(args.project.resolve(strict=True), args.runtime.resolve(strict=True))


if __name__ == "__main__":
    main()
