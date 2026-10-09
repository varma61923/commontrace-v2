"""A marketplace listing's pure checks: digests, lift evidence, licence and signatures.

Shared by the local marketplace (`commontrace.marketplace`) and the Hub, so it
depends only on the standard library, `origin` and `memory_guard` (Ed25519
verification additionally needs `cryptography`).
"""
from __future__ import annotations

import hashlib
import math
import re

from commontrace import memory_guard, origin

SCHEMA_VERSION = 1
LISTING_KIND = "lesson-listing"
ROLES = {"publisher": "market-publisher", "referee": "certificate", "fleet": "fleet-effect"}
PORTABLE_KEYS = ("name", "description", "tags", "agent_type", "domain", "importance",
                 "importance_rationale", "applies_when", "do_not_apply_when")
MIN_ORGANIZATIONS = 2
MAX_LISTING_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PER = ("install", "month", "year")


class MarketError(ValueError):
    pass


def _lesson_problems(fm: dict) -> list[str]:
    """The lesson schema's constraints on the portable keys, without loading the schema file."""
    problems = [f"the listed lesson lacks {k}" for k in PORTABLE_KEYS if k not in fm]
    for key in ("name", "description", "agent_type", "domain", "importance_rationale",
                "applies_when", "do_not_apply_when"):
        if key in fm and (not isinstance(fm[key], str) or not fm[key].strip()):
            problems.append(f"the listed lesson's {key} must be non-empty text")
    if "tags" in fm and (not isinstance(fm["tags"], list) or not all(isinstance(t, str) for t in fm["tags"])):
        problems.append("the listed lesson's tags must be a list of text")
    importance = fm.get("importance")
    if "importance" in fm and (isinstance(importance, bool) or not isinstance(importance, int)
                               or not 1 <= importance <= 5):
        problems.append("the listed lesson's importance must be an integer from 1 to 5")
    return problems


def lesson_digest(portable: dict) -> str:
    return hashlib.sha256(origin._bytes(portable)).hexdigest()


def _screen(portable: dict) -> None:
    fields = {k: v for k, v in portable["frontmatter"].items() if isinstance(v, (str, list))}
    report = memory_guard.scan_fields({**fields, "body": portable["body"]})
    if report.should_block:
        raise MarketError("the lesson carries a secret or an injection payload (" + report.summary() + ")")


def _licence(licence: dict) -> dict:
    if not isinstance(licence, dict) or not _ID.match(str(licence.get("id", ""))):
        raise MarketError("a licence needs an id (an SPDX identifier or your own terms id)")
    terms = str(licence.get("terms", "")).strip()
    if not 1 <= len(terms) <= 4000:
        raise MarketError("a licence needs terms text of 1-4000 characters")
    out = {"id": licence["id"], "terms": terms}
    price = licence.get("price")
    if price is not None:
        amount, per = price.get("amount_usd"), price.get("per", "install")
        if (isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount)
                or not 0 <= amount <= 1_000_000 or per not in _PER):
            raise MarketError(f"a price needs amount_usd in 0..1000000 and per in {', '.join(_PER)}")
        out["price"] = {"amount_usd": round(float(amount), 2), "per": per}
    return out


def _lift_problems(certificate: dict, digest: str) -> list[str]:
    record = certificate.get("record") if isinstance(certificate, dict) else None
    if not isinstance(record, dict) or record.get("kind") != "replicated-lift-certificate":
        return ["the certificate is not a replicated-lift certificate"]
    problems = []
    pooled = record.get("pooled") or {}
    if record.get("artifact_sha256") != digest:
        problems.append("the certificate measured different lesson text")
    if record.get("simulated") is not False:
        problems.append("the certificate rests on simulated evidence")
    if not isinstance(pooled.get("organizations"), int) or pooled["organizations"] < MIN_ORGANIZATIONS:
        problems.append(f"fewer than {MIN_ORGANIZATIONS} independent organizations")
    ci_low = pooled.get("ci_low")
    if isinstance(ci_low, bool) or not isinstance(ci_low, (int, float)) or not ci_low > 0:
        problems.append("the pooled lift's interval does not exclude zero")
    return problems


def listing_id(listing: dict) -> str:
    return str(listing.get("digest", ""))


def verify(listing: dict, trusted: dict[str, dict[str, origin.Principal]]) -> dict:
    """Every check a buyer needs, against the public keys the buyer trusts."""
    problems = []
    if not isinstance(listing, dict) or not isinstance(listing.get("record"), dict):
        return {"ok": False, "problems": ["not a signed listing"], "lift": None}
    record = listing["record"]
    if record.get("kind") != LISTING_KIND or record.get("schema_version") != SCHEMA_VERSION:
        problems.append("unsupported listing kind or schema version")
    if listing.get("principal") not in trusted["publisher"]:
        problems.append(f"publisher {listing.get('principal')!r} is not trusted here")
    elif not origin.verify(listing, trusted["publisher"], authority=ROLES["publisher"]):
        problems.append("the publisher signature does not verify")
    certificate = record.get("certificate") or {}
    if certificate.get("principal") not in trusted["referee"]:
        problems.append(f"referee {certificate.get('principal')!r} is not trusted here")
    elif not origin.verify(certificate, trusted["referee"], authority=ROLES["referee"]):
        problems.append("the referee signature does not verify")
    portable = record.get("lesson")
    if (not isinstance(portable, dict) or set(portable) != {"frontmatter", "body"}
            or not isinstance(portable.get("frontmatter"), dict) or not isinstance(portable.get("body"), str)
            or set(portable["frontmatter"]) - set(PORTABLE_KEYS)):
        problems.append("the listed lesson is malformed")
        return {"ok": False, "problems": problems, "lift": None}
    problems += _lesson_problems(portable["frontmatter"])
    digest = lesson_digest(portable)
    if record.get("lesson_sha256") != digest:
        problems.append("the lesson text does not match its digest")
    problems += _lift_problems(certificate, digest)
    try:
        _screen(portable)
    except MarketError as exc:
        problems.append(str(exc))
    try:
        _licence(record.get("licence"))
    except MarketError as exc:
        problems.append(str(exc))
    pooled = (certificate.get("record") or {}).get("pooled")
    return {"ok": not problems, "problems": problems, "lift": pooled, "lesson_sha256": digest,
            "publisher": listing.get("organization"), "licence": record.get("licence")}


def summary(listing: dict) -> dict:
    record = listing.get("record", {})
    fm = record.get("lesson", {}).get("frontmatter", {})
    pooled = (record.get("certificate", {}).get("record") or {}).get("pooled") or {}
    return {"id": listing_id(listing), "name": fm.get("name"), "description": fm.get("description"),
            "publisher": listing.get("organization"), "licence": (record.get("licence") or {}).get("id"),
            "price": (record.get("licence") or {}).get("price"), "lift": pooled.get("effect"),
            "lift_ci": [pooled.get("ci_low"), pooled.get("ci_high")],
            "organizations": pooled.get("organizations"), "listed_at": record.get("listed_at")}
