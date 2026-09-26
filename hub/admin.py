"""The operator console, served by the Hub itself at /admin.

WHY THIS EXISTS
---------------
Every operator question this project can answer -- which orgs exist, what
they are using against their plan, whether retrieval is finding anything,
what needs review in the Knowledge Base -- was answerable only by running a
`python -m hub.manage ...` command and reading text. That is fine for an
operator who already knows what to ask. It is bad at the thing an operator
actually needs, which is *noticing*: a retrieval miss rate climbing, an org
sitting at its trace cap, a review queue growing. Nobody runs a command for
a question they have not thought to ask yet.

READ-WRITE FOR REVERSIBLE ACTIONS, NEVER FOR IRREVERSIBLE ONES
----------------------------------------------------------------
Before this module the Hub had NO browser-facing surface at all. Its whole
security posture follows from that: bearer tokens, no cookies, no sessions,
and hub/observability.py sets `X-Frame-Options: DENY` with a comment noting
there was nothing browser-rendered to protect.

A mutating operator action belongs here only if undoing a mistake made
through it costs nothing more than clicking the undo action: approving,
retracting, and restoring a Knowledge Base entry; releasing a quarantine;
placing and releasing a legal hold; setting and clearing a retention
policy. Every one of those calls the SAME `hub/crud.py`/`hub/retention.py`
function the CLI does, audited as `_ADMIN_ACTOR` ("operator-console") so
the trail distinguishes a web click from a terminal command, and is
protected the same way: authentication (HTTP Basic, the shared operator
token), an action-and-target-scoped CSRF token (`_csrf_token`/`_csrf_ok`),
and a `Sec-Fetch-Site` check (`_same_site`) as defence in depth behind it
-- CSRF matters specifically because Basic auth means a browser re-sends
the credential on every request, cross-site or not.

What stays CLI-only is drawn on the same line, not a shorter list because
this module grew: `purge-org`/`purge-trace`/`purge-subject-traces`
irreversibly destroy history, `retention-apply` permanently deletes
whatever a plan describes, and `issue-key`/`generate-encryption-key`
render a raw secret that would then live in browser history, in the page
cache, and in any screenshot of it. Those stay in a terminal that already
has an explicit confirmation prompt (`_confirm_destructive`) and writes an
audit row -- a hijacked BROWSER session must not be able to do what a
hijacked terminal session can.

ESCAPING IS THE SECURITY-CRITICAL PART OF THIS FILE
---------------------------------------------------
An operator's console renders content from EVERY tenant: trace titles, tags,
quarantine reasons, Knowledge Base submissions. All of it is
customer-supplied. Interpolating any of it into HTML unescaped is stored XSS
that crosses a tenant boundary and lands in the one browser session with
visibility over every customer. `h()` below is the only way text reaches the
page, and hub/tests/test_admin.py asserts that a trace titled with a
`<script>` tag renders inert.
"""

from __future__ import annotations

import hmac
import html
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from hub import audit as audit_module
from hub import auth, crud, events, manage, plans, rbac, retention, scopes
from hub.abuse import RateLimiter, resolve_client_key
from hub.config import HubConfig
from hub.db import session_scope
from hub.models import (
    ApiKey,
    AuditLogEntry,
    KnowledgeBaseSubmission,
    Organization,
    Trace,
    UsageCounter,
    User,
    Vote,
)

logger = logging.getLogger("commontrace.hub.admin")

ADMIN_PATH = "/admin"
# Every moderation decision made here writes the same audit row the CLI path
# writes, under an actor that says which surface it came from -- so "who
# published this entry" is answerable after the fact, and a console decision
# is distinguishable from a terminal one.
_ADMIN_ACTOR = "operator-console"
_REALM = "CommonTrace Hub operator console"

# How many rows each list renders. A console is for noticing, not for bulk
# export -- an unbounded query here would be a way to turn one page load
# into a full table scan of every tenant's traces.
_MAX_ROWS = 200
_AUDIT_ROWS = 40


def h(value: object) -> str:
    """Escape anything before it reaches the page.

    `quote=True` so the result is safe inside an attribute as well as in
    text. See this module's docstring: the content passing through here is
    customer-supplied and the reader is the one operator with cross-tenant
    visibility.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _num(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else h(value)


def _limit(value: object) -> str:
    """A plan limit, where UNLIMITED is a sentinel rather than a number."""
    return "unlimited" if value == plans.UNLIMITED else _num(value)


# --- Chrome -----------------------------------------------------------------

_CSS = """
:root{
  --paper:#F6F7F8; --surface:#FFF; --ink:#111820; --muted:#5A6672;
  --rule:#DDE2E7; --rule-soft:#EAEEF1; --accent:#23506B;
  --ok:#1D6B45; --warn:#8A5D12; --bad:#A33C2B; --code:#EEF2F5;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#0C1216; --surface:#131B21; --ink:#E5EAEE; --muted:#93A1AC;
  --rule:#25313A; --rule-soft:#1B242B; --accent:#79B0CD;
  --ok:#4EA878; --warn:#D2A149; --bad:#D86F5B; --code:#101820;
}}
*{box-sizing:border-box}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--accent)}
code,.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
header.bar{border-bottom:1px solid var(--rule);background:var(--surface)}
header.bar .in{max-width:1100px;margin:0 auto;padding:.85rem 1.25rem;
  display:flex;align-items:baseline;gap:1.25rem;flex-wrap:wrap}
header.bar b{font-size:.95rem;letter-spacing:.01em}
header.bar .ro{font-family:ui-monospace,monospace;font-size:.66rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted);border:1px solid var(--rule);
  padding:.15rem .45rem;border-radius:2px}
header.bar nav{margin-left:auto;display:flex;flex-wrap:wrap;gap:.35rem 1rem;font-size:.9rem}
.live-toggle{font:inherit;font-size:.72rem;padding:.1rem .45rem;border:1px solid var(--rule);
  border-radius:2px;background:var(--surface);color:var(--muted);cursor:pointer}
.live-toggle[aria-pressed=true]{color:var(--ink);border-color:var(--ink)}
header.bar nav a[aria-current=page]{color:var(--ink);font-weight:600;text-decoration:none}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
.skip{position:absolute;left:-9999px;top:0}.skip:focus{left:1rem;top:.5rem;z-index:10;
  background:var(--surface);color:var(--ink);padding:.4rem .7rem;border:1px solid var(--rule)}
@media (max-width:640px){header.bar nav{margin-left:0;width:100%}}
main{max-width:1100px;margin:0 auto;padding:2rem 1.25rem 4rem;
  display:flex;flex-direction:column;gap:2.25rem}
h1{font-size:1.5rem;margin:0 0 .2rem;letter-spacing:-.01em}
h2{font-size:1.05rem;margin:0 0 .75rem;padding-bottom:.4rem;border-bottom:1px solid var(--rule)}
.sub{color:var(--muted);font-size:.9rem;margin:0}
section{display:flex;flex-direction:column}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.tile{background:var(--surface);padding:.85rem 1rem}
.tile .k{font-family:ui-monospace,monospace;font-size:.63rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted)}
.tile .v{font-size:1.5rem;font-variant-numeric:tabular-nums;line-height:1.2;margin-top:.15rem}
.tile .v.warn{color:var(--warn)} .tile .v.bad{color:var(--bad)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;font-family:ui-monospace,monospace;font-size:.64rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);font-weight:500;
  padding:0 .8rem .45rem 0;border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:.55rem .8rem .55rem 0;border-bottom:1px solid var(--rule-soft);vertical-align:top}
td.n{font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:1px solid var(--rule)}
.pill{display:inline-block;font-family:ui-monospace,monospace;font-size:.66rem;
  letter-spacing:.04em;padding:.1rem .4rem;border-radius:2px;border:1px solid currentColor}
.pill.ok{color:var(--ok)} .pill.warn{color:var(--warn)} .pill.bad{color:var(--bad)}
.pill.mute{color:var(--muted)}
.concerns{margin-top:.3rem;display:flex;flex-wrap:wrap;gap:.25rem}
.cmds{display:flex;flex-direction:column;gap:.5rem}
.cmd{display:grid;grid-template-columns:minmax(11rem,auto) 1fr;gap:.3rem 1rem;align-items:baseline}
.cmd .what{color:var(--muted);font-size:.88rem}
.cmd code{background:var(--code);padding:.25rem .5rem;border-radius:2px;
  display:block;overflow-x:auto;white-space:pre}
.note{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
  padding:.9rem 1.1rem;font-size:.9rem;color:var(--muted)}
.note b{color:var(--ink)}
.empty{color:var(--muted);font-size:.9rem;padding:.6rem 0}
.cards{display:flex;flex-direction:column;gap:1rem}
.card{background:var(--surface);border:1px solid var(--rule);padding:1.1rem 1.25rem;
  display:flex;flex-direction:column;gap:.7rem}
.card h3{font-size:1.02rem;margin:0}
.card .meta{color:var(--muted);font-size:.82rem;margin:0}
.field{display:flex;flex-direction:column;gap:.15rem}
.field .lbl{font-family:ui-monospace,monospace;font-size:.62rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted)}
.field p{margin:0;font-size:.92rem;max-width:70ch;white-space:pre-wrap}
.tags{display:flex;gap:.35rem;flex-wrap:wrap}
.acts{display:flex;gap:.75rem;flex-wrap:wrap;align-items:center;
  border-top:1px solid var(--rule-soft);padding-top:.75rem}
form.act{display:flex;gap:.4rem;align-items:center;margin:0}
form.act input[type=text]{font:inherit;font-size:.85rem;padding:.3rem .45rem;
  border:1px solid var(--rule);border-radius:2px;background:var(--paper);color:var(--ink);
  min-width:12rem}
.btn{font:inherit;font-size:.85rem;font-weight:600;padding:.35rem .8rem;cursor:pointer;
  border:1px solid var(--muted);border-radius:2px;background:var(--paper);color:var(--ink)}
.btn:hover{border-color:var(--ink)}
.btn.ok{border-color:var(--ok);color:var(--ok)}
.btn.warn{border-color:var(--warn);color:var(--warn)}
.btn:focus-visible,form.act input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.disabled{color:var(--muted);font-size:.85rem;font-style:italic}
.flash{background:var(--surface);border:1px solid var(--ok);border-left:3px solid var(--ok);
  padding:.75rem 1.1rem;font-size:.9rem}
form.stack{display:flex;flex-direction:column;gap:.7rem;margin:0;max-width:40rem}
form.stack label{font-family:ui-monospace,monospace;font-size:.62rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);display:block;margin-bottom:.2rem}
form.stack input[type=text],form.stack textarea{font:inherit;font-size:.9rem;
  padding:.4rem .55rem;border:1px solid var(--rule);border-radius:2px;
  background:var(--paper);color:var(--ink);width:100%}
form.stack textarea{min-height:5rem;resize:vertical;font-family:inherit}
form.stack input:focus-visible,form.stack textarea:focus-visible{
  outline:2px solid var(--accent);outline-offset:1px}
@media (max-width:640px){.cmd{grid-template-columns:1fr}
  form.act{flex-wrap:wrap}form.act input:not([type=hidden]):not([type=checkbox]),
  form.act select{min-width:0;flex:1 1 9rem}}
