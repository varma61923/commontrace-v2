"""Self-serve organization signup: the public on-ramp hub/plans.py's own
free-tier docstring already argues for --

    "Evaluate on real memory, with Knowledge Base access included -- an
    on-ramp nobody can try requires nothing to demonstrate its value."

-- but that argument had no route behind it until this module. Every
account before this feature existed was sales- or support-assisted by
construction: the only way to get an org_id and a first API key was
`python -m hub.manage create-org`, run by an operator, on request. Nothing
about the free plan itself required that; the CLI command was simply the
only door.

WHAT THIS DOES NOT DO
-----------------------
No email verification. This Hub has no outbound email integration to build
one on (see hub/DEPLOYMENT.md) -- adding a fake "check your inbox" step
with nothing behind it would be worse than not claiming it. Treat this as
v1: the blast radius of an uncontactable or fraudulent signup is already
bounded by the free plan's own limits (hub/plans.py: 1,000 traces, 5
agents, 20 Knowledge Base queries/month) -- the same ceiling every
evaluator gets, verified or not.

What IS enforced: a tight per-address rate limit (an unauthenticated route
that mints a usable credential is the closest thing this Hub has to an
open account-creation oracle -- see `_SIGNUP_RATE_LIMIT` below for the
actual numbers and why they're deliberately conservative without a
CAPTCHA behind them) and a honeypot field, the standard mitigation for a
public form with no CAPTCHA in front of it.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from hub import audit
from hub.abuse import RateLimiter, resolve_client_key
from hub.admin import _CSS, h
from hub.auth import issue_api_key
from hub.db import session_scope
from hub.models import Organization

logger = logging.getLogger("commontrace.hub.signup")

SIGNUP_PATH = "/signup"
ACTOR_SELF_SERVE_SIGNUP = "self-serve-signup"

MIN_NAME_CHARS = 2
MAX_NAME_CHARS = 200

_EXTRA_CSS = """
.signin{max-width:34rem;margin:3rem auto}
.signin input{width:100%;padding:.6rem .7rem;font:inherit;border:1px solid var(--rule);
  border-radius:8px;margin:.5rem 0 .8rem}
.signin button{padding:.55rem 1.1rem;font:inherit;border-radius:8px;border:1px solid var(--ink);
  background:var(--ink);color:#fff;cursor:pointer}
.err{color:#C0392B;font-size:.9rem;margin:.4rem 0}
.share-box{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
  padding:.9rem 1.1rem;margin:0 0 1.25rem}
.share-box input{width:100%;padding:.5rem .6rem;font:inherit;font-size:.85rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid var(--rule);
  border-radius:8px;margin:.4rem 0;background:var(--paper)}
.rev{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem;color:var(--muted)}
.hp{position:absolute;left:-9999px;top:-9999px}
"""

_FORM = """
<div class="signin">
<h1>Create your CommonTrace account</h1>
<p class="sub">Free plan: 1,000 traces, 5 agents, 20 Knowledge Base queries/month. No credit
card. Your API key is shown once on the next page, so keep this tab open until you copy it.</p>
{error}
<form method="post" action="{path}">
<label>Organization or team name<input name="org_name" maxlength="200" required autofocus></label>
<div class="hp" aria-hidden="true">
<label>Leave this field blank<input name="website" tabindex="-1" autocomplete="off"></label>
</div>
<button type="submit">Create account</button>
</form>
</div>
"""

_ISSUED = """
<div class="signin">
<h1>Account created</h1>
<p class="sub">Store this key now -- it is never shown again, and anyone holding it has full
access to this organization's memory.</p>
<div class="share-box"><b>Your API key</b>
<input type="text" readonly value="{key}" onclick="this.select()"></div>
<p class="sub">Organization id: <span class="rev">{org_id}</span></p>
<p><a href="{console_path}/signin">Sign in to the console →</a>, or use this key directly with
the <code>commontrace</code> CLI / an MCP client.</p>
</div>
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{h(title)} · CommonTrace</title><style>{_CSS}{_EXTRA_CSS}</style></head><body>"
        '<header class="bar"><div class="in"><b>CommonTrace</b></div></header>'
        f"<main>{body}</main></body></html>",
        headers={"Cache-Control": "no-store, private", "Referrer-Policy": "same-origin",
                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"},
    )


def add_signup_routes(app, session_factory, *, trusted_proxy_hops: int = 0, console_path: str = "/app") -> None:
    """Mount the public signup routes. Call only when self-serve signup is
    enabled (HUB_SIGNUP_ENABLED) -- omitted entirely otherwise, the same
    absent-unless-configured posture `/admin` and `/app` already follow."""

    # Deliberately tight, and deliberately not CAPTCHA-strength: this Hub
    # has no CAPTCHA integration, so the rate limit is the only thing
    # standing between this route and a scripted flood of free-plan orgs.
    # burst=2 caps a legitimate retry (typo the org name, resubmit) without
    # a wait; per_minute=1 means a sustained attacker gets one more org per
    # minute per source address after that, not an unbounded rate.
    signup_limiter = RateLimiter(per_minute=1, burst=2)

    async def signup_page(request: Request) -> Response:
        return _page("Create account", _FORM.format(path=SIGNUP_PATH, error=""))

    async def signup(request: Request) -> Response:
        allowed, _retry_after = await signup_limiter.check(
            resolve_client_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return _page("Create account", _FORM.format(
                path=SIGNUP_PATH,
                error='<p class="err">Too many signups from this address. Try again shortly.</p>',
            ))
        form = await request.form()
        if str(form.get("website") or "").strip():
            # Honeypot tripped: a human never fills a field that is both
            # visually hidden and explicitly labeled "leave this blank".
            # Rejected the same way a validation failure is, so a bot's
            # response looks identical to a real one and gives it nothing
            # to distinguish "flagged as automated" from "made a mistake".
            logger.info("signup rejected: honeypot field filled")
            return _page("Create account", _FORM.format(
                path=SIGNUP_PATH,
                error='<p class="err">Something went wrong. Please try again.</p>',
            ))
        org_name = str(form.get("org_name") or "").strip()
        if not (MIN_NAME_CHARS <= len(org_name) <= MAX_NAME_CHARS):
            return _page("Create account", _FORM.format(
                path=SIGNUP_PATH,
                error=f'<p class="err">Organization name must be {MIN_NAME_CHARS}-'
                      f'{MAX_NAME_CHARS} characters.</p>',
            ))
        async with session_scope(session_factory) as session:
            org = Organization(name=org_name)
            session.add(org)
            await session.flush()
            issued = await issue_api_key(session, org.id)
            await audit.record(
                session, actor=ACTOR_SELF_SERVE_SIGNUP, action="create_org",
                org_id=org.id, target_type="org", target_id=org.id,
                summary=f"name={org_name!r} via self-serve signup",
            )
            org_id = org.id
        return _page("Account created", _ISSUED.format(
            key=h(issued.raw_key), org_id=h(org_id), console_path=console_path,
        ))

    app.add_route(SIGNUP_PATH, signup_page, methods=["GET"])
    app.add_route(SIGNUP_PATH, signup, methods=["POST"])
