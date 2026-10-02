from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from hub.config import HubConfig

_NOT_DISCLOSED = "not disclosed by this deployment's operator"


def _field(value: str) -> str:
    return value.strip() or _NOT_DISCLOSED


def add_disclosure_route(app, config: HubConfig) -> None:
    async def disclosure(request: Request) -> JSONResponse:
        return JSONResponse({
            "data_region": _field(config.data_region),
            "region_enforced_for_pinned_orgs": bool(config.data_region.strip()),
            "operator_legal_name": _field(config.operator_legal_name),
            "operator_support_contact": _field(config.operator_support_contact),
            "note": (
                "Self-reported by this deployment's operator, not independently "
                "verified. This endpoint asserts no certification, audit, or "
                "attestation of any kind."
            ),
        })

    app.add_route("/disclosure", disclosure, methods=["GET"])
