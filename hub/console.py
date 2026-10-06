"""The customer's console -- the fleet's own view of its own memory."""

from __future__ import annotations

import base64
import contextvars
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
from hub import alerts, audit, auth, commons, crud, events, manage, plans, rbac, scopes, ui_kit
from hub.abuse import RateLimited, TraceRejected, make_named_limiter, make_rate_limiter, rate_limit_key
from hub.admin import (
    _FORM_GUARD_SCRIPT,
    _limit,
    _num,
    auto_refresh_script,
    h,
    html_headers,
    live_badge,
    refuse_cross_origin,
    secret_field,
)
from hub.billing import StripeSettings, create_billing_portal_session, create_checkout_session
from hub.config import HubConfig
from hub.db import session_scope
from hub.encryption import NULL_CIPHER, EnvelopeCipher
from hub.models import AlertRule, ApiKey, AuditLogEntry, Organization, User, WebhookEndpoint

logger = logging.getLogger("commontrace.hub.console")

CONSOLE_PATH = "/app"
SESSION_COOKIE = "ct_console"

SESSION_TTL_SECONDS = 8 * 60 * 60


def _sign(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def issue_session(secret: str, org_id: str, key_prefix: str = "") -> str:
    """A signed, self-contained session token."""
    payload = json.dumps(
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
    if claims.get("kind") == "share_proof":
        return None
    return claims


SHARE_TOKEN_TTL_SECONDS = 14 * 24 * 60 * 60


def issue_share_token(
    secret: str, org_id: str, ttl_seconds: int = SHARE_TOKEN_TTL_SECONDS, generation: int = 0
) -> str:
    payload = json.dumps(
        {"kind": "share_proof", "org": org_id, "exp": int(time.time()) + ttl_seconds, "gen": int(generation)},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(secret, payload)}"


def read_share_token(secret: str, token: str) -> dict | None:
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


_FAVICON_LINK = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='6' fill='%23111'/%3E"
    "%3Ctext x='16' y='23' font-size='18' font-family='ui-monospace,monospace' "
    "text-anchor='middle' fill='%23fff'%3EC%3C/text%3E%3C/svg%3E\">"
)


_ACTIONS_TH = "<th><span class='sr-only'>Actions</span></th>"

_NAV_GROUPS = (
    ("Insights", (
        ("", "Overview", "Your fleet", "overview", "g o"),
        ("/proof", "Proof", "Proof", "proof", "g p"),
        ("/memory", "Memory", "Memory", "memory", "g m"),
        ("/kb", "Knowledge Base", "Knowledge Base", "kb", "g k"),
    )),
    ("Administration", (
        ("/users", "Users", "Users & roles", "users", "g u"),
        ("/keys", "API Keys", "API keys", "keys", "g a"),
        ("/alerts", "Alerts", "Alerts", "alerts", "g l"),
        ("/webhooks", "Webhooks", "Webhooks", "webhooks", "g w"),
        ("/audit", "Audit log", "Audit log", "audit", "g t"),
    )),
)
_NAV = tuple((path, label, title) for _group, items in _NAV_GROUPS for path, label, title, *_rest in items)


_VIEW: contextvars.ContextVar[dict] = contextvars.ContextVar("console_view", default={})


_SIGN_OUT_FORM = (
    f'<form method="post" action="{CONSOLE_PATH}/signout">'
    f'<button type="submit" class="linkish">{ui_kit.icon("logout")}Sign out</button></form>'
)


def _sidebar(title: str) -> str:
    view = _VIEW.get()
    org_name = str(view.get("org_name") or "Your organisation")
    access = "admin access" if view.get("is_admin") else "read access"
    key = str(view.get("key_prefix") or "")
    groups = []
    for group, items in _NAV_GROUPS:
        links = "".join(
            f'<a href="{CONSOLE_PATH}{path}" data-key="{key_hint}"'
            f'{" aria-current=page" if title == page_title else ""}>'
            f"{ui_kit.icon(icon_name)}<span>{label}</span><kbd aria-hidden=\"true\">{key_hint}</kbd></a>"
            for path, label, page_title, icon_name, key_hint in items
        )
        groups.append(f'<div class="grp" aria-hidden="true">{group}</div>{links}')
    plan = str(view.get("plan") or "")
    plan_line = (
        f'<div class="plan"><span>Plan</span><span class="badge">{h(plan)}</span></div>' if plan else ""
    )
    return (
        '<aside class="side">'
        f'<a class="brand" href="{CONSOLE_PATH}"><span class="mark">{ui_kit.icon("mark")}</span>CommonTrace</a>'
        f'<div class="orgcard"><span class="avatar" aria-hidden="true">{h(ui_kit.initials(org_name))}</span>'
        f'<div class="who"><b title="{h(org_name)}">{h(org_name)}</b>'
        f'<span>{h(access)}{" · " + h(key) if key else ""}</span></div></div>'
        f'<nav aria-label="Console">{"".join(groups)}</nav>'
        f'<div class="side-foot">{plan_line}{_SIGN_OUT_FORM}</div>'
        "</aside>"
    )


def _topbar(title: str, badge: str) -> str:
    org_name = str(_VIEW.get().get("org_name") or "")
    crumbs = (f'<span>{h(org_name)}</span><span class="sep" aria-hidden="true">/</span>' if org_name else "")
    theme_icons = "".join(
        f'<span data-theme-icon="{mode}">{ui_kit.icon(name)}</span>'
        for mode, name in (("system", "system"), ("light", "sun"), ("dark", "moon"))
    )
    return (
        '<header class="top">'
        f'<div class="crumbs">{crumbs}<b>{h(title)}</b></div>'
        f'<div class="top-actions">{badge}'
        '<button type="button" class="search-btn" data-palette data-js hidden aria-label="Open the command palette">'
        f'{ui_kit.icon("search")}<span>Search or jump to…</span><kbd data-mod-key>Ctrl K</kbd></button>'
        '<button type="button" class="icon-btn" data-theme-toggle data-js hidden aria-label="Change colour theme">'
        f"{theme_icons}</button>"
        "</div></header>"
    )


def _document(
    title: str, body: str, *scripts: str, referrer: str = "same-origin", theme_script: bool = True,
) -> HTMLResponse:
    head_script = ui_kit.THEME_SCRIPT if theme_script else ""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{h(title)} · CommonTrace</title>{_FAVICON_LINK}{head_script}"
        f"<style>{ui_kit.CSS}</style></head><body>{body}{''.join(scripts)}</body></html>",
        headers=html_headers(head_script, *scripts, referrer=referrer),
    )


def _page(
    title: str, body: str, *, signed_in: bool = True, auto_refresh_seconds: int = 0,
) -> HTMLResponse:
    if not signed_in:
        return _document(title, f'<main id="main" class="auth">{body}</main>', _FORM_GUARD_SCRIPT)
    badge = live_badge(auto_refresh_seconds)
    refresh_script = auto_refresh_script(auto_refresh_seconds) if auto_refresh_seconds else ""
    shell = (
        '<a class="skip" href="#main">Skip to content</a>'
        f'<div class="shell">{_sidebar(title)}<div class="col">{_topbar(title, badge)}'
        f'<main id="main" tabindex="-1">{body}</main></div></div>'
    )
    return _document(title, shell, refresh_script, _FORM_GUARD_SCRIPT, ui_kit.APP_SCRIPT)


def _shared_page(body: str, *, expires_at: int) -> HTMLResponse:
    _expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    until = f"{_expires_dt:%B} {_expires_dt.day}, {_expires_dt:%Y}"
    banner = (
        f'<div class="shared-banner">{ui_kit.icon("shield")}<span>Shared, read-only report — generated '
        f"from live data by a CommonTrace customer. Link active until {h(until)}.</span></div>"
    )
    top = (
        '<header class="shared-top">'
        f'<span class="brand"><span class="mark">{ui_kit.icon("mark")}</span>CommonTrace</span>'
        '<span class="badge">shared report</span></header>'
    )
    return _document("Proof", f"{top}<main id=\"main\">{banner}{body}</main>", referrer="no-referrer",
                     theme_script=False)


def _tiles(items: list[tuple]) -> str:
    cells = []
    for item in items:
        k, v = item[0], item[1]
        extra = item[2] if len(item) > 2 else ""
        cells.append(f'<div class="tile"><div class="k">{h(k)}</div><div class="v">{v}</div>{extra}</div>')
    return f'<div class="tiles">{"".join(cells)}</div>'


_TONE = {"OK": "ok", "WEAKENS": "warn", "INVALIDATES": "bad"}
_VERDICT_TONE = {"SOUND": "good", "WEAKENED": "warn", "COMPROMISED": "bad"}


def _validity_block(report: dict) -> str:
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


def _projection_block(report: dict, titles: dict | None = None) -> str:
    pending = [p for p in report.get("projections", []) if p.get("still_needed")]
    if not pending:
        return ""
    titles = titles or {}
    rows = "".join(
        f"<tr><td>{h(titles.get(str(p.get('trace_id'))) or p.get('trace_id'))}</td>"
        f"<td class=\"n\">{_num(p.get('n_injected'))} / {_num(p.get('n_withheld'))}</td>"
        f"<td>{_num(p.get('still_needed'))} more in the {h(p.get('binding_arm'))} arm</td>"
        f"<td>{h(p.get('eta') or '—')}</td></tr>"
        for p in pending
    )
    advice = pending[0].get("advice") or ""
    return (
        _section("When will this be answerable?",
                 "Underpowered on the last day of a pilot is a spent pilot. "
                 "The same fact now is a holdout rate you can still change.")
        + "<div class='scroll'><table><thead><tr><th>Memory</th><th>injected / withheld</th>"
        f"<th>Needs</th><th>At the current rate</th></tr></thead><tbody>{rows}</tbody></table></div>"
        + (f'<p class="muted">{h(advice)}</p>' if advice else "")
    )


