"""GitHub: pull request and push events -> signals, for coding agents.

Built from GitHub's published documentation:
  * signature (docs.github.com .../validating-webhook-deliveries):
    `X-Hub-Signature-256` is `sha256=` + hex HMAC-SHA256(secret, raw body),
    compared in constant time. GitHub signs no timestamp, so replay protection
    is `X-GitHub-Delivery` (a GUID, documented in webhook-events-and-payloads),
    recorded in the ledger; a delivery without it is refused.
  * payloads (the vendor's own examples, kept as test fixtures): `pull_request`
    with `action`, `number`, `pull_request.merged`, `pull_request.merge_commit_sha`,
    `repository.full_name`; `push` with `commits[].message`.
  * a revert is recognised by git's own documented message,
    `This reverts commit <full sha>.` (git-revert), on a pushed commit whose
    sha is a merged PR's `merge_commit_sha`. A revert made another way is not seen.

What it emits, for the occasion `<occasion_prefix><PR number>`:
  pull_request closed, merged       CANDIDATE   (matures after window_days;
                                                 14 for the coding kit)
  pull_request closed, not merged   FAILURE
  push containing a revert of a     REVERSAL    (by the merge commit sha)
    merged PR's merge commit
Everything else is acknowledged and ignored. `repository` in the config limits
a connector to one repo, which matters because PR numbers repeat across repos.
CI results are not read here: a failing CI that still merged is the team's call.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import datetime, timezone

from hub.connectors import base

name = "github"

_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})\b")


def validate_config(config: object) -> dict:
    out = base.common_config(config, allowed={"repository"})
    repo = out.get("repository", "")
    if repo and (not isinstance(repo, str) or repo.count("/") != 1):
        raise base.ConfigError("repository must look like owner/name")
    return out


def verify(headers: Mapping[str, str], body: bytes, secret: str, *, now: datetime) -> None:
    supplied = base.header(headers, "x-hub-signature-256")
    if not supplied.startswith("sha256="):
        raise base.SignatureError("missing or malformed X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not base.constant_time_equal(expected, supplied):
        raise base.SignatureError("GitHub signature does not match")


def delivery_id(headers: Mapping[str, str], payload: dict) -> str:
    value = base.header(headers, "x-github-delivery").strip()
    if not value or len(value) > base.MAX_ID_CHARS:
        raise base.SignatureError("missing X-GitHub-Delivery, so no replay protection", status=400)
    return value


def signals(headers: Mapping[str, str], payload: dict, config: dict) -> list[base.Signal]:
    event = base.header(headers, "x-github-event")
    repository = payload.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else ""
    wanted = config.get("repository", "")
    if wanted and repo != wanted:
        return []
    event_id = delivery_id(headers, payload)
    now = datetime.now(timezone.utc)
    prefix = config.get("occasion_prefix", "")

    if event == "pull_request" and payload.get("action") == "closed":
        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            raise ValueError("pull_request must be an object")
        occasion = prefix + base.clean_id(payload.get("number", pr.get("number")), "pull request number")
        stamp = pr.get("merged_at") or pr.get("closed_at")
        at = base.parse_timestamp(stamp) if stamp else now
        if pr.get("merged") is True:
            return [base.Signal(base.CANDIDATE, occasion, at, event_id,
                                ref=str(pr.get("merge_commit_sha") or ""), note="merged")]
        return [base.Signal(base.FAILURE, occasion, at, event_id, note="closed without merging")]

    if event == "push":
        out = []
        commits = payload.get("commits")
        for commit in commits if isinstance(commits, list) else []:
            message = commit.get("message") if isinstance(commit, dict) else ""
            for sha in _REVERTS.findall(str(message or "")):
                out.append(base.Signal(base.REVERSAL, "", now, event_id, ref=sha,
                                       note=f"reverts {sha[:12]}"))
        return out
    return []
