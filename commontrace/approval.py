"""Who is allowed to activate a lesson, and whether they may be its author.

WHY THIS EXISTS
---------------
Activating a lesson is the one action in this product with fleet-wide blast
radius: an `active` lesson is injected into every later retrieval, verbatim,
into decisions nobody reviews individually. protocol/PROTOCOL.md puts a
Validator between proposing and activating precisely for that reason, and
README.md says the gate is real.

It was real in one sense and not in another. `approve_lesson` refused
scaffolding, refused a schema violation, and recorded who approved -- but
the same actor that drafted a lesson could approve it a second later, so
the "second, independent judgement" the Validator role exists to supply was
optional in exactly the case where it matters: an agent curating its own
output, unattended, at machine speed. Recording an agent-approved lesson as
agent-approved makes that auditable after the fact. It does not make it
reviewed.

This module makes the approval rule a POLICY the store states, rather than
a property of whichever code path happened to call `write_lesson`:

    single      Anyone may approve, including the lesson's own author.
                The behaviour this product shipped with, and still the
                default, so nothing changes for an existing store.
    two-person  The approver must not appear among the actors who wrote the
                lesson's content. Separation of duties, enforced rather
                than documented.

plus an orthogonal switch:

    require_human  An approval recorded by an agent actor (`mcp:...`) does
                   not satisfy the gate at all, whoever authored the
                   content. For a fleet where an agent may draft and
                   propose but a person must sign.

WHAT "AUTHOR" MEANS, AND WHY IT IS READ FROM THE JOURNAL
--------------------------------------------------------
`commontrace/lesson_io.py` already journals every content change with the
actor that made it, precisely so a later reader can tell an agent's edit
from a person's. That journal is the only record of authorship this product
has, so it is the one this reads: the authors of a lesson are the actors of
every journaled change to it up to the moment of approval.

Two consequences worth stating, because both are deliberate:

  * An actor who only *renamed a tag* counts as an author. Conservative on
    purpose: a rule that has to decide which edits were "substantive"
    becomes a rule an author can route around by making their real change
    look trivial.

  * A lesson with NO journal history has no known author, and under
    `two-person` that is a refusal, not a pass. A lesson that appeared on
    disk with no recorded provenance is exactly the case where a
    separation-of-duties rule should decline to certify anything -- it
    cannot tell whether the approver wrote it.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not authenticate anybody. The local tier has no identity system
(commontrace/mcp_server.py's docstring is explicit that its trust boundary
is the OS), so an actor string is an attribution, not a proof, and a person
determined to approve their own work can set `--approved-by` to anything.
That is the same honesty the revision journal already states about itself.
What this changes is the DEFAULT path: with `two-person` set, the ordinary
way to approve your own lesson stops silently working, which is what a
control is for. A store that needs cryptographic approver identity needs
the Hub's authenticated surface, and that is a different (documented) gap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from commontrace import lesson_io, paths

POLICY_SINGLE = "single"
POLICY_TWO_PERSON = "two-person"
POLICIES = (POLICY_SINGLE, POLICY_TWO_PERSON)

#: Actors recorded by the MCP surface (commontrace/mcp_server.py:_agent_actor).
AGENT_ACTOR_PREFIX = "mcp:"

POLICY_FILENAME = "approval-policy.yaml"


class ApprovalDenied(PermissionError):
    """This approver may not activate this lesson under the store's policy."""


class PolicyError(ValueError):
    """The store's approval policy file cannot be understood. Loud on
    purpose: a malformed policy that silently fell back to `single` would
    disable a control the operator believes is on, which is the one failure
    mode a security setting must not have."""


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = POLICY_SINGLE
    require_human: bool = False

    @property
    def separation_required(self) -> bool:
        return self.mode == POLICY_TWO_PERSON


def policy_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), POLICY_FILENAME)


def load_policy(root: str) -> ApprovalPolicy:
    """Read the store's policy, defaulting to today's behaviour.

    No file means `single` with no human requirement -- exactly what every
    existing store does now, so this module changes nothing until an
    operator writes the file.
    """
    path = policy_path(root)
    if not os.path.isfile(path):
        return ApprovalPolicy()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = _parse_policy_yaml(fh.read())
    except OSError as exc:
        raise PolicyError(f"could not read {path}: {exc}") from exc

    mode = str(raw.get("mode", POLICY_SINGLE)).strip().lower()
    if mode not in POLICIES:
        raise PolicyError(
            f"{path}: mode must be one of {', '.join(POLICIES)}, got {mode!r}"
        )
    require_human = raw.get("require_human", False)
    if isinstance(require_human, str):
        require_human = require_human.strip().lower() in ("true", "yes", "1")
    return ApprovalPolicy(mode=mode, require_human=bool(require_human))


def _parse_policy_yaml(text: str) -> dict:
    """Two scalar keys, parsed without a YAML dependency.

    `commontrace` installs with PyYAML, so this could use it -- but the
    policy file is two `key: value` lines by design, and keeping the parser
    here means a malformed file produces a message about the approval
    policy rather than a YAML traceback the operator has to interpret.
    """
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
        out[key.strip()] = value.strip().strip("'\"")
    return out


def is_agent(actor: str) -> bool:
    return str(actor or "").startswith(AGENT_ACTOR_PREFIX)


def authors_of(root: str, slug: str) -> tuple[str, ...]:
    """Every actor that has journaled a content change to `slug`, oldest
    first, deduplicated. See the module docstring for why this counts every
    edit rather than trying to judge which ones were substantive."""
    seen: list[str] = []
    for record in lesson_io.history(root, slug):
        actor = str(record.get("actor") or "").strip()
        if actor and actor != "unknown" and actor not in seen:
            seen.append(actor)
    return tuple(seen)


def check(
    policy: ApprovalPolicy, *, slug: str, approver: str, authors: tuple[str, ...]
) -> None:
    """Raise `ApprovalDenied` if this approval does not satisfy the policy.

    Returns None on success so callers read as `approval.check(...)` before
    the state change, in the same shape as the scaffolding and schema gates
    that already guard activation.
    """
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
