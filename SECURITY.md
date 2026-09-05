# Security Policy

CommonTrace's Hub tier is a multi-tenant service that stores customer trace
data and holds an Argon2-hashed API key per organization; the local CLI tier
touches nothing beyond the operator's own machine (see
[`DATA_RETENTION.md`](DATA_RETENTION.md) for exactly what each tier stores).
That combination — other people's data, on infrastructure this project's own
code is responsible for isolating — is why a vulnerability here deserves a
private report rather than a public issue, and why this file exists before
anyone finds a reason to need it.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security vulnerability.**
A public issue is a disclosure to every reader before a fix exists, including
anyone who wants to use it against a real deployment.

Use **[GitHub Security Advisories](security/advisories/new)** for this
repository ("Report a vulnerability" under the Security tab) to report
privately. It reaches maintainers directly, supports attachments and a
private discussion thread, and — unlike a shared inbox — doesn't depend on
one person's mail client.

**What to include**, so a report is actionable on first read: the affected
component (`hub/` server, `commontrace` CLI/MCP client, or the protocol
schemas), the version or commit, reproduction steps or a proof of concept,
and the impact you believe it has (e.g., cross-tenant data access, auth
bypass, injection).

**What this project does not yet have**, stated plainly rather than implied:
a dedicated security contact distinct from the maintainers reachable through
the advisory above, a published response-time SLA, and a bug bounty program.
Those are commitments a project earns the standing to make, not defaults —
this file will be updated if and when they exist, rather than claiming them
now.

## Supported versions

There is one actively maintained line — the `main` branch and the releases
cut from it. This is a young project with no long-term-support branch yet;
if that changes, this section will say so and name which versions still get
fixes.

## What is, and is not, in scope

**In scope**: anything that would let one organization's API key read,
modify, or infer another organization's data on a Hub deployment; an
authentication or authorization bypass anywhere in `hub/`; a way to make the
operator console (`/admin`) or customer console (`/app`) execute
attacker-controlled content in another user's browser; a way to defeat the
rate limiting or quota enforcement in `hub/abuse.py`/`hub/plans.py` at scale;
and any of the standard classes (injection, deserialization, SSRF, path
traversal) reachable through a documented CLI flag, MCP tool argument, or
Hub API call.

**Out of scope**: findings that require an already-privileged position this
project's own trust model assumes (e.g., an operator with direct database
access, or a customer's own API key holder acting on their own org's data,
which they are supposed to be able to do); a vulnerability in a third-party
dependency with no CommonTrace-specific exploitation path (report it
upstream, though we'd still like to know so the pin in
`requirements.txt`/`hub/requirements.txt`/`pyproject.toml` can move); and
denial-of-service reports that only demonstrate resource exhaustion at a
volume ordinary abuse controls (rate limits, size caps) are already
documented to allow through by design.

If you are not sure whether something qualifies, report it anyway through
the advisory link above — a report that turns out to be out of scope costs
a maintainer a few minutes; a real vulnerability reported nowhere costs a
customer their data.
