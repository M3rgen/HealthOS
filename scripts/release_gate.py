#!/usr/bin/env python3
"""Gate a release tag: format, project version, main ancestry and CI on that exact commit.

  release_gate.py preflight --tag vX.Y.Z[.N] --commit <sha> # needs a clone with origin/main
  release_gate.py ci --commit <sha>                       # needs GH_TOKEN and GH_REPO

Standard library only: it runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

TAG = re.compile(r"v(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(\.[1-9]\d*)?)")
WORKFLOW = "ci.yml"
REQUIRED_JOB = "summary"
WAIT_SECONDS = 100 * 60
POLL_SECONDS = 30.0

Fetch = Callable[[str, str], dict[str, Any]]


class GateError(Exception):
    """The release must not proceed; the message says why."""


def check_tag(tag: str, pyproject: str) -> None:
    """Accept a target or interim release tag whose version is the project's own version."""
    match = TAG.fullmatch(tag)
    if match is None:
        raise GateError(
            f"release tag {tag!r} is not vMAJOR.MINOR.PATCH or "
            "vMAJOR.MINOR.PATCH.N (interim, N ≥ 1)"
        )
    version = tomllib.loads(pyproject)["project"]["version"]
    if match["version"] != version:
        raise GateError(f"tag {tag} does not match project version {version}")


def check_on_main(commit: str, git: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    result = git("merge-base", "--is-ancestor", commit, "origin/main")
    if result.returncode != 0:
        raise GateError(f"commit {commit} is not on main: {result.stderr.strip()}")


def fetch_json(url: str, token: str) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https API
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def summary_passed(run: dict[str, Any], token: str, fetch: Fetch) -> bool:
    jobs = fetch(f"{run['url']}/jobs?per_page=100", token)["jobs"]
    return any(j["name"] == REQUIRED_JOB and j["conclusion"] == "success" for j in jobs)


def green_ci_run(
    *,
    api: str,
    repository: str,
    commit: str,
    token: str,
    fetch: Fetch = fetch_json,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = WAIT_SECONDS,
) -> dict[str, Any]:
    """Return a successful ci.yml push run on main for ``commit``; never pass without one."""
    query = urllib.parse.urlencode(
        {"head_sha": commit, "event": "push", "branch": "main", "per_page": 100}
    )
    url = f"{api}/repos/{repository}/actions/workflows/{WORKFLOW}/runs?{query}"
    deadline = now() + wait_seconds
    while True:
        runs = [run for run in fetch(url, token)["workflow_runs"] if run["head_sha"] == commit]
        for run in runs:
            if (
                run["status"] == "completed"
                and run["conclusion"] == "success"
                and summary_passed(run, token, fetch)
            ):
                return run
        pending = [run for run in runs if run["status"] != "completed"]
        if runs and not pending:
            details = ", ".join(f"{run['html_url']}: {run['conclusion']}" for run in runs)
            raise GateError(f"CI did not succeed for {commit}: {details}")
        if now() >= deadline:
            raise GateError(f"no successful CI run for {commit} within {wait_seconds:.0f} s")
        sleep(POLL_SECONDS)


def require_green_ci(**arguments: Any) -> str:
    """Return the URL of the green run; see ``green_ci_run``."""
    return green_ci_run(**arguments)["html_url"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--tag", required=True)
    preflight.add_argument("--commit", required=True)
    ci = commands.add_parser("ci")
    ci.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            check_tag(args.tag, Path("pyproject.toml").read_text(encoding="utf-8"))
            check_on_main(
                args.commit,
                lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=False),
            )
            print(f"release preflight passed: {args.tag} at {args.commit}")
        else:
            run = green_ci_run(
                api=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                repository=os.environ["GH_REPO"],
                commit=args.commit,
                token=os.environ.get("GH_TOKEN", ""),
                fetch=fetch_json,
                wait_seconds=float(os.environ.get("RELEASE_CI_WAIT_SECONDS", WAIT_SECONDS)),
            )
            print(f"CI gate passed: {run['html_url']}")
            if output := os.environ.get("GITHUB_OUTPUT"):
                # release.yml downloads this run's record of the smoke-tested image.
                with open(output, "a", encoding="utf-8") as stream:
                    print(f"run_id={run['id']}", file=stream)
    except GateError as error:
        print(f"release gate refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