async def _overview_data(session, org_id: str) -> dict:
    return {
        "entitlements": await crud.entitlements(session, org_id),
        "search": await crud.search_health(session, org_id),
        "recent": await crud.search_traces(session, org_id, limit=5),
    }


def _render_billing_block(billing: dict | None) -> str:
    if not billing or not billing.get("enabled"):
        return ""
    plan = str(billing.get("plan") or plans.DEFAULT_PLAN)
    if billing.get("has_subscription"):
        return (
            '<div class="share-box"><b>Billing</b>'
            f'<span class="muted">Current plan: {h(plan)}. Manage your payment method, '
            "invoices, or change plans in Stripe's billing portal.</span>"
            f'<form method="post" action="{CONSOLE_PATH}/billing/portal">'
            '<button type="submit">Manage billing</button></form></div>'
        )
    upgrades = "".join(
        f'<form method="post" action="{CONSOLE_PATH}/billing/checkout" class="inline">'
        f'<input type="hidden" name="plan" value="{name}">'
        f'<button type="submit">Upgrade to {h(name.capitalize())}</button></form>'
        for name in billing.get("available_plans") or []
    )
    if not upgrades:
        return ""
    return (
        '<div class="share-box"><b>Billing</b>'
        f'<span class="muted">Current plan: {h(plan)}. Upgrade for more storage, agents, and '
        "Knowledge Base queries.</span>"
        f'<div class="form-actions">{upgrades}</div></div>'
    )


def _render_overview(
    data: dict, causal: dict, billing: dict | None = None,
    activity: dict | None = None, setup: dict | None = None,
) -> str:
    ent = data["entitlements"]
    traces = ent.get("traces") or {}
    agents = ent.get("agents") or {}
    search = data["search"] or {}

    body = [_head(
        "Your fleet",
        "Everything on this page is your organisation's own data. Nothing here is shared with, "
        "or drawn from, another customer.",
        actions=(f'<a class="btn secondary" href="{CONSOLE_PATH}/memory" data-command="Search your memory">'
                 f'{ui_kit.icon("search")}Search memory</a>'
                 f'<a class="btn" href="{CONSOLE_PATH}/proof">{ui_kit.icon("proof")}Proof report</a>'),
    )]
    body.append(_tiles([
        ("Traces stored", f'{_num(traces.get("used", 0))} <span class="muted">of '
                          f'{_limit(traces.get("limit"))}</span>',
         ui_kit.meter(traces.get("used", 0), traces.get("limit"))),
        ("Agents under management", f'{_num(agents.get("active", 0))} <span class="muted">of '
                                    f'{_limit(agents.get("limit"))}</span>',
         ui_kit.meter(agents.get("active", 0), agents.get("limit"))),
        ("Searches this month", _num(search.get("searches", 0))),
        ("Searches that found nothing", _miss(search)),
        ("Plan", h(ent.get("plan", "—")), f'<div class="foot">Billing period {h(ent.get("period", "—"))}</div>'),
    ]))
    body.append(_render_billing_block(billing))
    body.append(_setup_steps(data, causal, setup))
    body.append(_working_panel(causal, activity))

    if activity:
        body.append(
            '<section class="panel" aria-labelledby="activity-h"><div class="panel-head"><div>'
            '<h2 id="activity-h">Captured experience</h2>'
            f'<p class="muted">Traces your fleet captured each week, last {len(activity.get("weeks", []))} weeks.'
            "</p></div></div>"
            + ui_kit.bar_chart("captured", activity.get("weeks", []), activity.get("traces", []),
                               title="Traces captured per week", unit="traces")
            + "</section>"
        )

    rows = data["recent"].get("traces") or []
    body.append(_section("Most recent", f'<a href="{CONSOLE_PATH}/memory">Everything in memory →</a>'))
    if rows:
        cells = "".join(
            f"<tr><td>{h(t.get('title'))}</td><td>{h(t.get('agent_type'))}</td>"
            f"<td class=\"n\">{_num(t.get('retrievals', 0))}</td>"
            f"<td>{ui_kit.time_html(t.get('created_at'))}</td></tr>"
            for t in rows
        )
        body.append("<div class='scroll'><table><thead><tr><th>Trace</th><th>Agent type</th>"
                    f"<th>Retrieved</th><th>Captured</th></tr></thead><tbody>{cells}</tbody>"
                    "</table></div>")
    else:
        body.append(_empty("inbox", "No traces yet",
                           "Your agents capture them with <code>contribute_trace</code>."))
    return "".join(body)


def _miss(search: dict) -> str:
    rate = search.get("miss_rate")
    if rate is None:
        return '<span class="muted">no searches yet</span>'
    return f'{rate:.0%} <span class="muted">of {_num(search.get("searches_with_terms", 0))}</span>'


def _policy_block(policy: dict) -> str:
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


def _render_share_revoke(is_admin: bool) -> str:
    if not is_admin:
        return ""
    return (
        f'<form method="post" action="{CONSOLE_PATH}/proof/share/revoke" class="inline" '
        'data-confirm="Revoke every share link issued for this report? Anyone holding one '
        'will see Not found. This cannot be undone.">'
        '<button type="submit" class="danger">Revoke all share links</button></form>'
    )


_SHARE_FLASH = {"revoked": "Every share link issued before now has been revoked."}


def _render_share_form(share_url: str | None, is_admin: bool = False) -> str:
    days = SHARE_TOKEN_TTL_SECONDS // 86400
    revoke = _render_share_revoke(is_admin)
    revoke_line = (
        f'<div class="form-actions"><span class="muted">Sent a link somewhere it should not have gone?</span>'
        f"{revoke}</div>" if revoke else ""
    )
    if share_url:
        return (
            '<div class="share-box" data-command="Share this report">'
            f'<b id="share-url-label">{ui_kit.icon("share")} Shareable link generated.</b>'
            f'<span class="muted">Valid {days} days, always shows LIVE data (not a frozen snapshot), visible to '
            "anyone who has the link -- treat it like the report data it is.</span>"
            f'{secret_field(share_url, "share-url-label")}{revoke_line}</div>'
        )
    return (
        '<div class="share-box" data-command="Share this report">'
        "<b>Share this report</b>"
        '<span class="muted">A read-only link to this live page -- no sign-in required to view '
        f"it, always shows current data, expires in {days} days.</span>"
        f'<form method="post" action="{CONSOLE_PATH}/proof/share" class="inline">'
        f'<button type="submit">{ui_kit.icon("share")}Generate shareable link</button></form>'
        f"{revoke_line}</div>"
    )


def _render_experiment_controls(causal: dict, is_admin: bool, *, error: str = "") -> str:
    if not is_admin:
        return ""
    body = ['<div class="share-box" id="experiment" data-command="Experiment control">']
    if error:
        body.append(f'<p class="err" role="alert">{h(error)}</p>')
    if causal.get("experiment_running"):
        body.append(
            "<b>Experiment control</b>"
            '<span class="muted">Stopping keeps every observation recorded so far -- it '
            "only stops withholding memory on new occasions.</span>"
            f'<form method="post" action="{CONSOLE_PATH}/proof/experiment/stop" class="inline" '
            'data-confirm="Stop the running experiment?">'
            '<button type="submit" class="danger">Stop experiment</button></form>'
        )
    else:
        body.append(
            "<b>Start a randomized holdout</b>"
            '<span class="muted">Starts a NEW experiment with a fresh randomization -- any '
            "prior observations stop being pooled with what comes next. Your agents must "
            "call <code>holdout_assign</code> before injecting and "
            "<code>record_occasion_outcome</code> afterwards, or nothing is measured."
            "</span>"
            f'<form method="post" action="{CONSOLE_PATH}/proof/experiment/start" class="stack" style="width:100%">'
            '<div class="form-grid">'
            '<div><label for="exp-rate">Holdout rate</label>'
            f'<input type="number" id="exp-rate" name="rate" step="0.01" min="0.01" '
            f'max="0.99" value="{manage.DEFAULT_HOLDOUT_RATE}" required aria-describedby="exp-rate-help">'
            '<span class="faint" id="exp-rate-help" style="font-size:.78rem">fraction of eligible injections '
            "withheld</span></div>"
            '<div><label for="exp-outcome">Primary outcome label</label>'
            '<input type="text" id="exp-outcome" name="outcome" value="resolved" required '
            'aria-describedby="exp-outcome-help">'
            '<span class="faint" id="exp-outcome-help" style="font-size:.78rem">what <code>succeeded=true</code> '
            "means when your agents call <code>record_occasion_outcome</code></span></div>"
            '<div><label for="exp-notes">Notes</label>'
            '<input type="text" id="exp-notes" name="notes" placeholder="optional"></div></div>'
            '<button type="submit">Start experiment</button></form>'
        )
    body.append("</div>")
    return "".join(body)


