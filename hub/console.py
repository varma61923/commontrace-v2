"""The customer's console -- the fleet's own view of its own memory.

WHY THIS EXISTS
---------------
`hub/admin.py` is the OPERATOR console: one vendor employee, cross-tenant
visibility, moderating a shared Knowledge Base. Until this module, that was
the only HTML the Hub served. A paying customer had an MCP tool surface and
a CLI, and nothing else.

That is a real gap and not a cosmetic one. The product's central claim
(STRATEGY.md 13.3) is that it can prove causally, on the customer's own
data, that the memory changed outcomes -- and three commits of work went
into making that number trustworthy: a validity audit, an attrition check,
a treatment pinned to a content revision. All of it renders in a terminal,
to whoever runs `commontrace prove outcomes`. The person who decides whether
to renew does not run that command, and a claim nobody in the buying
organisation can see is not doing the job it was built for.

So this console has one organising idea: **answer "is this working, and can
I trust the answer" in a browser, in that order, without hiding either
half.** The validity verdict is rendered above the effect sizes on the page
for the same reason the CLI prints it first -- a report that leads with a
significant number and caveats it underneath is how a broken one gets
quoted.

TENANT ISOLATION
----------------
Every figure on every page comes from a function in `hub/crud.py` that
already takes `org_id` and filters on it. This module writes NO queries of
its own, and that is a deliberate constraint rather than a convenience: a
cross-tenant leak here is the worst failure this product has available, and
the isolation argument should rest on the one set of filters that
`hub/tests/test_tenant_isolation.py` already exercises rather than on a
second set that a new file introduced.

MOSTLY READ-ONLY, ON PURPOSE, WITH NAMED EXCEPTIONS
--------------------------------------------------------
Overview/Memory/Knowledge Base change nothing. Not because mutation is
hard, but because of what those mutations WOULD be: altering an outcome
or a shared corpus (a Knowledge Base submission). Both already have
audited, authenticated paths through MCP and the CLI that record who did
what, and adding a second way in through a browser session widens that
surface for a convenience nobody has asked for.

Users & Roles, API Keys, Alerts, Webhooks, and starting/stopping the
randomized holdout on the Proof page (below) are the deliberate
exceptions -- this Hub's own identity/credential/alerting/egress/
experiment management (audit 1.2, 8.3), previously CLI-only, gated
behind the SAME check every one of those CLI commands already enforces
(`scopes.SCOPE_ADMIN` on the signed-in session's own key) plus an
explicit org-ownership check on every id-addressed mutation, since
`auth.revoke_api_key`/`rotate_api_key`, `alerts.delete_rule`, and
`events.rotate_secret` take no org_id argument at all -- they trust an
operator's own direct DB access to be scoped correctly already, which a
customer's browser session is not. Every mutation here calls the SAME
`hub/manage.py`/`hub/auth.py`/`hub/alerts.py`/`hub/events.py` functions
the CLI does (no second implementation) and is audited with the ACTUAL
originating credential (`audit.actor_for_api_key`), not a borrowed
`operator-cli` label -- `start_experiment`/`stop_experiment` take an
`actor` parameter for exactly this reason, the same pattern
`create_user`/`set_user_role` already used. A merely `read`- or
`write`-scoped session sees these pages exist but cannot act on them --
the same `satisfies()` check `hub/rbac.py` uses everywhere else in this
Hub. The Audit log page, and the assignments CSV export on Proof, are
read-only for every signed-in user, admin or not: an org's own record of
what happened, or of its own experiment's raw arm decisions, is not a
credential.

Read-only pages need no CSRF token: `hub/admin.py` needed one precisely
because it moderates; a route with no state-changing request has no
forged request to defend against. The admin-scoped mutations above are
POST, same-origin, `SameSite=Strict` cookie -- the same protection
`billing_checkout`/`proof_share` already rely on, not a new defence
invented for this.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote as _url_quote

from sqlalchemy import or_, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from commontrace import raw_export
from hub import alerts, audit, auth, crud, events, manage, plans, rbac, scopes
from hub.abuse import RateLimiter, resolve_client_key
from hub.admin import _CSS, _limit, _num, h
from hub.billing import StripeSettings, create_billing_portal_session, create_checkout_session
from hub.db import session_scope
from hub.encryption import NULL_CIPHER, EnvelopeCipher
from hub.models import AlertRule, ApiKey, AuditLogEntry, Organization, User, WebhookEndpoint

logger = logging.getLogger("commontrace.hub.console")

CONSOLE_PATH = "/app"
SESSION_COOKIE = "ct_console"

# How long a browser session lasts before the key must be presented again.
# Short, because the credential behind it is an API key with full org scope
# and a console session is a bearer of that scope in a browser -- the place
# it is least likely to be noticed if it leaks.
SESSION_TTL_SECONDS = 8 * 60 * 60


def _sign(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def issue_session(secret: str, org_id: str, key_prefix: str = "") -> str:
    """A signed, self-contained session token.

    Self-contained rather than a server-side session table because the Hub
    runs as more than one process behind a load balancer, and an in-memory
    session store silently logs people out on every deploy and every
    scale-out. Signed with HMAC so the org_id inside it cannot be edited by
    the holder -- which is the whole tenant boundary for this surface.

    The API KEY ITSELF IS NOT IN THE TOKEN. It is verified once at sign-in
    and then discarded: a cookie is a long-lived, widely-copied artifact,
    and putting a full-scope credential in one turns every browser
    misconfiguration into a key disclosure.
    """
    payload = json.dumps(
        # `key_prefix`, not the key: the first few characters, which is what
        # the audit log already records. Enough to answer "which key opened
        # this session" and not enough to be one.
        {"org": org_id, "key": key_prefix, "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(secret, payload)}"


def read_session(secret: str, token: str) -> dict | None:
    """The claims in `token`, or None if it is unsigned, forged or expired."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception:  # noqa: BLE001 - a malformed cookie is simply not a session
        return None
    # compare_digest, not ==: signature comparison is the one place in this
    # module where a timing difference is an oracle for forging a session.
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(claims, dict) or not claims.get("org"):
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    # A share token (below) is signed with the same secret and would
    # otherwise pass every check above -- explicitly reject it here so it
    # can never be replayed as a full, mutating-scope session, only ever
    # through read_share_token's narrower surface.
    if claims.get("kind") == "share_proof":
        return None
    return claims


# --- Shareable, read-only Proof links ---------------------------------------
#
# WHY THIS EXISTS. hub/plans.py's whole pricing model is "share of measured
# value" (STRATEGY.md), and the Proof page below is the only place that
# value is actually shown -- with the SOUND/WEAKENED/COMPROMISED verdict
# rendered ABOVE the number it qualifies, not as a footnote (see
# _validity_block's docstring: "a page that shows the number first ... is
# how the number travels without the caveat"). Until now that page only
# ever rendered behind a signed-in session, so the one artifact that proves
# this product's central claim could never leave the browser it was viewed
# in -- not into a renewal conversation, a procurement deck, or a
# forwarded email, which is exactly where a number like this needs to
# travel to do its job.
#
# WHAT THIS IS NOT. Not a snapshot: a share link re-runs the same live
# crud.causal_effects/value_delivered queries the authenticated page does,
# so it can never go stale into something misleading -- a viewer six weeks
# from now sees the CURRENT verdict, including a COMPROMISED one the
# customer generated the link before they knew about. Not permanent: it
# expires (SHARE_TOKEN_TTL_SECONDS) and there is no revocation list, so an
# org that wants a link truly dead has to wait it out -- a deliberate v1
# simplification, not an oversight; a customer who needs a shorter-lived
# link can generate one closer to when they intend to use it. Not
# customer-identifying beyond org_id: no viewer name, no recipient email,
# nothing that would make this a tracking pixel.
SHARE_TOKEN_TTL_SECONDS = 14 * 24 * 60 * 60


