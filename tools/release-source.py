#!/usr/bin/env python3
"""Verify an existing compiler build before resuming only its publication."""
import json
import os
import re
import subprocess


def validate(run, jobs, repository):
    if (run.get("path") != ".github/workflows/build.yml"
            or run.get("event") != "workflow_dispatch"
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("status") != "completed"
            or run.get("conclusion") not in ("success", "failure")
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("expected a completed compiler build in this repository")
    builds = [job for job in jobs if not job["name"].startswith("Publish toolchain release")]
    if (not any(job["name"] == "Select toolchains" and job["conclusion"] == "success" for job in builds)
            or not any(job["name"].startswith("Build ") and job["conclusion"] == "success" for job in builds)
            or any(job["status"] != "completed" or job["conclusion"] not in ("success", "skipped") for job in builds)):
        raise ValueError("every selected compiler build must have passed before publication")
    return run["head_sha"]


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["BUILD_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("invalid source build run ID")
    endpoint = f"repos/{repository}/actions/runs/{run_id}"
    run = json.loads(subprocess.check_output(["gh", "api", endpoint]))
    jobs = []
    page = 1
    while True:
        batch = json.loads(subprocess.check_output(["gh", "api", endpoint + f"/jobs?per_page=100&page={page}"]))["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 100:
            raise ValueError("source run has too many jobs")
    commit = validate(run, jobs, repository)
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"SOURCE_SHA={commit}\n")
    print(f"Publishing verified compiler artifacts from run {run_id}, commit {commit}")


if __name__ == "__main__":
    main()
