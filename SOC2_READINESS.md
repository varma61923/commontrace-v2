# SOC 2 readiness mapping

**This is not a SOC 2 attestation, and must never be presented as one.**
Audit §7.1 named "No SOC 2 Type II" — that stays correctly out of scope:
a Type II report needs an accredited audit firm and a staffed observation
window over real production operations, neither of which a repository
file can substitute for. What this document *is*: an honest map from the
AICPA Trust Services Criteria to the specific, already-implemented
controls in this codebase, evidence-referenced by file and test, so that
a future engagement with a real audit firm starts from an accurate
inventory instead of a blank page. Every row cites code an auditor could
actually go read; nothing here is a claim about audited effectiveness
over time, which is exactly what a Type II report tests and this
document cannot.

## Security (the criterion every SOC 2 report includes)

| Criterion | Control | Evidence |
|---|---|---|
| Logical access — least privilege | Scoped API keys (`read`/`write`/`admin`/`scim`, no implication between them) | `hub/scopes.py`, `hub/tests/test_api_key_scopes.py` (22 tests) |
| Logical access — role-based authorization | Human users with named roles, checked as a second gate on every tool call | `hub/rbac.py`, `hub/tests/test_rbac.py` (16 tests) |
| Logical access — deprovisioning | `disable-user` blocks access on the very next call, not at token expiry | `hub/auth.py:verify_user_token`, documented in `hub/README.md` "Human users, roles, and OIDC SSO" |
| Logical access — automated provisioning/deprovisioning | SCIM 2.0 Users (create/deactivate) | `hub/scim.py`, `hub/tests/test_scim.py` (50 tests) |
| Multi-tenant data isolation | Postgres row-level security, enforced at the database layer, fails closed at startup if bypassable | `hub/alembic/versions/d5c8b3a91e77_row_level_security.py`, `hub/db.py:check_row_level_security`, `hub/tests/test_row_level_security.py` (16 tests) |
| Credential storage | API keys never stored raw — HMAC fast path + argon2 fallback, both irreversible | `hub/auth.py` module docstring |
| Audit logging | Every consequential action recorded (actor, action, target, org, timestamp), survives an org purge on purpose | `hub/audit.py`, `hub/models.py:AuditLogEntry` |
| Change management | Migrations reviewed and verified (upgrade/`alembic check`/downgrade round-trip) before merge, on every schema change this session made | `hub/alembic/versions/`, this repository's own commit history |
| Vulnerability management | Static analysis (`bandit`) and dependency audit (`pip-audit`) run in CI on every push | `.github/workflows/ci.yml` "security scan" job |
| Vulnerability disclosure | A private reporting channel exists | `SECURITY.md` |
| Network access control | Application-level IP allowlisting, CIDR-based, fails closed | `hub/server.py:IpAllowlistMiddleware`, `hub/tests/test_ip_allowlist.py` (9 tests) |
| Rate limiting / abuse controls | Per-org token-bucket limiting on writes and auth attempts | `hub/abuse.py` |
| Incident detection | Automatic webhook alert the moment anyone holds the most privileged role | `hub/events.py` (`user.privileged_role_granted`), `hub/tests/test_manage.py::TestPrivilegedRoleGrantAlert` |

## Availability

| Criterion | Control | Evidence |
|---|---|---|
| Health/readiness monitoring | `/healthz` (liveness), `/readyz` (checks the database), `/metrics` (Prometheus) | `hub/observability.py` |
| Backup and restore, rehearsed | Documented procedure, actually run end to end with measured timings | `hub/DEPLOYMENT.md` §9/§9b |
| **Not yet evidenced**: production SLO, staffed on-call/escalation | — | Needs a real production deployment and a staffed rota; audit §7.4 |

## Processing integrity

| Criterion | Control | Evidence |
|---|---|---|
| Idempotent writes | Retried writes with the same idempotency key return the original row, never a duplicate | `hub/crud.py:contribute_trace`, `hub/tests/` idempotency coverage |
| Immutable audit trail with supersession, not silent mutation | An amended trace supersedes rather than overwrites | `hub/models.py:Trace` bi-temporal supersession |
| Statistical validity of billed measurements | Joint confidence intervals, sequential-testing correction, pre-registration, evidence-decay handling — five real correctness defects found and fixed by this project's own internal audit | `commontrace/experiment.py`, `AUDIT_RESPONSE.md` §3.1–3.6 |

## Confidentiality

| Criterion | Control | Evidence |
|---|---|---|
| Encryption in transit | TLS assumed terminated in front of the Hub (operator's reverse proxy/load balancer) | `hub/DEPLOYMENT.md` §10 security checklist |
| Encryption at rest — Trace content | Deliberately NOT encrypted at the application layer: `title`/`context_text`/`solution_text` back a Postgres `GENERATED` full-text-search column and `subject_ids` backs an exact-match GIN index, both of which application-layer encryption would silently break rather than protect. Satisfied at the storage layer instead (managed-provider disk encryption, LUKS, or a Postgres TDE extension) — an operator responsibility, documented rather than left unstated | `hub/encryption.py` module docstring, `hub/DEPLOYMENT.md` §4 "Encryption at rest" |
| Encryption at rest — `WebhookEndpoint.url` | AES-256-GCM, opt-in via `HUB_ENCRYPTION_KEY`; unset means unaffected (plaintext, as before this existed). Covers the one column that carries no search/index conflict and sometimes embeds a bearer token or shared secret in its path or query string | `hub/encryption.py`, `hub/tests/test_encryption.py` |
| Secrets never logged | API keys, webhook signing material never appear in logs or audit rows in recoverable form | `hub/auth.py`, `hub/audit.py` module docstrings |
| Tenant data never crosses org boundaries, even in the shared Knowledge Base | Consultation via signature only, never raw text; explicit consent required to publish | `hub/commons.py`, `DATA_RETENTION.md` |

## Privacy

| Criterion | Control | Evidence |
|---|---|---|
| Right to erasure — whole account | Two-call confirmed deletion (request → wait out a grace window → confirm), cascades to every org-scoped row | `hub/crud.py` account deletion, `hub/tests/test_manage.py::test_purge_org_cascades_to_its_traces` |
| Right to erasure — per-subject, within an account | Structured subject tagging + exact-match find/purge, plus free-text search for untagged content | `hub/crud.py:tag_trace_subjects`/`purge_traces_by_subject`, `hub/tests/test_subject_tagging.py` (20 tests) |
| Data residency | Determined entirely by where an operator points `HUB_DATABASE_URL` — this codebase asserts no region of its own | `AUDIT_RESPONSE.md` §2.3 |
| Subprocessor disclosure | The one subprocessor this code actually implies is named | `SUBPROCESSORS.md` |

## What a real Type II engagement still needs, that this document cannot provide

- **An accredited audit firm** to test these controls' *operating
  effectiveness over an observation window* (commonly 6–12 months) — a
  Type II report is evidence controls actually ran correctly over time,
  which only production operation under audit can produce.
- **A production deployment to observe.** Every control above is
  evidenced against this codebase's own test suite, not a live customer
  environment — audit §7.4's standing note that `hub/` is implemented but
  not deployed anywhere still applies.
- **A staffed operations function** (on-call, incident response,
  change-approval records) a Type II report also examines, which needs
  the same missing staffed rota named in §7.4/§7.6.

This document should be handed to whichever audit firm a future
engagement selects as a starting inventory, not represented to a customer
as compliance in itself.
