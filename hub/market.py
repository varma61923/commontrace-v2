"""Hub lesson marketplace: publisher keys and listings verified on upload.

An org registers its publisher's Ed25519 public key, then uploads listings that
key signed. The Hub accepts a listing only if every check a buyer would run
passes against that key and a referee the Hub operator configured
(``HUB_MARKET_REFEREES_FILE``): signatures, the lesson digest, the content
screen, the licence, and replicated positive lift across two or more
organizations. Every org can browse the catalog; only the owner withdraws.

The Hub's acceptance is a filter, not the buyer's trust decision: buyers verify
again locally against keys they chose (``commontrace market verify``), and an
install always lands in review.
"""
from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone

from sqlalchemy import or_, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from commontrace import market_listing, origin
from hub import audit, scopes
from hub.db import session_scope
from hub.models import MarketListing, MarketPublisher

API_PREFIX = "/api/v1/market"
ACTOR = "rest-api-market"
_MAX_SEARCH = 100


def _error(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": error, **({"detail": detail} if detail else {})}, status_code=status)


def _referees(config) -> dict[str, origin.Principal]:
    return {pid: origin.Principal(pid, org, market_listing.ROLES["referee"], b"", "ed25519",
                                  base64.b64decode(key))
            for pid, org, key in config.market_referees}


def _publisher_principal(row: MarketPublisher) -> origin.Principal:
    return origin.Principal(row.principal_id, row.organization, market_listing.ROLES["publisher"], b"",
                            "ed25519", base64.b64decode(row.public_key))


def _card(row: MarketPublisher) -> dict:
    return {"id": row.principal_id, "organization": row.organization, "role": "publisher",
            "authority": market_listing.ROLES["publisher"], "algorithm": "ed25519", "public_key": row.public_key}


def add_market_routes(app, session_factory, *, config, authenticate, require_scope) -> None:
    async def _body(request: Request) -> dict | None:
        raw = await request.body()
        if len(raw) > market_listing.MAX_LISTING_BYTES + 4096:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _caller(request: Request, scope: str):
        authenticated, denied = await authenticate(request)
        if denied is None:
            denied = require_scope(authenticated, scope)
        return authenticated, denied

    async def register_publisher(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_WRITE)
        if denied is not None:
            return denied
        payload = await _body(request)
        card = (payload or {}).get("card")
        if not isinstance(card, dict):
            return _error(400, "bad_request", "body must be {\"card\": <publisher card>}")
        try:
            key = base64.b64decode(str(card.get("public_key", "")), validate=True)
        except ValueError:
            key = b""
        principal_id, organization = str(card.get("id", "")), str(card.get("organization", ""))
        if (len(key) != 32 or not market_listing._ID.match(principal_id)
                or not market_listing._ID.match(organization)):
            return _error(400, "bad_request", "a card needs an id, an organization and a 32-byte Ed25519 key")
        # Publisher rows are readable by every org, so this scoped read sees a claim by another org.
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            row = (await session.execute(
                select(MarketPublisher).where(MarketPublisher.principal_id == principal_id))).scalar_one_or_none()
            if row is not None and row.org_id != authenticated.org_id:
                return _error(409, "conflict", "another org registered that publisher id")
            if row is None:
                row = MarketPublisher(org_id=authenticated.org_id, principal_id=principal_id,
                                      organization=organization, public_key=card["public_key"])
                session.add(row)
            else:
                row.organization, row.public_key = organization, card["public_key"]
            await audit.record(session, actor=ACTOR, action="market_publisher_register",
                               org_id=authenticated.org_id, target_type="market_publisher",
                               target_id=principal_id)
            await session.flush()
            return JSONResponse(_card(row), status_code=201)

    async def get_publisher(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_READ)
        if denied is not None:
            return denied
        principal_id = request.path_params["principal_id"]
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            row = (await session.execute(
                select(MarketPublisher).where(MarketPublisher.principal_id == principal_id))).scalar_one_or_none()
            if row is None:
                return _error(404, "not_found", "no such publisher")
            return JSONResponse(_card(row))

    async def upload_listing(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_WRITE)
        if denied is not None:
            return denied
        payload = await _body(request)
        listing = (payload or {}).get("listing")
        if not isinstance(listing, dict):
            return _error(400, "bad_request", "body must be {\"listing\": <signed listing>} within 256 KiB")
        referees = _referees(config)
        if not referees:
            return _error(503, "market_unconfigured", "this Hub trusts no referee (HUB_MARKET_REFEREES_FILE)")
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            publisher = (await session.execute(select(MarketPublisher).where(
                MarketPublisher.principal_id == str(listing.get("principal", "")),
                MarketPublisher.org_id == authenticated.org_id))).scalar_one_or_none()
            if publisher is None:
                return _error(403, "forbidden", "register this listing's publisher key for your org first")
            report = market_listing.verify(listing, {"publisher": {publisher.principal_id:
                                                                   _publisher_principal(publisher)},
                                                     "referee": referees})
            if not report["ok"]:
                return _error(422, "unverified_listing", "; ".join(report["problems"]))
            lid = market_listing.listing_id(listing)
            existing = await session.get(MarketListing, lid)
            if existing is not None:
                return JSONResponse({"id": lid, "status": existing.status}, status_code=200)
            record, summary = listing["record"], market_listing.summary(listing)
            fm = record["lesson"]["frontmatter"]
            lift = report["lift"]
            if not all(isinstance(lift.get(k), (int, float)) and math.isfinite(lift[k])
                       for k in ("effect", "ci_low", "ci_high")):
                return _error(422, "unverified_listing", "the pooled lift is not finite")
            price = record["licence"].get("price") or {}
            session.add(MarketListing(
                id=lid, org_id=authenticated.org_id, publisher_principal=publisher.principal_id,
                publisher_org=publisher.organization, name=str(fm.get("name", ""))[:200],
                description=str(fm.get("description", "")), tags=[str(t) for t in fm.get("tags") or []][:50],
                lesson_sha256=report["lesson_sha256"], lift_effect=float(lift["effect"]),
                lift_ci_low=float(lift["ci_low"]), lift_ci_high=float(lift["ci_high"]),
                organizations=int(lift["organizations"]), licence_id=record["licence"]["id"],
                price_usd=price.get("amount_usd"), price_per=price.get("per"), listing=listing))
            await audit.record(session, actor=ACTOR, action="market_listing_upload", org_id=authenticated.org_id,
                               target_type="market_listing", target_id=lid, summary=summary["name"] or "")
        return JSONResponse({"id": lid, "status": "listed"}, status_code=201)

    def _row_summary(row: MarketListing) -> dict:
        return {"id": row.id, "name": row.name, "description": row.description, "tags": row.tags,
                "publisher": row.publisher_org, "publisher_id": row.publisher_principal,
                "lift": row.lift_effect, "lift_ci": [row.lift_ci_low, row.lift_ci_high],
                "organizations": row.organizations, "licence": row.licence_id,
                "price": ({"amount_usd": row.price_usd, "per": row.price_per}
                          if row.price_usd is not None else None),
                "status": row.status, "listed_at": row.created_at.isoformat() if row.created_at else None}

    async def search_listings(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_READ)
        if denied is not None:
            return denied
        query = request.query_params.get("q", "").strip()[:200]
        try:
            limit = max(1, min(int(request.query_params.get("limit", "20")), _MAX_SEARCH))
        except ValueError:
            limit = 20
        statement = select(MarketListing).where(MarketListing.status == "listed")
        for word in query.split()[:8]:
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            statement = statement.where(or_(MarketListing.name.ilike(pattern, escape="\\"),
                                            MarketListing.description.ilike(pattern, escape="\\")))
        statement = statement.order_by(MarketListing.lift_ci_low.desc(), MarketListing.created_at.desc()).limit(limit)
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            rows = (await session.execute(statement)).scalars().all()
            return JSONResponse({"listings": [_row_summary(r) for r in rows]})

    async def get_listing(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_READ)
        if denied is not None:
            return denied
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            row = await session.get(MarketListing, request.path_params["listing_id"])
            if row is None or (row.status != "listed" and row.org_id != authenticated.org_id):
                return _error(404, "not_found", "no such listing")
            return JSONResponse({"listing": row.listing, "summary": _row_summary(row)})

    async def withdraw_listing(request: Request) -> Response:
        authenticated, denied = await _caller(request, scopes.SCOPE_WRITE)
        if denied is not None:
            return denied
        async with session_scope(session_factory, org_id=authenticated.org_id) as session:
            row = await session.get(MarketListing, request.path_params["listing_id"])
            if row is None or row.org_id != authenticated.org_id:
                return _error(404, "not_found", "no listing of yours with that id")
            if row.status == "listed":
                row.status, row.withdrawn_at = "withdrawn", datetime.now(timezone.utc)
                await audit.record(session, actor=ACTOR, action="market_listing_withdraw",
                                   org_id=authenticated.org_id, target_type="market_listing", target_id=row.id)
            return JSONResponse({"id": row.id, "status": row.status})

    app.add_route(f"{API_PREFIX}/publishers", register_publisher, methods=["POST"])
    app.add_route(f"{API_PREFIX}/publishers/{{principal_id}}", get_publisher, methods=["GET"])
    app.add_route(f"{API_PREFIX}/listings", upload_listing, methods=["POST"])
    app.add_route(f"{API_PREFIX}/listings", search_listings, methods=["GET"])
    app.add_route(f"{API_PREFIX}/listings/{{listing_id}}", get_listing, methods=["GET"])
    app.add_route(f"{API_PREFIX}/listings/{{listing_id}}", withdraw_listing, methods=["DELETE"])
