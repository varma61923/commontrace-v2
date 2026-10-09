"""Lesson marketplace: listings that carry signed, replicated proof of lift.

A lesson is worth buying only if it has been shown to help somewhere other than
where it was written. The chain here makes that claim checkable by anyone:

1. **attest** -- each fleet that ran the lesson under its randomized holdout signs
   its own effect estimate for the exact lesson text (``fleet-effect``).
2. **certify** -- a referee checks two or more independent fleet receipts and signs
   a replicated-lift certificate (``federation.replicated_lift``).
3. **publish** -- the lesson's publisher signs a listing binding the portable
   lesson, that certificate and the licence terms.
4. **verify / install** -- a buyer verifies every signature against public keys it
   chose to trust, then installs the lesson **for review only**. It never becomes
   active without the buyer's operator approving it, and the buyer's own holdout
   measures it again from zero: someone else's lift is a reason to try a lesson,
   not proof it helps here.

Signatures are Ed25519, so a listing verifies with public keys alone. Nothing in
this module sends anything over a network; listings are plain JSON files that a
gateway catalog, the Hub or any file share can carry.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import math
import os
import re

from commontrace import _jsonl, frontmatter, lesson_io, memory_guard, origin, paths

SCHEMA_VERSION = 1
LISTING_KIND = "lesson-listing"
ROLES = {"publisher": "market-publisher", "referee": "certificate", "fleet": "fleet-effect"}
TRUST_ROLES = {"publisher": "publishers", "referee": "referees", "fleet": "fleets"}
PORTABLE_KEYS = ("name", "description", "tags", "agent_type", "domain", "importance",
                 "importance_rationale", "applies_when", "do_not_apply_when")
MIN_ORGANIZATIONS = 2
MAX_LISTING_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PER = ("install", "month", "year")


class MarketError(ValueError):
    pass


# ---------------------------------------------------------------- identities

def _market_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "market")


def _key_path(root: str, role: str) -> str:
    return os.path.join(paths.memory_dir(root), f".market-{role}-ed25519-key")


def _card_path(root: str, role: str) -> str:
    return os.path.join(_market_dir(root), f"identity-{role}.json")


def identity(root: str, role: str, *, principal_id: str = "", organization: str = "",
             create: bool = False) -> origin.Principal:
    """This store's signing identity for one role, created on first use when asked."""
    if role not in ROLES:
        raise MarketError(f"role must be one of {', '.join(ROLES)}")
    key_path, card_path = _key_path(root, role), _card_path(root, role)
    if os.path.exists(key_path) and os.path.exists(card_path):
        with open(card_path, encoding="utf-8") as fh:
            card = json.load(fh)
        with open(key_path, "rb") as fh:
            private = fh.read()
        return origin.Principal(card["id"], card["organization"], ROLES[role], private, "ed25519",
                                base64.b64decode(card["public_key"]))
    if not create:
        raise MarketError(f"no {role} identity yet: run `commontrace market identity --role {role} "
                          "--id <id> --organization <org>`")
    if not _ID.match(principal_id or "") or not _ID.match(organization or ""):
        raise MarketError("an identity needs an --id and --organization of 1-128 letters, digits, . _ : -")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    os.makedirs(_market_dir(root), exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(private)
    card = {"id": principal_id, "organization": organization, "role": role,
            "authority": ROLES[role], "algorithm": "ed25519",
            "public_key": base64.b64encode(public).decode("ascii")}
    with open(card_path, "w", encoding="utf-8") as fh:
        json.dump(card, fh, indent=2, sort_keys=True)
    return origin.Principal(principal_id, organization, ROLES[role], private, "ed25519", public)


def card(principal: origin.Principal) -> dict:
    """The shareable public half of an identity."""
    role = next(r for r, a in ROLES.items() if a == principal.authority)
    return {"id": principal.id, "organization": principal.organization, "role": role,
            "authority": principal.authority, "algorithm": "ed25519",
            "public_key": base64.b64encode(principal.public_key).decode("ascii")}


# --------------------------------------------------------------------- trust

def _trust_path(root: str) -> str:
    return os.path.join(_market_dir(root), "trust.json")


def _principal_from_card(entry: dict, role: str) -> origin.Principal:
    try:
        public = base64.b64decode(entry["public_key"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise MarketError("a trusted card needs a base64 Ed25519 public_key") from None
    if (len(public) != 32 or entry.get("algorithm", "ed25519") != "ed25519"
            or not _ID.match(str(entry.get("id", ""))) or not _ID.match(str(entry.get("organization", "")))):
        raise MarketError("a trusted card needs an id, an organization and a 32-byte Ed25519 key")
    return origin.Principal(entry["id"], entry["organization"], ROLES[role], b"", "ed25519", public)


def load_trust(root: str) -> dict[str, dict[str, origin.Principal]]:
    """Public keys this store's operator chose to trust, by role."""
    try:
        with open(_trust_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    return {role: {e["id"]: _principal_from_card(e, role) for e in raw.get(TRUST_ROLES[role], [])}
            for role in ROLES}


def trust(root: str, entry: dict, *, role: str) -> dict:
    """Add (or replace) one public card in the trust store. An operator decision."""
    if role not in ROLES:
        raise MarketError(f"role must be one of {', '.join(ROLES)}")
    _principal_from_card(entry, role)
    clean = {k: entry[k] for k in ("id", "organization", "public_key")}
    os.makedirs(_market_dir(root), exist_ok=True)
    path = _trust_path(root)
    with _jsonl.locked(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        bucket = [e for e in raw.get(TRUST_ROLES[role], []) if e.get("id") != clean["id"]]
        raw[TRUST_ROLES[role]] = sorted([*bucket, clean], key=lambda e: e["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    return clean


# ------------------------------------------------------------------- lessons

def portable_lesson(root: str, slug: str) -> dict:
    """The shareable part of an active lesson: its text, never its local history."""
    path = lesson_io.lesson_path(root, slug)
    if path is None or not os.path.exists(path):
        raise MarketError(f"no lesson named {slug!r}")
    fm, body = frontmatter.read(path)
    if fm.get("status") != "active":
        raise MarketError(f"{slug} is {fm.get('status')!r}; only an approved, active lesson can be listed")
    return {"frontmatter": {k: fm[k] for k in PORTABLE_KEYS if k in fm}, "body": body}


def lesson_digest(portable: dict) -> str:
    return hashlib.sha256(origin._bytes(portable)).hexdigest()


def _screen(portable: dict) -> None:
    fields = {k: v for k, v in portable["frontmatter"].items() if isinstance(v, (str, list))}
    report = memory_guard.scan_fields({**fields, "body": portable["body"]})
    if report.should_block:
        raise MarketError("the lesson carries a secret or an injection payload (" + report.summary() + ")")


# --------------------------------------------------------------- the chain

def measured_effect(root: str, slug: str, *, min_arm: int = 20) -> dict:
    """This store's randomized holdout estimate for one lesson, as attest needs it."""
    from commontrace.commands import experiment_cmd

    all_rows, _rate, corrupt = experiment_cmd._load(root)
    if corrupt:
        raise MarketError("the holdout log has unparseable lines; fix it before attesting")
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, all_rows)
    canonical = lesson_io.canonical_slug(slug)
    rows = [r for r in rows if lesson_io.canonical_slug(r.lesson) == canonical]
    if any(len(revs) > 1 for revs in experiment_cmd._revisions_under_test(rows).values()):
        raise MarketError("the lesson text changed during the experiment; attest a run on one revision")
    obs = experiment_cmd._observations(rows)
    injected = [o.succeeded for o in obs if o.injected]
    withheld = [o.succeeded for o in obs if not o.injected]
    if len(injected) < min_arm or len(withheld) < min_arm:
        raise MarketError(f"each arm needs at least {min_arm} resolved occasions "
                          f"(have {len(injected)} injected, {len(withheld)} withheld)")
    p1, p0 = sum(injected) / len(injected), sum(withheld) / len(withheld)
    # Agresti-Caffo style smoothing keeps the standard error positive at 0% or 100%.
    q1, q0 = (sum(injected) + 1) / (len(injected) + 2), (sum(withheld) + 1) / (len(withheld) + 2)
    se = math.sqrt(q1 * (1 - q1) / (len(injected) + 2) + q0 * (1 - q0) / (len(withheld) + 2))
    return {"effect": p1 - p0, "standard_error": se, "n_injected": len(injected), "n_withheld": len(withheld)}


def attest(root: str, slug: str, *, effect: dict | None = None, metric: str = "resolved",
           simulated: bool = False) -> dict:
    """A fleet's signed receipt of the lift it measured for this exact lesson text."""
    principal = identity(root, "fleet")
    portable = portable_lesson(root, slug)
    measured = effect or measured_effect(root, slug)
    record = {"artifact_sha256": lesson_digest(portable), "comparison": "randomized-holdout",
              "metric": metric, "effect": float(measured["effect"]),
              "standard_error": float(measured["standard_error"]), "simulated": bool(simulated)}
    return origin.bind(record, principal)


def certify(root: str, receipts: list[dict]) -> dict:
    """Referee: verify independent fleet receipts and sign the replicated-lift certificate."""
    from commontrace import federation

    signer = identity(root, "referee")
    fleets = load_trust(root)["fleet"]
    if len(receipts) < MIN_ORGANIZATIONS:
        raise MarketError(f"a certificate needs receipts from at least {MIN_ORGANIZATIONS} organizations")
    try:
        return federation.replicated_lift(receipts, principals=fleets, signer=signer)
    except (PermissionError, ValueError) as exc:
        raise MarketError(str(exc)) from None


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


def publish(root: str, slug: str, certificate: dict, licence: dict, *, now: str | None = None) -> dict:
    """Sign a listing for an active lesson whose certificate shows replicated positive lift."""
    principal = identity(root, "publisher")
    portable = portable_lesson(root, slug)
    _screen(portable)
    digest = lesson_digest(portable)
    problems = _lift_problems(certificate, digest)
    if problems:
        raise MarketError("cannot list: " + "; ".join(problems))
    record = {"kind": LISTING_KIND, "schema_version": SCHEMA_VERSION, "lesson": portable,
              "lesson_sha256": digest, "certificate": certificate, "licence": _licence(licence),
              "listed_at": now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    listing = origin.bind(record, principal)
    if len(json.dumps(listing).encode("utf-8")) > MAX_LISTING_BYTES:
        raise MarketError("the listing exceeds 256 KiB")
    add_to_catalog(root, listing)
    return listing


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


# ------------------------------------------------------------- catalog/install

def _catalog_dir(root: str) -> str:
    return os.path.join(_market_dir(root), "listings")


def add_to_catalog(root: str, listing: dict) -> str:
    lid = listing_id(listing)
    if not re.fullmatch(r"[0-9a-f]{64}", lid):
        raise MarketError("a listing needs its signed digest")
    os.makedirs(_catalog_dir(root), exist_ok=True)
    path = os.path.join(_catalog_dir(root), lid + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(listing, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def catalog(root: str, query: str = "") -> list[dict]:
    """Listings held by this store, optionally filtered by words in name/description/tags."""
    directory = _catalog_dir(root)
    words = [w for w in re.findall(r"\w+", query.lower()) if w]
    out = []
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if not re.fullmatch(r"[0-9a-f]{64}\.json", name):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            listing = json.load(fh)
        fm = listing.get("record", {}).get("lesson", {}).get("frontmatter", {})
        haystack = " ".join([str(fm.get("name", "")), str(fm.get("description", "")),
                             " ".join(map(str, fm.get("tags") or []))]).lower()
        if all(w in haystack for w in words):
            out.append(listing)
    return out


def summary(listing: dict) -> dict:
    record = listing.get("record", {})
    fm = record.get("lesson", {}).get("frontmatter", {})
    pooled = (record.get("certificate", {}).get("record") or {}).get("pooled") or {}
    return {"id": listing_id(listing), "name": fm.get("name"), "description": fm.get("description"),
            "publisher": listing.get("organization"), "licence": (record.get("licence") or {}).get("id"),
            "price": (record.get("licence") or {}).get("price"), "lift": pooled.get("effect"),
            "lift_ci": [pooled.get("ci_low"), pooled.get("ci_high")],
            "organizations": pooled.get("organizations"), "listed_at": record.get("listed_at")}


def _installed_path(root: str) -> str:
    return os.path.join(_market_dir(root), "installed.jsonl")


def install(root: str, listing: dict, *, accept_licence: str, actor: str = "") -> dict:
    """Verify, then write the lesson at status=review. Activation stays an operator decision."""
    report = verify(listing, load_trust(root))
    if not report["ok"]:
        raise MarketError("refused: " + "; ".join(report["problems"]))
    licence = listing["record"]["licence"]
    if accept_licence != licence["id"]:
        raise MarketError(f"installing means accepting licence {licence['id']!r}: pass --accept-licence "
                          f"{licence['id']} after reading its terms")
    lid = listing_id(listing)
    ledger = _installed_path(root)
    os.makedirs(_market_dir(root), exist_ok=True)
    with _jsonl.locked(ledger):
        prior = next((r for r in _jsonl.read_rows(ledger) if r.get("listing") == lid), None)
        if prior:
            return {**prior, "already_installed": True}
        portable = listing["record"]["lesson"]
        fm = dict(portable["frontmatter"])
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", lesson_io.canonical_slug(str(fm.get("name") or "")))
        base = base.strip("-")[:80] or "market-lesson"
        slug, n = base, 2
        while lesson_io.lesson_path(root, slug) is not None:
            slug, n = f"{base}-{n}", n + 1
        fm.update({"name": slug, "status": "review", "uses": 0, "last_hit": "NEVER"})
        path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
        os.makedirs(paths.lessons_dir(root), exist_ok=True)
        lesson_io.write_lesson(path, fm, portable["body"], root=root,
                               actor=actor or f"market:{listing['organization']}",
                               reason=f"installed from marketplace listing {lid[:12]} under licence {licence['id']}")
        row = {"listing": lid, "slug": slug, "lesson_sha256": report["lesson_sha256"],
               "publisher": listing["organization"], "licence": licence,
               "certified_lift": report["lift"], "installed_at":
               datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "status": "review", "local_proof_required": True}
        _jsonl.append_row(ledger, row)
    return row


def installed(root: str) -> list[dict]:
    return _jsonl.read_rows(_installed_path(root))