def _render_proof(
    outcomes: dict, causal: dict, worth: dict | None = None,
    *, is_admin: bool = False, experiment_error: str = "", controls: str = "", shared: bool = False,
) -> str:
    actions = "" if shared else (
        f'<a class="btn secondary" href="{CONSOLE_PATH}/proof/assignments.csv" '
        f'data-command="Download every arm decision as CSV">{ui_kit.icon("download")}Assignments CSV</a>'
        f'<button type="button" class="secondary" data-print data-js hidden>{ui_kit.icon("print")}'
        "Print or save as PDF</button>"
    )
    body = [_head("Proof",
                  "Two different questions, deliberately not merged: what changed since your baseline, "
                  "and what this memory <em>caused</em>.", actions)]
    experiment_controls = "" if shared else _render_experiment_controls(causal, is_admin, error=experiment_error)
    if controls or experiment_controls:
        body.append(f'<div class="grid-2">{controls}{experiment_controls}</div>')
    body.append(_value_block(worth or {}))

    integrity = causal.get("integrity") or {}
    body.append(_section("Caused by the memory (randomized holdout)"))
    if not shared:
        body.append(
            f'<p class="muted"><a href="{CONSOLE_PATH}/proof/assignments.csv">Download every arm '
            "decision as CSV</a> -- the signed artifact your own analyst re-runs the comparison "
            "from, including the occasions the estimate above had to drop for never reporting an "
            "outcome.</p>"
        )
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
                f"<td data-v=\"{e.get('effect') if isinstance(e.get('effect'), (int, float)) else ''}\">"
                f"{_signed(e.get('effect'))}</td>"
                f"<td>{_ci(e.get('ci_95'))}</td>"
                f"<td>{_significance(e)}</td></tr>"
                for e in effects
            )
            body.append(
                '<section class="panel" aria-labelledby="forest-h"><div class="panel-head"><div>'
                '<h2 id="forest-h">Effect of each memory</h2>'
                '<p class="muted">Change in success rate when the memory was injected, against occasions '
                "it was withheld from. The bar is the 95% interval: one that crosses zero is not yet an "
                "answer either way.</p></div></div>"
                + ui_kit.forest_plot(effects)
                + '<details><summary class="muted" style="font-size:.82rem;cursor:pointer">'
                "Every figure as a table</summary><div class='scroll'><table><thead><tr><th>Memory</th>"
                "<th>Verdict</th><th>With</th><th>Without</th><th>Effect</th><th>95% CI</th>"
                f"<th>Significance</th></tr></thead><tbody>{rows}</tbody></table></div></details>"
                "</section>"
            )
            notes = [e for e in effects if e.get("note")]
            if notes:
                body.append(
                    f'<details class="more"><summary>Why {len(notes)} of these cannot answer yet</summary><ul>'
                    + "".join(
                        f"<li><b>{h(e.get('title') or e.get('trace_id'))}</b> — {h(e.get('note'))}</li>"
                        for e in notes
                    ) + "</ul></details>"
                )
        elif effects:
            body.append('<p class="sub">Effect sizes are withheld while the validity '
                        "verdict above is COMPROMISED. They would not be estimates of the "
                        "causal effect, and showing them with a caveat is how the caveat "
                        "gets separated from the number.</p>")
        else:
            body.append(_empty("proof", "No memory has enough observations in both arms yet",
                               "Every resolved occasion adds to one of the two arms; the estimate appears "
                               "once both have enough."))
        titles = {str(e.get("trace_id")): e.get("title") for e in effects if e.get("title")}
        body.append(_projection_block(integrity, titles))

    body.append(_section("Observed change since your baseline window"))
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
        body.append("<div class='scroll'><table><thead><tr><th>Metric</th><th>Baseline</th><th>Now</th>"
                    f"<th>Change</th><th>Verdict</th></tr></thead><tbody>{cells}</tbody></table></div>")
        notes = [r for r in rows if r.get("note")]
        if notes:
            body.append('<details class="more"><summary>What each verdict is based on</summary><ul>' + "".join(
                f"<li><b>{h(r.get('metric'))}</b> — {h(r.get('note'))}</li>" for r in notes
            ) + "</ul></details>")
    body.append('<p class="muted"><em>This half is an OBSERVED change, not a causal '
                "effect. `baseline` marks a time window, so a model upgrade or a shift in "
                "your task mix sits inside it. That is why the holdout above exists.</em></p>")
    return "".join(body)


def _pct(value: object) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "—"


def _rate(window: object) -> str:
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
    body = [_head("Memory",
                  "What your fleet has captured. Ranked by how recently it was written; "
                  "searchable the same way your agents search it.")]
    body.append(
        f'<form method="get" action="{CONSOLE_PATH}/memory" class="act" role="search" '
        'data-command="Search your memory">'
        '<label for="memory-q" class="sr-only">Search your memory</label>'
        f'<input type="search" id="memory-q" name="q" style="flex:1 1 24rem" '
        f'placeholder="Describe a task in your own words…" '
        f'value="{h(result.get("query", ""))}">'
        f'<button type="submit">{ui_kit.icon("search")}Search</button></form>'
    )
    terms = result.get("terms") or []
    ignored = result.get("terms_ignored") or []
    if result.get("query"):
        body.append(f'<p class="muted">Matched on: {h(", ".join(terms)) or "nothing searchable"}'
                    + (f' · too common to discriminate: {h(", ".join(ignored))}' if ignored else "")
                    + "</p>")
    if traces:
        rows = "".join(
            f"<tr><td><b>{h(t.get('title'))}</b>"
            + ('<div class="concerns">' + "".join(
                f'<span class="pill">{h(tag)}</span>' for tag in (t.get("tags") or [])[:6]
            ) + "</div>" if t.get("tags") else "")
            + f"</td><td>{h(t.get('agent_type'))}</td><td class=\"n\">{_num(t.get('retrievals', 0))}</td>"
            f"<td>{ui_kit.time_html(t.get('created_at'))}</td></tr>"
            for t in traces
        )
        body.append("<div class='scroll'><table><thead><tr><th>Trace</th><th>Agent type</th>"
                    f"<th>Retrieved</th><th>Captured</th></tr></thead><tbody>{rows}</tbody></table></div>")
        limit = int(result.get("limit") or len(traces) or 1)
        offset = int(result.get("offset") or 0)
        query_param = f'&q={_url_quote(result.get("query", ""))}' if result.get("query") else ""
        body.append(_pager(f"{CONSOLE_PATH}/memory", offset, limit, bool(result.get("has_more")), query_param))
    elif result.get("query"):
        body.append(_empty("search", "Nothing matched",
                           "That is an answer about this corpus, not an error — and the terms above say "
                           "whether the query reduced to anything searchable."))
    else:
        body.append(_empty("inbox", "No traces yet",
                           "Your agents capture them with <code>contribute_trace</code>."))
    if tags:
        body.append(_section("Tags in use"))
        body.append('<div class="concerns">' + "".join(
            f'<a class="pill" href="{CONSOLE_PATH}/memory?q={_url_quote(t)}">{h(t)}</a>' for t in tags[:60]
        ) + "</div>")
    return "".join(body)


_STANDING_TONE = {
    "established": "ok",
    "stale": "warn",
    "disputed": "bad",
    "unproven": "",
}

_STANDING_MEANING = {
    "established": "enough fleets tried it and it worked",
    "stale": "past its review date — may have drifted",
    "disputed": "fleets tried it and a majority say it did NOT work",
    "unproven": "not enough votes yet to say either way",
}


_CONCERN_LABEL = {
    "security_concern": "security concern",
    "outdated": "outdated",
    "wrong": "does not work",
    "spam": "spam",
}

_CONCERN_TONE = {
    "security_concern": "bad",
    "wrong": "bad",
    "outdated": "warn",
    "spam": "mute",
}


def _render_kb_concerns(entry: dict) -> str:
    concerns = entry.get("concerns") or {}
    if not concerns:
        return ""
    ordered = sorted(
        concerns.items(),
        key=lambda kv: (kv[0] != "security_concern", -kv[1], kv[0]),
    )
    pills = "".join(
        f'<span class="pill {_CONCERN_TONE.get(tag, "")}" '
        f'title="{_num(count)} organisation(s) reported this">'
        f'{h(_CONCERN_LABEL.get(tag, tag))} &times;{_num(count)}</span> '
        for tag, count in ordered
    )
    return f'<div class="concerns">{pills}</div>'


def _render_kb_vote(entry: dict, can_vote: bool) -> str:
    if not can_vote:
        return '<span class="muted">—</span>'
    trace_id = h(entry.get("id"))
    mine = str(entry.get("my_vote") or "")
    up_state = " voted" if mine == "up" else ""
    down_state = " voted" if mine == "down" else ""
    tags = "".join(
        f'<option value="{h(t)}">{h(t or "reason (optional)")}</option>'
        for t in ("", "outdated", "wrong", "security_concern", "spam")
    )
    return (
        f'<form method="post" action="{CONSOLE_PATH}/kb/vote" class="vote">'
        f'<input type="hidden" name="trace_id" value="{trace_id}">'
        f'<button type="submit" name="vote" value="up" class="v{up_state}" '
        f'title="This worked for us">&#9650;</button>'
        f'<button type="submit" name="vote" value="down" class="v{down_state}" '
        f'title="This did not work for us">&#9660;</button>'
        f'<label class="sr-only" for="fb-{trace_id}">Why (for a downvote)</label>'
        f'<select id="fb-{trace_id}" name="feedback_tag">{tags}</select>'
        "</form>"
    )


