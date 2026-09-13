"""A public, unauthenticated place for an operator to state facts a
procurement reviewer asks for and this codebase cannot answer on its own
behalf: where data lives, and who is legally answerable for this
deployment (audit §2.3 "unknown data residency", §4.4 "no canonical
product identity: owner, legal entity ... support contact").

WHAT THIS DOES NOT DO
------------------------
It does not pick a region, register a company, or invent a support
contact. Those are exactly the business decisions AUDIT_RESPONSE.md §2.3
and §4.4 name as still outstanding, and no code change makes them for an
operator. What was actually missing was a PLACE to state them once a real
decision exists -- every deployment re-answering "where is my data" with
a bespoke status page, or not answering it at all, is a worse outcome
than one small, honest, unauthenticated endpoint.

UNSET FIELDS SAY SO, NEVER SILENTLY OMIT
--------------------------------------------
A field with no configured value renders as `"not disclosed by this
deployment's operator"`, not `null`, not an empty string, and not left out
of the response body. The distinction matters: a reviewer who sees a
field missing from a JSON body cannot tell whether that means "no data"
or "this API version doesn't have that field yet." A reviewer who sees
the field present and saying nothing was configured has an unambiguous
answer, and knows to ask the operator directly rather than assume either
compliance or its absence.

THIS ASSERTS NOTHING ABOUT COMPLIANCE
------------------------------------------
`/disclosure` states facts an operator configured; it makes no claim
about SOC 2, a penetration test, or any other attestation (see
AUDIT_RESPONSE.md §7.1-§7.3, SOC2_READINESS.md). Conflating "here is
where we say our data lives" with "we are independently verified" is
exactly the failure mode a trust center risks, which is why this is named
`/disclosure`, returns only self-reported facts, and links to
AUDIT_RESPONSE.md for what has and has not been independently verified.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from hub.config import HubConfig

_NOT_DISCLOSED = "not disclosed by this deployment's operator"


def _field(value: str) -> str:
    return value.strip() or _NOT_DISCLOSED


def add_disclosure_route(app, config: HubConfig) -> None:
    """Always mounted, like `/healthz` -- there is no shared secret to gate
    a page whose entire content is "here is what this deployment's
    operator chose to state," and an unconfigured deployment answering
    with three honest "not disclosed" lines costs nothing to expose."""

    async def disclosure(request: Request) -> JSONResponse:
        return JSONResponse({
            "data_region": _field(config.data_region),
            "operator_legal_name": _field(config.operator_legal_name),
            "operator_support_contact": _field(config.operator_support_contact),
            "note": (
                "Self-reported by this deployment's operator, not independently "
                "verified. This endpoint asserts no certification, audit, or "
                "attestation of any kind."
            ),
        })

    app.add_route("/disclosure", disclosure, methods=["GET"])
