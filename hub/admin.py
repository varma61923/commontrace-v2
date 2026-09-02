"""A read-only operator console, served by the Hub itself at /admin.

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

WHY IT IS READ-ONLY, DELIBERATELY
---------------------------------
Before this module the Hub had NO browser-facing surface at all. Its whole
security posture follows from that: bearer tokens, no cookies, no sessions,
no CSRF surface, and hub/observability.py sets `X-Frame-Options: DENY` with
a comment noting there is nothing browser-rendered to protect.

Putting *mutating* operator actions behind a web session would change that
materially, and the actions in question are the worst ones to get wrong:
`purge-org` irreversibly destroys one customer's entire history, and
`issue-key` renders a raw credential that would then live in browser
history, in the page cache, and in any screenshot of it. A hijacked console
session is a wiped tenant.

So this console renders state and, for anything that changes state, shows
the exact `hub.manage` command to run. The operator still sees everything in
one place; the last keystroke happens in a terminal that already has an
explicit confirmation prompt (`_confirm_destructive`) and writes an audit
row. Making it read-write is a deliberate second phase with its own security
work -- session management, CSRF tokens, and a re-authentication step in
front of the destructive commands -- not a flag flip.

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

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from hub import crud, plans
from hub.abuse import RateLimiter, resolve_client_key
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace, Vote

logger = logging.getLogger("commontrace.hub.admin")

ADMIN_PATH = "/admin"
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
header.bar nav{margin-left:auto;display:flex;gap:1rem;font-size:.9rem}
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
.cmds{display:flex;flex-direction:column;gap:.5rem}
.cmd{display:grid;grid-template-columns:minmax(11rem,auto) 1fr;gap:.3rem 1rem;align-items:baseline}
.cmd .what{color:var(--muted);font-size:.88rem}
.cmd code{background:var(--code);padding:.25rem .5rem;border-radius:2px;
  display:block;overflow-x:auto;white-space:pre}
.note{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
  padding:.9rem 1.1rem;font-size:.9rem;color:var(--muted)}
.note b{color:var(--ink)}
.empty{color:var(--muted);font-size:.9rem;padding:.6rem 0}
@media (max-width:640px){.cmd{grid-template-columns:1fr}}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{h(title)} · CommonTrace Hub</title><style>{_CSS}</style></head><body>"
        "<header class=\"bar\"><div class=\"in\"><b>CommonTrace Hub</b>"
        "<span class=\"ro\">read-only console</span>"
        f"<nav><a href=\"{ADMIN_PATH}\">Overview</a>"
        f"<a href=\"{ADMIN_PATH}/kb\">Knowledge Base</a>"
        "<a href=\"/metrics\">Metrics</a></nav></div></header>"
        f"<main>{body}</main></body></html>",
        # No caching: this is live operational state, and a cached copy in a
        # shared browser is one more place tenant data sits at rest.
        headers={"Cache-Control": "no-store"},
    )


def _tile(key: str, value: str, tone: str = "") -> str:
    cls = f" {tone}" if tone else ""
    return f'<div class="tile"><div class="k">{h(key)}</div><div class="v{cls}">{value}</div></div>'


def _cmd(what: str, command: str) -> str:
    return f'<div class="cmd"><div class="what">{h(what)}</div><code>{h(command)}</code></div>'


# --- Auth -------------------------------------------------------------------


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
        "total_traces": sum(r["traces"] for r in rows),
        "total_quarantined": sum(r["quarantined"] for r in rows),
        "total_keys": sum(r["active_keys"] for r in rows),
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

    return {
        "org": org, "entitlements": ent, "agents": agents, "health": health,
        "keys": keys, "quarantined": quarantined, "audit": audit,
    }


# --- Rendering --------------------------------------------------------------


def _render_overview(data: dict) -> str:
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

    return (
        '<section><h1>Overview</h1>'
        '<p class="sub">Every organization on this Hub, and what it is using.</p></section>'
        f'<section><div class="tiles">{tiles}</div></section>'
        f'<section><h2>Organizations</h2>{table}</section>'
        '<section><h2>Operator commands</h2>'
        '<div class="note">This console never changes state. Destructive and '
        'credential-issuing actions stay in the CLI, where they prompt for '
        'confirmation and write an audit row — a hijacked browser session '
        'must not be able to wipe a tenant or mint a key. See <b>hub/admin.py</b> '
        'for the reasoning.</div>'
        '<div class="cmds" style="margin-top:1rem">'
        + _cmd("Create an organization", 'python -m hub.manage create-org "Acme"')
        + _cmd("Issue a key (90-day)", "python -m hub.manage issue-key <org_id> 90")
        + _cmd("Change a plan", "python -m hub.manage set-plan <org_id> team")
        + _cmd("Fleet-wide counts", "python -m hub.manage stats")
        + _cmd("Revenue by plan", "python -m hub.manage revenue")
        + '</div></section>'
    )


def _render_org(d: dict) -> str:
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
            rows.append(
                f'<tr><td class="m">{h(k.key_prefix)}…</td><td>{state}</td>'
                f'<td class="n">{_iso(k.created_at)}</td>'
                f'<td class="n">{_iso(k.expires_at)}</td>'
                f'<td class="n">{_iso(k.last_used_at)}</td>'
                f'<td class="m" style="color:var(--muted)">{h(k.id)}</td></tr>'
            )
        keys_tbl = ('<div class="scroll"><table><thead><tr><th>Prefix</th><th>State</th>'
                    '<th>Created</th><th>Expires</th><th>Last used</th><th>Key id</th>'
                    f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    else:
        keys_tbl = '<p class="empty">No keys issued — this org cannot reach the Hub yet.</p>'

    if d["quarantined"]:
        rows = "".join(
            f'<tr><td>{h(t.title)}</td><td>{h(t.quarantine_reason)}</td>'
            f'<td class="n">{_iso(t.created_at)}</td>'
            f'<td class="m" style="color:var(--muted)">{h(t.id)}</td></tr>'
            for t in d["quarantined"]
        )
        quar_tbl = ('<div class="scroll"><table><thead><tr><th>Title</th><th>Why</th>'
                    f'<th>Captured</th><th>Trace id</th></tr></thead><tbody>{rows}</tbody>'
                    '</table></div>')
    else:
        quar_tbl = '<p class="empty">Nothing quarantined.</p>'

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
        f'billing period {h(ent.get("period"))}</p></section>'
        f'<section><div class="tiles">{tiles}</div>{floor_note}</section>'
        f'<section><h2>API keys</h2>{keys_tbl}</section>'
        f'<section><h2>Quarantined traces</h2>{quar_tbl}</section>'
        f'<section><h2>Recent audited actions</h2>{audit_tbl}</section>'
        '<section><h2>Operator commands for this org</h2><div class="cmds">'
        + _cmd("Rotate a key", "python -m hub.manage rotate-key <key_id>")
        + _cmd("Revoke a key", "python -m hub.manage revoke-key <key_id>")
        + _cmd("Change plan", f"python -m hub.manage set-plan {org.id} team")
        + _cmd("Release a quarantine", "python -m hub.manage release-quarantine <trace_id>")
        + _cmd("Is it working?", f"python -m hub.manage outcomes {org.id}")
        + _cmd("Start a holdout", f"python -m hub.manage start-experiment {org.id} 0.2")
        + _cmd("Delete this org", f"python -m hub.manage purge-org {org.id}")
        + '</div></section>'
    )


def _render_kb(queue: list[dict], submissions: list[dict]) -> str:
    if queue:
        rows = "".join(
            f'<tr><td>{h(e.get("title"))}</td>'
            f'<td><span class="pill warn">{h(e.get("why"))}</span></td>'
            f'<td class="n">{_num(e.get("commons_hits", 0))}</td>'
            f'<td class="m" style="color:var(--muted)">{h(e.get("id"))}</td></tr>'
            for e in queue
        )
        queue_tbl = ('<div class="scroll"><table><thead><tr><th>Entry</th><th>Why it is listed</th>'
                     f'<th>Hits</th><th>Trace id</th></tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        queue_tbl = '<p class="empty">Nothing in the Knowledge Base needs a human right now.</p>'

    if submissions:
        rows = "".join(
            f'<tr><td>{h(s.get("title"))}</td>'
            f'<td><span class="pill mute">{h(s.get("status"))}</span></td>'
            f'<td class="n">{h(s.get("created_at"))}</td>'
            f'<td class="m" style="color:var(--muted)">{h(s.get("id"))}</td></tr>'
            for s in submissions
        )
        subs_tbl = ('<div class="scroll"><table><thead><tr><th>Proposed entry</th><th>Status</th>'
                    f'<th>Submitted</th><th>Submission id</th></tr></thead><tbody>{rows}</tbody>'
                    '</table></div>')
    else:
        subs_tbl = '<p class="empty">No pending submissions.</p>'

    return (
        '<section><h1>Knowledge Base</h1>'
        '<p class="sub">Operator-curated content, and what needs a decision. '
        'Nothing a customer contributes is ever published to another org without '
        'passing through this queue.</p></section>'
        f'<section><h2>Needs review — worst first</h2>{queue_tbl}</section>'
        f'<section><h2>Community submissions</h2>{subs_tbl}</section>'
        '<section><h2>Operator commands</h2><div class="cmds">'
        + _cmd("Review the queue", "python -m hub.manage kb-review 50")
        + _cmd("Accept a submission", "python -m hub.manage approve-submission <id> <operator_org_id>")
        + _cmd("Decline a submission", 'python -m hub.manage reject-submission <id> "reason"')
        + _cmd("Withdraw an entry", 'python -m hub.manage kb-retract <trace_id> "reason"')
        + _cmd("Content quality", "python -m hub.manage kb-stats")
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
) -> None:
    """Register the console. Call ONLY when an admin token is configured.

    hub/server.py does not call this at all when `HUB_ADMIN_TOKEN` is unset,
    so a deployment that has not opted in has no /admin route to find --
    the same "absent, not merely refused" treatment the Knowledge Base tools
    get from `HUB_COMMONS_ENABLED`. An unauthenticated prober gets a 404 from
    the router, which is a stronger property than a 401 from a handler.
    """
    if not admin_token:
        raise ValueError("add_admin_routes requires a non-empty admin token")

    limiter = rate_limiter or RateLimiter(per_minute=120, burst=30)

    def _guard(request: Request) -> Response | None:
        # Rate limited BEFORE the credential check, keyed by client address,
        # for the same reason hub/server.py limits auth attempts: the compare
        # is cheap here, but an unauthenticated endpoint that hits Postgres on
        # every request is a lever without one.
        client_key = resolve_client_key(request, trusted_proxy_hops)
        allowed, retry_after = limiter.check(client_key)
        if not allowed:
            import math
            seconds = str(max(1, math.ceil(retry_after)))
            return Response("too many requests", status_code=429,
                            headers={"Retry-After": seconds})
        if not _authorized(request, admin_token):
            return _unauthorized()
        return None

    async def overview(request: Request) -> Response:
        denied = _guard(request)
        if denied is not None:
            return denied
        async with session_scope(session_factory) as session:
            data = await _overview(session)
        return _page("Overview", _render_overview(data))

    async def org_detail(request: Request) -> Response:
        denied = _guard(request)
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
        return _page(data["org"].name, _render_org(data))

    async def kb(request: Request) -> Response:
        denied = _guard(request)
        if denied is not None:
            return denied
        if not commons_enabled:
            return _page("Knowledge Base", (
                '<section><h1>Knowledge Base</h1><p class="sub">This deployment runs with '
                '<code>HUB_COMMONS_ENABLED=false</code>, so the Knowledge Base tools do not '
                'exist on this server at all — there is nothing to review.</p></section>'))
        async with session_scope(session_factory) as session:
            queue = await crud.kb_review_queue(session, limit=_MAX_ROWS)
            submissions = await crud.list_kb_submissions(session, status="pending")
        return _page("Knowledge Base", _render_kb(queue, list(submissions)))

    app.add_route(ADMIN_PATH, overview, methods=["GET"])
    app.add_route(f"{ADMIN_PATH}/org/{{org_id}}", org_detail, methods=["GET"])
    app.add_route(f"{ADMIN_PATH}/kb", kb, methods=["GET"])