def issue_share_token(secret: str, org_id: str, ttl_seconds: int = SHARE_TOKEN_TTL_SECONDS) -> str:
    """A signed, read-only, org-scoped link to that org's OWN live Proof
    page -- mintable only by someone already holding a real session for
    that org (see the `proof_share` route below), never guessable, and
    incapable of being upgraded into a session (read_session's explicit
    `kind` check above)."""
    payload = json.dumps(
        {"kind": "share_proof", "org": org_id, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(secret, payload)}"


def read_share_token(secret: str, token: str) -> dict | None:
    """The claims in a share token, or None if it is unsigned, forged,
    expired, or -- the other direction of read_session's guard -- actually
    a full session token presented here instead."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception:  # noqa: BLE001 - a malformed link is simply not a valid one
        return None
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("kind") != "share_proof" or not claims.get("org"):
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    return claims


# --- Chrome -----------------------------------------------------------------

_EXTRA_CSS = """
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
fieldset{border:0;padding:0;margin:.5rem 0}
fieldset legend{font-size:.8rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;padding:0;margin:0 0 .3rem}
.verdict{border-radius:10px;padding:1rem 1.15rem;margin:0 0 1.25rem;
  border:1px solid var(--rule);background:var(--surface)}
.verdict.bad{border-color:#C0392B;background:#FDF3F2}
.verdict.warn{border-color:#B7791F;background:#FDF8EE}
.verdict.good{border-color:#1E7A4B;background:#F1F9F4}
.verdict h2{border:0;margin:0 0 .35rem;font-size:1.05rem}
.verdict p{margin:.3rem 0;font-size:.93rem;max-width:78ch}
.check{display:flex;gap:.6rem;align-items:flex-start;margin:.45rem 0;font-size:.9rem}
.pill{font-size:.7rem;letter-spacing:.04em;padding:.12rem .45rem;border-radius:999px;
  border:1px solid var(--rule);white-space:nowrap;margin-top:.1rem}
.pill.ok{color:#1E7A4B;border-color:#9FD4B5}
.pill.warn{color:#B7791F;border-color:#E3C99A}
.pill.bad{color:#C0392B;border-color:#E8B0A8}
.signin{max-width:34rem;margin:3rem auto}
.signin input{width:100%;padding:.6rem .7rem;font:inherit;border:1px solid var(--rule);
  border-radius:8px;margin:.5rem 0 .8rem}
.signin button{padding:.55rem 1.1rem;font:inherit;border-radius:8px;border:1px solid var(--ink);
  background:var(--ink);color:#fff;cursor:pointer}
.err{color:#C0392B;font-size:.9rem;margin:.4rem 0}
.muted{color:var(--muted)}
.rev{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem;color:var(--muted)}
.shared-banner{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
  padding:.7rem 1rem;margin:0 0 1.25rem;font-size:.85rem;color:var(--muted)}
.share-box{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
  padding:.9rem 1.1rem;margin:0 0 1.25rem}
.share-box input{width:100%;padding:.5rem .6rem;font:inherit;font-size:.85rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid var(--rule);
  border-radius:8px;margin:.4rem 0;background:var(--paper)}
.share-box button{padding:.4rem .9rem;font:inherit;font-size:.85rem;border-radius:8px;
  border:1px solid var(--ink);background:var(--ink);color:#fff;cursor:pointer}
"""


# An inline data-URI icon rather than a static file or route: this
# console has no static-asset serving infrastructure at all (deliberately
# -- the Hub image ships no frontend build step), and every browser
# requests `/favicon.ico` once per origin unprompted. Without this,
# every real browser session against the console logged a 404 on that
# request from the moment the page loaded, on every page -- harmless to
# the response actually served, but a real console.error a customer's own
# browser devtools would show them looking at nothing else. A monogram,
# not a logo: this product has no shipped brand mark to embed instead.
_FAVICON_LINK = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='6' fill='%23111'/%3E"
    "%3Ctext x='16' y='23' font-size='18' font-family='ui-monospace,monospace' "
    "text-anchor='middle' fill='%23fff'%3EC%3C/text%3E%3C/svg%3E\">"
)


def _auto_refresh_script(seconds: int) -> str:
    """A dependency-free "this page is live" mechanism -- see
    hub/admin.py's own copy of this helper for the full reasoning
    (reload on a timer; skip and retry shortly while a form control is
    focused, so a half-typed search or reason is never silently wiped;
    round-trip scroll position through sessionStorage so a reload does
    not snap back to the top of the page).
    """
    return (
        "<script>(function(){"
        f"var KEY='ct-scroll-'+location.pathname+location.search;"
        "var y=sessionStorage.getItem(KEY);"
        "if(y!==null){window.scrollTo(0,parseInt(y,10)||0);sessionStorage.removeItem(KEY);}"
        "function isEditing(){var el=document.activeElement;"
        "return !!el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.tagName==='SELECT');}"
        "function tick(){if(isEditing()){setTimeout(tick,3000);return;}"
        "sessionStorage.setItem(KEY,String(window.scrollY));location.reload();}"
        f"setTimeout(tick,{int(seconds)}*1000);"
        "})();</script>"
    )


def _page(
    title: str, body: str, *, signed_in: bool = True, auto_refresh_seconds: int = 0,
) -> HTMLResponse:
    nav = (
        f'<nav><a href="{CONSOLE_PATH}">Overview</a>'
        f'<a href="{CONSOLE_PATH}/proof">Proof</a>'
        f'<a href="{CONSOLE_PATH}/memory">Memory</a>'
        f'<a href="{CONSOLE_PATH}/kb">Knowledge Base</a>'
        f'<a href="{CONSOLE_PATH}/users">Users</a>'
        f'<a href="{CONSOLE_PATH}/keys">API Keys</a>'
        f'<a href="{CONSOLE_PATH}/alerts">Alerts</a>'
        f'<a href="{CONSOLE_PATH}/webhooks">Webhooks</a>'
        f'<a href="{CONSOLE_PATH}/audit">Audit log</a>'
        f'<a href="{CONSOLE_PATH}/signout">Sign out</a></nav>'
        if signed_in else ""
    )
    live_badge = (
        f'<span class="ro" title="Refreshes automatically every {int(auto_refresh_seconds)}s '
        'unless you are typing in a field">live</span>'
        if auto_refresh_seconds else ""
    )
    refresh_script = _auto_refresh_script(auto_refresh_seconds) if auto_refresh_seconds else ""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{h(title)} · CommonTrace</title>{_FAVICON_LINK}"
        f"<style>{_CSS}{_EXTRA_CSS}</style></head><body>"
        '<header class="bar"><div class="in"><b>CommonTrace</b>'
        '<span class="ro">your fleet</span>'
        f"{live_badge}"
        f"{nav}</div></header><main>{body}</main>{refresh_script}</body></html>",
        # A customer console renders that org's own operational data. A cached
        # copy in a shared or kiosk browser is one more place it sits at rest,
        # and it outlives the session cookie that was supposed to gate it.
        headers={"Cache-Control": "no-store, private", "Referrer-Policy": "same-origin",
                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"},
    )


def _shared_page(body: str, *, expires_at: int) -> HTMLResponse:
    """A read-only Proof view for someone with no session at all -- no nav
    (there is nothing else this link grants access to), a banner naming
    what it is and when it stops working, and no outbound link: this
    product has no established public URL in its own codebase to send a
    viewer to, so the banner names CommonTrace rather than pointing
    somewhere invented.
    """
    until = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%B %-d, %Y")
    banner = (
        '<div class="shared-banner">Shared, read-only report — generated from live data by a '
        f"CommonTrace customer. Link active until {h(until)}.</div>"
    )
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Proof · CommonTrace</title>{_FAVICON_LINK}"
        f"<style>{_CSS}{_EXTRA_CSS}</style></head><body>"
        '<header class="bar"><div class="in"><b>CommonTrace</b>'
        '<span class="ro">shared report</span></div></header>'
        f"<main>{banner}{body}</main></body></html>",
        headers={
            # Distinct from _page's headers in one deliberate way: this
            # response carries no session cookie and no mutating capability
            # at all, so there is nothing here for a cache to leak beyond
            # the same numbers the org itself chose to put in the link --
            # but it is still that org's un-published business data, so it
            # stays no-store rather than becoming cacheable-by-default.
            "Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        },
    )


def _tiles(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="tile"><div class="k">{h(k)}</div><div class="v">{v}</div></div>'
        for k, v in items
    )
    return f'<div class="tiles">{cells}</div>'


_TONE = {"OK": "ok", "WEAKENS": "warn", "INVALIDATES": "bad"}
_VERDICT_TONE = {"SOUND": "good", "WEAKENED": "warn", "COMPROMISED": "bad"}


def _validity_block(report: dict) -> str:
    """The trust verdict, rendered ABOVE whatever it is a verdict about.

    Placement is the whole point. This console exists so a renewal
    conversation can look at the causal number, and a page that shows the
    number first and its caveat lower down is how the number travels without
    the caveat.
    """
    if not report:
        return ""
    verdict = str(report.get("verdict", ""))
    tone = _VERDICT_TONE.get(verdict, "")
    headline = {
        "SOUND": "Nothing in the assignment record undermines the effects below.",
        "WEAKENED": "Still an estimate — with less behind it than its confidence "
                    "interval implies.",
        "COMPROMISED": "Do not quote the effect sizes below. A specific mechanism is "
                       "biasing them, and more data will not fix it.",
    }.get(verdict, "Validity not assessed.")

    checks = "".join(
        f'<div class="check"><span class="pill {_TONE.get(str(f.get("severity")), "")}">'
        f'{h(f.get("severity"))}</span><span>{h(f.get("headline"))}'
        + (f'<br><span class="muted">{h(f.get("detail"))}</span>' if f.get("detail") else "")
        + "</span></div>"
        for f in report.get("findings", [])
    )
    return (
        f'<div class="verdict {tone}"><h2>Can this be trusted? — {h(verdict or "UNKNOWN")}</h2>'
        f"<p>{h(headline)}</p>"
        f'<p class="muted">{_num(report.get("n_resolved", 0))} of '
        f'{_num(report.get("n_assignments", 0))} assignments have a recorded outcome. '
        "Only those enter the comparison.</p>"
        f"{checks}"
        f'<p class="muted"><em>Not checkable here: whether an agent used a memory it was '
        "told to withhold. That leaves no trace in the record and biases the effect "
        "toward zero — it is honoured by your agents or not at all.</em></p></div>"
    )


def _projection_block(report: dict) -> str:
    pending = [p for p in report.get("projections", []) if p.get("still_needed")]
    if not pending:
        return ""
    rows = "".join(
        f"<tr><td>{h(p.get('trace_id'))}</td>"
        f"<td>{_num(p.get('n_injected'))} / {_num(p.get('n_withheld'))}</td>"
        f"<td>{_num(p.get('still_needed'))} more in the {h(p.get('binding_arm'))} arm</td>"
        f"<td>{h(p.get('eta') or '—')}</td></tr>"
        for p in pending
    )
    advice = pending[0].get("advice") or ""
    return (
        "<h2>When will this be answerable?</h2>"
        '<p class="sub">Underpowered on the last day of a pilot is a spent pilot. '
        "The same fact now is a holdout rate you can still change.</p>"
        "<table><thead><tr><th>Memory</th><th>injected / withheld</th>"
        f"<th>Needs</th><th>At the current rate</th></tr></thead><tbody>{rows}</tbody></table>"
        f'<p class="muted">{h(advice)}</p>'
    )


# --- Pages ------------------------------------------------------------------


async def _overview_data(session, org_id: str) -> dict:
    return {
        "entitlements": await crud.entitlements(session, org_id),
        "search": await crud.search_health(session, org_id),
        "recent": await crud.search_traces(session, org_id, limit=5),
    }


def _render_billing_block(billing: dict | None) -> str:
    """Rendered on the Overview page, right after the entitlement tiles --
    the same place "Plan" and "Billing period" already sit. Absent entirely
    (not just disabled) when this deployment has no Stripe prices
    configured, matching the rest of this console's "nothing to see if you
    haven't opted in" posture."""
    if not billing or not billing.get("enabled"):
        return ""
    plan = str(billing.get("plan") or plans.DEFAULT_PLAN)
    if billing.get("has_subscription"):
        return (
            '<div class="share-box"><b>Billing</b><br>'
            f'<span class="muted">Current plan: {h(plan)}. Manage your payment method, '
            "invoices, or change plans in Stripe's billing portal.</span><br>"
            f'<form method="post" action="{CONSOLE_PATH}/billing/portal">'
            '<button type="submit">Manage billing</button></form></div>'
        )
    upgrades = "".join(
        f'<form method="post" action="{CONSOLE_PATH}/billing/checkout" '
        'style="display:inline-block;margin:.3rem .6rem .3rem 0">'
        f'<input type="hidden" name="plan" value="{name}">'
        f'<button type="submit">Upgrade to {h(name.capitalize())}</button></form>'
        for name in billing.get("available_plans") or []
    )
    if not upgrades:
        return ""
    return (
        '<div class="share-box"><b>Billing</b><br>'
        f'<span class="muted">Current plan: {h(plan)}. Upgrade for more storage, agents, and '
        "Knowledge Base queries.</span><br>" + upgrades + "</div>"
    )


def _render_overview(data: dict, causal: dict, billing: dict | None = None) -> str:
    ent = data["entitlements"]
    traces = ent.get("traces") or {}
    agents = ent.get("agents") or {}
    search = data["search"] or {}

    body = ["<h1>Your fleet</h1>",
            '<p class="sub">Everything on this page is your organisation\'s own data. '
            "Nothing here is shared with, or drawn from, another customer.</p>"]
    body.append(_tiles([
        ("Traces stored", f'{_num(traces.get("used", 0))} <span class="muted">of '
                          f'{_limit(traces.get("limit"))}</span>'),
        ("Agents under management", f'{_num(agents.get("active", 0))} <span class="muted">of '
                                    f'{_limit(agents.get("limit"))}</span>'),
        ("Searches this month", _num(search.get("searches", 0))),
        # The number an operator would otherwise never see: how often an agent
        # asked this corpus something and got nothing back. It is the live
        # version of the retrieval benchmark, on this fleet's own queries, and
        # it is stored as a count -- no query text is retained anywhere.
        ("Searches that found nothing", _miss(search)),
        ("Plan", h(ent.get("plan", "—"))),
        ("Billing period", h(ent.get("period", "—"))),
    ]))
    body.append(_render_billing_block(billing))

    running = bool(causal.get("experiment_running"))
    integrity = causal.get("integrity") or {}
    if running and causal.get("n_observations"):
        verdict = str(integrity.get("verdict", ""))
        tone = _VERDICT_TONE.get(verdict, "")
        body.append(
            f'<div class="verdict {tone}"><h2>Is the memory working?</h2>'
            f'<p>A randomized holdout is running: '
            f'{_num(causal.get("n_observations", 0))} resolved observation(s) across '
            f'{_num(causal.get("n_occasions", 0))} occasion(s). '
            f'Validity: <b>{h(verdict)}</b>.</p>'
            f'<p><a href="{CONSOLE_PATH}/proof">See the causal report →</a></p></div>'
        )
    elif running:
        body.append(
            '<div class="verdict warn"><h2>Is the memory working?</h2>'
            "<p>An experiment is running, but no occasion has been reported yet. Your "
            "agents need to call <code>holdout_assign</code> before injecting and "
            "<code>record_occasion_outcome</code> afterwards — without the second, "
            "nothing joins and nothing can be measured.</p></div>"
        )
    else:
        body.append(
            '<div class="verdict"><h2>Is the memory working?</h2>'
            "<p>No randomized holdout is running, so nothing here is causal yet. Ask your "
            "operator to start one — it is the only thing that separates this product's "
            "effect from everything else that changed in the same window.</p>"
            f'<p><a href="{CONSOLE_PATH}/proof">See the observed change →</a></p></div>'
        )

    rows = data["recent"].get("traces") or []
    if rows:
        cells = "".join(
            f"<tr><td>{h(t.get('title'))}</td><td>{h(t.get('agent_type'))}</td>"
            f"<td>{_num(t.get('retrievals', 0))}</td>"
            f"<td>{h(str(t.get('created_at'))[:16])}</td></tr>"
            for t in rows
        )
        body.append("<h2>Most recent</h2><table><thead><tr><th>Trace</th><th>Agent type</th>"
                    f"<th>Retrieved</th><th>Captured</th></tr></thead><tbody>{cells}</tbody>"
                    "</table>")
    else:
        body.append('<h2>Most recent</h2><p class="sub">No traces yet. Your agents capture '
                    "them with <code>contribute_trace</code>.</p>")
    return "".join(body)


def _miss(search: dict) -> str:
    """How often a search came back empty.

    `None` means no search has run yet, which is not the same as 0% and must
    not render as one -- a fleet that has never searched would otherwise read
    as a fleet whose every search succeeds.
    """
    rate = search.get("miss_rate")
    if rate is None:
        return '<span class="muted">no searches yet</span>'
    return f'{rate:.0%} <span class="muted">of {_num(search.get("searches_with_terms", 0))}</span>'



def _policy_block(policy: dict) -> str:
    """The aggregate that stays valid when the per-trace sum does not:
    occasions that received any memory against occasions that received none,
    counting each occasion exactly once."""
    if not policy or not policy.get("readable"):
        reason = (policy or {}).get("reason", "")
        return (
            '<p class="muted">A whole-policy comparison is not available yet'
            + (f": {h(reason)}" if reason else "")
            + "</p>"
        )
    ci = policy.get("ci_95") or [0.0, 0.0]
    return (
        f"<p><b>{_signed(policy.get('effect'))}</b> across "
        f"{_num(policy.get('n_treated', 0))} occasions that received a memory, against "
        f"{_num(policy.get('n_control', 0))} that received none "
        f"(95% CI {_ci(ci)}) — "
        f"<b>{policy.get('occasions_improved', 0.0):+,.0f} occasions</b>.</p>"
        '<p class="muted">Every occasion counts once here, however many memories it '
        "received. That is what makes this figure addable when the per-memory ones are "
        "not; it attributes nothing to an individual memory.</p>"
    )


def _value_block(worth: dict) -> str:
    """What the memory was worth, in occasions -- and a refusal when it cannot
    be said.

    This is the number a renewal conversation is actually about, which is
    exactly why it is the one most worth being strict with. A COMPROMISED
    experiment shows the refusal, not a hedged figure; underpowered memories
    contribute nothing; memories measured as HURTING are subtracted rather
    than dropped. See commontrace/value.py.
    """
    if not worth:
        return ""
    if not worth.get("readable"):
        return (
            '<div class="verdict bad"><h2>What has this been worth?</h2>'
            "<p><b>Not stated.</b> " + h(worth.get("reason", "")) + "</p>"
            "<p class=\"muted\">A value figure is the one artifact where a caveat "
            "reliably gets separated from the number it qualifies, so there is no "
            "figure to separate.</p></div>"
        )

    # The per-trace contributions may not always be added: on this Hub one
    # occasion routinely receives several traces (holdout_assign takes a
    # list), and summing them would attribute one improved occasion more
    # than once -- then price it more than once. When that is the case there
    # is no total to render, and the policy-level comparison over unique
    # occasions is what this page shows instead (commontrace/value.py).
    if not worth.get("aggregate_readable", True):
        return (
            '<div class="verdict"><h2>What has this been worth?</h2>'
            "<p><b>No total is stated.</b> " + h(worth.get("aggregate_reason", "")) + "</p>"
            + _policy_block(worth.get("policy_effect") or {})
            + '<p class="muted">Each memory\'s own measured effect is unaffected and '
            "is shown below -- it is adding them together that would count the same "
            "improved occasion twice.</p></div>"
        )

    improved = worth.get("occasions_improved") or 0.0
    ci = worth.get("ci_95") or [0.0, 0.0]
    tone = "good" if improved > 0 else ("bad" if improved < 0 else "")
    lines = [
        f'<div class="verdict {tone}"><h2>What has this been worth?</h2>',
        f"<p><b>{improved:+,.0f} occasions</b> went differently because of this memory, "
        f"over the measured window (95% CI {ci[0]:+,.0f} to {ci[1]:+,.0f}).</p>",
        f'<p class="muted">From {_num(worth.get("n_counted", 0))} memory/memories whose '
        f'causal effect is established; {_num(worth.get("n_excluded", 0))} contributed '
        "nothing.</p>",
    ]
    if worth.get("money") is not None:
        low, high = worth.get("money_range") or [0.0, 0.0]
        lines.append(
            f"<p><b>{worth['money']:+,.0f}</b> at the "
            f"{worth['value_per_occasion']:,.2f} per resolved occasion you supplied "
            f"({low:+,.0f} to {high:+,.0f}).</p>"
        )
    else:
        lines.append(
            '<p class="muted">Add <code>?per_occasion=25</code> to this URL to see it in '
            "your own currency. The occasion count is measured here; the rate is yours, "
            "and nothing about it is stored.</p>"
        )
    hurt = [m for m in worth.get("memories", []) if m.get("verdict") == "HURTS"]
    if hurt:
        lines.append(
            '<p class="muted"><em>' + _num(len(hurt)) + " memory/memories measured as "
            "making outcomes WORSE are subtracted above, not dropped. A figure that "
            "sums only the winners is a brochure.</em></p>"
        )
    return "".join(lines) + "</div>"


def _render_share_form(share_url: str | None) -> str:
    """Prepended to the AUTHENTICATED Proof page only -- never to the shared
    view itself, which has no session and must not be able to mint more
    links for an org it isn't signed into."""
    days = SHARE_TOKEN_TTL_SECONDS // 86400
    if share_url:
        return (
            '<div class="share-box"><b id="share-url-label">Shareable link generated.</b><br>'
            f"Valid {days} days, always shows LIVE data (not a frozen snapshot), visible to "
            "anyone who has the link -- treat it like the report data it is."
            f'<input type="text" readonly aria-labelledby="share-url-label" '
            f'value="{h(share_url)}" onclick="this.select()"></div>'
        )
    return (
        f'<form method="post" action="{CONSOLE_PATH}/proof/share" class="share-box">'
        "<b>Share this report</b><br>"
        '<span class="muted">A read-only link to this live page -- no sign-in required to view '
        f"it, always shows current data, expires in {days} days.</span><br>"
        '<button type="submit">Generate shareable link</button></form>'
    )


def _render_experiment_controls(causal: dict, is_admin: bool, *, error: str = "") -> str:
    """Self-service start/stop for the randomized holdout -- the same
    mutation `hub.manage start-experiment`/`stop-experiment` performs,
    admin-scope-gated like every other mutating page in this console.
    Absent entirely for a non-admin viewer, same as this file's other
    admin-only forms: showing a disabled form still tells a non-admin
    viewer these actions exist and invites a permission-escalation
    attempt for no reader benefit.
    """
    if not is_admin:
        return ""
    body = ['<div class="share-box">']
    if error:
        body.append(f'<p class="err">{h(error)}</p>')
    if causal.get("experiment_running"):
        body.append(
            "<b>Experiment control</b><br>"
            '<span class="muted">Stopping keeps every observation recorded so far -- it '
            "only stops withholding memory on new occasions.</span><br>"
            f'<form method="post" action="{CONSOLE_PATH}/proof/experiment/stop" '
            "onsubmit=\"return confirm('Stop the running experiment?')\">"
            '<button type="submit">Stop experiment</button></form>'
        )
    else:
        body.append(
            "<b>Start a randomized holdout</b><br>"
            '<span class="muted">Starts a NEW experiment with a fresh randomization -- any '
            "prior observations stop being pooled with what comes next. Your agents must "
            "call <code>holdout_assign</code> before injecting and "
            "<code>record_occasion_outcome</code> afterwards, or nothing is measured."
            "</span><br>"
            f'<form method="post" action="{CONSOLE_PATH}/proof/experiment/start">'
            '<label for="exp-rate">Holdout rate</label> '
            f'<input type="number" id="exp-rate" name="rate" step="0.01" min="0.01" '
            f'max="0.99" value="{manage.DEFAULT_HOLDOUT_RATE}" required> '
            '<span class="muted">fraction of eligible injections withheld</span><br>'
            '<label for="exp-outcome">Primary outcome label</label> '
            '<input type="text" id="exp-outcome" name="outcome" value="resolved" required> '
            '<span class="muted">what <code>succeeded=true</code> means when your agents '
            "call <code>record_occasion_outcome</code></span><br>"
            '<label for="exp-notes">Notes</label> '
            '<input type="text" id="exp-notes" name="notes" placeholder="optional"><br>'
            '<button type="submit">Start experiment</button></form>'
        )
    body.append("</div>")
    return "".join(body)


def _render_proof(
    outcomes: dict, causal: dict, worth: dict | None = None,
    *, is_admin: bool = False, experiment_error: str = "",
) -> str:
    body = ["<h1>Proof</h1>",
            '<p class="sub">Two different questions, deliberately not merged: what changed '
            "since your baseline, and what this memory <em>caused</em>.</p>"]
    body.append(_value_block(worth or {}))

    integrity = causal.get("integrity") or {}
    body.append("<h2>Caused by the memory (randomized holdout)</h2>")
    body.append(
        f'<p class="muted"><a href="{CONSOLE_PATH}/proof/assignments.csv">Download every arm '
        "decision as CSV</a> -- the signed artifact your own analyst re-runs the comparison "
        "from, including the occasions the estimate above had to drop for never reporting an "
        "outcome.</p>"
    )
    body.append(_render_experiment_controls(causal, is_admin, error=experiment_error))
    if not causal.get("experiment_running"):
        body.append('<p class="sub">No experiment is running, so nothing in this section '
                    "is causal. The observed change below is real but confounded with "
                    "everything else that moved in the same window.</p>")
    else:
        body.append(_validity_block(integrity))
        effects = causal.get("effects") or []
        readable = integrity.get("effects_readable", True)
        if effects and readable:
            rows = "".join(
                f"<tr><td>{h(e.get('title') or e.get('trace_id'))}</td>"
                f"<td><b>{h(e.get('verdict'))}</b></td>"
                f"<td>{_pct(e.get('rate_injected'))} (n={_num(e.get('n_injected'))})</td>"
                f"<td>{_pct(e.get('rate_withheld'))} (n={_num(e.get('n_withheld'))})</td>"
                f"<td>{_signed(e.get('effect'))}</td>"
                f"<td>{_ci(e.get('ci_95'))}</td>"
                f"<td>{_significance(e)}</td></tr>"
                for e in effects
            )
            body.append("<table><thead><tr><th>Memory</th><th>Verdict</th><th>With</th>"
                        "<th>Without</th><th>Effect</th><th>95% CI</th><th></th></tr></thead>"
                        f"<tbody>{rows}</tbody></table>")
            notes = [e for e in effects if e.get("note")]
            if notes:
                body.append('<ul class="muted">' + "".join(
                    f"<li><b>{h(e.get('title') or e.get('trace_id'))}</b> — "
                    f"{h(e.get('note'))}</li>" for e in notes
                ) + "</ul>")
        elif effects:
            # Withheld rather than shown-with-a-caveat. On a page built to be
            # read in a renewal conversation, a number on screen gets quoted.
            body.append('<p class="sub">Effect sizes are withheld while the validity '
                        "verdict above is COMPROMISED. They would not be estimates of the "
                        "causal effect, and showing them with a caveat is how the caveat "
                        "gets separated from the number.</p>")
        else:
            body.append('<p class="sub">No memory has enough observations in both arms yet.</p>')
        body.append(_projection_block(integrity))

    body.append("<h2>Observed change since your baseline window</h2>")
    body.append(f'<p>{h(outcomes.get("headline", ""))}</p>')
    rows = outcomes.get("metrics") or []
    if rows:
        cells = "".join(
            f"<tr><td>{h(r.get('metric'))}</td>"
            f"<td>{_rate(r.get('baseline'))}</td><td>{_rate(r.get('current'))}</td>"
            f"<td>{_signed(r.get('delta'))}</td>"
            f"<td>{h(str(r.get('verdict', '')).replace('_', ' '))}</td></tr>"
            for r in rows
        )
        body.append("<table><thead><tr><th>Metric</th><th>Baseline</th><th>Now</th>"
                    f"<th>Change</th><th></th></tr></thead><tbody>{cells}</tbody></table>")
        # Every inconclusive row carries WHY, including the minimum effect the
        # sample could have detected. Dropping that is how "we could not tell"
        # gets read as "no effect".
        notes = [r for r in rows if r.get("note")]
        if notes:
            body.append('<ul class="muted">' + "".join(
                f"<li><b>{h(r.get('metric'))}</b> — {h(r.get('note'))}</li>" for r in notes
            ) + "</ul>")
    body.append('<p class="muted"><em>This half is an OBSERVED change, not a causal '
                "effect. `baseline` marks a time window, so a model upgrade or a shift in "
                "your task mix sits inside it. That is why the holdout above exists.</em></p>")
    return "".join(body)


def _pct(value: object) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "—"


def _rate(window: object) -> str:
    """One `{rate, n}` window from fleet_outcomes.

    `n` is rendered beside the rate rather than hidden: 100% of two
    observations and 100% of two thousand are the same number on a slide and
    entirely different facts.
    """
    if not isinstance(window, dict):
        return "—"
    rate = window.get("rate")
    if rate is None:
        return '<span class="muted">no data</span>'
    return f'{rate:.0%} <span class="muted">(n={_num(window.get("n", 0))})</span>'


def _significance(effect: dict) -> str:
    p = effect.get("p_value")
    if not isinstance(p, (int, float)):
        return "—"
    return f'p={p:.3g}' + ("" if effect.get("significant") else ' <span class="muted">(n.s.)</span>')


def _signed(value: object) -> str:
    return f"{value:+.1%}" if isinstance(value, (int, float)) else "—"


def _ci(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(v, (int, float)) for v in value
    ):
        return f"[{value[0]:+.1%}, {value[1]:+.1%}]"
    return "—"


def _render_memory(result: dict, tags: list[str]) -> str:
    traces = result.get("traces") or []
    body = ["<h1>Memory</h1>",
            '<p class="sub">What your fleet has captured. Ranked by how recently it was '
            "written; searchable the same way your agents search it.</p>"]
    body.append(
        f'<form method="get" action="{CONSOLE_PATH}/memory">'
        '<label for="memory-q" class="sr-only">Search your memory</label>'
        f'<input type="search" id="memory-q" name="q" '
        f'placeholder="Describe a task in your own words…" '
        f'value="{h(result.get("query", ""))}" style="width:26rem;padding:.5rem .6rem;'
        'font:inherit;border:1px solid var(--rule);border-radius:8px">'
        ' <button type="submit" style="padding:.5rem 1rem;font:inherit;border-radius:8px;'
        'border:1px solid var(--ink);background:var(--ink);color:#fff;cursor:pointer">'
        "Search</button></form>"
    )
    terms = result.get("terms") or []
    ignored = result.get("terms_ignored") or []
    if result.get("query"):
        body.append(f'<p class="muted">Matched on: {h(", ".join(terms)) or "nothing searchable"}'
                    + (f' · too common to discriminate: {h(", ".join(ignored))}' if ignored else "")
                    + "</p>")
    if traces:
        rows = "".join(
            f"<tr><td>{h(t.get('title'))}</td><td>{h(', '.join(t.get('tags') or []))}</td>"
            f"<td>{h(t.get('agent_type'))}</td><td>{_num(t.get('retrievals', 0))}</td>"
            f"<td>{h(str(t.get('created_at'))[:16])}</td></tr>"
            for t in traces
        )
        body.append("<table><thead><tr><th>Trace</th><th>Tags</th><th>Agent type</th>"
                    f"<th>Retrieved</th><th>Captured</th></tr></thead><tbody>{rows}</tbody></table>")
        # search_traces caps at `limit` and signals whether more rows exist
        # via `has_more` (fetched as one extra row, not a second COUNT) --
        # this used to be dropped on the floor here, so a corpus with more
        # than 50 matches showed exactly 50 with no indication, and no way
        # to reach the rest from this page at all.
        limit = int(result.get("limit") or len(traces) or 1)
        offset = int(result.get("offset") or 0)
        query_param = f'&q={_url_quote(result.get("query", ""))}' if result.get("query") else ""
        nav = []
        if offset > 0:
            nav.append(
                f'<a href="{CONSOLE_PATH}/memory?offset={max(0, offset - limit)}{query_param}">'
                "&larr; Newer</a>"
            )
        if result.get("has_more"):
            nav.append(
                f'<a href="{CONSOLE_PATH}/memory?offset={offset + limit}{query_param}">Older &rarr;</a>'
            )
        if nav:
            body.append(f'<p class="muted">{" · ".join(nav)}</p>')
    elif result.get("query"):
        body.append('<p class="sub">Nothing matched. That is an answer about this corpus, '
                    "not an error — and the terms above say whether the query reduced to "
                    "anything searchable.</p>")
    else:
        body.append('<p class="sub">No traces yet.</p>')
    if tags:
        body.append("<h2>Tags in use</h2><p class=\"muted\">"
                    + h(", ".join(tags[:60])) + "</p>")
    return "".join(body)


def _render_kb(submissions: list[dict], ent: dict) -> str:
    body = ["<h1>Knowledge Base</h1>",
            '<p class="sub">The one surface where anything crosses an organisation '
            "boundary — and it crosses it through a person. You consult the Knowledge Base "
            "by sending a <em>signature</em>, never your text; you propose an entry and an "
            "operator decides. No other customer sees your traces, ever.</p>"]
    queries = ent.get("commons_queries") or {}
    body.append(_tiles([
        ("Consultations used", f'{_num(queries.get("used", 0))} <span class="muted">of '
                               f'{_num(queries.get("allowance", 0))}</span>'),
        ("Remaining", _num(queries.get("remaining", 0))),
        ("Earned by accepted proposals", _num(queries.get("bonus_from_accepted_submissions", 0))),
        ("Proposals sent", str(len(submissions))),
    ]))
    if submissions:
        rows = "".join(
            f"<tr><td>{h(s.get('title'))}</td><td>{h(s.get('status'))}</td>"
            f"<td>{h(str(s.get('created_at'))[:16])}</td>"
            f"<td>{h(s.get('rejection_reason') or '—')}</td></tr>"
            for s in submissions
        )
        body.append("<h2>Your proposals</h2><table><thead><tr><th>Title</th><th>Status</th>"
                    f"<th>Sent</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        body.append('<h2>Your proposals</h2><p class="sub">None yet. Your agents propose '
                    "one with <code>submit_to_commons</code>; an accepted proposal is "
                    "published under the operator's name, not yours, and earns you bonus "
                    "consultations.</p>")
    return "".join(body)


def _render_users(users: list[User], is_admin: bool, error: str = "") -> str:
    body = ["<h1>Users &amp; roles</h1>",
            '<p class="sub">A person, distinct from your org\'s shared API key — a named '
            "role checked as a second, additive gate on every tool call. No SSO is linked "
            "by creating a row here; that is always a separate, explicit step "
            "(<code>hub.manage link-sso</code>).</p>"]
    if error:
        body.append(f'<p class="err">{h(error)}</p>')
    if users:
        rows = []
        for u in users:
            state = "disabled" if u.disabled_at is not None else "active"
            linked = "linked" if u.external_subject else "no SSO linked"
            actions = ""
            if is_admin:
                role_options = "".join(
                    f'<option value="{h(r)}"{" selected" if r == u.role else ""}>{h(r)}</option>'
                    for r in rbac.ROLES
                )
                actions = (
                    f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/role" '
                    f'style="display:inline">'
                    f'<label for="role-{h(u.id)}" class="sr-only">Role for {h(u.email)}</label>'
                    f'<select id="role-{h(u.id)}" name="role">{role_options}</select> '
                    f'<button type="submit">Set role</button></form> '
                )
                if u.disabled_at is not None:
                    actions += (
                        f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/enable" '
                        f'style="display:inline"><button type="submit">Enable</button></form>'
                    )
                else:
                    actions += (
                        f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/disable" '
                        f'style="display:inline"><button type="submit">Disable</button></form>'
                    )
            rows.append(
                f"<tr><td>{h(u.email)}</td><td>{h(u.role)}</td><td>{h(state)}</td>"
                f"<td>{h(linked)}</td><td>{actions}</td></tr>"
            )
        body.append(
            "<table><thead><tr><th>Email</th><th>Role</th><th>State</th>"
            f"<th>SSO</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        body.append('<p class="sub">No users yet.</p>')
    if is_admin:
        role_options = "".join(f'<option value="{h(r)}">{h(r)}</option>' for r in rbac.ROLES)
        body.append(
            "<h2>Create a user</h2>"
            f'<form method="post" action="{CONSOLE_PATH}/users/create">'
            '<label for="new-user-email" class="sr-only">Email</label>'
            '<input type="email" id="new-user-email" name="email" '
            'placeholder="person@example.com" required> '
            '<label for="new-user-name" class="sr-only">Display name</label>'
            '<input type="text" id="new-user-name" name="display_name" '
            'placeholder="Display name (optional)"> '
            '<label for="new-user-role" class="sr-only">Role</label>'
            f'<select id="new-user-role" name="role">{role_options}</select> '
            '<button type="submit">Create</button></form>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to create or '
                    "change a user's role.</p>")
    return "".join(body)


def _render_keys(keys: list[ApiKey], is_admin: bool, fresh: dict | None = None) -> str:
    body = ["<h1>API keys</h1>",
            '<p class="sub">Scopes do not imply each other: '
            "<code>admin</code> alone cannot read a trace. A production agent wants "
            "<code>read,write</code>; a dashboard wants <code>read</code>.</p>"]
    if fresh:
        body.append(
            '<div class="verdict warn"><h2>New key — shown once</h2>'
            f'<p class="rev">{h(fresh["raw_key"])}</p>'
            "<p>Store this now. It will not be shown again, and this Hub keeps only its "
            "hash.</p></div>"
        )
    if keys:
        rows = []
        for k in keys:
            state = "revoked" if k.revoked_at is not None else "active"
            key_scopes = ",".join(k.scopes) if k.scopes is not None else "(all — legacy key)"
            expires = k.expires_at.isoformat()[:10] if k.expires_at else "never"
            actions = ""
            if is_admin and k.revoked_at is None:
                actions = (
                    f'<form method="post" action="{CONSOLE_PATH}/keys/{h(k.id)}/rotate" '
                    f'style="display:inline"><button type="submit">Rotate</button></form> '
                    f'<form method="post" action="{CONSOLE_PATH}/keys/{h(k.id)}/revoke" '
                    f'style="display:inline"><button type="submit">Revoke</button></form>'
                )
            rows.append(
                f"<tr><td>{h(k.key_prefix)}</td><td>{h(key_scopes)}</td>"
                f"<td>{h(expires)}</td><td>{h(state)}</td><td>{actions}</td></tr>"
            )
        body.append(
            "<table><thead><tr><th>Prefix</th><th>Scopes</th><th>Expires</th>"
            f"<th>State</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        body.append('<p class="sub">No keys yet.</p>')
    if is_admin:
        scope_boxes = "".join(
            f'<label><input type="checkbox" name="scopes" value="{h(s)}"> {h(s)}</label> '
            for s in scopes.ALL_SCOPES if s != scopes.SCOPE_SCIM
        )
        body.append(
            "<h2>Issue a new key</h2>"
            f'<form method="post" action="{CONSOLE_PATH}/keys/issue">'
            f'<fieldset><legend>Scopes</legend>{scope_boxes}</fieldset>'
            '<label for="new-key-expires" class="sr-only">Expires in N days</label>'
            '<input type="number" id="new-key-expires" name="expires_days" '
            'placeholder="Expires in N days (blank = never)" min="1"> '
            '<button type="submit">Issue</button></form>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to issue, '
                    "rotate, or revoke keys.</p>")
    return "".join(body)


def _render_alerts(
    rules: list[AlertRule], is_admin: bool, error: str = "", report: dict | None = None,
) -> str:
    body = ["<h1>Alerts</h1>",
            '<p class="sub">Fires <code>alert.triggered</code> through your existing '
            "webhook endpoint(s) when a metric crosses a threshold you set — no polling "
            "needed. A closed, named set of metrics, never a free-form query.</p>"]
    if error:
        body.append(f'<p class="err">{h(error)}</p>')
    if report:
        body.append(
            '<div class="verdict good"><h2>Report queued</h2>'
            f'<p>{h(report["period"])}, plan={h(report["plan"])}: '
            f'{h(report["traces_total"])} traces, '
            f'{h(report["commons_queries_used"])}/{h(report["commons_queries_allowance"])} '
            "Knowledge Base consultations used.</p>"
            "<p>Delivered as <code>report.generated</code> to this org's webhook "
            "endpoint(s), the same as a scheduled one would be.</p></div>"
        )
    if rules:
        rows = []
        for r in rules:
            state = "enabled" if r.enabled else "disabled"
            last = r.last_triggered_at.isoformat()[:16] if r.last_triggered_at else "never"
            actions = ""
            if is_admin:
                actions = (
                    f'<form method="post" action="{CONSOLE_PATH}/alerts/{h(r.id)}/delete" '
                    f'style="display:inline"><button type="submit">Delete</button></form>'
                )
            rows.append(
                f"<tr><td>{h(r.metric)}</td><td>{h(r.comparator)}</td><td>{h(r.threshold)}</td>"
                f"<td>{h(r.cooldown_minutes)}m</td><td>{h(state)}</td><td>{h(last)}</td>"
                f"<td>{actions}</td></tr>"
            )
        body.append(
            "<table><thead><tr><th>Metric</th><th>Comparator</th><th>Threshold</th>"
            "<th>Cooldown</th><th>State</th><th>Last fired</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        body.append('<p class="sub">No alert rules yet.</p>')
    if is_admin:
        metric_options = "".join(f'<option value="{h(m)}">{h(m)}</option>' for m in alerts.METRICS)
        comparator_options = "".join(
            f'<option value="{h(c)}">{h(c)}</option>' for c in alerts.COMPARATORS
        )
        body.append(
            "<h2>Create a rule</h2>"
            f'<form method="post" action="{CONSOLE_PATH}/alerts/create">'
            '<label for="new-alert-metric" class="sr-only">Metric</label>'
            f'<select id="new-alert-metric" name="metric">{metric_options}</select> '
            '<label for="new-alert-comparator" class="sr-only">Comparator</label>'
            f'<select id="new-alert-comparator" name="comparator">{comparator_options}</select> '
            '<label for="new-alert-threshold" class="sr-only">Threshold</label>'
            '<input type="number" step="any" id="new-alert-threshold" name="threshold" '
            'placeholder="Threshold" required> '
            '<label for="new-alert-cooldown" class="sr-only">Cooldown minutes</label>'
            '<input type="number" id="new-alert-cooldown" name="cooldown_minutes" '
            f'placeholder="Cooldown minutes" value="{alerts.DEFAULT_COOLDOWN_MINUTES}" min="1"> '
            '<button type="submit">Create</button></form>'
        )
        body.append(
            "<h2>Usage report</h2>"
            '<p class="sub">A one-off <code>report.generated</code> event, the same '
            "shape a scheduled cron run or an operator's own "
            "<code>generate-report</code> would emit — for a customer who wants one "
            "now rather than waiting for the next cycle.</p>"
            f'<form method="post" action="{CONSOLE_PATH}/alerts/generate-report">'
            '<button type="submit">Generate now</button></form>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to create or '
                    "delete an alert rule, or generate a usage report.</p>")
    return "".join(body)


def _render_webhooks(
    endpoints: list[dict], pending: int, failed: list[dict], is_admin: bool,
    *, error: str = "", fresh: dict | None = None,
) -> str:
    body = ["<h1>Webhooks</h1>",
            '<p class="sub">Tell your own systems what happened here — a trace quarantined, '
            "an experiment reaching a verdict — without polling for it. Every payload carries "
            "only ids, counts and verdicts; a webhook is egress to a third party, and this "
            "product's memory content never is.</p>"]
    if error:
        body.append(f'<p class="err">{h(error)}</p>')
    if fresh and fresh.get("secret"):
        body.append(
            '<div class="share-box"><b id="webhook-secret-label">Signing secret '
            "(shown once)</b><br>"
            '<span class="muted">Verify the delivery signature with this. It cannot be shown '
            "again — rotate the endpoint to get a new one.</span>"
            f'<input type="text" readonly aria-labelledby="webhook-secret-label" '
            f'value="{h(fresh["secret"])}" onclick="this.select()"></div>'
        )
    body.append(_tiles([
        ("Endpoints", _num(len(endpoints))),
        ("Deliveries pending", _num(pending)),
    ]))
    if endpoints:
        rows = []
        for e in endpoints:
            state = "enabled" if e["enabled"] else "DISABLED"
            subscribed = ", ".join(e["events"]) or "(none)"
            actions = ""
            if is_admin and e["enabled"]:
                actions = (
                    f'<form method="post" action="{CONSOLE_PATH}/webhooks/{h(e["id"])}/rotate" '
                    f'style="display:inline"><button type="submit">Rotate secret</button>'
                    "</form> "
                    f'<form method="post" action="{CONSOLE_PATH}/webhooks/{h(e["id"])}/disable" '
                    f'style="display:inline"><button type="submit">Disable</button></form>'
                )
            rows.append(
                f"<tr><td>{h(e['url'])}</td><td>{h(state)}</td><td>{h(subscribed)}</td>"
                f"<td>v{h(e['key_version'])}</td><td>{actions}</td></tr>"
            )
        body.append(
            "<table><thead><tr><th>URL</th><th>State</th><th>Subscribed to</th>"
            f"<th>Key</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        body.append('<p class="sub">No webhook endpoints configured.</p>')
    if failed:
        frows = "".join(
            f"<tr><td>{h(str(d['created_at'])[:16])}</td><td>{h(d['event_type'])}</td>"
            f"<td>{h(d['last_error'] or '')}</td></tr>"
            for d in failed
        )
        body.append(
            "<h2>Gave up on delivering</h2>"
            '<p class="sub">Retried and failed enough times that this stopped retrying — a '
            "misconfigured endpoint, not a transient blip.</p>"
            "<table><thead><tr><th>When</th><th>Event</th><th>Last error</th></tr></thead>"
            f"<tbody>{frows}</tbody></table>"
        )
    if is_admin:
        event_boxes = "".join(
            f'<label><input type="checkbox" name="events" value="{h(name)}"> {h(name)}</label> '
            for name in events.EVENT_NAMES
        )
        body.append(
            "<h2>Add an endpoint</h2>"
            '<p class="sub">HTTPS only. Leave every box unchecked to subscribe to '
            "everything.</p>"
            f'<form method="post" action="{CONSOLE_PATH}/webhooks/create">'
            '<label for="new-webhook-url" class="sr-only">URL</label>'
            '<input type="url" id="new-webhook-url" name="url" '
            'placeholder="https://…" required><br>'
            f'<fieldset><legend>Events</legend>{event_boxes}</fieldset>'
            '<button type="submit">Add endpoint</button></form>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to add, rotate, or '
                    "disable a webhook endpoint.</p>")
    return "".join(body)


def _render_audit_log(entries: list[AuditLogEntry], offset: int, limit: int, has_more: bool) -> str:
    body = ["<h1>Audit log</h1>",
            '<p class="sub">Consequential actions on your organisation — writes through the '
            "MCP tools and every admin action, including the ones taken from this console "
            "itself. Ordinary reads (searches, lookups) are not logged here.</p>"]
    if not entries:
        body.append('<p class="sub">No audit entries yet.</p>')
        return "".join(body)
    rows = "".join(
        f"<tr><td>{h(str(e.created_at)[:19])}</td><td>{h(e.actor)}</td><td>{h(e.action)}</td>"
        f"<td>{h(f'{e.target_type}:{e.target_id}' if e.target_type else '—')}</td>"
        f"<td>{h(e.summary)}</td></tr>"
        for e in entries
    )
    body.append(
        "<table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th>"
        f"<th>Detail</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    nav = []
    if offset > 0:
        nav.append(f'<a href="{CONSOLE_PATH}/audit?offset={max(0, offset - limit)}">&larr; Newer</a>')
    if has_more:
        nav.append(f'<a href="{CONSOLE_PATH}/audit?offset={offset + limit}">Older &rarr;</a>')
    if nav:
        body.append(f'<p class="muted">{" · ".join(nav)}</p>')
    return "".join(body)


_SIGNIN = """
<div class="signin">
  <h1>Sign in</h1>
  <p class="sub">Use an API key for your organisation — the same key your agents
  authenticate with. It is verified once and never stored in your browser.</p>
  <form method="post" action="{path}/signin">
    <label for="api_key" class="sr-only">API key</label>
    <input type="password" id="api_key" name="api_key" placeholder="ct_…" autocomplete="off"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
  {error}
  <p class="muted" style="margin-top:1.5rem">Most of this console is read-only —
  capturing a trace, running the experiment, proposing to the Knowledge Base still goes
  through your agents or the CLI. An admin-scoped key can also manage users and API keys
  here directly; every change is still authenticated and audited the same way.</p>
</div>
"""


def add_console_routes(
    app,
    session_factory,
    *,
    console_secret: str,
    trusted_proxy_hops: int = 0,
    commons_enabled: bool = True,
    stripe: StripeSettings | None = None,
    signing_key: str = "",
    cipher: EnvelopeCipher = NULL_CIPHER,
) -> None:
    """Mount the customer console. Registered only when a secret is set."""
    stripe = stripe or StripeSettings()

    # Sign-in is a credential-checking endpoint, so it is rate limited on the
    # client key exactly as the MCP auth path is: without it this is an
    # unauthenticated, unthrottled oracle for testing API keys, reachable from
    # a browser, which is a strictly easier target than the MCP transport.
    signin_limiter = RateLimiter(per_minute=10, burst=5)

    # Guards hub/console.py's shared, unauthenticated Proof view (below):
    # each real causal_effects() call is genuine statistical work, not a
    # cheap read (hub/SCALING.md measures it up to 1.4s on a large org), and
    # this route has no session to charge a per-org read limiter against.
    # Generous on purpose -- a link embedded in a live deck or forwarded
    # thread can get a real burst of legitimate views -- but not unbounded.
    share_view_limiter = RateLimiter(per_minute=60, burst=20)

    def _secret() -> str:
        return console_secret

    async def _claims(request: Request) -> dict | None:
        """The session behind this request, or None.

        Two gates, not one. The signature proves the cookie was issued here
        and has not been edited. The database check proves the key that
        opened it is STILL live -- because otherwise revoking a key would not
        end the browser sessions it opened, and an operator revoking a
        compromised key would be told the problem was handled while the
        console kept serving that org's data for the rest of the session TTL.
        Revocation that does not revoke is worse than no revocation: it is a
        false belief about the state of a credential.

        One indexed lookup on `key_prefix` per page, which is the right price.
        """
        claims = read_session(_secret(), request.cookies.get(SESSION_COOKIE, ""))
        if claims is None:
            return None
        prefix = str(claims.get("key") or "")
        if not prefix:
            return None
        now = datetime.now(timezone.utc)
        async with session_scope(session_factory) as session:
            row = (
                await session.execute(
                    select(ApiKey.id, ApiKey.scopes).where(
                        ApiKey.org_id == str(claims["org"]),
                        ApiKey.key_prefix == prefix,
                        ApiKey.revoked_at.is_(None),
                        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
                    ).limit(1)
                )
            ).first()
        if row is None:
            return None
        # Refetched live on every page load, same as the liveness check
        # above and for the same reason: a key narrowed from admin to
        # read-only must lose console-mutation access on its very next
        # request, not merely at the browser session's own TTL.
        claims["scopes"] = row[1]
        return claims

    def _is_admin(claims: dict) -> bool:
        return scopes.satisfies(claims.get("scopes"), scopes.SCOPE_ADMIN)

    def _redirect_to_signin() -> RedirectResponse:
        return RedirectResponse(f"{CONSOLE_PATH}/signin", status_code=303)

    async def signin_page(request: Request) -> Response:
        if await _claims(request):
            return RedirectResponse(CONSOLE_PATH, status_code=303)
        return _page("Sign in", _SIGNIN.format(path=CONSOLE_PATH, error=""), signed_in=False)

    async def signin(request: Request) -> Response:
        allowed, retry_after = await signin_limiter.check(
            resolve_client_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return _page(
                "Sign in",
                _SIGNIN.format(
                    path=CONSOLE_PATH,
                    error='<p class="err">Too many attempts. Try again shortly.</p>',
                ),
                signed_in=False,
            )
        form = await request.form()
        raw_key = str(form.get("api_key") or "")
        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        if authenticated is None:
            # One message for every failure mode -- unknown key, revoked key,
            # expired key. Distinguishing them tells an attacker which of
            # those a guessed key was.
            logger.info("console sign-in rejected")
            return _page(
                "Sign in",
                _SIGNIN.format(
                    path=CONSOLE_PATH,
                    error='<p class="err">That key was not accepted.</p>',
                ),
                signed_in=False,
            )
        # The auth limiter charges failures only, so a legitimate sign-in does
        # not consume the budget a brute-force attempt is meant to exhaust.
        signin_limiter.refund(resolve_client_key(request, trusted_proxy_hops))
        response = RedirectResponse(CONSOLE_PATH, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            issue_session(_secret(), authenticated.org_id, authenticated.key_prefix),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,      # not readable by script, so XSS cannot lift the session
            samesite="strict",  # not sent cross-site, which is why no CSRF token is needed
            secure=request.url.scheme == "https",
            path=CONSOLE_PATH,  # never sent to /mcp, /admin or /metrics
        )
        return response

    async def signout(request: Request) -> Response:
        response = RedirectResponse(f"{CONSOLE_PATH}/signin", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path=CONSOLE_PATH)
        return response

    async def overview(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        async with session_scope(session_factory) as session:
            data = await _overview_data(session, org_id)
            causal = await crud.causal_effects(session, org_id)
            org = await session.get(Organization, org_id)
        current_plan = str(data["entitlements"].get("plan") or plans.DEFAULT_PLAN)
        billing_state = {
            "enabled": stripe.checkout_configured,
            "has_subscription": bool(org and org.stripe_subscription_id),
            "plan": current_plan,
            "available_plans": [
                name for name in plans.BILLABLE_PLANS
                if stripe.price_for_plan(name) and name != current_plan
            ],
        }
        return _page(
            "Your fleet", _render_overview(data, causal, billing_state),
            # Longer than the other auto-refreshing pages: causal_effects is
            # real statistical work (hub/SCALING.md measures it up to 1.4s on
            # a large org), and this is the page most likely left open in a
            # background tab.
            auto_refresh_seconds=45,
        )

    async def billing_checkout(request: Request) -> Response:
        """Mints a fresh Checkout Session for a plan the signed-in org does
        not yet subscribe to, and redirects the browser to Stripe's own
        hosted page. POST, not GET: like proof_share, this creates real
        state (an org gains a pending checkout / Stripe customer) and must
        not be triggerable by a prefetch or a crawled link."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        if not stripe.checkout_configured:
            return RedirectResponse(CONSOLE_PATH, status_code=303)
        form = await request.form()
        plan = str(form.get("plan") or "")
        if plan not in plans.BILLABLE_PLANS or not stripe.price_for_plan(plan):
            return RedirectResponse(CONSOLE_PATH, status_code=303)
        org_id = str(claims["org"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        if org is None:
            return _redirect_to_signin()
        if org.stripe_subscription_id:
            # The Overview page never shows this button to an already-
            # subscribed org (billing.get("has_subscription") swaps it for
            # "Manage billing"), but that is a UI nicety, not enforcement --
            # a stale page, a browser back-button resubmit, or a direct POST
            # would otherwise reach here anyway. Checkout always mints a NEW
            # subscription (billing.py's own module docstring); minting a
            # second one on a customer who already has one is not a smaller
            # version of this feature, it is silent double billing. Refused
            # here, not just hidden in the UI.
            return RedirectResponse(CONSOLE_PATH, status_code=303)
        base_url = str(request.url.replace(path=CONSOLE_PATH, query=""))
        try:
            checkout_url = await create_checkout_session(
                stripe, org=org, plan=plan,
                success_url=f"{base_url}?upgraded=1", cancel_url=base_url,
            )
        except Exception:  # noqa: BLE001 - Stripe being unreachable must not 500 the console
            logger.exception("stripe checkout session creation failed for org %s", org_id)
            return RedirectResponse(f"{CONSOLE_PATH}?billing_error=1", status_code=303)
        return RedirectResponse(checkout_url, status_code=303)

    async def billing_portal(request: Request) -> Response:
        """Redirects an already-subscribed org to Stripe's Billing Portal,
        where Stripe itself (not this code) handles plan changes,
        cancellation, payment method updates and invoice history.

        Requires `stripe.webhook_secret`, not just `secret_key`, for the
        same reason billing_checkout does: every change a customer makes
        in the Portal (cancel, switch plan, a payment failure) reaches
        this Hub ONLY through /billing/webhook. An org that reaches the
        Portal with that route unregistered can cancel and keep its paid
        entitlement forever, or change plans in a way this Hub never
        applies -- silently stale state is the failure mode here, not a
        500, so it is refused before ever redirecting to Stripe.
        """
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        if org is None or not org.stripe_customer_id or not stripe.secret_key or not stripe.webhook_secret:
            return RedirectResponse(CONSOLE_PATH, status_code=303)
        return_url = str(request.url.replace(path=CONSOLE_PATH, query=""))
        try:
            portal_url = await create_billing_portal_session(
                stripe, customer_id=org.stripe_customer_id, return_url=return_url,
            )
        except Exception:  # noqa: BLE001 - Stripe being unreachable must not 500 the console
            logger.exception("stripe billing portal session creation failed for org %s", org_id)
            return RedirectResponse(f"{CONSOLE_PATH}?billing_error=1", status_code=303)
        return RedirectResponse(portal_url, status_code=303)

    async def _proof_view(
        request: Request, org_id: str, is_admin: bool, *, experiment_error: str = "",
    ) -> Response:
        # An optional rate the reader supplies in the URL. Never stored: this
        # product ships the quantity and takes the price from whoever is
        # reading, which is what keeps a number nobody agreed to out of the
        # one place people treat as authoritative (STRATEGY.md 11.5).
        try:
            rate = float(request.query_params.get("per_occasion") or 0) or None
        except ValueError:
            rate = None
        async with session_scope(session_factory) as session:
            outcomes = await crud.fleet_outcomes(session, org_id)
            causal = await crud.causal_effects(session, org_id)
            worth = await crud.value_delivered(session, org_id, value_per_occasion=rate)
        # Set immediately after proof_share's redirect (below) -- rendered
        # once, not persisted, so refreshing the page without the query
        # param drops back to the plain "generate a link" form rather than
        # re-displaying a link that may since have been superseded.
        share_url = request.query_params.get("share_url")
        share_box = _render_share_form(share_url)
        return _page("Proof", share_box + _render_proof(
            outcomes, causal, worth, is_admin=is_admin, experiment_error=experiment_error,
        ))

    async def proof(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        return await _proof_view(request, str(claims["org"]), _is_admin(claims))

    async def proof_share(request: Request) -> Response:
        """Mints a new share link for the signed-in org and redirects back
        to the Proof page with it. POST, not GET: this creates a new
        capability (a live, un-guessable link to the org's own data) and
        must not be triggerable by a prefetch, a browser extension
        crawling links, or a `<img>` tag someone points at it."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        token = issue_share_token(_secret(), org_id)
        url = str(request.url.replace(path=f"{CONSOLE_PATH}/proof/shared/{token}", query=""))
        return RedirectResponse(
            f"{CONSOLE_PATH}/proof?share_url={_url_quote(url, safe='')}", status_code=303
        )

    async def proof_shared(request: Request) -> Response:
        """The public, unauthenticated view a share link resolves to. No
        _claims call anywhere in this handler -- that is the point of this
        route existing separately from `proof` above, not an oversight."""
        token = request.path_params.get("token", "")
        claims = read_share_token(_secret(), token)
        if claims is None:
            # 404, not 401/403: a share link is meant to be handed to
            # someone with no other relationship to this Hub, and "invalid"
            # vs. "expired" vs. "never existed" is not a distinction they
            # can act on -- it would only tell a prober which token shapes
            # are worth continuing to guess.
            return HTMLResponse("Not found.", status_code=404)
        org_id = str(claims["org"])
        # Public and unauthenticated, so unlike every other console route
        # this one is reachable by anyone who has ever seen the link -- and
        # crud.causal_effects is real statistical work (hub/SCALING.md
        # measures it at up to 1.4s on a large org), not a cheap read. Keyed
        # by org_id (from the verified token), not client address: the
        # threat here is one link being hit hard by whoever holds it, from
        # however many addresses, not a fleet of distinct guessers -- an
        # address-keyed limiter would not bound that at all.
        allowed, retry_after = await share_view_limiter.check(f"share:{org_id}")
        if not allowed:
            return HTMLResponse(
                "This report is being viewed heavily right now -- try again shortly.",
                status_code=429, headers={"Retry-After": str(int(retry_after) + 1)},
            )
        async with session_scope(session_factory) as session:
            outcomes = await crud.fleet_outcomes(session, org_id)
            causal = await crud.causal_effects(session, org_id)
            worth = await crud.value_delivered(session, org_id)
        return _shared_page(_render_proof(outcomes, causal, worth), expires_at=int(claims["exp"]))

    async def assignments_csv(request: Request) -> Response:
        """Every arm decision for this org's current experiment, as CSV --
        the browser counterpart to `hub.manage export-assignments`. Not
        admin-gated: read-only, and this org's own record of its own
        experiment is not a credential, same reasoning as Overview/Proof/
        Memory/Knowledge Base above."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        async with session_scope(session_factory) as session:
            assignments = await crud.holdout_assignments(session, org_id)
        result = raw_export.export(assignments)
        return Response(
            result.csv_text, media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="commontrace-assignments.csv"',
                "Cache-Control": "no-store, private",
            },
        )

    async def experiment_start(request: Request) -> Response:
        """Calls hub/manage.py's start_experiment directly -- the same
        function `hub.manage start-experiment` calls -- audited with the
        console session's own credential via the `actor` parameter that
        function accepts for exactly this reason, same as create_user/
        set_user_role above."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return await _proof_view(request, org_id, False)
        form = await request.form()
        rate_raw = str(form.get("rate") or "")
        outcome = str(form.get("outcome") or "").strip() or "resolved"
        notes = str(form.get("notes") or "").strip()
        try:
            rate = float(rate_raw)
        except ValueError:
            return await _proof_view(
                request, org_id, True, experiment_error="Holdout rate must be a number.")
        if not 0 < rate < 1:
            return await _proof_view(
                request, org_id, True,
                experiment_error="Holdout rate must be strictly between 0 and 1.")
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        ok = await manage.start_experiment(
            org_id, rate=str(rate), outcome=outcome, notes=notes,
            session_factory=session_factory, actor=actor,
        )
        if not ok:
            return await _proof_view(
                request, org_id, True, experiment_error="Could not start the experiment.")
        return RedirectResponse(f"{CONSOLE_PATH}/proof", status_code=303)

    async def experiment_stop(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return await _proof_view(request, org_id, False)
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        ok = await manage.stop_experiment(org_id, session_factory=session_factory, actor=actor)
        if not ok:
            return await _proof_view(
                request, org_id, True, experiment_error="No experiment is running.")
        return RedirectResponse(f"{CONSOLE_PATH}/proof", status_code=303)

    async def memory(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        query = str(request.query_params.get("q") or "")[:500]
        try:
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            offset = 0
        async with session_scope(session_factory) as session:
            try:
                result = await crud.search_traces(session, org_id, query=query, limit=50, offset=offset)
            except ValueError as exc:
                result = {"traces": [], "terms": [], "error": str(exc), "offset": offset, "has_more": False}
            tags = await crud.list_tags(session, org_id)
        result["query"] = query
        return _page("Memory", _render_memory(result, tags))

    async def knowledge_base(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not commons_enabled:
            return _page("Knowledge Base",
                         "<h1>Knowledge Base</h1><p class=\"sub\">The Knowledge Base is "
                         "not enabled on this deployment.</p>")
        async with session_scope(session_factory) as session:
            submissions = await crud.list_my_kb_submissions(session, org_id)
            ent = await crud.entitlements(session, org_id)
        return _page("Knowledge Base", _render_kb(submissions, ent))

    async def _list_users(org_id: str) -> list[User]:
        async with session_scope(session_factory) as session:
            return list((
                await session.execute(
                    select(User).where(User.org_id == org_id).order_by(User.created_at)
                )
            ).scalars().all())

    async def users_page(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        users = await _list_users(org_id)
        return _page("Users & roles", _render_users(users, _is_admin(claims)))

    async def users_create(request: Request) -> Response:
        """Admin-scope-gated: creates a User row through the SAME
        hub/manage.py function `hub.manage create-user` calls, audited
        with the actual console session's own credential (not a borrowed
        `operator-cli` label) via `actor=`."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            users = await _list_users(org_id)
            return _page("Users & roles", _render_users(
                users, False, error="An admin-scoped API key is required."))
        form = await request.form()
        email = str(form.get("email") or "").strip()
        role = str(form.get("role") or "").strip()
        display_name = str(form.get("display_name") or "").strip()
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        await manage.create_user(
            org_id, email, role, display_name,
            session_factory=session_factory, actor=actor,
        )
        return RedirectResponse(f"{CONSOLE_PATH}/users", status_code=303)

    async def _mutate_own_org_user(
        request: Request, user_id: str, action,
    ) -> Response:
        """Shared body for the three per-user mutating routes below: admin
        gate, then an explicit org-ownership check before calling into
        hub/manage.py -- `set_user_role`/`disable_user`/`enable_user` take
        only a bare user_id (correct for a trusted, cross-tenant operator
        CLI caller) and do not themselves verify which org a user belongs
        to, so a customer's own browser session must check that here
        before ever reaching them."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            users = await _list_users(org_id)
            return _page("Users & roles", _render_users(
                users, False, error="An admin-scoped API key is required."))
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
        if user is None or user.org_id != org_id:
            users = await _list_users(org_id)
            return _page("Users & roles", _render_users(
                users, True, error="No such user in your organization."))
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        await action(user_id, actor)
        return RedirectResponse(f"{CONSOLE_PATH}/users", status_code=303)

    async def users_set_role(request: Request) -> Response:
        role = str((await request.form()).get("role") or "")

        async def action(user_id: str, actor: str) -> None:
            await manage.set_user_role(
                user_id, role, session_factory=session_factory, actor=actor)

        return await _mutate_own_org_user(request, request.path_params["user_id"], action)

    async def users_disable(request: Request) -> Response:
        async def action(user_id: str, actor: str) -> None:
            await manage.disable_user(user_id, session_factory=session_factory, actor=actor)

        return await _mutate_own_org_user(request, request.path_params["user_id"], action)

    async def users_enable(request: Request) -> Response:
        async def action(user_id: str, actor: str) -> None:
            await manage.enable_user(user_id, session_factory=session_factory, actor=actor)

        return await _mutate_own_org_user(request, request.path_params["user_id"], action)

    async def _list_keys(org_id: str) -> list[ApiKey]:
        async with session_scope(session_factory) as session:
            return list((
                await session.execute(
                    select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at)
                )
            ).scalars().all())

    async def keys_page(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        keys = await _list_keys(org_id)
        return _page("API keys", _render_keys(keys, _is_admin(claims)))

    async def keys_issue(request: Request) -> Response:
        """Calls hub/auth.py directly rather than hub/manage.py's own
        `issue_key` -- that CLI wrapper only prints the result, and this
        route needs the raw key back as data to render it once, not on
        stdout. Audited the same way manage.py's issue_key audits it,
        with the console session's own credential as actor."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            keys = await _list_keys(org_id)
            return _page("API keys", _render_keys(keys, False))
        form = await request.form()
        chosen_scopes = form.getlist("scopes")
        if not chosen_scopes:
            keys = await _list_keys(org_id)
            return _page("API keys", _render_keys(keys, True))
        raw_days = str(form.get("expires_days") or "").strip()
        try:
            expires_days = int(raw_days) if raw_days else None
        except ValueError:
            expires_days = None
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(
                session, org_id, expires_days=expires_days, scopes=chosen_scopes,
            )
            await audit.record(
                session, actor=actor, action="issue_key",
                org_id=org_id, target_type="api_key", target_id=issued.key_id,
                summary=f"prefix={issued.key_prefix} scopes={','.join(issued.scopes)}",
            )
        keys = await _list_keys(org_id)
        return _page("API keys", _render_keys(
            keys, True, fresh={"raw_key": issued.raw_key}))

    async def keys_rotate(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            keys = await _list_keys(org_id)
            return _page("API keys", _render_keys(keys, False))
        key_id = request.path_params["key_id"]
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, key_id)
        # Explicit org-ownership check -- auth.rotate_api_key takes only a
        # bare key_id and, like revoke below, trusts a cross-tenant
        # operator caller to have already scoped it; a customer's own
        # session must not be able to rotate (or even discover the
        # existence of) another org's key by guessing its id.
        if key is None or key.org_id != org_id:
            keys = await _list_keys(org_id)
            return _page("API keys", _render_keys(keys, True))
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            issued = await auth.rotate_api_key(session, key_id)
            await audit.record(
                session, actor=actor, action="rotate_key",
                org_id=org_id, target_type="api_key", target_id=issued.key_id,
                summary=f"replaces={key_id}",
            )
        keys = await _list_keys(org_id)
        return _page("API keys", _render_keys(
            keys, True, fresh={"raw_key": issued.raw_key}))

    async def keys_revoke(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return RedirectResponse(f"{CONSOLE_PATH}/keys", status_code=303)
        key_id = request.path_params["key_id"]
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, key_id)
            if key is None or key.org_id != org_id:
                return RedirectResponse(f"{CONSOLE_PATH}/keys", status_code=303)
            await auth.revoke_api_key(session, key_id)
            actor = audit.actor_for_api_key(str(claims.get("key") or ""))
            await audit.record(
                session, actor=actor, action="revoke_key",
                org_id=org_id, target_type="api_key", target_id=key_id,
            )
        return RedirectResponse(f"{CONSOLE_PATH}/keys", status_code=303)

    async def _list_alert_rules(org_id: str) -> list[AlertRule]:
        async with session_scope(session_factory) as session:
            return await alerts.list_rules(session, org_id)

    async def alerts_page(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        rules = await _list_alert_rules(org_id)
        return _page("Alerts", _render_alerts(rules, _is_admin(claims)))

    async def alerts_create(request: Request) -> Response:
        """Calls hub/alerts.py directly, same as manage.py's own
        create-alert-rule CLI command -- create_rule already takes a
        `created_by` actor and is inherently org-scoped (it writes
        org_id straight onto the new row), so no separate ownership
        check is needed here the way key/user mutations require."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            rules = await _list_alert_rules(org_id)
            return _page("Alerts", _render_alerts(rules, False))
        form = await request.form()
        metric = str(form.get("metric") or "")
        comparator = str(form.get("comparator") or "")
        try:
            # str(...) first, like expires_days below: form.get() can return
            # an UploadFile (a form field submitted as a file part rather
            # than plain text), and float()/int() raise TypeError -- not
            # ValueError -- on that, which this except would not catch,
            # turning a malformed request into an unhandled 500 instead of
            # the clean validation error this branch exists to return.
            threshold = float(str(form.get("threshold") or ""))
        except ValueError:
            rules = await _list_alert_rules(org_id)
            return _page("Alerts", _render_alerts(
                rules, True, error="Threshold must be a number."))
        try:
            cooldown_minutes = int(str(form.get("cooldown_minutes") or alerts.DEFAULT_COOLDOWN_MINUTES))
        except ValueError:
            cooldown_minutes = alerts.DEFAULT_COOLDOWN_MINUTES
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            try:
                await alerts.create_rule(
                    session, org_id, metric, comparator, threshold,
                    cooldown_minutes=cooldown_minutes, created_by=actor,
                )
            except alerts.AlertError as exc:
                rules = await _list_alert_rules(org_id)
                return _page("Alerts", _render_alerts(rules, True, error=str(exc)))
        return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)

    async def alerts_delete(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)
        rule_id = request.path_params["rule_id"]
        async with session_scope(session_factory) as session:
            rule = await session.get(AlertRule, rule_id)
            # Explicit org-ownership check -- alerts.delete_rule takes only
            # a bare rule_id and, like the API-key routes above, trusts a
            # cross-tenant operator caller to have already scoped it.
            if rule is None or rule.org_id != org_id:
                return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)
            await alerts.delete_rule(session, rule_id)
        return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)

    async def alerts_generate_report(request: Request) -> Response:
        """Audit §8.3's `generate-report` CLI command, reachable from the
        browser too. Not a GET: this queues a real `report.generated`
        webhook delivery (`alerts.generate_report` calls `events.emit`),
        so loading a page must never trigger it -- only a deliberate POST,
        the same reasoning `proof_share`/`billing_checkout` already rely
        on for their own state-creating actions."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            rules = await _list_alert_rules(org_id)
            return _page("Alerts", _render_alerts(rules, False))
        async with session_scope(session_factory) as session:
            try:
                report = await alerts.generate_report(session, org_id)
            except alerts.AlertError as exc:
                rules = await _list_alert_rules(org_id)
                return _page("Alerts", _render_alerts(rules, True, error=str(exc)))
        rules = await _list_alert_rules(org_id)
        return _page("Alerts", _render_alerts(rules, True, report=report))

    async def _webhooks_view(
        org_id: str, is_admin: bool, *, error: str = "", fresh: dict | None = None,
    ) -> Response:
        async with session_scope(session_factory) as session:
            endpoints = await events.endpoints_for(session, org_id)
            pending = await events.pending_count(session, org_id)
            failed = await events.failed_deliveries(session, org_id, limit=10)
        rows = [
            {
                "id": e.id, "url": cipher.decrypt(e.url), "enabled": e.enabled,
                "events": list(e.events), "key_version": e.key_version,
            }
            for e in endpoints
        ]
        frows = [
            {"created_at": d.created_at, "event_type": d.event_type, "last_error": d.last_error}
            for d in failed
        ]
        return _page(
            "Webhooks", _render_webhooks(rows, pending, frows, is_admin, error=error, fresh=fresh)
        )

    async def webhooks_page(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        return await _webhooks_view(org_id, _is_admin(claims))

    async def webhooks_create(request: Request) -> Response:
        """Calls hub/events.py's add_endpoint directly -- the same function
        `hub.manage webhook-add` calls -- audited with the console
        session's own credential rather than a borrowed operator-cli
        label, same discipline as keys_issue/users_create above."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return await _webhooks_view(org_id, False)
        form = await request.form()
        url = str(form.get("url") or "").strip()
        chosen_events = form.getlist("events") or None
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            try:
                endpoint, secret = await events.add_endpoint(
                    session, org_id, url, events=chosen_events,
                    signing_key=signing_key, cipher=cipher,
                )
            except events.EventError as exc:
                return await _webhooks_view(org_id, True, error=str(exc))
            await audit.record(
                session, actor=actor, action="webhook.add",
                org_id=org_id, target_type="webhook_endpoint", target_id=endpoint.id,
                # The URL, not the secret. Never the secret.
                summary=f"{url} ({len(endpoint.events)} event types)",
            )
        return await _webhooks_view(org_id, True, fresh={"secret": secret})

    async def webhooks_rotate(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return await _webhooks_view(org_id, False)
        endpoint_id = request.path_params["endpoint_id"]
        async with session_scope(session_factory) as session:
            endpoint = await session.get(WebhookEndpoint, endpoint_id)
        # Explicit org-ownership check -- events.rotate_secret takes only a
        # bare endpoint_id and, like the API-key routes above, trusts a
        # cross-tenant operator caller to have already scoped it.
        if endpoint is None or endpoint.org_id != org_id:
            return await _webhooks_view(org_id, True)
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            secret = await events.rotate_secret(session, endpoint_id, signing_key=signing_key)
            endpoint = await session.get(WebhookEndpoint, endpoint_id)
            await audit.record(
                session, actor=actor, action="webhook.rotate",
                org_id=org_id, target_type="webhook_endpoint",
                target_id=endpoint_id, summary=f"key v{endpoint.key_version}",
            )
        return await _webhooks_view(org_id, True, fresh={"secret": secret})

    async def webhooks_disable(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return RedirectResponse(f"{CONSOLE_PATH}/webhooks", status_code=303)
        endpoint_id = request.path_params["endpoint_id"]
        async with session_scope(session_factory) as session:
            endpoint = await session.get(WebhookEndpoint, endpoint_id)
            if endpoint is None or endpoint.org_id != org_id:
                return RedirectResponse(f"{CONSOLE_PATH}/webhooks", status_code=303)
            endpoint.enabled = False
            actor = audit.actor_for_api_key(str(claims.get("key") or ""))
            await audit.record(
                session, actor=actor, action="webhook.disable",
                org_id=org_id, target_type="webhook_endpoint",
                target_id=endpoint_id, summary=cipher.decrypt(endpoint.url),
            )
        return RedirectResponse(f"{CONSOLE_PATH}/webhooks", status_code=303)

    async def audit_page(request: Request) -> Response:
        """Read-only for every signed-in user, like Proof/Memory/Knowledge
        Base -- an org's own audit trail is not a credential and gating it
        behind admin scope would hide from a non-admin viewer the very
        actions an admin took on their behalf."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        try:
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            offset = 0
        limit = 50
        async with session_scope(session_factory) as session:
            rows = list((
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
                    .order_by(AuditLogEntry.created_at.desc())
                    .limit(limit + 1).offset(offset)
                )
            ).scalars().all())
        has_more = len(rows) > limit
        return _page(
            "Audit log", _render_audit_log(rows[:limit], offset, limit, has_more),
            auto_refresh_seconds=20,
        )

    app.add_route(f"{CONSOLE_PATH}/signin", signin_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/signin", signin, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/signout", signout, methods=["GET", "POST"])
    app.add_route(CONSOLE_PATH, overview, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof", proof, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/share", proof_share, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/proof/shared/{{token}}", proof_shared, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/assignments.csv", assignments_csv, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/experiment/start", experiment_start, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/proof/experiment/stop", experiment_stop, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/billing/checkout", billing_checkout, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/billing/portal", billing_portal, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/memory", memory, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/kb", knowledge_base, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/users", users_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/users/create", users_create, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/role", users_set_role, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/disable", users_disable, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/enable", users_enable, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys", keys_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/keys/issue", keys_issue, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys/{{key_id}}/rotate", keys_rotate, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys/{{key_id}}/revoke", keys_revoke, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/alerts", alerts_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/alerts/create", alerts_create, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/alerts/{{rule_id}}/delete", alerts_delete, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/alerts/generate-report", alerts_generate_report, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/webhooks", webhooks_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/webhooks/create", webhooks_create, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/webhooks/{{endpoint_id}}/rotate", webhooks_rotate, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/webhooks/{{endpoint_id}}/disable", webhooks_disable, methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/audit", audit_page, methods=["GET"])


__all__ = ["CONSOLE_PATH", "add_console_routes", "issue_session", "read_session"]