def _render_kb_entry(entry: dict, can_vote: bool = False) -> str:
    standing = str(entry.get("standing") or "")
    tone = _STANDING_TONE.get(standing, "")
    revisions = int(entry.get("revisions") or 0)
    revised = (
        f'<br><span class="pill mute" title="Corrected {_num(revisions)} time(s). '
        'Votes reset on each correction, because they judged text that is no '
        'longer there.">revised &times;'
        f'{_num(revisions)}</span>'
        if revisions else ""
    )
    tags = "".join(
        f'<span class="pill">{h(t)}</span> ' for t in (entry.get("tags") or [])[:6]
    )
    return (
        f"<tr><td><b>{h(entry.get('title'))}</b><br>"
        f'<span class="muted">{h(entry.get("solution_preview"))}</span><br>{tags}</td>'
        f'<td><span class="pill {tone}" title="{h(_STANDING_MEANING.get(standing, ""))}">'
        f"{h(standing)}</span>{revised}{_render_kb_concerns(entry)}</td>"
        f'<td class="rev">{_num(entry.get("votes", 0))} vote(s)</td>'
        f'<td class="rev">{_num(entry.get("hits", 0))}</td>'
        f"<td>{_render_kb_vote(entry, can_vote)}</td>"
        f'<td class="rev">{h(entry.get("id"))}</td></tr>'
    )


def _render_kb(
    submissions: list[dict],
    ent: dict,
    browse: dict | None = None,
    *,
    tag: str = "",
    can_submit: bool = False,
    can_vote: bool = False,
    vote_counts: bool = True,
    auto_contribute: bool = False,
    error: str = "",
    flash: str = "",
) -> str:
    actions = (f'<a class="btn" href="#propose" data-command="Propose a Knowledge Base entry">'
               f'{ui_kit.icon("plus")}Propose an entry</a>' if can_submit else "")
    body = [_head("Knowledge Base",
                  "The one surface where anything crosses an organisation boundary — and it crosses it "
                  "through a person. You consult the Knowledge Base by sending a <em>signature</em>, never "
                  "your text; you propose an entry and an operator decides. No other customer sees your "
                  "traces, ever.", actions)]
    if flash:
        body.append(f'<div class="flash" role="status">{h(flash)}</div>')
    if error:
        body.append(f'<p class="err" role="alert">{h(error)}</p>')
    queries = ent.get("commons_queries") or {}
    body.append(_tiles([
        ("Consultations used", f'{_num(queries.get("used", 0))} <span class="muted">of '
                               f'{_num(queries.get("allowance", 0))}</span>'),
        ("Remaining", _num(queries.get("remaining", 0))),
        ("Earned by accepted proposals", _num(queries.get("bonus_from_accepted_submissions", 0))),
        ("Proposals sent", str(len(submissions))),
    ]))

    if browse is not None:
        body.append(_section("Browse the open repository", anchor="browse", command="Browse the open repository"))
        body.append(
            '<p class="sub">Every entry here is operator-curated and public to all '
            "organisations — never another customer's private trace. Browsing costs no "
            "consultation: what you see below are previews, and asking the Knowledge Base "
            "about a <em>specific</em> failure of yours (which is what spends one) still "
            "goes through your agents with a signature, never your text.</p>"
        )
        body.append(
            f'<form method="get" action="{CONSOLE_PATH}/kb" class="act">'
            '<label class="sr-only" for="kb-tag">Filter by tag</label>'
            f'<input type="text" id="kb-tag" name="tag" value="{h(tag)}" '
            'placeholder="filter by tag (blank = everything)">'
            "<button type=\"submit\">Filter</button></form>"
        )
        entries = browse.get("entries") or []
        if entries:
            rows = "".join(_render_kb_entry(e, can_vote) for e in entries)
            body.append(
                "<div class='scroll'><table><thead><tr><th>Entry</th><th>Standing</th><th>Votes</th>"
                f"<th>Times used</th><th>Your vote</th><th>Id</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>"
            )
            body.append(
                '<p class="sub">Your vote is what moves an entry between '
                '<em>unproven</em>, <em>established</em> and <em>disputed</em> — the same '
                "signal the operator's review queue sorts on. Voting is per organisation, "
                "and voting again changes your vote rather than adding one.</p>"
            )
            if can_vote and not vote_counts:
                body.append(
                    '<p class="sub">Your votes are <b>recorded but not yet counted</b> '
                    "toward an entry's standing. Organisations qualify once they have "
                    f"captured {commons.COMMONS_VOTER_MIN_TRACES} traces and are more than "
                    f"{commons.COMMONS_VOTER_MIN_AGE_HOURS} hours old — the bar that "
                    "keeps a handful of throwaway signups from deciding what the field "
                    "thinks. Nothing is lost in the meantime: votes you cast now start "
                    "counting the moment you qualify.</p>"
                )
            total = browse.get("total", 0)
            shown = len(entries)
            offset = browse.get("offset", 0)
            nav = []
            if offset > 0:
                previous = max(0, offset - browse.get("limit", 25))
                nav.append(
                    f'<a href="{CONSOLE_PATH}/kb?tag={h(tag)}&offset={previous}">&larr; Newer</a>'
                )
            if browse.get("has_more"):
                nxt = offset + browse.get("limit", 25)
                nav.append(
                    f'<a href="{CONSOLE_PATH}/kb?tag={h(tag)}&offset={nxt}">Older &rarr;</a>'
                )
            body.append(
                f'<div class="pager"><span>Showing {_num(shown)} of {_num(total)} entries</span>'
                + "".join(nav) + "</div>"
            )
        elif tag:
            body.append(_empty("kb", f"No entries tagged {h(tag)!r}", "Clear the filter to see everything."))
        else:
            body.append(_empty("kb", "The Knowledge Base has no published entries yet"))

    body.append(_section("Contribute back"))
    if can_submit:
        state = "on" if auto_contribute else "off"
        turning = "off" if auto_contribute else "on"
        body.append(
            '<p class="sub">Automatic contribution is currently '
            f"<b>{state}</b>. When it is on, every trace your agents capture is also "
            "proposed to the Knowledge Base — you stop having to remember to propose "
            "them one at a time. Nothing is published by this: an operator still reviews "
            "every proposal, and until one is accepted no other organisation can see it. "
            "Quarantined traces are never proposed.</p>"
            f'<form method="post" action="{CONSOLE_PATH}/kb/auto-contribute" class="act">'
            f'<input type="hidden" name="enabled" value="{"0" if auto_contribute else "1"}">'
            f"<button type=\"submit\">Turn automatic contribution {turning}</button></form>"
        )
    else:
        body.append(
            '<p class="sub">Automatic contribution is '
            f"<b>{'on' if auto_contribute else 'off'}</b> for this organisation. "
            "Changing it needs an admin-scoped key.</p>"
        )

    body.append(_section("Propose an entry", anchor="propose"))
    if can_submit:
        body.append(
            '<p class="sub">An accepted proposal is published under the operator\'s name, '
            "not yours, and permanently raises your consultation allowance. Nothing you "
            "send here is visible to another organisation unless and until an operator "
            "accepts it. Never include secrets, credentials or customer data.</p>"
            f'<form method="post" action="{CONSOLE_PATH}/kb/submit" class="stack">'
            '<div><label for="kb-title">Title</label>'
            '<input type="text" id="kb-title" name="title" required maxlength="200"></div>'
            '<div><label for="kb-context">The problem</label>'
            '<textarea id="kb-context" name="context_text" required></textarea></div>'
            '<div><label for="kb-solution">What actually fixed it</label>'
            '<textarea id="kb-solution" name="solution_text" required></textarea></div>'
            '<div><label for="kb-tags">Tags, comma-separated</label>'
            '<input type="text" id="kb-tags" name="tags"></div>'
            '<div><label for="kb-rationale">Why this is worth publishing (optional)</label>'
            '<input type="text" id="kb-rationale" name="rationale" maxlength="300"></div>'
            '<div><button type="submit">Propose to the Knowledge Base</button></div></form>'
        )
    else:
        body.append(
            '<p class="sub">Proposing an entry needs an admin-scoped key. Your agents can '
            "also propose one directly with the <code>submit_kb_entry</code> tool.</p>"
        )

    if submissions:
        rows = "".join(
            f"<tr><td>{h(s.get('title'))}</td><td>{h(s.get('status'))}</td>"
            f"<td>{ui_kit.time_html(s.get('created_at'))}</td>"
            f"<td>{h(s.get('rejection_reason') or '—')}</td></tr>"
            for s in submissions
        )
        body.append(_section("Your proposals") + "<div class='scroll'><table><thead><tr><th>Title</th><th>Status</th>"
                    f"<th>Sent</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        body.append(_section("Your proposals") + _empty(
            "inbox", "None yet",
            "An accepted proposal is published under the operator's name, not yours, and earns you bonus "
            "consultations."))
    return "".join(body)


_ROLE_TONE = {"admin": "info", "owner": "info"}


