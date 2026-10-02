"""Who is allowed to activate a lesson, and whether they may be its author."""

from __future__ import annotations

import os
from dataclasses import dataclass

from commontrace import lesson_io, paths

POLICY_SINGLE = "single"
POLICY_TWO_PERSON = "two-person"
POLICIES = (POLICY_SINGLE, POLICY_TWO_PERSON)

AGENT_ACTOR_PREFIX = "mcp:"

POLICY_FILENAME = "approval-policy.yaml"


class ApprovalDenied(PermissionError):
    """This approver may not activate this lesson under the store's policy."""


class PolicyError(ValueError):
    ...


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = POLICY_SINGLE
    require_human: bool = False
    auto_approve_drafts: bool = False

    @property
    def separation_required(self) -> bool:
        return self.mode == POLICY_TWO_PERSON


ALLOWED_KEYS = frozenset({"mode", "require_human", "auto_approve_drafts"})
AUTO_APPROVE_MIN_HOLDOUT = 0.05
AUTO_APPROVER = "auto-approve"
VALID_BOOL_STRINGS = frozenset({"true", "yes", "1", "false", "no", "0"})
TRUE_BOOL_STRINGS = frozenset({"true", "yes", "1"})


def policy_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), POLICY_FILENAME)


def validate_policy(raw: dict, path: str = "approval-policy.yaml") -> None:
    """Validate parsed policy dictionary strictly to prevent fail-open security bypass."""
    unknown = set(raw.keys()) - ALLOWED_KEYS
    if unknown:
        raise PolicyError(f"{path}: unrecognized policy key(s): {', '.join(sorted(unknown))}")

    if "mode" in raw:
        mode = str(raw["mode"]).strip().lower()
        if mode not in POLICIES:
            raise PolicyError(f"{path}: mode must be one of {', '.join(POLICIES)}, got {mode!r}")

    for key in ("require_human", "auto_approve_drafts"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, str):
            if value.strip().lower() not in VALID_BOOL_STRINGS:
                raise PolicyError(f"{path}: {key} must be a boolean (true/false), got {value!r}")
        elif not isinstance(value, bool):
            raise PolicyError(
                f"{path}: {key} must be a boolean (true/false), got {type(value).__name__}"
            )

    if _as_bool(raw.get("require_human", False)) and _as_bool(raw.get("auto_approve_drafts", False)):
        raise PolicyError(
            f"{path}: auto_approve_drafts and require_human contradict each other -- "
            "auto-approval activates a lesson with no person involved."
        )


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in TRUE_BOOL_STRINGS
    return bool(value)


def load_policy(root: str) -> ApprovalPolicy:
    """Read the store's policy, defaulting to today's behaviour."""
    path = policy_path(root)
    if not os.path.isfile(path):
        return ApprovalPolicy()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = _parse_policy_yaml(fh.read())
    except OSError as exc:
        raise PolicyError(f"could not read {path}: {exc}") from exc

    validate_policy(raw, path)

    mode = str(raw.get("mode", POLICY_SINGLE)).strip().lower()
    return ApprovalPolicy(
        mode=mode,
        require_human=_as_bool(raw.get("require_human", False)),
        auto_approve_drafts=_as_bool(raw.get("auto_approve_drafts", False)),
    )


def _parse_policy_yaml(text: str) -> dict:
    out: dict = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise PolicyError(
                f"line {lineno}: expected `key: value`, got {line.strip()!r}"
            )
        key, _, value = stripped.partition(":")
        clean_key = key.strip().strip("'\"")
        if not clean_key:
            raise PolicyError(f"line {lineno}: empty key in policy file")
        if clean_key in out:
            raise PolicyError(f"line {lineno}: duplicate key {clean_key!r} in policy file")
        out[clean_key] = value.strip().strip("'\"")
    return out


def is_agent(actor: str) -> bool:
    return str(actor or "").startswith(AGENT_ACTOR_PREFIX)


def authors_of(root: str, slug: str) -> tuple[str, ...]:
    seen: list[str] = []
    for record in lesson_io.history(root, slug):
        actor = str(record.get("actor") or "").strip()
        if actor and actor != "unknown" and actor not in seen:
            seen.append(actor)
    return tuple(seen)


def check(
    policy: ApprovalPolicy, *, slug: str, approver: str, authors: tuple[str, ...]
) -> None:
    """Raise `ApprovalDenied` if this approval does not satisfy the policy."""
    approver = str(approver or "").strip()

    if policy.require_human and is_agent(approver):
        raise ApprovalDenied(
            f"this store requires a human approval and {approver!r} is an agent "
            "actor. An agent may draft and propose a lesson; activating it -- which "
            "injects it into every later retrieval -- needs a person. Approve with "
            f"`commontrace lesson approve {slug}` as the reviewer."
        )

    if not policy.separation_required:
        return

    if not authors:
        raise ApprovalDenied(
            f"this store requires separation of duties, and {slug!r} has no recorded "
            "authorship to separate from: nothing journaled a content change for it "
            "(commontrace/lesson_io.py). A lesson that appeared with no provenance is "
            "exactly the case this policy should decline to certify -- edit it through "
            "`commontrace lesson edit` or `draft_lesson` so the change is recorded, "
            "then have a second reviewer approve."
        )

    if approver in authors:
        raise ApprovalDenied(
            f"this store requires separation of duties: {approver!r} wrote "
            f"{slug!r} and cannot also approve it. An active lesson is injected into "
            "every later retrieval verbatim, and the value of a second judgement is "
            "that it is independent. Recorded authors: " + ", ".join(authors) + "."
        )