"""


def auto_refresh_script(seconds: int) -> str:
    """A dependency-free "this page is live" mechanism: reloads on a
    timer, so an operator watching for a queue to grow or a rate to
    climb (this console's whole reason to exist -- see the module
    docstring's "actually needs, which is *noticing*") does not have to
    remember to hit reload.

    Skipped, and retried shortly after, while the visitor has a form
    control focused -- a background reload that wiped a half-typed
    reason or subject id would make "live" read as "broken", and this
    page has more forms on it than most. Scroll position round-trips
    through sessionStorage because a plain reload otherwise snaps back
    to the top of what can be a long page.
    """
    return (
        "<script>(function(){"
        "var KEY='ct-scroll-'+location.pathname+location.search,PAUSE='ct-live-paused',timer=null;"
        "var y=sessionStorage.getItem(KEY);"
        "if(y!==null){window.scrollTo(0,parseInt(y,10)||0);sessionStorage.removeItem(KEY);}"
        "function paused(){try{return localStorage.getItem(PAUSE)==='1';}catch(e){return false;}}"
        "var btn=document.querySelector('[data-live-toggle]');"
        "function label(){if(!btn)return;var p=paused();btn.hidden=false;"
        "btn.textContent=p?'Resume live updates':'Pause live updates';"
        "btn.setAttribute('aria-pressed',p?'true':'false');}"
        "function isEditing(){var el=document.activeElement;"
        "return !!el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.tagName==='SELECT');}"
        # A tab nobody is looking at does not reload: it waits until it is
        # shown again, instead of re-running the page's queries (up to 1.4s
        # of statistics on the console overview) every few seconds forever.
        "function tick(){if(paused())return;"
        "if(document.hidden){document.addEventListener('visibilitychange',function v(){"
        "if(!document.hidden){document.removeEventListener('visibilitychange',v);tick();}});return;}"
        "if(isEditing()){timer=setTimeout(tick,3000);return;}"
        "sessionStorage.setItem(KEY,String(window.scrollY));location.reload();}"
        f"function schedule(){{clearTimeout(timer);if(!paused())timer=setTimeout(tick,{int(seconds)}*1000);}}"
        "if(btn)btn.addEventListener('click',function(){"
        "try{localStorage.setItem(PAUSE,paused()?'0':'1');}catch(e){}label();schedule();});"
        "label();schedule();"
        "})();</script>"
    )


def live_badge(seconds: int) -> str:
    """The header's "live" marker for an auto-refreshing page, and the
    control that pauses it. WCAG 2.2.1 asks that a time limit -- here, a
    reload every few seconds -- can be turned off: someone reading slowly,
    or with a screen reader, loses their place on every reload. The button
    stays hidden unless the refresh script runs, and the choice is kept for
    every page in this browser."""
    if not seconds:
        return ""
    return (
        f'<span class="ro" title="Refreshes automatically every {int(seconds)}s '
        'unless you are typing in a field or have paused it">live</span>'
        '<button type="button" class="live-toggle" data-live-toggle hidden>Pause live updates</button>'
    )


# Every form here is POST-then-redirect (see _back's own comment), so a
# double click or an impatient second click while the first request is
# still in flight would fire the SAME mutation twice before either
# response comes back -- harmless for an idempotent one, but a second
# "issue a key" or "purge" click is not a no-op. Marking the form as
# in-flight and cancelling any SECOND submit closes that window without a
# network call of its own; the browser's native first submit proceeds
# untouched.
#
# It deliberately does NOT disable the clicked button, which is what an
# earlier version of this guard did. A disabled control is barred from
# form submission, so disabling the submitter drops its own name/value
# pair -- and a multi-button form carries its ACTION there
# (`<button name="vote" value="up">`). That silently turned "vote up"
# into a request with no vote at all. Caught in a real browser; every
# test here posts with httpx, which runs no JavaScript and so could not
# have seen it. The class is cosmetic precisely so that nothing about
# what gets submitted depends on this script running.
#
# It also carries the pages' two other behaviours, so that no page needs an
# inline event handler (which the Content-Security-Policy below forbids):
# a form with `data-confirm` asks first, and a read-only input with
# `data-autoselect` selects itself on click, for copying a key or link.
_FORM_GUARD_SCRIPT = (
    "<script>document.addEventListener('submit',function(ev){"
    "var f=ev.target;"
    "if(f.dataset.ctSubmitting==='1'){ev.preventDefault();return;}"
    "if(ev.defaultPrevented)return;"
    "if(f.dataset.confirm&&!window.confirm(f.dataset.confirm)){ev.preventDefault();return;}"
    "f.dataset.ctSubmitting='1';"
    "if(ev.submitter){ev.submitter.classList.add('busy');}"
    "},true);"
    "document.addEventListener('click',function(ev){"
    "var t=ev.target;if(t&&t.matches&&t.matches('input[data-autoselect]')){t.select();}"
    "});</script>"
)


def content_security_policy(*scripts: str) -> str:
    """A Content-Security-Policy that lets exactly these inline scripts run.

    Every page here is server-rendered with its scripts inline, so the
    policy allows each by its SHA-256 and nothing else: text that escaped
    `h()` by some future mistake still could not run. Styles stay inline
    (style attributes cannot be hashed), and forms may post only to this
    origin or to Stripe, where the billing buttons redirect.
    """
    import base64
    import hashlib
    import re

    hashes = []
    for script in scripts:
        for body in re.findall(r"<script>(.*?)</script>", script, flags=re.S):
            digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
            hashes.append(f"'sha256-{digest}'")
    return (
        "default-src 'none'; "
        f"script-src {' '.join(hashes) if hashes else chr(39) + 'none' + chr(39)}; "
        "style-src 'unsafe-inline'; img-src data:; "
        "form-action 'self' https://*.stripe.com; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


# Headers every HTML page here sends: the policy above, and no caching --
# this is live tenant data, and a cached copy in a shared browser is one
# more place it sits at rest.
def html_headers(*scripts: str, referrer: str = "same-origin") -> dict[str, str]:
    return {
        "Cache-Control": "no-store, private", "Referrer-Policy": referrer,
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": content_security_policy(*scripts),
    }


def _page(title: str, body: str, *, auto_refresh_seconds: int = 0) -> HTMLResponse:
    badge = live_badge(auto_refresh_seconds)
    refresh_script = auto_refresh_script(auto_refresh_seconds) if auto_refresh_seconds else ""
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{h(title)} · CommonTrace Hub</title><style>{_CSS}</style></head><body>"
        "<header class=\"bar\"><div class=\"in\"><b>CommonTrace Hub</b>"
        "<span class=\"ro\">operator console</span>"
        f"{badge}"
        f"<nav><a href=\"{ADMIN_PATH}\">Overview</a>"
        f"<a href=\"{ADMIN_PATH}/kb\">Knowledge Base</a>"
        "<a href=\"/metrics\">Metrics</a></nav></div></header>"
        f"<main>{body}</main>{refresh_script}{_FORM_GUARD_SCRIPT}</body></html>",
        headers=html_headers(refresh_script, _FORM_GUARD_SCRIPT),
    )


def _tile(key: str, value: str, tone: str = "") -> str:
    cls = f" {tone}" if tone else ""
    return f'<div class="tile"><div class="k">{h(key)}</div><div class="v{cls}">{value}</div></div>'


def _cmd(what: str, command: str) -> str:
    return f'<div class="cmd"><div class="what">{h(what)}</div><code>{h(command)}</code></div>'


# --- Auth -------------------------------------------------------------------


# WHICH ACTIONS THIS CONSOLE MAY PERFORM
# --------------------------------------
# The original rule here was "read-only, everything else is CLI". That was
# right about the destructive actions and wrong about moderation, and the
# difference is reversibility:
#
#   IRREVERSIBLE or CREDENTIAL-BEARING -> stays in the CLI.
#     purge-org destroys one customer's entire history for good; issue-key
#     and rotate-key render a live credential that would then sit in browser
#     history, the page cache, and any screenshot. A hijacked console session
#     must not reach these.
#
#   REVERSIBLE MODERATION -> belongs here.
#     Accepting a submission publishes an entry that kb-retract withdraws;
#     retract and restore are an explicitly reversible pair. These are also
#     the HIGH-FREQUENCY actions: a Knowledge Base is only as good as its
#     review queue, and a queue that can only be worked from a terminal is a
#     queue that does not get worked. Every one of them writes the same audit
#     row the CLI path writes.
#
# CSRF matters specifically because auth here is HTTP Basic: a browser
# re-sends those credentials on a cross-site form POST, so a mutating
# endpoint without a token is forgeable by any page the operator visits while
# authenticated. The token below is an HMAC of the action and its target
# under the admin secret -- unforgeable without that secret, and scoped so a
# token minted for "reject submission X" cannot be replayed as "approve
# submission Y".
def _csrf_token(admin_token: str, action: str, target: str) -> str:
    import hashlib
    return hmac.new(
        admin_token.encode("utf-8"), f"{action}:{target}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _csrf_ok(admin_token: str, action: str, target: str, presented: str) -> bool:
    return hmac.compare_digest(_csrf_token(admin_token, action, target), presented or "")


def _same_site(request: Request) -> bool:
    """Reject a cross-site form post outright where the browser tells us.

    Defence in depth behind the HMAC, not instead of it: `Sec-Fetch-Site` is
    set by current browsers and cannot be forged by page script, but it is
    absent on older ones -- so a missing header is allowed through to the
    token check rather than treated as an attack.
    """
    site = request.headers.get("sec-fetch-site")
    return site in (None, "", "same-origin", "same-site", "none")


def _unauthorized() -> Response:
    return Response(
        "authentication required",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"'},
    )


def _authorized(request: Request, token: str) -> bool:
    """HTTP Basic, checked in constant time.

    Basic rather than a login form and a session cookie: this console is
    read-only, so there is no state-changing request for a cookie to be
    replayed against, and skipping sessions entirely means there is no
    session fixation, no CSRF token to get wrong, and nothing to invalidate
    on logout. The browser handles the credential prompt, and the credential
    travels exactly as the Hub's existing bearer tokens do -- which is why
    the Hub refuses to serve a non-loopback bind without an explicit
    acknowledgment that TLS terminates in front (hub/config.py).

    The username is ignored; the password is the shared operator token.
    `hmac.compare_digest` rather than `==` so a wrong token cannot be
    recovered a character at a time from response timing.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    import base64
    import binascii

    try:
        decoded = base64.b64decode(header[len("basic "):].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    _, _, presented = decoded.partition(":")
    return hmac.compare_digest(presented, token)


# --- Data -------------------------------------------------------------------


async def _overview(session) -> dict:
    """Fleet-wide counts plus a row per org. One query per fact, not one per
    org: an operator with fifty tenants should not pay fifty round trips to
    load a page they open all day."""
    # Bounded like every other list here. An operator with thousands of
    # tenants should not have one page load render all of them -- and an
    # unbounded query on the page an operator leaves open all day is a slow
    # self-inflicted load problem.
    orgs = (await session.execute(
        select(Organization).order_by(Organization.name).limit(_MAX_ROWS)
    )).scalars().all()
    total_orgs = int(await session.scalar(select(func.count()).select_from(Organization)) or 0)

    traces_by_org = dict(
        (await session.execute(
            select(Trace.org_id, func.count()).group_by(Trace.org_id)
        )).all()
    )
    quarantined_by_org = dict(
        (await session.execute(
            select(Trace.org_id, func.count()).where(Trace.quarantined.is_(True)).group_by(Trace.org_id)
        )).all()
    )
    keys_by_org = dict(
        (await session.execute(
            select(ApiKey.org_id, func.count())
            .where(ApiKey.revoked_at.is_(None))
            .group_by(ApiKey.org_id)
        )).all()
    )

    rows = []
    for org in orgs:
        rows.append({
            "id": org.id,
            "name": org.name,
            "plan": org.plan,
            "created_at": org.created_at,
            "traces": int(traces_by_org.get(org.id, 0)),
            "quarantined": int(quarantined_by_org.get(org.id, 0)),
            "active_keys": int(keys_by_org.get(org.id, 0)),
            "max_traces": plans.get(org.plan).max_traces,
        })

    return {
        "orgs": rows,
        "total_orgs": total_orgs,
        "truncated": total_orgs > len(rows),
        # Summed from the *_by_org dicts, not `rows`: those group-by queries
        # are already unrestricted across every org (no `orgs` join, no
        # _MAX_ROWS on them), so they hold the true fleet-wide counts. `rows`
        # covers only the first _MAX_ROWS organizations by name -- summing
        # from it silently undercounted these three tiles the moment an
        # operator had more than _MAX_ROWS organizations, with no truncation
        # notice near the tiles to say so (the one that exists is below the
        # per-org table, nowhere near the top-line summary an operator scans
        # first).
        "total_traces": sum(traces_by_org.values()),
        "total_quarantined": sum(quarantined_by_org.values()),
        "total_keys": sum(keys_by_org.values()),
        "total_votes": int(await session.scalar(select(func.count()).select_from(Vote)) or 0),
    }


async def _org_detail(session, org_id: str) -> dict | None:
    org = await session.get(Organization, org_id)
    if org is None:
        return None

    ent = await crud.entitlements(session, org_id)
    agents = await crud.agents_under_management(session, org_id)
    health = await crud.search_health(session, org_id)

    keys = (await session.execute(
        select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc())
    )).scalars().all()

    quarantined = (await session.execute(
        select(Trace)
        .where(Trace.org_id == org_id, Trace.quarantined.is_(True))
        .order_by(Trace.created_at.desc())
        .limit(_MAX_ROWS)
    )).scalars().all()

    audit = (await session.execute(
        select(AuditLogEntry)
        .where(AuditLogEntry.org_id == org_id)
        .order_by(AuditLogEntry.created_at.desc())
        .limit(_AUDIT_ROWS)
    )).scalars().all()

    policies = await retention.policies_for(session, org_id)
    holds = await retention.active_holds(session, org_id)

    users = (await session.execute(
        select(User).where(User.org_id == org_id).order_by(User.created_at)
    )).scalars().all()

    return {
        "org": org, "entitlements": ent, "agents": agents, "health": health,
        "keys": keys, "quarantined": quarantined, "audit": audit,
        "policies": policies, "holds": holds, "users": users,
    }


async def _kb_data(session) -> dict:
    """Everything the Knowledge Base page renders, in one pass.

    `pending` reads the submission table directly rather than through
    crud.list_kb_submissions because the operator needs one field that
    projection deliberately omits: WHICH ORG proposed it. That omission is
    right for the customer-facing tool (an org has no business knowing who
    else submits) and wrong here -- accepting credits that org's allowance,
    so the reviewer has to see it.
    """
    corpus = int(await session.scalar(
        select(func.count()).select_from(Trace).where(*crud.commons_visible())
    ) or 0)
    hits = int(await session.scalar(
        select(func.coalesce(func.sum(Trace.commons_hits), 0))
        .select_from(Trace).where(*crud.commons_visible())
    ) or 0)
    consuming = int(await session.scalar(
        select(func.count(func.distinct(UsageCounter.org_id)))
        .where(UsageCounter.metric == crud.METRIC_COMMONS_QUERIES)
    ) or 0)

    rows = (await session.execute(
        select(KnowledgeBaseSubmission)
        .where(KnowledgeBaseSubmission.status == "pending")
        .order_by(KnowledgeBaseSubmission.created_at)
        .limit(_MAX_ROWS)
    )).scalars().all()
    pending = [{
        "id": s.id, "org_id": s.org_id, "title": s.title,
        "context_text": s.context_text, "solution_text": s.solution_text,
        "tags": list(s.tags or []), "agent_type": s.agent_type,
        "rationale": s.rationale, "created_at": _iso(s.created_at),
    } for s in rows]
    pending_total = int(await session.scalar(
        select(func.count()).select_from(KnowledgeBaseSubmission)
        .where(KnowledgeBaseSubmission.status == "pending")
    ) or 0)

    retracted_rows = (await session.execute(
        select(Trace)
        .where(Trace.commons_source == "seed", Trace.commons_retracted_at.isnot(None))
        .order_by(Trace.commons_retracted_at.desc())
        .limit(_MAX_ROWS)
    )).scalars().all()
    retracted = [{
        "id": t.id, "title": t.title,
        "reason": t.commons_retraction_reason, "at": t.commons_retracted_at,
    } for t in retracted_rows]
    retracted_total = int(await session.scalar(
        select(func.count()).select_from(Trace)
        .where(Trace.commons_source == "seed", Trace.commons_retracted_at.isnot(None))
    ) or 0)

    result = {
        "corpus": corpus, "hits": hits, "consuming_orgs": consuming,
        "pending": pending, "pending_total": pending_total,
        "retracted": retracted, "retracted_total": retracted_total,
    }
    # A summary tile from the CAPPED list above (len(queue)) would silently
    # read as a total and stop matching the actual queue size past
    # _MAX_ROWS, exactly like the overview page's fleet-wide tiles once did
    # (see _overview's fix) -- queue_total is the true, unbounded count
    # behind the same classification `queue` (bounded, for the table below)
    # already uses. One call, not kb_review_queue + count_kb_review_queue
    # separately: each independently re-runs the same Trace + Vote queries
    # this dashboard load would otherwise pay for twice.
    queue, queue_total = await crud.kb_review_queue_and_total(session, limit=_MAX_ROWS)
    return {**result, "queue": queue, "queue_total": queue_total}


# --- Rendering --------------------------------------------------------------


def _render_overview(data: dict, admin_token: str, flash: str = "", fresh_key: str = "") -> str:
    tiles = "".join([
        _tile("organizations", _num(data.get("total_orgs", len(data["orgs"])))),
        _tile("traces", _num(data["total_traces"])),
        _tile("quarantined", _num(data["total_quarantined"]),
              "warn" if data["total_quarantined"] else ""),
        _tile("active keys", _num(data["total_keys"])),
        _tile("votes", _num(data["total_votes"])),
    ])

    if data["orgs"]:
        body = []
        for r in data["orgs"]:
            cap = r["max_traces"]
            near_cap = cap != plans.UNLIMITED and cap > 0 and r["traces"] >= cap * 0.8
            usage = f'{_num(r["traces"])} / {_limit(cap)}'
            if near_cap:
                usage += ' <span class="pill warn">near cap</span>'
            quar = (f'<span class="pill warn">{_num(r["quarantined"])}</span>'
                    if r["quarantined"] else '<span class="pill mute">0</span>')
            keys = (_num(r["active_keys"]) if r["active_keys"]
                    else '<span class="pill bad">none</span>')
            body.append(
                f'<tr><td><a href="{ADMIN_PATH}/org/{h(r["id"])}">{h(r["name"])}</a><br>'
                f'<span class="m" style="color:var(--muted)">{h(r["id"])}</span></td>'
                f'<td><span class="pill mute">{h(r["plan"])}</span></td>'
                f'<td class="n">{usage}</td><td class="n">{quar}</td>'
                f'<td class="n">{keys}</td><td class="n">{_iso(r["created_at"])}</td></tr>'
            )
        table = (
            '<div class="scroll"><table><thead><tr><th>Organization</th><th>Plan</th>'
            '<th>Traces</th><th>Quarantined</th><th>Active keys</th><th>Created</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        )
    else:
        table = ('<p class="empty">No organizations yet. Create one with '
                 '<code>python -m hub.manage create-org "Acme"</code>.</p>')

    if data.get("truncated"):
        table += (f'<p class="empty">Showing the first {_MAX_ROWS} organizations by name, of '
                  f'{_num(data["total_orgs"])}. Use <code>python -m hub.manage list-orgs</code> '
                  f'for the full list.</p>')

    flash_html = f'<div class="flash">{h(flash)}</div>' if flash else ""
    fresh_key_html = ""
    if fresh_key:
        fresh_key_html = (
            '<div class="flash" style="border-color:var(--warn)">'
            '<b>New encryption key -- shown once, not stored anywhere</b><br>'
            f'<code style="user-select:all">{h(fresh_key)}</code>'
            '<p class="empty" style="margin-top:.4rem">Set as <code>HUB_ENCRYPTION_KEY</code> in '
            "this deployment's secret store. Rotating: move the current value into "
            '<code>HUB_ENCRYPTION_KEY_PREVIOUS</code> first, so values already encrypted under '
            'it still decrypt.</p></div>'
        )
    create_form = (
        f'<form method="post" action="{ADMIN_PATH}/create-org" class="act" '
        'style="margin-top:1rem">'
        '<input type="hidden" name="target" value="new">'
        f'<input type="hidden" name="csrf" value="{h(_csrf_token(admin_token, "create_org", "new"))}">'
        '<label class="sr-only" for="new-org-name">Organization name</label>'
        '<input type="text" id="new-org-name" name="name" placeholder="Organization name" '
        'required style="min-width:16rem">'
        '<button type="submit" class="btn">Create organization</button></form>'
    )
    generate_key_form = (
        f'<form method="post" action="{ADMIN_PATH}/generate-encryption-key" class="act">'
        '<input type="hidden" name="target" value="new">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "generate_encryption_key", "new"))}">'
        '<button type="submit" class="btn">Generate a new encryption key</button></form>'
        '<p class="empty" style="margin-top:.4rem">A deployment-wide utility, not tied to any '
        'organization or row in this database -- generating one changes nothing until you set '
        'it as <code>HUB_ENCRYPTION_KEY</code> yourself.</p>'
    )

    return (
        f'<section><h1>Overview</h1>'
        f'<p class="sub">Every organization on this Hub, and what it is using.</p>{flash_html}'
        f'{fresh_key_html}</section>'
        f'<section><div class="tiles">{tiles}</div></section>'
        f'<section><h2>Organizations</h2>{table}{create_form}</section>'
        f'<section><h2>Encryption key</h2>{generate_key_form}</section>'
        '<section><h2>Operator commands</h2>'
        '<div class="note">The line this console draws is <b>reversibility</b>: every '
        'mutating action reachable from a button here (creating an org, changing a plan, '
        'issuing/rotating a key, a legal hold, a retention policy, purging a specific trace '
        'or organization) can be undone or is itself the undo of a mistake, and the '
        'irreversible ones (purge-org, purge-trace, purge-subject-traces, retention-apply) '
        'require retyping the exact id/name being destroyed on top of the same CSRF token '
        'every action here needs. What is NOT here: anything with no organization or row to '
        'scope it to, or that this Hub has no way to make more confirmable than a terminal '
        'already is. See <b>hub/admin.py</b> for the full reasoning.</div>'
        '<div class="cmds" style="margin-top:1rem">'
        + _cmd("Fleet-wide counts", "python -m hub.manage stats")
        + _cmd("Revenue by plan", "python -m hub.manage revenue")
        + '</div></section>'
    )


def _render_org(d: dict, admin_token: str, flash: str = "", fresh_key: str = "") -> str:
    org, ent, agents, health = d["org"], d["entitlements"], d["agents"], d["health"]
    q = ent.get("commons_queries", {})
    tr = ent.get("traces", {})

    miss = health.get("miss_rate")
    miss_txt = "—" if miss is None else f"{miss:.0%}"
    miss_tone = "" if miss is None else ("bad" if miss >= 0.5 else "warn" if miss >= 0.25 else "")

    agent_count = agents.get("active", 0)
    agent_txt = f'{_num(agent_count)}{"+" if agents.get("is_floor") else ""}'

    tiles = "".join([
        _tile("plan", f'<span class="pill mute">{h(ent.get("plan"))}</span>'),
        _tile("traces", f'{_num(tr.get("used"))} / {_limit(tr.get("limit"))}'),
        _tile("agents (30d)", agent_txt),
        _tile("kb queries", f'{_num(q.get("used"))} / {_limit(q.get("allowance"))}'),
        _tile("searches", _num(health.get("searches", 0))),
        _tile("miss rate", miss_txt, miss_tone),
    ])

    floor_note = ""
    if agents.get("is_floor"):
        floor_note = (
            '<div class="note" style="margin-top:1rem">The agent count is a '
            '<b>floor</b>, not a total: this org has traces whose client sent no '
            '<code>agent_id</code>, and an unknown number of real agents hides behind '
            'the single sentinel they collapse into.</div>'
        )

    plan_options = "".join(
        f'<option value="{h(name)}"{" selected" if name == ent.get("plan") else ""}>{h(name)}</option>'
        for name in plans.PLANS
    )
    plan_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/set-plan" class="act" '
        'style="margin-top:.75rem">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "set_plan", org.id))}">'
        '<label class="sr-only" for="org-plan-select">Plan</label>'
        f'<select id="org-plan-select" name="plan_name">{plan_options}</select> '
        '<button type="submit" class="btn">Change plan</button></form>'
    )

    if d["keys"]:
        rows = []
        for k in d["keys"]:
            if k.revoked_at is not None:
                state = '<span class="pill bad">revoked</span>'
            elif k.expires_at is not None and k.expires_at <= datetime.now(timezone.utc):
                state = '<span class="pill bad">expired</span>'
            elif k.expires_at is None:
                state = '<span class="pill warn">never expires</span>'
            else:
                state = '<span class="pill ok">active</span>'
            key_actions = ""
            if k.revoked_at is None:
                key_actions = (
                    f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/keys/rotate" '
                    'class="act" style="display:inline">'
                    f'<input type="hidden" name="key_id" value="{h(k.id)}">'
                    f'<input type="hidden" name="csrf" '
                    f'value="{h(_csrf_token(admin_token, "rotate_key", str(k.id)))}">'
                    '<button type="submit" class="btn">Rotate</button></form> '
                    f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/keys/revoke" '
                    'class="act" style="display:inline">'
                    f'<input type="hidden" name="key_id" value="{h(k.id)}">'
                    f'<input type="hidden" name="csrf" '
                    f'value="{h(_csrf_token(admin_token, "revoke_key", str(k.id)))}">'
                    '<button type="submit" class="btn">Revoke</button></form>'
                )
            rows.append(
                f'<tr><td class="m">{h(k.key_prefix)}…</td><td>{state}</td>'
                f'<td class="n">{_iso(k.created_at)}</td>'
                f'<td class="n">{_iso(k.expires_at)}</td>'
                f'<td class="n">{_iso(k.last_used_at)}</td>'
                f'<td class="m" style="color:var(--muted)">{h(k.id)}</td>'
                f'<td>{key_actions}</td></tr>'
            )
        keys_tbl = ('<div class="scroll"><table><thead><tr><th>Prefix</th><th>State</th>'
                    '<th>Created</th><th>Expires</th><th>Last used</th><th>Key id</th>'
                    f'<th>Action</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    else:
        keys_tbl = '<p class="empty">No keys issued — this org cannot reach the Hub yet.</p>'
    fresh_key_html = ""
    if fresh_key:
        fresh_key_html = (
            '<div class="flash" style="border-color:var(--warn)">'
            '<b id="fresh-key-label">New key -- shown once, never stored anywhere as plaintext</b><br>'
            f'<code style="user-select:all">{h(fresh_key)}</code></div>'
        )
    key_scope_options = "".join(
        f'<label><input type="checkbox" name="scopes" value="{h(s)}"'
        f'{" checked" if s != scopes.SCOPE_SCIM else ""}> {h(s)}</label> '
        for s in scopes.ALL_SCOPES
    )
    issue_key_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/keys/issue" class="act" '
        'style="margin-top:.75rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "issue_key", org.id))}">'
        f'<fieldset style="border:0;padding:0;display:contents"><legend class="sr-only">Scopes'
        f'</legend>{key_scope_options}</fieldset>'
        '<label class="sr-only" for="issue-key-expires">Expires in N days</label>'
        '<input type="number" id="issue-key-expires" name="expires_days" min="1" '
        'placeholder="expires in N days (blank = never)">'
        '<button type="submit" class="btn warn">Issue a key</button></form>'
        '<p class="empty" style="margin-top:.4rem">Needed once, at onboarding: a brand-new '
        "organization has no key yet, so it cannot sign into its own console to issue one "
        "itself.</p>"
    )

    if d["quarantined"]:
        rows = "".join(
            f'<tr><td>{h(t.title)}</td><td>{h(t.quarantine_reason)}</td>'
            f'<td class="n">{_iso(t.created_at)}</td>'
            f'<td class="m" style="color:var(--muted)">{h(t.id)}</td>'
            f'<td><form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/quarantine/release" '
            'class="act">'
            f'<input type="hidden" name="trace_id" value="{h(t.id)}">'
            f'<input type="hidden" name="csrf" '
            f'value="{h(_csrf_token(admin_token, "release_quarantine", str(t.id)))}">'
            '<button type="submit" class="btn">Release</button></form></td></tr>'
            for t in d["quarantined"]
        )
        quar_tbl = ('<div class="scroll"><table><thead><tr><th>Title</th><th>Why</th>'
                    '<th>Captured</th><th>Trace id</th><th>Action</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table></div>')
    else:
        quar_tbl = '<p class="empty">Nothing quarantined.</p>'

    amend_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/amend-trace" class="stack">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "amend_trace", org.id))}">'
        '<div><label for="amend-trace-id">Trace id</label>'
        '<input type="text" id="amend-trace-id" name="trace_id" required></div>'
        '<div><label for="amend-title">Title (blank = unchanged)</label>'
        '<input type="text" id="amend-title" name="title"></div>'
        '<div><label for="amend-context">Context (blank = unchanged)</label>'
        '<textarea id="amend-context" name="context_text"></textarea></div>'
        '<div><label for="amend-solution">Solution (blank = unchanged)</label>'
        '<textarea id="amend-solution" name="solution_text"></textarea></div>'
        '<div><label for="amend-tags">Tags, comma-separated (blank = unchanged)</label>'
        '<input type="text" id="amend-tags" name="tags_csv"></div>'
        '<div><button type="submit" class="btn">Amend trace</button></div></form>'
        '<p class="empty" style="margin-top:.4rem">Creates a new trace that supersedes this '
        "one, carrying forward any field left blank -- the original is kept, not overwritten, "
        'so nothing is lost even if this is the wrong trace id.</p>'
    )

    holds = d.get("holds") or []
    if holds:
        rows = "".join(
            f'<tr><td>{h(hold.object_type or "everything")}'
            f'{("/" + h(hold.target_id)) if hold.target_id else ""}</td>'
            f'<td>{h(hold.reason)}</td><td class="n">{_iso(hold.placed_at)}</td>'
            f'<td class="m">{h(hold.placed_by)}</td>'
            f'<td><form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/legal-hold/release" '
            'class="act">'
            f'<input type="hidden" name="hold_id" value="{h(hold.id)}">'
            f'<input type="hidden" name="csrf" '
            f'value="{h(_csrf_token(admin_token, "release_hold", str(hold.id)))}">'
            f'<label class="sr-only" for="hold-reason-{h(hold.id)}">Release reason</label>'
            f'<input type="text" id="hold-reason-{h(hold.id)}" name="reason" maxlength="200" '
            'placeholder="reason (optional)">'
            '<button type="submit" class="btn">Release</button></form></td></tr>'
            for hold in holds
        )
        holds_tbl = ('<div class="scroll"><table><thead><tr><th>Scope</th><th>Reason</th>'
                     '<th>Placed</th><th>By</th><th>Action</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
    else:
        holds_tbl = '<p class="empty">No legal holds in force.</p>'
    hold_object_options = "".join(
        f'<option value="{h(name)}">{h(name)}</option>' for name in retention.KINDS
    )
    holds_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/legal-hold/place" '
        'class="act" style="margin-top:.75rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "place_hold", org.id))}">'
        '<label class="sr-only" for="hold-object-type">Object type (optional)</label>'
        f'<select id="hold-object-type" name="object_type"><option value="">everything</option>'
        f'{hold_object_options}</select> '
        '<label class="sr-only" for="hold-target-id">Specific id (optional)</label>'
        '<input type="text" id="hold-target-id" name="target_id" placeholder="specific id (optional)"> '
        '<label class="sr-only" for="hold-reason">Reason</label>'
        '<input type="text" id="hold-reason" name="reason" placeholder="reason" required maxlength="500" '
        'style="min-width:16rem"> '
        '<button type="submit" class="btn warn">Place hold</button></form>'
    )

    policies = d.get("policies") or []
    if policies:
        rows = "".join(
            f'<tr><td>{h(p.object_type)}</td><td>{h(p.status)}</td>'
            f'<td class="n">{_num(p.max_age_days)}d</td><td>{h(p.note or "—")}</td>'
            f'<td><form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/retention/clear" '
            'class="act">'
            f'<input type="hidden" name="object_type" value="{h(p.object_type)}">'
            f'<input type="hidden" name="status" value="{h(p.status)}">'
            f'<input type="hidden" name="target" value="{h(p.object_type)}/{h(p.status)}">'
            f'<input type="hidden" name="csrf" value="'
            f'{h(_csrf_token(admin_token, f"clear_retention:{org.id}", f"{p.object_type}/{p.status}"))}">'
            '<button type="submit" class="btn">Clear</button></form></td></tr>'
            for p in policies
        )
        policies_tbl = ('<div class="scroll"><table><thead><tr><th>Object type</th><th>Status</th>'
                        '<th>Keep for</th><th>Note</th><th>Action</th></tr></thead>'
                        f'<tbody>{rows}</tbody></table></div>')
    else:
        policies_tbl = '<p class="empty">No retention policy: nothing expires on its own.</p>'
    policy_object_options = "".join(
        f'<option value="{h(name)}">{h(name)}</option>' for name in retention.KINDS
    )
    policy_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/retention/set" '
        'class="act" style="margin-top:.75rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "set_retention", org.id))}">'
        '<label class="sr-only" for="policy-object-type">Object type</label>'
        f'<select id="policy-object-type" name="object_type">{policy_object_options}</select> '
        '<label class="sr-only" for="policy-status">Status (blank = any)</label>'
        '<input type="text" id="policy-status" name="status" placeholder="status (blank = any)"> '
        '<label class="sr-only" for="policy-days">Keep for N days</label>'
        '<input type="number" id="policy-days" name="days" min="1" placeholder="keep for N days" '
        'required> '
        '<button type="submit" class="btn">Set policy</button></form>'
        '<p class="empty" style="margin-top:.4rem">Setting a policy only changes what a plan '
        'WOULD delete.</p>'
    )
    preview = d.get("retention_preview")
    if preview:
        if preview["n_doomed"]:
            apply_form = (
                f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/retention/apply" '
                'class="act" style="margin-top:.5rem">'
                f'<input type="hidden" name="digest" value="{h(preview["digest"])}">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, "retention_apply", preview["digest"]))}">'
                '<label class="sr-only" for="retention-confirm">Type the digest to confirm</label>'
                '<input type="text" id="retention-confirm" name="confirm_digest" '
                'placeholder="paste the digest above to confirm" required style="width:20rem">'
                '<button type="submit" class="btn warn">Permanently delete these rows</button>'
                '</form>'
            )
        else:
            apply_form = ""
        retention_preview_html = (
            '<div class="flash" style="border-color:var(--warn)">'
            f'<pre style="white-space:pre-wrap;margin:0">{h(preview["render"])}</pre>'
            f'{apply_form}</div>'
        )
    else:
        retention_preview_html = (
            f'<form method="get" action="{ADMIN_PATH}/org/{h(org.id)}" class="act">'
            '<input type="hidden" name="preview_retention" value="1">'
            '<button type="submit" class="btn">Preview what a plan would delete</button></form>'
        )
    flash_html = f'<div class="flash">{h(flash)}</div>' if flash else ""

    users = d.get("users") or []
    role_options = "".join(f'<option value="{h(r)}">{h(r)}</option>' for r in rbac.ROLES)
    if users:
        rows = []
        for u in users:
            sso = f"{h(u.issuer)} / {h(u.external_subject)}" if u.external_subject else "not linked"
            state = "disabled" if u.disabled_at is not None else "active"
            role_form = (
                f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/users/set-role" '
                'class="act">'
                f'<input type="hidden" name="user_id" value="{h(u.id)}">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, "set_user_role", str(u.id)))}">'
                f'<label class="sr-only" for="role-{h(u.id)}">Role for {h(u.email)}</label>'
                f'<select id="role-{h(u.id)}" name="role">'
                + "".join(
                    f'<option value="{h(r)}"{" selected" if r == u.role else ""}>{h(r)}</option>'
                    for r in rbac.ROLES
                )
                + '</select> <button type="submit" class="btn">Set</button></form>'
            )
            toggle_action = "enable" if u.disabled_at is not None else "disable"
            toggle_form = (
                f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/users/{toggle_action}" '
                'class="act">'
                f'<input type="hidden" name="user_id" value="{h(u.id)}">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, f"{toggle_action}_user", str(u.id)))}">'
                f'<button type="submit" class="btn">{toggle_action.capitalize()}</button></form>'
            )
            if u.external_subject:
                sso_form = (
                    f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/users/unlink-sso" '
                    'class="act">'
                    f'<input type="hidden" name="user_id" value="{h(u.id)}">'
                    f'<input type="hidden" name="csrf" '
                    f'value="{h(_csrf_token(admin_token, "unlink_sso", str(u.id)))}">'
                    '<button type="submit" class="btn">Unlink SSO</button></form>'
                )
            else:
                sso_form = (
                    f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/users/link-sso" '
                    'class="act">'
                    f'<input type="hidden" name="user_id" value="{h(u.id)}">'
                    f'<input type="hidden" name="csrf" '
                    f'value="{h(_csrf_token(admin_token, "link_sso", str(u.id)))}">'
                    f'<label class="sr-only" for="issuer-{h(u.id)}">Issuer</label>'
                    f'<input type="text" id="issuer-{h(u.id)}" name="issuer" placeholder="issuer" '
                    'style="width:8rem">'
                    f'<label class="sr-only" for="subject-{h(u.id)}">Subject</label>'
                    f'<input type="text" id="subject-{h(u.id)}" name="external_subject" '
                    'placeholder="subject" style="width:8rem">'
                    '<button type="submit" class="btn">Link</button></form>'
                )
            rows.append(
                f'<tr><td>{h(u.email)}</td><td>{role_form}</td><td>{sso}<br>{sso_form}</td>'
                f'<td>{state}<br>{toggle_form}</td>'
                f'<td class="n">{_iso(u.created_at)}</td></tr>'
            )
        users_tbl = ('<div class="scroll"><table><thead><tr><th>Email</th><th>Role</th>'
                    '<th>SSO identity</th><th>State</th><th>Created</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        users_tbl = '<p class="empty">No individual users yet -- only the org\'s shared API key(s).</p>'
    create_user_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/users/create" class="act" '
        'style="margin-top:.75rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "create_user", org.id))}">'
        '<label class="sr-only" for="new-user-email">Email</label>'
        '<input type="email" id="new-user-email" name="email" placeholder="email" required>'
        '<label class="sr-only" for="new-user-role">Role</label>'
        f'<select id="new-user-role" name="role">{role_options}</select>'
        '<button type="submit" class="btn">Add user</button></form>'
    )

    search = d.get("subject_search")
    if search:
        results = search.get("results") or []
        if results:
            rows = "".join(
                f'<tr><td>{h(r["title"])}</td>'
                f'<td>{"quarantined" if r["quarantined"] else "active"}</td>'
                f'<td class="n">{h(str(r["created_at"])[:16])}</td>'
                f'<td class="m" style="color:var(--muted)">{h(r["id"])}</td></tr>'
                for r in results
            )
            subject_results_html = (
                f'<p class="sub">{_num(len(results))} trace(s) tagged with '
                f'{h(search["subject_id"])!r}.</p>'
                '<div class="scroll"><table><thead><tr><th>Title</th><th>State</th>'
                f'<th>Captured</th><th>Trace id</th></tr></thead><tbody>{rows}</tbody></table></div>'
            )
        else:
            subject_results_html = (
                f'<p class="empty">No traces in this org are tagged with '
                f'{h(search["subject_id"])!r}.</p>'
            )
    else:
        subject_results_html = ""
    find_subject_form = (
        f'<form method="get" action="{ADMIN_PATH}/org/{h(org.id)}" class="act">'
        '<label class="sr-only" for="find-subject-id">Subject id</label>'
        '<input type="text" id="find-subject-id" name="subject_id" placeholder="subject id" required>'
        '<button type="submit" class="btn">Find traces</button></form>'
        f'{subject_results_html}'
    )
    tag_subject_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/tag-subjects" class="act" '
        'style="margin-top:.75rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "tag_trace_subjects", org.id))}">'
        '<label class="sr-only" for="tag-trace-id">Trace id</label>'
        '<input type="text" id="tag-trace-id" name="trace_id" placeholder="trace id" required '
        'style="width:20rem">'
        '<label class="sr-only" for="tag-subject-ids">Subject ids (comma-separated)</label>'
        '<input type="text" id="tag-subject-ids" name="subject_ids" '
        'placeholder="subject ids, comma-separated (blank clears)" style="width:20rem">'
        '<button type="submit" class="btn">Tag</button></form>'
    )
    purge_subject_form = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/purge-subject-traces" '
        'class="act" style="margin-top:1rem">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "purge_subject_traces", org.id))}">'
        '<label class="sr-only" for="purge-subject-id">Subject id</label>'
        '<input type="text" id="purge-subject-id" name="subject_id" placeholder="subject id" '
        'required style="width:20rem">'
        '<label class="sr-only" for="purge-subject-confirm">Retype the subject id to confirm'
        '</label>'
        '<input type="text" id="purge-subject-confirm" name="confirm_subject_id" '
        'placeholder="retype the subject id to confirm" required style="width:20rem">'
        '<button type="submit" class="btn warn">Permanently erase these traces</button></form>'
        '<p class="empty" style="margin-top:.4rem">Irreversible: deletes every trace tagged with '
        'this subject id, plus each one\'s full amendment chain.</p>'
    )

    danger_zone = (
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/purge-trace" class="act">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "purge_trace", org.id))}">'
        '<label class="sr-only" for="purge-trace-id">Trace id</label>'
        '<input type="text" id="purge-trace-id" name="trace_id" placeholder="trace id" required '
        'style="width:20rem">'
        '<label class="sr-only" for="purge-trace-confirm">Retype the trace id to confirm</label>'
        '<input type="text" id="purge-trace-confirm" name="confirm_trace_id" '
        'placeholder="retype the trace id to confirm" required style="width:20rem">'
        '<button type="submit" class="btn warn">Permanently delete this trace</button></form>'
        '<p class="empty" style="margin:.4rem 0 1rem">Deletes one trace and its full amendment '
        'chain.</p>'
        f'<form method="post" action="{ADMIN_PATH}/org/{h(org.id)}/purge" class="act">'
        f'<input type="hidden" name="org_id" value="{h(org.id)}">'
        f'<input type="hidden" name="csrf" '
        f'value="{h(_csrf_token(admin_token, "purge_org", org.id))}">'
        '<label class="sr-only" for="purge-org-confirm">Retype the organization name to confirm'
        '</label>'
        f'<input type="text" id="purge-org-confirm" name="confirm_name" '
        f'placeholder="retype {h(org.name)!r} to confirm" required style="width:20rem">'
        '<button type="submit" class="btn warn">Permanently delete this organization</button>'
        '</form>'
        '<p class="empty" style="margin-top:.4rem">Deletes this organization and everything '
        'scoped to it -- api keys, traces, votes. Cancels its Stripe subscription first, if it '
        'has one.</p>'
    )

    if d["audit"]:
        rows = "".join(
            f'<tr><td class="n">{_iso(a.created_at)}</td><td class="m">{h(a.actor)}</td>'
            f'<td>{h(a.action)}</td><td class="m" style="color:var(--muted)">'
            f'{h(a.target_type)}{":" if a.target_type else ""}{h(a.target_id)}</td>'
            f'<td>{h(a.summary)}</td></tr>'
            for a in d["audit"]
        )
        audit_tbl = ('<div class="scroll"><table><thead><tr><th>When</th><th>Actor</th>'
                     f'<th>Action</th><th>Target</th><th>Summary</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
    else:
        audit_tbl = '<p class="empty">No audited actions for this org yet.</p>'

    return (
        f'<section><h1>{h(org.name)}</h1>'
        f'<p class="sub m">{h(org.id)} · created {_iso(org.created_at)} · '
        f'billing period {h(ent.get("period"))}</p>{flash_html}</section>'
        f'<section><div class="tiles">{tiles}</div>{floor_note}{plan_form}</section>'
        f'<section><h2>API keys</h2>{fresh_key_html}{keys_tbl}{issue_key_form}</section>'
        f'<section><h2>Users</h2>{users_tbl}{create_user_form}</section>'
        f'<section><h2>Quarantined traces</h2>{quar_tbl}</section>'
        f'<section><h2>Amend a trace</h2>{amend_form}</section>'
        f'<section><h2>Legal holds</h2>{holds_tbl}{holds_form}</section>'
        f'<section><h2>Retention policy</h2>{policies_tbl}{policy_form}{retention_preview_html}</section>'
        f'<section><h2>Subject rights</h2>{find_subject_form}{tag_subject_form}{purge_subject_form}</section>'
        f'<section><h2>Recent audited actions</h2>{audit_tbl}</section>'
        '<section><h2>Danger zone</h2>'
        '<div class="note">Nothing above this line can lose data permanently. Everything '
        'below is irreversible -- retyping the exact id/name is required, in addition to the '
        'same CSRF token every other action here needs, so a wrong click cannot reach it.'
        '</div>'
        f'<div style="margin-top:1rem">{danger_zone}</div></section>'
        '<section><h2>Operator commands for this org</h2><div class="cmds">'
        + _cmd("Is it working?", f"python -m hub.manage outcomes {org.id}")
        + _cmd("Start a holdout", f"python -m hub.manage start-experiment {org.id} 0.2")
        + '</div></section>'
    )


def _render_kb(data: dict, admin_token: str, operator_org_id: str, flash: str = "") -> str:
    """The Knowledge Base page: the ONE place orgs and the Hub exchange
    anything, and the only surface where content crosses an org boundary --
    which it does by passing through an operator, never directly.

    Written to make that boundary legible rather than assumed. An operator
    reading this page should be able to see, without opening the source,
    that a customer proposes and an operator publishes; that what gets
    published is owned by the operator's org and not the submitter's; and
    that nothing a customer sends is visible to anyone until that happens.
    """
    corpus, hits = data["corpus"], data["hits"]
    pending, queue, retracted = data["pending"], data["queue"], data["retracted"]
    pending_total = data.get("pending_total", len(pending))
    queue_total = data.get("queue_total", len(queue))
    retracted_total = data.get("retracted_total", len(retracted))

    tiles = "".join([
        _tile("published entries", _num(corpus)),
        _tile("answers delivered", _num(hits)),
        _tile("orgs consulting", _num(data["consuming_orgs"])),
        _tile("awaiting review", _num(pending_total), "warn" if pending_total else ""),
        _tile("needs attention", _num(queue_total), "warn" if queue_total else ""),
        _tile("retracted", _num(retracted_total)),
    ])

    exchange = (
        '<div class="note">'
        '<b>Orgs never exchange anything with each other.</b> A fleet\'s own traces '
        'stay private to that fleet — there is no tool on this Hub that shows one '
        'org another org\'s data, and the query filter that makes it true lives in '
        'one place (<code>commons_visible()</code>) rather than in each read path. '
        'This page is the only exchange that exists, and it has exactly two '
        'directions: an org <b>consults</b> the Knowledge Base by sending a signature '
        '(never its text), and an org <b>proposes</b> an entry that sits invisible to '
        'everyone until you accept it here. An accepted proposal is published under '
        'the operator\'s own org, not the submitter\'s — so what other orgs read is '
        'operator-curated substrate knowledge, never a customer\'s record.'
        '</div>'
    )

    # --- Proposals awaiting a decision ------------------------------------
    if pending:
        cards = []
        for s in pending:
            sid = s["id"]
            approve_ready = bool(operator_org_id)
            if approve_ready:
                accept = (
                    f'<form method="post" action="{ADMIN_PATH}/kb/review" class="act">'
                    f'<input type="hidden" name="submission_id" value="{h(sid)}">'
                    f'<input type="hidden" name="decision" value="approve">'
                    f'<input type="hidden" name="csrf" '
                    f'value="{h(_csrf_token(admin_token, "approve", sid))}">'
                    f'<button type="submit" class="btn ok">Accept &amp; publish</button></form>'
                )
            else:
                accept = ('<span class="disabled" title="Set HUB_OPERATOR_ORG_ID to publish '
                          'from here">Accept needs HUB_OPERATOR_ORG_ID</span>')
            reject = (
                f'<form method="post" action="{ADMIN_PATH}/kb/review" class="act">'
                f'<input type="hidden" name="submission_id" value="{h(sid)}">'
                f'<input type="hidden" name="decision" value="reject">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, "reject", sid))}">'
                f'<label for="reason-{h(sid)}" class="sr-only">Reason for declining</label>'
                f'<input type="text" id="reason-{h(sid)}" name="reason" maxlength="200" '
                f'placeholder="reason (optional)">'
                f'<button type="submit" class="btn">Decline</button></form>'
            )
            tags = " ".join(f'<span class="pill mute">{h(t)}</span>' for t in (s.get("tags") or []))
            cards.append(
                f'<article class="card"><h3>{h(s.get("title"))}</h3>'
                f'<p class="meta">proposed {h(s.get("created_at"))} · '
                f'agent_type {h(s.get("agent_type") or "—")} · '
                f'from org <span class="m">{h(s.get("org_id"))}</span></p>'
                f'<div class="field"><span class="lbl">Why this is substrate, not our business logic</span>'
                f'<p>{h(s.get("rationale") or "— no rationale given —")}</p></div>'
                f'<div class="field"><span class="lbl">When it applies</span>'
                f'<p>{h(s.get("context_text"))}</p></div>'
                f'<div class="field"><span class="lbl">What to do</span>'
                f'<p>{h(s.get("solution_text"))}</p></div>'
                f'<div class="tags">{tags}</div>'
                f'<div class="acts">{accept}{reject}</div></article>'
            )
        pending_html = f'<div class="cards">{"".join(cards)}</div>'
        if pending_total > len(pending):
            pending_html += (f'<p class="empty">Showing the {len(pending)} oldest, of '
                             f'{_num(pending_total)} total awaiting review.</p>')
    else:
        pending_html = ('<p class="empty">No proposals waiting. Orgs propose entries with '
                        '<code>commontrace commons submit</code>.</p>')

    # --- Published entries that need a human ------------------------------
    if queue:
        rows = []
        for e in queue:
            tid = e.get("id")
            rows.append(
                f'<tr><td>{h(e.get("title"))}</td>'
                f'<td><span class="pill warn">{h(e.get("why"))}</span></td>'
                f'<td class="n">{_num(e.get("commons_hits", 0))}</td>'
                f'<td><form method="post" action="{ADMIN_PATH}/kb/retract" class="act">'
                f'<input type="hidden" name="trace_id" value="{h(tid)}">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, "retract", str(tid)))}">'
                f'<label for="retract-reason-{h(tid)}" class="sr-only">Reason for retracting</label>'
                f'<input type="text" id="retract-reason-{h(tid)}" name="reason" maxlength="200" '
                f'placeholder="reason">'
                f'<button type="submit" class="btn warn">Retract</button></form></td></tr>'
            )
        queue_html = ('<div class="scroll"><table><thead><tr><th>Entry</th>'
                      '<th>Why it is listed</th><th>Answers given</th><th>Action</th>'
                      f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
        if queue_total > len(queue):
            queue_html += (f'<p class="empty">Showing the first {len(queue)} entries, worst first, '
                           f'of {_num(queue_total)} total.</p>')
    else:
        queue_html = '<p class="empty">Nothing published needs a decision right now.</p>'

    # --- Retracted -------------------------------------------------------
    if retracted:
        rows = []
        for e in retracted:
            tid = e["id"]
            rows.append(
                f'<tr><td>{h(e["title"])}</td><td>{h(e["reason"] or "—")}</td>'
                f'<td class="n">{_iso(e["at"])}</td>'
                f'<td><form method="post" action="{ADMIN_PATH}/kb/restore" class="act">'
                f'<input type="hidden" name="trace_id" value="{h(tid)}">'
                f'<input type="hidden" name="csrf" '
                f'value="{h(_csrf_token(admin_token, "restore", str(tid)))}">'
                f'<button type="submit" class="btn">Restore</button></form></td></tr>'
            )
        retracted_html = ('<div class="scroll"><table><thead><tr><th>Entry</th><th>Reason</th>'
                          '<th>Withdrawn</th><th>Action</th></tr></thead>'
                          f'<tbody>{"".join(rows)}</tbody></table></div>')
        if retracted_total > len(retracted):
            retracted_html += (f'<p class="empty">Showing the {len(retracted)} most recently '
                               f'withdrawn, of {_num(retracted_total)} total.</p>')
    else:
        retracted_html = '<p class="empty">Nothing is currently withdrawn.</p>'

    flash_html = f'<div class="flash">{h(flash)}</div>' if flash else ""

    return (
        '<section><h1>Knowledge Base</h1>'
        '<p class="sub">The shared substrate every org may consult — and the review '
        'queue that decides what goes into it.</p></section>'
        f'{flash_html}'
        f'<section><div class="tiles">{tiles}</div>{exchange}</section>'
        f'<section><h2>Proposals awaiting a decision</h2>'
        '<p class="sub" style="margin-bottom:1rem">Accepting publishes the entry under the '
        'operator org and permanently raises the proposing org\'s query allowance. '
        'Declining awards nothing — that silence is the adverse-selection defence.</p>'
        f'{pending_html}</section>'
        f'<section><h2>Published entries needing attention</h2>{queue_html}</section>'
        f'<section><h2>Withdrawn entries</h2>{retracted_html}</section>'
        '<section><h2>Operator commands</h2><div class="cmds">'
        + _cmd("Bulk-load curated content", "python -m hub.manage commons-seed entries.jsonl <operator_org_id>")
        + _cmd("Content quality", "python -m hub.manage kb-stats")
        + _cmd("Review queue (CLI)", "python -m hub.manage kb-review 50")
        + '</div></section>'
    )


# --- Wiring -----------------------------------------------------------------


def add_admin_routes(
    app,
    session_factory: async_sessionmaker,
    admin_token: str,
    rate_limiter: RateLimiter | None = None,
    trusted_proxy_hops: int = 0,
    commons_enabled: bool = True,
    operator_org_id: str = "",
    config: HubConfig | None = None,
) -> None:
    """Register the console. Call ONLY when an admin token is configured.

    hub/server.py does not call this at all when `HUB_ADMIN_TOKEN` is unset,
    so a deployment that has not opted in has no /admin route to find --
    the same "absent, not merely refused" treatment the Knowledge Base tools
    get from `HUB_COMMONS_ENABLED`. An unauthenticated prober gets a 404 from
    the router, which is a stronger property than a 401 from a handler.

    `config` is only needed for the "Amend a trace" form, which calls
    `manage.amend_trace` -- the one action here that validates and stores
    trace content (`crud.amend_trace`'s size/rate limits), unlike every
    other handler, which only ever touches rows that already exist.
    Passing it explicitly, rather than letting `manage.amend_trace` fall
    back to its own `HubConfig.from_env()`, matters specifically for
    tests: they build a `HubConfig` from fixtures and never set
    `HUB_DATABASE_URL` as a real process environment variable, so that
    fallback raises in exactly the harness this module's own tests run
    in. `None` (the default) preserves the pre-existing from-env fallback
    for a caller that has no config object handy.
    """
    if not admin_token:
        raise ValueError("add_admin_routes requires a non-empty admin token")

    limiter = rate_limiter or RateLimiter(per_minute=120, burst=30)

    async def _guard(request: Request) -> Response | None:
        # Rate limited BEFORE the credential check, keyed by client address,
        # for the same reason hub/server.py limits auth attempts: the compare
        # is cheap here, but an unauthenticated endpoint that hits Postgres on
        # every request is a lever without one.
        client_key = resolve_client_key(request, trusted_proxy_hops)
        allowed, retry_after = await limiter.check(client_key)
        if not allowed:
            import math
            seconds = str(max(1, math.ceil(retry_after)))
            return Response("too many requests", status_code=429,
                            headers={"Retry-After": seconds})
        if not _authorized(request, admin_token):
            return _unauthorized()
        return None

    async def _overview_view(*, flash: str = "", fresh_key: str = "") -> Response:
        async with session_scope(session_factory) as session:
            data = await _overview(session)
        return _page(
            "Overview", _render_overview(data, admin_token, flash=flash, fresh_key=fresh_key),
            # Never auto-refresh a page currently showing a just-issued,
            # shown-once secret -- a reload before it's copied loses it
            # for good, since this Hub keeps no other record of it.
            auto_refresh_seconds=0 if fresh_key else 20,
        )

    async def overview(request: Request) -> Response:
        denied = await _guard(request)
        if denied is not None:
            return denied
        flash = request.query_params.get("done", "")[:200]
        return await _overview_view(flash=flash)

    async def generate_encryption_key_route(request: Request) -> Response:
        """A stateless utility -- see hub/encryption.py's generate_key. No
        database write, no org, and nothing to audit: this changes
        nothing until an operator sets the printed value as
        HUB_ENCRYPTION_KEY themselves."""
        from hub.encryption import generate_key

        _form, _target, denied = await _moderate(request, "target", action_of="generate_encryption_key")
        if denied is not None:
            return denied
        return await _overview_view(fresh_key=generate_key())

    async def org_detail(request: Request) -> Response:
        denied = await _guard(request)
        if denied is not None:
            return denied
        org_id = request.path_params["org_id"]
        # The id goes into a UUID column, so a malformed one raises at the
        # driver rather than returning None. Caught here so a mistyped URL is
        # a 404 page and not a 500 with a stack trace in the operator's face.
        try:
            async with session_scope(session_factory) as session:
                data = await _org_detail(session, org_id)
        except SQLAlchemyError:
            # org_id lands in a UUID column, so a mistyped URL is rejected by
            # the column type itself and surfaces as a driver-level error
            # rather than a None row (hub/manage.py's own main() catches the
            # same class for the same reason). Narrow on purpose: a database
            # outage must reach the error handler as an outage, not be
            # reported to the operator as "no such organization".
            logger.info("admin: unresolvable org id in URL", extra={"org_id_len": len(org_id)})
            data = None
        if data is None:
            return _page("Not found", '<section><h1>No such organization</h1>'
                                      f'<p class="sub">Nothing on this Hub has that id. '
                                      f'<a href="{ADMIN_PATH}">Back to the overview</a>.</p></section>')
        flash = request.query_params.get("done", "")[:200]
        subject_id = request.query_params.get("subject_id", "")[:200]
        if subject_id:
            async with session_scope(session_factory) as session:
                results = await crud.find_traces_by_subject(session, org_id, subject_id)
            data["subject_search"] = {"subject_id": subject_id, "results": results}
        if request.query_params.get("preview_retention"):
            async with session_scope(session_factory) as session:
                plan = await retention.plan(session, org_id)
            data["retention_preview"] = {
                "render": plan.render(), "digest": plan.digest, "n_doomed": plan.n_doomed,
            }
        return _page(
            data["org"].name, _render_org(data, admin_token, flash=flash),
            auto_refresh_seconds=25,
        )

    _COMMONS_OFF = (
        '<section><h1>Knowledge Base</h1><p class="sub">This deployment runs with '
        '<code>HUB_COMMONS_ENABLED=false</code>, so the Knowledge Base tools do not '
        'exist on this server at all — there is nothing to review.</p></section>'
    )

    async def kb(request: Request) -> Response:
        denied = await _guard(request)
        if denied is not None:
            return denied
        if not commons_enabled:
            return _page("Knowledge Base", _COMMONS_OFF)
        flash = request.query_params.get("done", "")[:200]
        async with session_scope(session_factory) as session:
            data = await _kb_data(session)
        return _page(
            "Knowledge Base", _render_kb(data, admin_token, operator_org_id, flash=flash),
            auto_refresh_seconds=15,
        )

    async def _moderate(request: Request, target_field: str, action_of=None, *, require_commons=False):
        """Shared front half of every mutating handler: authenticate, refuse
        a cross-site post, then check the action-scoped CSRF token.

        The token binds the action to its target, so `action_of` derives the
        action from the parsed form -- the review handler's action is the
        submitted decision itself, which means a token minted for "reject
        this submission" cannot be replayed to approve it. The form has to be
        read before the token can be checked, which is why the ordering here
        is deliberate rather than incidental.

        `require_commons` is the Knowledge Base handlers' own gate (KB
        moderation is meaningless with `HUB_COMMONS_ENABLED=false`, since
        there is no commons for a submission or an entry to belong to);
        every other mutating handler below leaves it False.

        Returns (form, target, None) to proceed, or (None, None, response).
        """
        denied = await _guard(request)
        if denied is not None:
            return None, None, denied
        if require_commons and not commons_enabled:
            return None, None, _page("Knowledge Base", _COMMONS_OFF)
        if not _same_site(request):
            return None, None, Response("cross-site request refused", status_code=403)
        form = await request.form()
        target = str(form.get(target_field, ""))
        action = action_of(form) if callable(action_of) else str(action_of)
        if not target or not _csrf_ok(admin_token, action, target, str(form.get("csrf", ""))):
            # Deliberately terse: a caller that failed this check is either
            # forging or replaying, and neither deserves a hint about which.
            return None, None, Response("invalid or missing request token", status_code=403)
        return form, target, None

    def _back(path: str, message: str) -> Response:
        # POST-then-redirect: without it a reload re-submits the decision,
        # and a moderation decision is not something to repeat by accident.
        from urllib.parse import quote
        return Response(status_code=303, headers={"Location": f"{path}?done={quote(message)}"})

    async def kb_review(request: Request) -> Response:
        form, submission_id, denied = await _moderate(
            request, "submission_id", action_of=lambda f: str(f.get("decision", "")),
            require_commons=True,
        )
        if denied is not None:
            return denied
        decision = str(form.get("decision", ""))
        if decision not in ("approve", "reject"):
            return Response("unknown decision", status_code=400)
        if decision == "approve" and not operator_org_id:
            # Fails closed: publishing under the wrong org would put a
            # customer's id on Knowledge Base content, which is the one
            # mistake this boundary exists to prevent.
            return Response(
                "refusing to publish: HUB_OPERATOR_ORG_ID is not set, so there is no "
                "operator org to own the entry. Set it, or use "
                "`python -m hub.manage approve-submission <id> <operator_org_id>`.",
                status_code=409,
            )
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, submission_id, decision,
                operator_org_id=operator_org_id,
                reviewer=_ADMIN_ACTOR,
                rejection_reason=str(form.get("reason", ""))[:200],
            )
        if result is None:
            return _back(f"{ADMIN_PATH}/kb", "That submission was already decided, or no longer exists.")
        return _back(f"{ADMIN_PATH}/kb", "Published to the Knowledge Base." if decision == "approve"
                     else "Proposal declined. No entry, no credit.")

    async def kb_retract(request: Request) -> Response:
        form, trace_id, denied = await _moderate(
            request, "trace_id", action_of="retract", require_commons=True)
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            result = await crud.retract_kb_entry(
                session, trace_id, reason=str(form.get("reason", ""))[:200],
                actor=_ADMIN_ACTOR,
            )
        if result is None:
            return _back(f"{ADMIN_PATH}/kb", "That entry is not a published Knowledge Base entry.")
        return _back(f"{ADMIN_PATH}/kb", "Withdrawn. It stops being served immediately; the row and "
                     "its history are kept, and Restore puts it back.")

    async def kb_restore(request: Request) -> Response:
        _form, trace_id, denied = await _moderate(
            request, "trace_id", action_of="restore", require_commons=True)
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            result = await crud.restore_kb_entry(session, trace_id, actor=_ADMIN_ACTOR)
        if result is None:
            return _back(f"{ADMIN_PATH}/kb", "That entry is not currently withdrawn.")
        return _back(f"{ADMIN_PATH}/kb", "Restored. It is being served again.")

    # --- Org-scoped mutations -----------------------------------------------
    #
    # Legal holds, retention policy, and quarantine release are all
    # REVERSIBLE (a hold is released, a policy is cleared, a release does
    # not delete anything), which is the line drawn in _render_overview's
    # own note: reversible state changes get a button here, and what
    # cannot be undone (`purge-org`, the digest-confirmed `retention-apply`)
    # or would put a live credential in the browser (`issue-key`) stays in
    # the CLI. Every handler below calls the SAME `hub/retention.py`
    # functions the CLI does, audited as `_ADMIN_ACTOR` rather than
    # `operator-cli` -- the same distinction kb_review/_retract/_restore
    # already draw above.
    #
    # CSRF tokens here bind to the ORG rather than to each row the way KB's
    # tokens bind to one submission/entry: an operator viewing one org's
    # page is already scoped to that org, and the threat this defends
    # against is a forged cross-site POST, not one row's token being
    # replayed against a sibling row in the SAME org the operator is
    # already looking at.

    async def quarantine_release(request: Request) -> Response:
        _form, trace_id, denied = await _moderate(
            request, "trace_id", action_of="release_quarantine")
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            if trace is None:
                return _back(ADMIN_PATH, "No such trace.")
            org_id = trace.org_id
            previous_reason = trace.quarantine_reason
            await session.execute(
                update(Trace).where(Trace.id == trace_id)
                .values(quarantined=False, quarantine_reason="")
            )
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="release_quarantine",
                org_id=org_id, target_type="trace", target_id=trace_id,
                summary=f"was={previous_reason[:100]}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Released from quarantine.")

    async def legal_hold_place(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="place_hold")
        if denied is not None:
            return denied
        reason = str(form.get("reason", ""))[:500]
        object_type = str(form.get("object_type", ""))
        target_id = str(form.get("target_id", ""))
        async with session_scope(session_factory) as session:
            try:
                hold = await retention.place_hold(
                    session, org_id, reason=reason, placed_by=_ADMIN_ACTOR,
                    object_type=object_type, target_id=target_id,
                )
            except retention.RetentionError as exc:
                return _back(f"{ADMIN_PATH}/org/{org_id}", f"Could not place hold: {exc}")
            await session.flush()
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="retention.place_hold",
                org_id=org_id, target_type="legal_hold", target_id=hold.id,
                summary=f"{object_type or 'everything'}: {reason[:200]}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Legal hold placed.")

    async def legal_hold_release(request: Request) -> Response:
        form, hold_id, denied = await _moderate(request, "hold_id", action_of="release_hold")
        if denied is not None:
            return denied
        reason = str(form.get("reason", ""))[:200]
        async with session_scope(session_factory) as session:
            try:
                hold = await retention.release_hold(session, hold_id, reason=reason)
            except retention.RetentionError as exc:
                return _back(ADMIN_PATH, f"Could not release hold: {exc}")
            org_id = hold.org_id
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="retention.release_hold",
                org_id=org_id, target_type="legal_hold", target_id=hold_id,
                summary=reason or "released",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Legal hold released.")

    async def retention_set(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="set_retention")
        if denied is not None:
            return denied
        object_type = str(form.get("object_type", ""))
        status = str(form.get("status", "")).strip() or retention.STATUS_ANY
        try:
            max_age_days = int(str(form.get("days", "")))
        except ValueError:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Days must be a whole number.")
        async with session_scope(session_factory) as session:
            try:
                await retention.set_policy(session, org_id, object_type, max_age_days, status=status)
            except retention.RetentionError as exc:
                return _back(f"{ADMIN_PATH}/org/{org_id}", f"Could not set policy: {exc}")
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="retention.set_policy",
                org_id=org_id, target_type="retention_policy",
                target_id=f"{object_type}/{status}", summary=f"keep {max_age_days}d",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}",
                     f"{object_type} [{status}]: keep {max_age_days} days. Nothing is deleted "
                     "until a plan is applied from the CLI.")

    async def retention_clear(request: Request) -> Response:
        form, _target, denied = await _moderate(
            request, "target", action_of=lambda f: f"clear_retention:{f.get('org_id', '')}")
        if denied is not None:
            return denied
        org_id = str(form.get("org_id", ""))
        object_type = str(form.get("object_type", ""))
        status = str(form.get("status", "")) or retention.STATUS_ANY
        async with session_scope(session_factory) as session:
            try:
                removed = await retention.remove_policy(session, org_id, object_type, status=status)
            except retention.RetentionError as exc:
                return _back(f"{ADMIN_PATH}/org/{org_id}", f"Could not clear policy: {exc}")
            if not removed:
                return _back(f"{ADMIN_PATH}/org/{org_id}", "No matching policy to clear.")
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="retention.clear_policy",
                org_id=org_id, target_type="retention_policy",
                target_id=f"{object_type}/{status}", summary="removed",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Retention policy cleared.")

    async def create_org(request: Request) -> Response:
        """Additive and reversible in the sense that matters here: there is
        nothing yet on a brand-new org for a mistaken click to lose. Unlike
        purge-org, undoing a wrong create-org is `purge-org` on an org with
        zero traces, keys, or history -- a fundamentally different risk."""
        form, _target, denied = await _moderate(request, "target", action_of="create_org")
        if denied is not None:
            return denied
        name = str(form.get("name", "")).strip()
        if not name:
            return _back(ADMIN_PATH, "Organization name is required.")
        async with session_scope(session_factory) as session:
            existing = (
                await session.execute(select(Organization.id).where(Organization.name == name))
            ).scalars().all()
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="create_org",
                org_id=org.id, target_type="org", target_id=org.id, summary=f"name={name!r}",
            )
            org_id = org.id
        if existing:
            return _back(
                ADMIN_PATH,
                f"Created {org_id} -- note another org already uses the name {name!r}.",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Organization created.")

    async def set_plan(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="set_plan")
        if denied is not None:
            return denied
        key = str(form.get("plan_name", "")).strip().lower()
        if key not in plans.PLANS:
            return _back(f"{ADMIN_PATH}/org/{org_id}", f"Unknown plan {key!r}.")
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            if org is None:
                return _back(ADMIN_PATH, "No such organization.")
            was, org.plan = org.plan, key
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="set_plan",
                org_id=org_id, target_type="org", target_id=org_id,
                summary=f"{was!r} -> {key!r}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", f"Plan changed: {was} → {key}.")

    # --- Users, SSO linking, and subject rights -----------------------------
    #
    # All reversible: a role can be set again, disable/enable round-trips,
    # SSO link/unlink round-trips, and tagging a trace's subjects REPLACES
    # rather than accumulates (same idempotent-under-retry shape
    # crud.tag_trace_subjects already gives set_user_role). Erasing a
    # subject's traces (`purge-subject-traces`) is the one irreversible
    # action in this family and stays CLI-only, same as purge-org.

    async def users_create(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="create_user")
        if denied is not None:
            return denied
        email = str(form.get("email", "")).strip()
        role = str(form.get("role", "")).strip()
        if not email:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Email is required.")
        try:
            rbac.check_role(role)
        except rbac.RoleError as exc:
            return _back(f"{ADMIN_PATH}/org/{org_id}", str(exc))
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            if org is None:
                return _back(ADMIN_PATH, "No such organization.")
            user = User(org_id=org_id, email=email, role=role, created_by=_ADMIN_ACTOR)
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return _back(f"{ADMIN_PATH}/org/{org_id}", f"{email!r} already exists in this org.")
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="create_user",
                org_id=org_id, target_type="user", target_id=user.id, summary=f"email={email} role={role}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "User added.")

    async def users_set_role(request: Request) -> Response:
        form, user_id, denied = await _moderate(request, "user_id", action_of="set_user_role")
        if denied is not None:
            return denied
        role = str(form.get("role", "")).strip()
        try:
            rbac.check_role(role)
        except rbac.RoleError as exc:
            return _back(ADMIN_PATH, str(exc))
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
            if user is None:
                return _back(ADMIN_PATH, "No such user.")
            org_id, previous = user.org_id, user.role
            user.role = role
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="set_user_role",
                org_id=org_id, target_type="user", target_id=user_id,
                summary=f"{previous} -> {role}",
            )
            if role in rbac.PRIVILEGED_ROLES:
                await events.emit(session, org_id, "user.privileged_role_granted", {
                    "user_id": user_id, "role": role, "actor": _ADMIN_ACTOR,
                })
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Role updated.")

    async def _toggle_user(request: Request, *, disable: bool) -> Response:
        action = "disable_user" if disable else "enable_user"
        _form, user_id, denied = await _moderate(request, "user_id", action_of=action)
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
            if user is None:
                return _back(ADMIN_PATH, "No such user.")
            org_id = user.org_id
            user.disabled_at = datetime.now(timezone.utc) if disable else None
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action=action,
                org_id=org_id, target_type="user", target_id=user_id,
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "User disabled." if disable else "User enabled.")

    async def users_disable(request: Request) -> Response:
        return await _toggle_user(request, disable=True)

    async def users_enable(request: Request) -> Response:
        return await _toggle_user(request, disable=False)

    async def users_link_sso(request: Request) -> Response:
        form, user_id, denied = await _moderate(request, "user_id", action_of="link_sso")
        if denied is not None:
            return denied
        issuer = str(form.get("issuer", "")).strip()
        external_subject = str(form.get("external_subject", "")).strip()
        if not issuer or not external_subject:
            return _back(ADMIN_PATH, "Issuer and subject are both required.")
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
            if user is None:
                return _back(ADMIN_PATH, "No such user.")
            org_id = user.org_id
            user.issuer = issuer
            user.external_subject = external_subject
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return _back(
                    f"{ADMIN_PATH}/org/{org_id}",
                    "That issuer/subject is already linked to a different user.",
                )
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="link_sso",
                org_id=org_id, target_type="user", target_id=user_id,
                summary=f"{issuer}/{external_subject}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "SSO identity linked.")

    async def users_unlink_sso(request: Request) -> Response:
        _form, user_id, denied = await _moderate(request, "user_id", action_of="unlink_sso")
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
            if user is None:
                return _back(ADMIN_PATH, "No such user.")
            org_id = user.org_id
            previous = f"{user.issuer}/{user.external_subject}"
            user.issuer = ""
            user.external_subject = ""
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="unlink_sso",
                org_id=org_id, target_type="user", target_id=user_id, summary=f"was {previous}",
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "SSO identity unlinked.")

    async def tag_subjects(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="tag_trace_subjects")
        if denied is not None:
            return denied
        trace_id = str(form.get("trace_id", "")).strip()
        subject_ids = [s.strip() for s in str(form.get("subject_ids", "")).split(",") if s.strip()]
        async with session_scope(session_factory) as session:
            try:
                result = await crud.tag_trace_subjects(
                    session, org_id, trace_id, subject_ids, actor=_ADMIN_ACTOR)
            except ValueError as exc:
                return _back(f"{ADMIN_PATH}/org/{org_id}", str(exc))
        if result is None:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "No such trace in this org.")
        return _back(
            f"{ADMIN_PATH}/org/{org_id}", f"Tagged with {len(result['subject_ids'])} subject id(s).")

    # --- Issuing/rotating/revoking keys, and the irreversible danger zone --
    #
    # Issuing and rotating are shown-once, never-cached (this whole
    # console sends Cache-Control: no-store) -- the same property that
    # already made hub/console.py's customer-facing key issuance safe to
    # ship. Needed here specifically for onboarding: a brand-new org has
    # no key yet, so it cannot sign into ITS OWN console to issue one.
    #
    # purge-trace/purge-org/purge-subject-traces retype the exact
    # id/name being destroyed -- a STRONGER confirmation than the CLI's
    # own `_confirm_destructive` (which only asks for the literal word
    # "yes"), on top of the same CSRF token every other mutation here
    # requires. Every one of them calls the SAME hub/manage.py or
    # hub/crud.py function the CLI does.

    async def _org_view(org_id: str, admin_token: str, *, flash: str = "", fresh_key: str = "") -> Response:
        async with session_scope(session_factory) as session:
            data = await _org_detail(session, org_id)
        if data is None:
            return _page("Not found", '<section><h1>No such organization</h1>'
                                      f'<p class="sub">Nothing on this Hub has that id. '
                                      f'<a href="{ADMIN_PATH}">Back to the overview</a>.</p></section>')
        return _page(
            data["org"].name, _render_org(data, admin_token, flash=flash, fresh_key=fresh_key),
            # Same rule as the overview page: never auto-refresh out from
            # under a shown-once secret.
            auto_refresh_seconds=0 if fresh_key else 25,
        )

    async def keys_issue(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="issue_key")
        if denied is not None:
            return denied
        chosen_scopes = form.getlist("scopes")
        if not chosen_scopes:
            return await _org_view(org_id, admin_token, flash="Select at least one scope.")
        raw_days = str(form.get("expires_days", "")).strip()
        try:
            expires_days = int(raw_days) if raw_days else None
        except ValueError:
            expires_days = None
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(
                session, org_id, expires_days=expires_days, scopes=chosen_scopes)
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="issue_key",
                org_id=org_id, target_type="api_key", target_id=issued.key_id,
                summary=f"prefix={issued.key_prefix} scopes={','.join(issued.scopes)}",
            )
        return await _org_view(org_id, admin_token, fresh_key=issued.raw_key)

    async def keys_rotate(request: Request) -> Response:
        form, key_id, denied = await _moderate(request, "key_id", action_of="rotate_key")
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, key_id)
        if key is None:
            return _back(ADMIN_PATH, "No such key.")
        org_id = key.org_id
        async with session_scope(session_factory) as session:
            issued = await auth.rotate_api_key(session, key_id)
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="rotate_key",
                org_id=org_id, target_type="api_key", target_id=issued.key_id,
                summary=f"replaces={key_id}",
            )
        return await _org_view(org_id, admin_token, fresh_key=issued.raw_key)

    async def keys_revoke(request: Request) -> Response:
        form, key_id, denied = await _moderate(request, "key_id", action_of="revoke_key")
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, key_id)
            if key is None:
                return _back(ADMIN_PATH, "No such key.")
            org_id = key.org_id
            await auth.revoke_api_key(session, key_id)
            await audit_module.record(
                session, actor=_ADMIN_ACTOR, action="revoke_key",
                org_id=org_id, target_type="api_key", target_id=key_id,
            )
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Key revoked.")

    async def retention_apply_route(request: Request) -> Response:
        form, digest, denied = await _moderate(request, "digest", action_of="retention_apply")
        if denied is not None:
            return denied
        org_id = request.path_params["org_id"]
        confirm = str(form.get("confirm_digest", "")).strip()
        if confirm != digest:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Digest confirmation did not match.")
        async with session_scope(session_factory) as session:
            try:
                await retention.apply(session, org_id, digest, actor=_ADMIN_ACTOR)
            except retention.StalePlanError as exc:
                return _back(
                    f"{ADMIN_PATH}/org/{org_id}",
                    f"The store moved since this plan was printed: {exc}. Preview again.",
                )
            except retention.RetentionError as exc:
                return _back(f"{ADMIN_PATH}/org/{org_id}", f"Could not apply: {exc}")
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Retention plan applied.")

    async def amend_trace_route(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="amend_trace")
        if denied is not None:
            return denied
        trace_id = str(form.get("trace_id", "")).strip()
        if not trace_id:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Trace id is required.")
        result = await manage.amend_trace(
            trace_id,
            str(form.get("title", "")),
            str(form.get("context_text", "")),
            str(form.get("solution_text", "")),
            str(form.get("tags_csv", "")),
            session_factory=session_factory,
            config=config,
            actor=_ADMIN_ACTOR,
        )
        if not result:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "No such trace in this org.")
        return _back(f"{ADMIN_PATH}/org/{org_id}", f"Amended trace {trace_id} -> new trace {result['id']}.")

    async def purge_trace_route(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="purge_trace")
        if denied is not None:
            return denied
        trace_id = str(form.get("trace_id", "")).strip()
        confirm = str(form.get("confirm_trace_id", "")).strip()
        if not trace_id or confirm != trace_id:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Trace id confirmation did not match.")
        ok = await manage.purge_trace(trace_id, session_factory=session_factory, actor=_ADMIN_ACTOR)
        if not ok:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "No such trace.")
        return _back(f"{ADMIN_PATH}/org/{org_id}", "Trace permanently deleted.")

    async def purge_org_route(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="purge_org")
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        if org is None:
            return _back(ADMIN_PATH, "No such organization.")
        confirm = str(form.get("confirm_name", "")).strip()
        if confirm != org.name:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Organization name confirmation did not match.")
        ok = await manage.purge_org(org_id, session_factory=session_factory, actor=_ADMIN_ACTOR)
        if not ok:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Could not delete this organization.")
        return _back(ADMIN_PATH, "Organization permanently deleted.")

    async def purge_subject_traces_route(request: Request) -> Response:
        form, org_id, denied = await _moderate(request, "org_id", action_of="purge_subject_traces")
        if denied is not None:
            return denied
        subject_id = str(form.get("subject_id", "")).strip()
        confirm = str(form.get("confirm_subject_id", "")).strip()
        if not subject_id or confirm != subject_id:
            return _back(f"{ADMIN_PATH}/org/{org_id}", "Subject id confirmation did not match.")
        async with session_scope(session_factory) as session:
            result = await crud.purge_traces_by_subject(
                session, org_id, subject_id, actor=_ADMIN_ACTOR)
        return _back(
            f"{ADMIN_PATH}/org/{org_id}",
            f"Permanently deleted {result.get('purged', 0)} trace(s).",
        )

    app.add_route(ADMIN_PATH, overview, methods=["GET"])
    app.add_route(f"{ADMIN_PATH}/create-org", create_org, methods=["POST"])
    app.add_route(
        f"{ADMIN_PATH}/generate-encryption-key", generate_encryption_key_route, methods=["POST"],
    )
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}", org_detail, methods=["GET"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/quarantine/release", quarantine_release, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/legal-hold/place", legal_hold_place, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/legal-hold/release", legal_hold_release, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/retention/set", retention_set, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/retention/clear", retention_clear, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/set-plan", set_plan, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/create", users_create, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/set-role", users_set_role, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/disable", users_disable, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/enable", users_enable, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/link-sso", users_link_sso, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/users/unlink-sso", users_unlink_sso, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/tag-subjects", tag_subjects, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/amend-trace", amend_trace_route, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/keys/issue", keys_issue, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/keys/rotate", keys_rotate, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/keys/revoke", keys_revoke, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/retention/apply", retention_apply_route, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/purge-trace", purge_trace_route, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}/purge", purge_org_route, methods=["POST"])
    app.add_route(
        f"{ADMIN_PATH}/org/{{org_id}}/purge-subject-traces", purge_subject_traces_route,
        methods=["POST"],
    )
    app.add_route(f"{ADMIN_PATH}/kb", kb, methods=["GET"])
    app.add_route(f"{ADMIN_PATH}/kb/review", kb_review, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/kb/retract", kb_retract, methods=["POST"])
    app.add_route(f"{ADMIN_PATH}/kb/restore", kb_restore, methods=["POST"])