def _render_users(users: list[User], is_admin: bool, error: str = "") -> str:
    actions = (f'<a class="btn" href="#create-user" data-command="Create a user">{ui_kit.icon("plus")}'
               "Create a user</a>" if is_admin else "")
    body = [_head("Users &amp; roles",
                  "A person, distinct from your org's shared API key — a named role checked as a second, "
                  "additive gate on every tool call. No SSO is linked by creating a row here; that is always "
                  "a separate, explicit step (<code>hub.manage link-sso</code>).", actions)]
    if error:
        body.append(f'<p class="err" role="alert">{h(error)}</p>')
    if users:
        rows = []
        for u in users:
            disabled = u.disabled_at is not None
            state = ('<span class="pill warn">disabled</span>' if disabled
                     else '<span class="pill ok">active</span>')
            linked = ('<span class="pill info">linked</span>' if u.external_subject
                      else '<span class="pill mute">no SSO linked</span>')
            actions_html = ""
            if is_admin:
                role_options = "".join(
                    f'<option value="{h(r)}"{" selected" if r == u.role else ""}>{h(r)}</option>'
                    for r in rbac.ROLES
                )
                actions_html = (
                    f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/role">'
                    f'<label for="role-{h(u.id)}" class="sr-only">Role for {h(u.email)}</label>'
                    f'<select id="role-{h(u.id)}" name="role">{role_options}</select>'
                    f'<button type="submit">Set role</button></form>'
                )
                if disabled:
                    actions_html += (
                        f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/enable">'
                        '<button type="submit">Enable</button></form>'
                    )
                else:
                    actions_html += (
                        f'<form method="post" action="{CONSOLE_PATH}/users/{h(u.id)}/disable" '
                        f'data-confirm="Disable {h(u.email)}? Their access ends on their next request.">'
                        '<button type="submit" class="danger">Disable</button></form>'
                    )
            name = getattr(u, "display_name", "") or ""
            rows.append(
                f"<tr><td><b>{h(name or u.email)}</b>"
                + (f'<div class="muted">{h(u.email)}</div>' if name else "")
                + f'</td><td><span class="pill {_ROLE_TONE.get(u.role, "")}">{h(u.role)}</span></td>'
                f"<td>{state}</td><td>{linked}</td><td>{actions_html}</td></tr>"
            )
        body.append(
            "<div class='scroll'><table><thead><tr><th>Person</th><th>Role</th><th>State</th>"
            f"<th>SSO</th>{_ACTIONS_TH}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        body.append(_empty("users", "No users yet",
                           "Add the people who should see this console under their own name and role."))
    if is_admin:
        role_options = "".join(f'<option value="{h(r)}">{h(r)}</option>' for r in rbac.ROLES)
        body.append(
            '<section class="panel" id="create-user" aria-labelledby="create-user-h">'
            '<h2 id="create-user-h">Create a user</h2>'
            f'<form method="post" action="{CONSOLE_PATH}/users/create" class="stack" style="max-width:none">'
            '<div class="form-grid">'
            '<div><label for="new-user-email">Email</label>'
            '<input type="email" id="new-user-email" name="email" placeholder="person@example.com" required></div>'
            '<div><label for="new-user-name">Display name</label>'
            '<input type="text" id="new-user-name" name="display_name" placeholder="optional"></div>'
            '<div><label for="new-user-role">Role</label>'
            f'<select id="new-user-role" name="role">{role_options}</select></div>'
            '<div><button type="submit">Create</button></div></div></form></section>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to create or '
                    "change a user's role.</p>")
    return "".join(body)


def _render_keys(keys: list[ApiKey], is_admin: bool, fresh: dict | None = None) -> str:
    actions = (f'<a class="btn" href="#issue-key" data-command="Issue a new API key">{ui_kit.icon("plus")}'
               "Issue a key</a>" if is_admin else "")
    body = [_head("API keys",
                  "Scopes do not imply each other: <code>admin</code> alone cannot read a trace. A production "
                  "agent wants <code>read,write</code>; a dashboard wants <code>read</code>.", actions)]
    if fresh:
        body.append(
            '<div class="verdict warn"><h2 id="new-key-label">New key — shown once</h2>'
            f'{secret_field(fresh["raw_key"], "new-key-label")}'
            "<p>Store this now. It will not be shown again, and this Hub keeps only its "
            "hash.</p></div>"
        )
    if keys:
        rows = []
        for k in keys:
            revoked = k.revoked_at is not None
            state = ('<span class="pill mute">revoked</span>' if revoked
                     else '<span class="pill ok">active</span>')
            scope_pills = (
                "".join(f'<span class="pill {"info" if sc == scopes.SCOPE_ADMIN else ""}">{h(sc)}</span> '
                        for sc in k.scopes)
                if k.scopes is not None else '<span class="pill warn">(all — legacy key)</span>'
            )
            expires = ui_kit.time_html(k.expires_at) if k.expires_at else '<span class="muted">never</span>'
            created = ui_kit.time_html(getattr(k, "created_at", None))
            actions_html = ""
            if is_admin and not revoked:
                actions_html = (
                    f'<form method="post" action="{CONSOLE_PATH}/keys/{h(k.id)}/rotate" '
                    f'data-confirm="Rotate key {h(k.key_prefix)}? It stops working now; agents using '
                    f'it need the new key.">'
                    '<button type="submit">Rotate</button></form>'
                    f'<form method="post" action="{CONSOLE_PATH}/keys/{h(k.id)}/revoke" '
                    f'data-confirm="Revoke key {h(k.key_prefix)}? Agents using it stop working, and '
                    f'this cannot be undone.">'
                    '<button type="submit" class="danger">Revoke</button></form>'
                )
            rows.append(
                f'<tr><td><code>{h(k.key_prefix)}</code></td><td>{scope_pills}</td>'
                f"<td>{created}</td><td>{expires}</td><td>{state}</td><td>{actions_html}</td></tr>"
            )
        body.append(
            "<div class='scroll'><table><thead><tr><th>Prefix</th><th>Scopes</th><th>Created</th><th>Expires</th>"
            f"<th>State</th>{_ACTIONS_TH}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        body.append(_empty("keys", "No keys yet", "A key is how an agent, a dashboard or this console signs in."))
    if is_admin:
        scope_boxes = "".join(
            f'<label><input type="checkbox" name="scopes" value="{h(s)}"> {h(s)}</label> '
            for s in scopes.ALL_SCOPES if s != scopes.SCOPE_SCIM
        )
        body.append(
            '<section class="panel" id="issue-key" aria-labelledby="issue-key-h">'
            '<h2 id="issue-key-h">Issue a new key</h2>'
            f'<form method="post" action="{CONSOLE_PATH}/keys/issue" class="stack" style="max-width:none">'
            f'<fieldset><legend>Scopes</legend>{scope_boxes}</fieldset>'
            '<div class="form-grid"><div><label for="new-key-expires">Expires in N days</label>'
            '<input type="number" id="new-key-expires" name="expires_days" '
            'placeholder="blank = never" min="1"></div>'
            '<div><button type="submit">Issue</button></div></div></form></section>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to issue, '
                    "rotate, or revoke keys.</p>")
    return "".join(body)


def _render_alerts(
    rules: list[AlertRule], is_admin: bool, error: str = "", report: dict | None = None,
) -> str:
    actions = (f'<a class="btn" href="#create-alert" data-command="Create an alert rule">{ui_kit.icon("plus")}'
               "Create a rule</a>" if is_admin else "")
    body = [_head("Alerts",
                  "Fires <code>alert.triggered</code> through your existing webhook endpoint(s) when a metric "
                  "crosses a threshold you set — no polling needed. A closed, named set of metrics, never a "
                  "free-form query.", actions)]
    if error:
        body.append(f'<p class="err" role="alert">{h(error)}</p>')
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
            state = ('<span class="pill ok">enabled</span>' if r.enabled
                     else '<span class="pill mute">disabled</span>')
            last = ui_kit.time_html(r.last_triggered_at) if r.last_triggered_at else '<span class="muted">never</span>'
            actions_html = ""
            if is_admin:
                actions_html = (
                    f'<form method="post" action="{CONSOLE_PATH}/alerts/{h(r.id)}/delete" '
                    'data-confirm="Delete this alert rule?">'
                    '<button type="submit" class="danger">Delete</button></form>'
                )
            rows.append(
                f"<tr><td><code>{h(r.metric)}</code></td><td>{h(r.comparator)}</td>"
                f'<td class="n">{h(r.threshold)}</td>'
                f'<td class="n">{h(r.cooldown_minutes)}m</td><td>{state}</td><td>{last}</td>'
                f"<td>{actions_html}</td></tr>"
            )
        body.append(
            "<div class='scroll'><table><thead><tr><th>Metric</th><th>Comparator</th><th>Threshold</th>"
            f"<th>Cooldown</th><th>State</th><th>Last fired</th>{_ACTIONS_TH}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        body.append(_empty("alerts", "No alert rules yet",
                           "A rule watches one metric and tells your webhook endpoints when it crosses the line."))
    if is_admin:
        metric_options = "".join(f'<option value="{h(m)}">{h(m)}</option>' for m in alerts.METRICS)
        comparator_options = "".join(
            f'<option value="{h(c)}">{h(c)}</option>' for c in alerts.COMPARATORS
        )
        body.append(
            '<div class="grid-2">'
            '<section class="panel" id="create-alert" aria-labelledby="create-alert-h">'
            '<h2 id="create-alert-h">Create a rule</h2>'
            f'<form method="post" action="{CONSOLE_PATH}/alerts/create" class="stack" style="max-width:none">'
            '<div class="form-grid">'
            '<div><label for="new-alert-metric">Metric</label>'
            f'<select id="new-alert-metric" name="metric">{metric_options}</select></div>'
            '<div><label for="new-alert-comparator">Comparator</label>'
            f'<select id="new-alert-comparator" name="comparator">{comparator_options}</select></div>'
            '<div><label for="new-alert-threshold">Threshold</label>'
            '<input type="number" step="any" id="new-alert-threshold" name="threshold" '
            'placeholder="Threshold" required></div>'
            '<div><label for="new-alert-cooldown">Cooldown minutes</label>'
            '<input type="number" id="new-alert-cooldown" name="cooldown_minutes" '
            f'placeholder="Cooldown minutes" value="{alerts.DEFAULT_COOLDOWN_MINUTES}" min="1"></div>'
            '</div><button type="submit">Create</button></form></section>'
            '<section class="panel" aria-labelledby="report-h"><h2 id="report-h">Usage report</h2>'
            '<p class="sub">A one-off <code>report.generated</code> event, the same '
            "shape a scheduled cron run or an operator's own "
            "<code>generate-report</code> would emit — for a customer who wants one "
            "now rather than waiting for the next cycle.</p>"
            f'<form method="post" action="{CONSOLE_PATH}/alerts/generate-report" class="inline" '
            'data-command="Generate a usage report now">'
            '<button type="submit">Generate now</button></form></section></div>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to create or '
                    "delete an alert rule, or generate a usage report.</p>")
    return "".join(body)


