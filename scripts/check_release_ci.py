"""Refuse publication unless the newest trusted CI run passed for this exact commit."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        if fp is not None:
            with contextlib.suppress(OSError):
                fp.close()
        raise ValueError("release CI lookup refused an API redirect")


def successful_run(payload: object, repository: str, sha: str) -> int:
    if not isinstance(payload, dict):
        raise ValueError("release CI lookup returned an invalid response")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("release CI lookup returned an invalid run list")
    candidates = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == sha
        and isinstance(run.get("head_repository"), dict)
        and run.get("head_repository", {}).get("full_name") == repository
        and run.get("event") in ("push", "workflow_dispatch")
        and isinstance(run.get("path"), str)
        and run["path"].split("@", 1)[0] == ".github/workflows/ci.yml"
    ]
    if not candidates:
        raise ValueError("no trusted CI run found for the release commit; run CI for this exact commit first")
    if any(type(run.get("id")) is not int or run["id"] <= 0 for run in candidates):
        raise ValueError("release CI lookup returned an invalid run identifier")
    latest = max(candidates, key=lambda run: run["id"])
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("the newest CI run for the release commit has not passed; finish or rerun CI first")
    return int(latest["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    try:
        repository, sha = os.environ["GITHUB_REPOSITORY"], args.sha
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("release CI lookup requires a repository and full commit SHA")
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        parsed = urllib.parse.urlsplit(api)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("release CI API must be an HTTPS origin without credentials")
        query = urllib.parse.urlencode({"head_sha": sha, "per_page": 100})
        request = urllib.request.Request(
            f"{api}/repos/{repository}/actions/workflows/ci.yml/runs?{query}",
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("release CI response exceeded 2 MiB")
        run_id = successful_run(json.loads(raw), repository, sha)
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            with contextlib.suppress(OSError):
                exc.close()
        # Never print API bodies, headers or credentials from an error response.
        print(f"release CI gate refused publication: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Release commit has successful CI evidence: run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
