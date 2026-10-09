# Security Policy

CommonTrace's Hub tier is a multi-tenant service that stores customer trace
data and holds an Argon2-hashed API key per organization; the local CLI tier
touches nothing beyond the operator's own machine.
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

## Triage and AI-assisted reports

Maintainers assess impact using CVSS alongside reachability, tenant isolation,
credential exposure and exploit evidence. Critical authentication or cross-tenant
failures take priority over lower-impact findings. Acknowledgement and patch dates
are coordinated privately; this policy does not introduce a response-time SLA.

AI-assisted reports are welcome when they include reproducible evidence. Disclose
which tools generated a finding, remove real credentials and customer data, and
validate the exploit independently before reporting. Generated claims alone do
not establish a vulnerability or justify changing the security boundary.

Memory authority binds the authenticated writer to immutable records. Summaries
inherit the least authority of their sources; retrieved text cannot approve a
tool action. Principal scopes, live-source admission and directive checks belong
at the action boundary. Forgetting receipts attest local non-delivery and lineage
revocation; they do not assert historical Git-byte or remote-replica erasure.

## Threat model

This section states what each tier defends against, so a report can be
checked against an intended boundary rather than a guess.

| Asset | Threat | Primary control | Where |
| --- | --- | --- | --- |
| One org's traces, lessons and outcomes on a Hub | Another org reads, writes or infers them | Org-scoped transactions, PostgreSQL row-level security, per-key scopes | `hub/crud.py`, `hub/scopes.py`, `hub/rbac.py` |
| Hub credentials | Guessing, replay or theft of an API key | HMAC fast-path plus Argon2 hashing, expiry and rotation, auth rate limits | `hub/auth.py`, `hub/abuse.py` |
| Organization history | One compromised key erases it | Two-step deletion with a mandatory delay; legal hold; retention policy | `hub/manage.py`, `hub/retention.py` |
| Injected context | A stored memory carries a prompt-injection payload or a secret to a later agent | Content screens at capture, approval and injection; credential redaction | `commontrace/memory_guard.py`, `commontrace/injection_guard.py`, `commontrace/defense.py` |
| Memory authority | Untrusted text is laundered (summarized, echoed, self-corroborated) into a trusted memory | Write-time origin receipts; derived authority never exceeds its least-trusted source; per-action authority policy | `commontrace/origin.py`, `commontrace/memory_authority.py` |
| Causal results and billing | A verdict or invoice is forged or quietly changed | Deterministic assignment (PROTOCOL 13.1), hash-chained value ledger, signed proof packages anyone can re-derive | `commontrace/proof.py`, `commontrace/value.py` |
| Lessons in production | A harmful or unreviewed lesson reaches agents | Review gate, optional two-person and human-only approval, harm withdrawal, release `gate` in CI | `commontrace/approval.py`, `commontrace/harm.py`, `commontrace/gate.py` |
| Local gateway | Another local process or a browser page calls it | Private file token, loopback default, Host/Origin checks, request admission limits, TLS required for remote listeners | `commontrace/gateway.py`, `commontrace/gateway_transport.py` |
| Outbound fetches | SSRF through ingestion or connectors | Scheme/host checks, no credential forwarding across origins | `commontrace/ingest/`, `commontrace/connectors/` |

**Assumed trusted** (outside this model): the operator of a deployment and
anyone with direct database, filesystem or key-management access; the Python
interpreter and installed dependencies; a model provider's handling of
prompts the operator chose to send it. The local tier has no identity system:
an actor string there is attribution, not authentication.

**Known limits**, stated so they are not mistaken for guarantees: origin
receipts do not stop a compromised operator or adaptive multi-writer
collusion; forgetting receipts cover local retrieval surfaces, not historical
Git bytes or remote replicas; HMAC commons signatures let any verifier also
sign (use the Ed25519 option for publicly verifiable signatures); content
screens are pattern-based and can miss novel payloads.

**Unauthenticated endpoints** are limited to health/readiness probes, the
OpenAPI document and Swagger UI, opt-in self-enrollment, and signed webhooks.
Probes report named check states only, never paths, exception text or
configuration values.

## What is, and is not, in scope

**In scope**: anything that would let one organization's API key read,
modify, or infer another organization's data on a Hub deployment; an
authentication or authorization bypass anywhere in `hub/`; a way to make the
operator console (`/admin`) or customer console (`/app`) execute
attacker-controlled content in another user's browser; a way to defeat the
rate limiting or quota enforcement in `hub/abuse.py`/`hub/plans.py` at scale;
a way to get a secret, PII, or a prompt-injection payload past the
content-safety screening in `commontrace/memory_guard.py` (contribute_trace/
amend_trace's quarantine gate, or the `commontrace lesson approve`/
`approve_lesson` activation gate) without tripping it; and any of the
standard classes (injection, deserialization, SSRF, path traversal)
reachable through a documented CLI flag, MCP tool argument, or Hub API call.

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