def _render_webhooks(
    endpoints: list[dict], pending: int, failed: list[dict], is_admin: bool,
    *, error: str = "", fresh: dict | None = None,
) -> str:
    actions = (f'<a class="btn" href="#add-endpoint" data-command="Add a webhook endpoint">{ui_kit.icon("plus")}'
               "Add an endpoint</a>" if is_admin else "")
    body = [_head("Webhooks",
                  "Tell your own systems what happened here — a trace quarantined, an experiment reaching a "
                  "verdict — without polling for it. Every payload carries only ids, counts and verdicts; a "
                  "webhook is egress to a third party, and this product's memory content never is.", actions)]
    if error:
        body.append(f'<p class="err" role="alert">{h(error)}</p>')
    if fresh and fresh.get("secret"):
        body.append(
            '<div class="share-box"><b id="webhook-secret-label">Signing secret '
            "(shown once)</b>"
            '<span class="muted">Verify the delivery signature with this. It cannot be shown '
            "again — rotate the endpoint to get a new one.</span>"
            f'{secret_field(fresh["secret"], "webhook-secret-label")}</div>'
        )
    body.append(_tiles([
        ("Endpoints", _num(len(endpoints))),
        ("Deliveries pending", _num(pending)),
        ("Gave up on delivering", f'<span class="{"bad" if failed else ""}">{_num(len(failed))}</span>'),
    ]))
    if endpoints:
        rows = []
        for e in endpoints:
            state = ('<span class="pill ok">enabled</span>' if e["enabled"]
                     else '<span class="pill bad">DISABLED</span>')
            subscribed = "".join(f'<span class="pill">{h(ev)}</span> ' for ev in e["events"]) or (
                '<span class="muted">(none)</span>')
            actions_html = ""
            if is_admin and e["enabled"]:
                actions_html = (
                    f'<form method="post" action="{CONSOLE_PATH}/webhooks/{h(e["id"])}/rotate" '
                    'data-confirm="Rotate the signing secret? Deliveries are signed with the new '
                    'one immediately.">'
                    '<button type="submit">Rotate secret</button></form>'
                    f'<form method="post" action="{CONSOLE_PATH}/webhooks/{h(e["id"])}/disable" '
                    'data-confirm="Disable this endpoint? It stops receiving events.">'
                    '<button type="submit" class="danger">Disable</button></form>'
                )
            rows.append(
                f"<tr><td><code>{h(e['url'])}</code></td><td>{state}</td><td>{subscribed}</td>"
                f"<td>v{h(e['key_version'])}</td><td>{actions_html}</td></tr>"
            )
        body.append(
            "<div class='scroll'><table><thead><tr><th>URL</th><th>State</th><th>Subscribed to</th>"
            f"<th>Key</th>{_ACTIONS_TH}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        body.append(_empty("webhooks", "No webhook endpoints configured",
                           "Add one to have verdicts, quarantines and alerts delivered to your own systems."))
    if failed:
        frows = "".join(
            f"<tr><td>{ui_kit.time_html(d['created_at'])}</td><td><code>{h(d['event_type'])}</code></td>"
            f"<td>{h(d['last_error'] or '')}</td></tr>"
            for d in failed
        )
        body.append(
            _section("Gave up on delivering",
                     "Retried and failed enough times that this stopped retrying — a misconfigured endpoint, "
                     "not a transient blip.")
            + "<div class='scroll'><table><thead><tr><th>When</th><th>Event</th><th>Last error</th></tr></thead>"
            f"<tbody>{frows}</tbody></table></div>"
        )
    if is_admin:
        event_boxes = "".join(
            f'<label><input type="checkbox" name="events" value="{h(name)}"> {h(name)}</label> '
            for name in events.EVENT_NAMES
        )
        body.append(
            '<section class="panel" id="add-endpoint" aria-labelledby="add-endpoint-h">'
            '<h2 id="add-endpoint-h">Add an endpoint</h2>'
            '<p class="sub">HTTPS only. Leave every box unchecked to subscribe to everything.</p>'
            f'<form method="post" action="{CONSOLE_PATH}/webhooks/create" class="stack" style="max-width:none">'
            '<div><label for="new-webhook-url">URL</label>'
            '<input type="url" id="new-webhook-url" name="url" placeholder="https://…" required></div>'
            f'<fieldset><legend>Events</legend>{event_boxes}</fieldset>'
            '<button type="submit">Add endpoint</button></form></section>'
        )
    else:
        body.append('<p class="muted">Sign in with an admin-scoped key to add, rotate, or '
                    "disable a webhook endpoint.</p>")
    return "".join(body)


