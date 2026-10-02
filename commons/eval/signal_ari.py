"""Do failure signals recover the failure modes a person would name?

    python -m commons.eval.signal_ari                 # 20 seeds (0-19, held out), default clustering
    python -m commons.eval.signal_ari --seeds 50 --json

A labelled, seeded dataset of failing traces: eight failure modes (support and engineering), each
written as several differently-worded descriptions with varying details, plus boilerplate sentences
that appear across modes (so word overlap alone is a weak signal) and one-off failures that belong to no
mode. The labels are fixed here before any clustering runs. The score is the adjusted Rand index
between the labels and what `failure_signals.build_signals` returns, where a trace in no signal counts
as its own cluster (so missing a mode and merging two modes both cost).

The target is ARI >= 0.8. The default similarity was chosen on seeds 100-104 and this reports seeds 0-19,
which were not used to choose it. The wording is synthetic: it checks
that the clustering is not brittle to paraphrase and boilerplate, not that it generalises to your
traces. `commontrace signals list` on your own store is the real check.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
from collections import Counter

from commontrace import failure_signals, frontmatter, paths, templates

TARGET = 0.8
TRACES_PER_MODE = 12
NOISE_TRACES = 14

MODES: dict[str, tuple[list[str], list[str]]] = {
    "refund-timeline": (
        ["customer asked why the refund for order {n} has not arrived after {d} days; policy says 5 to 10 days",
         "refund still missing {d} days after cancelling order {n}, the help centre and the invoice differ",
         "buyer angry the refund on {n} is late; refund window quoted by chat was {d} days but docs say longer",
         "contradictory refund timeline between the policy article and the confirmation email for order {n}",
         "asked when money comes back for order {n}, told {d} days then told 14, customer lost trust in the answer",
         "refund timeline unclear in the docs: customer waiting {d} days for order {n} and gets conflicting answers"],
        ["quote the single refund timeline from the policy page and link it",
         "check the processor status for the refund before promising a date",
         "state the policy window, then offer to escalate if it is exceeded"]),
    "duplicate-charge": (
        ["customer charged twice for order {n} after the payment page timed out and they retried",
         "duplicate charge on invoice {n}: the first request timed out and a retry created a second payment",
         "two identical payments for {n} appeared within seconds, the client retried without an idempotency key",
         "double billing reported for {n}; payment call retried after a gateway timeout and succeeded both times",
         "card was debited twice on {n} because the retry after the timeout was not deduplicated",
         "user sees two charges for {n} caused by retrying a timed out payment request"],
        ["send an idempotency key on every payment request and reuse it on retry",
         "look up the first charge before retrying and refund the duplicate",
         "deduplicate by order id on the server and return the original result"]),
    "reset-email": (
        ["customer says the password reset email never arrives at {m}; checked spam and waited {d} minutes",
         "reset link email not delivered to {m}, user waiting {d} minutes and locked out of the account",
         "no password reset message received at {m}; mail provider bounced silently",
         "user locked out because the reset email to {m} did not show up after {d} minutes",
         "forgot password flow sends nothing to {m}; support asked to resend the reset email manually",
         "password reset mail missing for {m} again, domain has strict spam filtering"],
        ["check the email provider's suppression list and the bounce log for the address",
         "resend through the secondary provider and ask the user to allow the sending domain",
         "verify the sender domain records before blaming the user's inbox"]),
    "webhook-signature": (
        ["webhook from the payment provider rejected: signature mismatch on event {n} after the body was parsed",
         "signature verification failing for webhook {n} because the framework re-serialised the JSON body",
         "webhook endpoint returns 400 invalid signature for {n}, raw body not preserved by the middleware",
         "events like {n} fail signature check; the handler verifies the parsed object instead of the raw bytes",
         "provider keeps retrying webhook {n}, our signature check compares against a modified payload",
         "invalid webhook signature on {n} since the proxy started stripping and re-encoding the body"],
        ["verify the signature over the raw request bytes before any parsing",
         "disable body re-serialisation on the webhook route and compare in constant time",
         "log the raw body hash at the edge to see what the proxy changed"]),
    "export-timeout": (
        ["export of {d} thousand rows times out in the browser and the job is killed at 30 seconds",
         "large csv export for report {n} fails with a gateway timeout, works for small date ranges",
         "report export hangs then 504 once the result passes {d} thousand rows",
         "the nightly export of {n} dies mid-way when the table is big, no partial file",
         "customer cannot download the full export of {d} thousand rows, request times out every time",
         "export endpoint blocks a worker for minutes on large results and the proxy cuts the connection"],
        ["run exports as a background job and email a download link when it finishes",
         "stream the file in chunks instead of building it in memory",
         "paginate the query and write rows as they arrive"]),
    "timezone-report": (
        ["scheduled report for {c} arrives at the wrong hour, it is using UTC instead of the account timezone",
         "daily digest for {c} sent {d} hours off after the clocks changed",
         "report schedule shows 9am but delivers at {d}pm local time for {c}",
         "customer in {c} says the summary email timestamps are in the wrong timezone",
         "weekly report for {c} covers the wrong day because the cutoff ignores the user's timezone",
         "daylight saving shift moved the scheduled report for {c} by an hour"],
        ["store the schedule with an explicit IANA timezone and compute the next run from it",
         "convert to the account timezone before truncating to the day",
         "test the schedule across a daylight saving boundary"]),
    "rate-limit": (
        ["after the deploy the api returns 429 too many requests for {c} integrations calling {n} endpoints",
         "clients hit rate limit 429 right after release {n}, the limiter key changed to the ip behind the proxy",
         "429 responses spiking since the new gateway: every customer shares one bucket",
         "integration for {c} throttled with 429 after we moved behind the load balancer",
         "rate limiter treats all traffic as one client address after release {n}, legitimate users blocked",
         "sudden wave of 429 errors from {c}, forwarded client address header is ignored"],
        ["key the limiter on the forwarded client address with a trusted proxy hop count",
         "key the limiter per account and keep ip only as a fallback",
         "roll back the limiter change and add a canary check for 429 rate"]),
    "sso-loop": (
        ["user from {c} is stuck in a login redirect loop with single sign on, bouncing between app and provider",
         "sso login loops forever for {c}, the session cookie is dropped on the callback",
         "identity provider redirects back to the app, the app redirects again, too many redirects for {c}",
         "login callback for {c} loses the state cookie so authentication restarts endlessly",
         "single sign on redirect loop for {c} after we set the cookie to strict same site",
         "users of {c} cannot sign in: callback to the app sends them to the provider again"],
        ["set the session cookie to lax same site so it survives the provider redirect",
         "verify the state parameter against a cookie scoped to the callback path",
         "trace the redirect chain and look for the first response that does not set the session"]),
}

BOILERPLATE = [
    "the customer was frustrated and asked for a manager",
    "the agent checked the knowledge base and found nothing relevant",
    "this was escalated to the second line after the first reply failed",
    "no workaround was offered at the time and the ticket stayed open",
    "the customer has been with us for over a year",
    "the first answer did not resolve it and the user wrote back",
]

NOISE = [
    "the printer driver for the warehouse label printer crashed after the firmware update on unit {n}",
    "a translation string for the settings page was missing in the portuguese build {n}",
    "the mobile app showed a blank screen on android {d} for a user with a very old device",
    "sales asked for a custom quote format that the template engine cannot produce for deal {n}",
    "calendar invite attachments were stripped by the corporate mail gateway at {c}",
    "the dark mode toggle did not persist after logout for one browser extension user",
    "a pdf invoice rendered with overlapping columns when the company name was very long {n}",
    "the training video link in the onboarding email pointed to a retired host",
    "an integration partner changed their field names without notice and broke the sync for {c}",
    "the status page showed degraded for a region we do not operate in",
    "the vpn client conflicted with the screen reader on one machine in {c}",
    "a spreadsheet import rejected a file because of a byte order mark at row {n}",
    "audio on the voice agent clipped the first second of every call for one carrier",
    "the keyboard shortcut for search was swallowed by a browser extension for {c}",
]

SLOTS = {
    "n": lambda r: str(r.randint(1000, 99999)),
    "d": lambda r: str(r.randint(2, 21)),
    "m": lambda r: (f"{r.choice(['sam', 'lee', 'ana', 'kai', 'noor'])}{r.randint(1, 99)}"
                    f"@{r.choice(['mail.test', 'corp.test', 'post.test'])}"),
    "c": lambda r: r.choice(["acme", "globex", "initech", "umbrella", "hooli", "stark", "wayne"]),
}


def _fill(template: str, rng: random.Random) -> str:
    out = template
    for key, make in SLOTS.items():
        while "{" + key + "}" in out:
            out = out.replace("{" + key + "}", make(rng), 1)
    return out


def generate(seed: int) -> list[dict]:
    rng = random.Random(f"signal-ari:{seed}")
    rows = []
    for mode, (contexts, solutions) in MODES.items():
        for i in range(TRACES_PER_MODE):
            context = _fill(rng.choice(contexts), rng)
            if rng.random() < 0.7:
                context += ". " + rng.choice(BOILERPLATE)
            rows.append({"id": f"{mode}-{i:02d}", "label": mode, "title": f"Failure: {mode.replace('-', ' ')}",
                         "context": context, "solution": _fill(rng.choice(solutions), rng)})
    for i, template in enumerate(rng.sample(NOISE, min(NOISE_TRACES, len(NOISE)))):
        context = _fill(template, rng)
        if rng.random() < 0.7:
            context += ". " + rng.choice(BOILERPLATE)
        rows.append({"id": f"noise-{i:02d}", "label": f"noise-{i}", "title": "Failure: one-off",
                     "context": context, "solution": "handled by hand"})
    rng.shuffle(rows)
    return rows


def adjusted_rand_index(true: list, pred: list) -> float:
    """ARI by the contingency-table formula (Hubert and Arabie 1985); no dependency."""
    n = len(true)
    if n < 2:
        return 1.0

    def comb2(x: int) -> float:
        return x * (x - 1) / 2

    table = Counter(zip(true, pred))
    sum_cells = sum(comb2(v) for v in table.values())
    sum_true = sum(comb2(v) for v in Counter(true).values())
    sum_pred = sum(comb2(v) for v in Counter(pred).values())
    expected = sum_true * sum_pred / comb2(n)
    maximum = (sum_true + sum_pred) / 2
    if maximum == expected:
        return 1.0
    return (sum_cells - expected) / (maximum - expected)


def _write(root: str, rows: list[dict]) -> None:
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    for row in rows:
        fm = templates.trace_frontmatter(row["id"], row["title"], "support", [], "", {"resolved": False})
        frontmatter.write(os.path.join(tdir, f"{row['id']}.md"), fm,
                          templates.trace_body(row["context"], row["solution"]))


def score_seed(seed: int, **cluster_kwargs) -> dict:
    rows = generate(seed)
    with tempfile.TemporaryDirectory(prefix="commontrace-ari-") as root:
        _write(root, rows)
        signals, _ = failure_signals.build_signals(root, **cluster_kwargs)
    assigned = {tid: i for i, s in enumerate(signals) for tid in s.trace_ids}
    true = [r["label"] for r in rows]
    pred = [assigned.get(r["id"], f"single-{r['id']}") for r in rows]
    return {"seed": seed, "ari": adjusted_rand_index(true, pred), "signals": len(signals),
            "modes": len(MODES), "traces": len(rows)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--similarity-threshold", type=float, default=failure_signals.DEFAULT_SIMILARITY)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    runs = [score_seed(s, similarity_threshold=args.similarity_threshold) for s in range(args.seeds)]
    aris = [r["ari"] for r in runs]
    report = {"seeds": args.seeds, "target": TARGET, "mean_ari": statistics.fmean(aris), "min_ari": min(aris),
              "seeds_meeting_target": sum(a >= TARGET for a in aris) / len(aris),
              "passed": statistics.fmean(aris) >= TARGET and min(aris) >= TARGET - 0.1}
    if args.json:
        print(json.dumps({**report, "runs": runs}, indent=2))
    else:
        print(f"{args.seeds} seeds, {runs[0]['modes']} modes, {runs[0]['traces']} traces each "
              f"(similarity threshold {args.similarity_threshold})")
        print(f"ARI mean {report['mean_ari']:.3f}  min {report['min_ari']:.3f}  "
              f"seeds >= {TARGET}: {report['seeds_meeting_target']:.0%}  "
              f"-> {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