def _render_audit_log(entries: list[AuditLogEntry], offset: int, limit: int, has_more: bool) -> str:
    body = [_head("Audit log",
                  "Consequential actions on your organisation — writes through the MCP tools and every admin "
                  "action, including the ones taken from this console itself. Ordinary reads (searches, "
                  "lookups) are not logged here.")]
    if not entries:
        body.append(_empty("audit", "No audit entries yet",
                           "Every write and every admin action will be recorded here, with who did it."))
        return "".join(body)
    rows = "".join(
        f"<tr><td>{ui_kit.time_html(e.created_at)}</td><td><code>{h(e.actor)}</code></td>"
        f'<td><span class="pill">{h(e.action)}</span></td>'
        f"<td>{h(f'{e.target_type}:{e.target_id}' if e.target_type else '—')}</td>"
        f"<td>{h(e.summary)}</td></tr>"
        for e in entries
    )
    body.append(
        "<div class='scroll'><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th>"
        f"<th>Detail</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )
    body.append(_pager(f"{CONSOLE_PATH}/audit", offset, limit, has_more))
    return "".join(body)


def _head(title: str, sub: str = "", actions: str = "") -> str:
    acts = f'<div class="actions">{actions}</div>' if actions else ""
    sub_html = f'<p class="sub">{sub}</p>' if sub else ""
    return f'<div class="page-head"><div><h1>{title}</h1>{sub_html}</div>{acts}</div>'


def _section(title: str, sub: str = "", anchor: str = "", command: str = "") -> str:
    attrs = f' id="{anchor}"' if anchor else ""
    if command:
        attrs += f' data-command="{h(command)}"'
    sub_html = f'<p class="sub">{sub}</p>' if sub else ""
    return f'<div class="section-head"{attrs}><h2>{title}</h2>{sub_html}</div>'


def _empty(icon_name: str, title: str, text: str = "") -> str:
    return (
        f'<div class="empty-state">{ui_kit.icon(icon_name)}<b>{title}</b>'
        + (f"<p>{text}</p>" if text else "")
        + "</div>"
    )


def _pager(base: str, offset: int, limit: int, has_more: bool, extra: str = "") -> str:
    links = []
    if offset > 0:
        links.append(f'<a href="{base}?offset={max(0, offset - limit)}{extra}">&larr; Newer</a>')
    if has_more:
        links.append(f'<a href="{base}?offset={offset + limit}{extra}">Older &rarr;</a>')
    return f'<nav class="pager" aria-label="Pages">{"".join(links)}</nav>' if links else ""


def _setup_steps(data: dict, causal: dict, setup: dict | None) -> str:
    setup = setup or {}
    traces = int(((data.get("entitlements") or {}).get("traces") or {}).get("used", 0) or 0)
    searches = int((data.get("search") or {}).get("searches", 0) or 0)
    steps = [
        (traces > 0, "Capture a first trace",
         "Your agents call <code>contribute_trace</code> after a task, or import one with "
         "<code>commontrace sync</code>."),
        (searches > 0, "Retrieve before a task",
         "Agents call <code>search_traces</code> with the task in their own words."),
        (bool(causal.get("experiment_running")), "Start a randomized holdout",
         f'From <a href="{CONSOLE_PATH}/proof">Proof</a>: the only thing that separates this memory\'s '
         "effect from everything else that changed."),
        (bool(causal.get("n_observations")), "Report outcomes",
         "Agents call <code>record_occasion_outcome</code> when a task ends — the experiment measures "
         "nothing without it."),
        (bool(setup.get("alerts") or setup.get("webhooks")), "Get told, not polled",
         f'Add an <a href="{CONSOLE_PATH}/alerts">alert</a> or a <a href="{CONSOLE_PATH}/webhooks">'
         "webhook</a> so a verdict reaches your own systems."),
    ]
    done = sum(1 for ok, *_ in steps if ok)
    if done == len(steps):
        return ""
    items = "".join(
        f'<li class="{"done" if ok else ""}"><span class="tick" aria-hidden="true">&#10003;</span>'
        f'<span class="what"><b>{title}</b><span class="muted">{text}</span></span>'
        f'<span class="sr-only">{"done" if ok else "not done yet"}</span></li>'
        for ok, title, text in steps
    )
    return (
        '<section class="panel" aria-labelledby="setup-h"><div class="panel-head">'
        '<h2 id="setup-h">Get to a measured answer</h2>'
        f'<span class="progress-label">{done} of {len(steps)} done</span></div>'
        f'{ui_kit.meter(done, len(steps), quota=False)}<ul class="steps">{items}</ul></section>'
    )


def _working_panel(causal: dict, activity: dict | None) -> str:
    running = bool(causal.get("experiment_running"))
    integrity = causal.get("integrity") or {}
    if running and causal.get("n_observations"):
        verdict = str(integrity.get("verdict", ""))
        tone = _VERDICT_TONE.get(verdict, "")
        pill = {"good": "ok", "warn": "warn", "bad": "bad"}.get(tone, "mute")
        hero = ""
        chart = ""
        if activity:
            t_n = sum(p[0] for p in activity.get("treated", []))
            t_ok = sum(p[1] for p in activity.get("treated", []))
            c_n = sum(p[0] for p in activity.get("control", []))
            c_ok = sum(p[1] for p in activity.get("control", []))
            if t_n and c_n:
                diff = t_ok / t_n - c_ok / c_n
                hero = (
                    '<div class="hero">'
                    f'<div><div class="big s1t">{t_ok / t_n:.0%}</div><div class="lbl">succeeded with memory '
                    f"({_num(t_n)} occasions)</div></div>"
                    f'<div><div class="big s2t">{c_ok / c_n:.0%}</div><div class="lbl">succeeded without it '
                    f"({_num(c_n)} held out)</div></div>"
                    f'<div><div class="big">{diff * 100:+.0f} pts</div><div class="lbl">difference, before '
                    "the validity checks on Proof</div></div></div>"
                )
            chart = ui_kit.rate_chart(
                "rate", activity.get("weeks", []), activity.get("treated", []), activity.get("control", []),
                title="Weekly success rate, with and without memory",
            )
        return (
            '<section class="panel" aria-labelledby="working-h"><div class="panel-head"><div>'
            '<h2 id="working-h">Is the memory working?</h2>'
            f'<p class="muted">A randomized holdout is running: {_num(causal.get("n_observations", 0))} '
            f'resolved observation(s) across {_num(causal.get("n_occasions", 0))} occasion(s). '
            f'Validity: <span class="pill {pill}">{h(verdict)}</span></p></div>'
            f'<a class="btn secondary" href="{CONSOLE_PATH}/proof">{ui_kit.icon("proof")}See the causal report</a>'
            f"</div>{hero}{chart}"
            '<p class="faint" style="font-size:.8rem">Each occasion counts once, in the week its outcome arrived. '
            "Whether the difference is caused by the memory, and how sure that is, is on Proof — "
            "under the validity verdict, not above it.</p></section>"
        )
    if running:
        return (
            '<div class="verdict warn"><h2>Is the memory working?</h2>'
            "<p>An experiment is running, but no occasion has been reported yet. Your "
            "agents need to call <code>holdout_assign</code> before injecting and "
            "<code>record_occasion_outcome</code> afterwards — without the second, "
            "nothing joins and nothing can be measured.</p></div>"
        )
    return (
        '<div class="verdict"><h2>Is the memory working?</h2>'
        "<p>No randomized holdout is running, so nothing here is causal yet. Start one from "
        "Proof (admin) or ask your operator — it is the only thing that separates this product's "
        "effect from everything else that changed in the same window.</p>"
        f'<p><a href="{CONSOLE_PATH}/proof">See the observed change →</a></p></div>'
    )


_SIGNIN = (
    '<div class="box">'
    f'<span class="brand"><span class="mark">{ui_kit.icon("mark")}</span>CommonTrace</span>'
    "<div><h1>Sign in to your console</h1>"
    '<p class="sub">Use an API key for your organisation — the same key your agents '
    "authenticate with. It is verified once and never stored in your browser.</p></div>"
    '<form method="post" action="{path}/signin">'
    '<label for="api_key">API key</label>'
    '<input type="password" id="api_key" name="api_key" placeholder="ct_live_…" autocomplete="off" '
    "autofocus required spellcheck=\"false\">"
    '<button type="submit">Sign in</button>'
    "</form>"
    "{error}"
    '<div class="trust">'
    f'<span>{ui_kit.icon("shield")}The key is checked once; the session cookie never contains it</span>'
    f'<span>{ui_kit.icon("lock")}Revoking the key ends every session it opened</span>'
    f'<span>{ui_kit.icon("audit")}Every change made here is authenticated and audited</span>'
    "</div>"
    '<p class="fine">Most of this console is read-only — capturing a trace, running the '
    "experiment, proposing to the Knowledge Base still goes through your agents or the CLI. "
    "An admin-scoped key can also manage users and API keys here directly.</p>"
    "</div>"
)


_KB_FLASH = {
    "proposed": "Proposal sent for operator review.",
    "auto_on": "Automatic contribution is on. New traces will also be proposed.",
    "auto_off": "Automatic contribution is off.",
    "voted": "Thanks — your vote was recorded.",
    "voted_uncounted": (
        "Your vote was recorded, but does not count toward this entry's standing yet "
        "— see the note below the catalogue."
    ),
}


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
    config: HubConfig | None = None,
    rate_limiter=None,
) -> None:
    """Mount the customer console. Registered only when a secret is set."""
    stripe = stripe or StripeSettings()
    _resolved: dict = {"config": config, "rate_limiter": rate_limiter}

    def _write_deps():
        if _resolved["config"] is None:
            _resolved["config"] = HubConfig.from_env()
        if _resolved["rate_limiter"] is None:
            _resolved["rate_limiter"] = make_rate_limiter(_resolved["config"])
        return _resolved["config"], _resolved["rate_limiter"]

    signin_limiter = make_named_limiter(config, 10, 5, "console_signin")

    share_view_limiter = make_named_limiter(config, 60, 20, "console_share_view")

    def _secret() -> str:
        return console_secret

    async def _claims(request: Request) -> dict | None:
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
                    select(ApiKey.id, ApiKey.scopes, Organization.name, Organization.plan)
                    .join(Organization, Organization.id == ApiKey.org_id)
                    .where(
                        ApiKey.org_id == str(claims["org"]),
                        ApiKey.key_prefix == prefix,
                        ApiKey.revoked_at.is_(None),
                        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
                    ).limit(1)
                )
            ).first()
        if row is None:
            return None
        claims["scopes"] = row[1]
        _VIEW.set({
            "org_name": row[2], "plan": row[3], "key_prefix": prefix,
            "is_admin": scopes.satisfies(row[1], scopes.SCOPE_ADMIN),
        })
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
            rate_limit_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return _page(
                "Sign in",
                _SIGNIN.format(
                    path=CONSOLE_PATH,
                    error='<p class="err" role="alert">Too many attempts. Try again shortly.</p>',
                ),
                signed_in=False,
            )
        form = await request.form()
        raw_key = str(form.get("api_key") or "")
        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        if authenticated is None:
            logger.info("console sign-in rejected")
            return _page(
                "Sign in",
                _SIGNIN.format(
                    path=CONSOLE_PATH,
                    error='<p class="err" role="alert">That key was not accepted.</p>',
                ),
                signed_in=False,
            )
        signin_limiter.refund(rate_limit_key(request, trusted_proxy_hops))
        response = RedirectResponse(CONSOLE_PATH, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            issue_session(_secret(), authenticated.org_id, authenticated.key_prefix),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or trusted_proxy_hops > 0,
            path=CONSOLE_PATH,
        )
        return response

    async def signout(request: Request) -> Response:
        if request.method != "POST":
            if await _claims(request) is None:
                return _redirect_to_signin()
            return _page("Sign out", (
                "<h1>Sign out?</h1>"
                f'<form method="post" action="{CONSOLE_PATH}/signout">'
                '<button type="submit" class="btn">Sign out</button></form>'
            ))
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
            activity = await crud.console_activity(session, org_id)
            setup = {
                "alerts": len(await alerts.list_rules(session, org_id)),
                "webhooks": len(await events.endpoints_for(session, org_id)),
            }
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
            "Your fleet", _render_overview(data, causal, billing_state, activity, setup),
            auto_refresh_seconds=45,
        )

    async def billing_checkout(request: Request) -> Response:
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
        share_url: str | None = None,
    ) -> Response:
        try:
            rate = float(request.query_params.get("per_occasion") or 0) or None
        except ValueError:
            rate = None
        async with session_scope(session_factory) as session:
            outcomes = await crud.fleet_outcomes(session, org_id)
            causal = await crud.causal_effects(session, org_id)
            worth = await crud.value_delivered(session, org_id, value_per_occasion=rate)
        share_box = _render_share_form(share_url, is_admin)
        flash = _SHARE_FLASH.get(request.query_params.get("done", ""), "")
        if flash:
            share_box = f'<p class="flash" role="status">{h(flash)}</p>' + share_box
        return _page("Proof", _render_proof(
            outcomes, causal, worth, is_admin=is_admin, experiment_error=experiment_error,
            controls=share_box,
        ))

    async def proof(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        return await _proof_view(request, str(claims["org"]), _is_admin(claims))

    async def proof_share(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            generation = int(org.share_generation) if org is not None else 0
        token = issue_share_token(_secret(), org_id, generation=generation)
        url = str(request.url.replace(path=f"{CONSOLE_PATH}/proof/shared/{token}", query=""))
        return await _proof_view(request, org_id, _is_admin(claims), share_url=url)

    async def proof_share_revoke(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not _is_admin(claims):
            return await _proof_view(request, org_id, False)
        actor = audit.actor_for_api_key(str(claims.get("key") or ""))
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id, with_for_update=True)
            if org is not None:
                org.share_generation = int(org.share_generation) + 1
                await audit.record(
                    session, actor=actor, action="revoke_share_links",
                    org_id=org_id, target_type="organization", target_id=org_id,
                    summary=f"share_generation={org.share_generation}",
                )
        return RedirectResponse(f"{CONSOLE_PATH}/proof?done=revoked", status_code=303)

    async def proof_shared(request: Request) -> Response:
        token = request.path_params.get("token", "")
        claims = read_share_token(_secret(), token)
        if claims is None:
            return HTMLResponse("Not found.", status_code=404)
        org_id = str(claims["org"])
        allowed, retry_after = await share_view_limiter.check(f"share:{org_id}")
        if not allowed:
            return HTMLResponse(
                "This report is being viewed heavily right now -- try again shortly.",
                status_code=429, headers={"Retry-After": str(int(retry_after) + 1)},
            )
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            if org is None or int(claims.get("gen", 0)) != int(org.share_generation):
                return HTMLResponse("Not found.", status_code=404)
            outcomes = await crud.fleet_outcomes(session, org_id)
            causal = await crud.causal_effects(session, org_id)
            worth = await crud.value_delivered(session, org_id)
        return _shared_page(_render_proof(outcomes, causal, worth, shared=True), expires_at=int(claims["exp"]))

    async def assignments_csv(request: Request) -> Response:
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
            offset = min(100_000, max(0, int(request.query_params.get("offset") or 0)))
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

    async def _kb_view(
        org_id: str, claims: dict, *, tag: str = "", offset: int = 0,
        error: str = "", flash: str = "",
    ) -> Response:
        async with session_scope(session_factory) as session:
            submissions = await crud.list_my_kb_submissions(session, org_id)
            ent = await crud.entitlements(session, org_id)
            organization = await session.get(Organization, org_id)
            auto_contribute = bool(
                organization is not None and organization.commons_auto_contribute
            )
            vote_counts = commons.vote_counts_toward_standing(
                trace_count=(organization.trace_count if organization else 0),
                org_created_at=(organization.created_at if organization else None),
            )
            try:
                browse = await crud.browse_commons(session, org_id, tag=tag, offset=offset)
            except plans.EntitlementExceeded:
                browse = None
        return _page(
            "Knowledge Base",
            _render_kb(
                submissions, ent, browse, tag=tag,
                can_submit=_is_admin(claims),
                can_vote=scopes.satisfies(claims.get("scopes"), scopes.SCOPE_WRITE),
                vote_counts=vote_counts,
                auto_contribute=auto_contribute,
                error=error, flash=flash,
            ),
        )

    async def knowledge_base(request: Request) -> Response:
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not commons_enabled:
            return _page("Knowledge Base",
                         "<h1>Knowledge Base</h1><p class=\"sub\">The Knowledge Base is "
                         "not enabled on this deployment.</p>")
        try:
            offset = min(100_000, max(0, int(request.query_params.get("offset", "0"))))
        except ValueError:
            offset = 0
        return await _kb_view(
            org_id, claims,
            tag=request.query_params.get("tag", "")[:64],
            offset=max(0, offset),
            flash=_KB_FLASH.get(request.query_params.get("done", ""), ""),
        )

    async def kb_submit(request: Request) -> Response:
        """Propose an entry from the browser."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not commons_enabled:
            return _redirect_to_signin()
        if not _is_admin(claims):
            return await _kb_view(
                org_id, claims,
                error="Proposing an entry needs an admin-scoped key.",
            )
        form = await request.form()
        title = str(form.get("title") or "").strip()
        context_text = str(form.get("context_text") or "").strip()
        solution_text = str(form.get("solution_text") or "").strip()
        if not (title and context_text and solution_text):
            return await _kb_view(
                org_id, claims,
                error="Title, the problem and what fixed it are all required.",
            )
        tags = [t.strip() for t in str(form.get("tags") or "").split(",") if t.strip()]
        submit_config, submit_limiter = _write_deps()
        try:
            async with session_scope(session_factory) as session:
                await crud.submit_kb_entry(
                    session, org_id, submit_config, submit_limiter,
                    title=title,
                    context_text=context_text,
                    solution_text=solution_text,
                    tags=tags,
                    rationale=str(form.get("rationale") or "").strip()[:300],
                    actor=audit.actor_for_api_key(str(claims.get("key") or "")),
                )
        except (TraceRejected, plans.EntitlementExceeded) as exc:
            return await _kb_view(org_id, claims, error=str(exc))
        except RateLimited:
            return await _kb_view(
                org_id, claims,
                error="Too many proposals just now. Try again shortly.",
            )
        return RedirectResponse(
            f"{CONSOLE_PATH}/kb?done=proposed", status_code=303,
        )

    async def kb_auto_contribute(request: Request) -> Response:
        """Turn this org's automatic contribution on or off."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not commons_enabled:
            return _redirect_to_signin()
        if not _is_admin(claims):
            return await _kb_view(
                org_id, claims,
                error="Changing automatic contribution needs an admin-scoped key.",
            )
        form = await request.form()
        enabled = str(form.get("enabled") or "").strip() == "1"
        async with session_scope(session_factory) as session:
            organization = await session.get(Organization, org_id)
            if organization is None:
                return _redirect_to_signin()
            organization.commons_auto_contribute = enabled
            await audit.record(
                session,
                actor=audit.actor_for_api_key(str(claims.get("key") or "")),
                action="set_commons_auto_contribute",
                org_id=org_id, target_type="org", target_id=org_id,
                summary=f"enabled={enabled}",
            )
        done = "auto_on" if enabled else "auto_off"
        return RedirectResponse(f"{CONSOLE_PATH}/kb?done={done}", status_code=303)

    async def kb_vote(request: Request) -> Response:
        """Cast this org's verdict on one Knowledge Base entry."""
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        if not commons_enabled:
            return _redirect_to_signin()
        if not scopes.satisfies(claims.get("scopes"), scopes.SCOPE_WRITE):
            return await _kb_view(
                org_id, claims, error="Voting needs a key with the 'write' scope.")
        form = await request.form()
        vote_type = str(form.get("vote") or "").strip()
        trace_id = str(form.get("trace_id") or "").strip()
        feedback_tag = str(form.get("feedback_tag") or "").strip()
        try:
            async with session_scope(session_factory) as session:
                voted = await crud.vote_trace(
                    session, org_id, trace_id, vote_type,
                    feedback_tag=feedback_tag,
                    actor=audit.actor_for_api_key(str(claims.get("key") or "")),
                )
        except ValueError as exc:
            return await _kb_view(org_id, claims, error=str(exc))
        if voted is None:
            return await _kb_view(
                org_id, claims,
                error="That entry is no longer in the Knowledge Base.",
            )
        done = "voted" if voted.get("vote_counted", True) else "voted_uncounted"
        return RedirectResponse(f"{CONSOLE_PATH}/kb?done={done}", status_code=303)

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
        if key is None or key.org_id != org_id or key.revoked_at is not None:
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
            if rule is None or rule.org_id != org_id:
                return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)
            await alerts.delete_rule(session, rule_id)
        return RedirectResponse(f"{CONSOLE_PATH}/alerts", status_code=303)

    async def alerts_generate_report(request: Request) -> Response:
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
        claims = await _claims(request)
        if claims is None:
            return _redirect_to_signin()
        org_id = str(claims["org"])
        try:
            offset = min(100_000, max(0, int(request.query_params.get("offset") or 0)))
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
    app.add_route(f"{CONSOLE_PATH}/signin", refuse_cross_origin(signin), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/signout", refuse_cross_origin(signout), methods=["GET", "POST"])
    app.add_route(CONSOLE_PATH, overview, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof", proof, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/share", refuse_cross_origin(proof_share), methods=["POST"])
    app.add_route(
        f"{CONSOLE_PATH}/proof/share/revoke", refuse_cross_origin(proof_share_revoke), methods=["POST"]
    )
    app.add_route(f"{CONSOLE_PATH}/proof/shared/{{token}}", proof_shared, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/assignments.csv", assignments_csv, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/proof/experiment/start", refuse_cross_origin(experiment_start), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/proof/experiment/stop", refuse_cross_origin(experiment_stop), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/billing/checkout", refuse_cross_origin(billing_checkout), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/billing/portal", refuse_cross_origin(billing_portal), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/memory", memory, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/kb", knowledge_base, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/kb/submit", refuse_cross_origin(kb_submit), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/kb/vote", refuse_cross_origin(kb_vote), methods=["POST"])
    app.add_route(
        f"{CONSOLE_PATH}/kb/auto-contribute", refuse_cross_origin(kb_auto_contribute), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users", users_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/users/create", refuse_cross_origin(users_create), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/role", refuse_cross_origin(users_set_role), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/disable", refuse_cross_origin(users_disable), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/users/{{user_id}}/enable", refuse_cross_origin(users_enable), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys", keys_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/keys/issue", refuse_cross_origin(keys_issue), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys/{{key_id}}/rotate", refuse_cross_origin(keys_rotate), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/keys/{{key_id}}/revoke", refuse_cross_origin(keys_revoke), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/alerts", alerts_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/alerts/create", refuse_cross_origin(alerts_create), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/alerts/{{rule_id}}/delete", refuse_cross_origin(alerts_delete), methods=["POST"])
    app.add_route(
        f"{CONSOLE_PATH}/alerts/generate-report", refuse_cross_origin(alerts_generate_report), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/webhooks", webhooks_page, methods=["GET"])
    app.add_route(f"{CONSOLE_PATH}/webhooks/create", refuse_cross_origin(webhooks_create), methods=["POST"])
    app.add_route(
        f"{CONSOLE_PATH}/webhooks/{{endpoint_id}}/rotate", refuse_cross_origin(webhooks_rotate), methods=["POST"])
    app.add_route(
        f"{CONSOLE_PATH}/webhooks/{{endpoint_id}}/disable", refuse_cross_origin(webhooks_disable), methods=["POST"])
    app.add_route(f"{CONSOLE_PATH}/audit", audit_page, methods=["GET"])


__all__ = ["CONSOLE_PATH", "add_console_routes", "issue_session", "read_session"]
