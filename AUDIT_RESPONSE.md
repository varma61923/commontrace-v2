# Audit Response Register

This document answers a third-party readiness audit of CommonTrace, finding
by finding. It exists because the useful half of an audit response is the
half that says **"not done"**, and that half is the half that quietly goes
missing when a vendor answers an audit in prose.

So: every finding gets one of four statuses, and the rules are fixed.

| Status | What it means |
|---|---|
| **Done** | Code exists, is reachable from a real entry point, and is covered by tests named in the row. Verify it by reading the file and running the tests. |
| **Partial** | Some of the exit criterion is met. The row says exactly which part is not, in the same sentence. |
| **Not applicable** | The finding does not describe this system. The row says why, and does not use "not applicable" to mean "we would rather not". |
| **Requires business action** | Not an engineering task. No amount of code closes it — it needs a legal entity, a signed contract, an auditor, a paid third party, or a staffed rota. **These cannot be closed by this repository and are not claimed as closed anywhere in it.** |

Two rules this register holds itself to:

1. **No status is claimed without something a reader can check.** Every
   "Done" row names a module and a test file. If a row cannot name one, it
   is not Done.
2. **"Requires business action" is never downgraded to "Partial" by
   building something adjacent.** Shipping an audit log is not progress
   toward SOC 2; shipping retention controls is not progress toward a DPA.
   Conflating the two is the specific dishonesty this register exists to
   prevent.

Test counts below are from `pytest tests/ hub/tests` (3,023 collected as of
this writing). Per-area counts name the file so they can be re-run
individually.

---

## 1. Security and tenancy

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 1.1 | The evaluation deployment displays an RLS story it does not enforce | **Done** | `hub/alembic/versions/d5c8b3a91e77_row_level_security.py` adds forced RLS; `hub/postgres-init/10-runtime-role.sql` creates a NOSUPERUSER/NOBYPASSRLS runtime role; `hub/db.py:check_row_level_security` fails startup closed when RLS is expected but bypassable (`HUB_REQUIRE_RLS`). Tests: `hub/tests/test_row_level_security.py` (16), `hub/tests/test_deployment_roles.py` (7). The RLS test *skips rather than passes* when it cannot subject itself to the policy — a test that cannot be bound by the policy has not verified it. |
| 1.2 | A full-org API key is the only Hub identity | **Partial** | Least-privilege **workload** tokens exist: `hub/scopes.py` defines `read`/`write`/`admin`, which do not imply each other; all 26 MCP tools are scope-gated in `hub/server.py`. Tests: `hub/tests/test_api_key_scopes.py` (22). **Human identity is now also implemented**: a `User` row (`hub/models.py`) is a person, distinct from the org's API key, with a named role (Viewer/Analyst/Curator/Validator/Deployer/Security Admin/Billing Admin/Owner) checked as a second, additive gate on every tool call (`hub/rbac.py`), and OIDC bearer-JWT verification (`hub/sso.py`) authenticates that person — asymmetric algorithms only, strict JWKS `kid` resolution, no auto-provisioning (an operator must run `hub.manage link-sso`), and deprovisioning (`disable-user`) blocks access on the very next call rather than waiting for token expiry. CLI: `hub.manage create-user \| list-users \| set-user-role \| disable-user \| enable-user \| link-sso \| unlink-sso`. Tests: `hub/tests/test_rbac.py` (16), `hub/tests/test_sso.py` (29), `hub/tests/test_user_identity.py` (21). A break-glass procedure for recovering access when every `ROLE_SECURITY_ADMIN`/`ROLE_OWNER` account is disabled or its IdP is unreachable is now documented (`hub/DEPLOYMENT.md` §9a: `hub.manage` run directly against the database, the same audited path as any other operator action). **Not done:** SAML, SCIM auto-provisioning, any browser-based login UI (`link-sso` is the only way a person gets an account, and there is no self-service sign-up), and dedicated break-glass *tooling* — a time-boxed emergency credential, an automatic alert on use, or a second-person witness requirement, beyond what the documented procedure and ordinary audit logging already give you. |
| 1.3 | Memory admission is not protected against ASI06 | **Done** | `commontrace/memory_guard.py` scans every capture/import/amend/distill/approval boundary for secrets, PII, injection markers and hidden Unicode (bidi/zero-width control characters). Tests: `tests/test_memory_guard.py` (30), `tests/test_lesson_content_safety.py` (5). |
| 1.4 | Approval policy is contradictory (an author can approve their own change) | **Done** | `commontrace/approval.py` reads `memory/approval-policy.yaml` and enforces `single`/`two-person` modes against the revision journal's recorded authors, raising `ApprovalDenied`. An author cannot satisfy a required human approval on their own change. Tests: `tests/test_approval_policy.py` (17). |
| 1.5 | No scoped workload tokens | **Done** | Same as 1.2's first half. `issue-key <org> [days] [scopes]`. |
| 1.6 | No IP allowlisting / private networking | **Requires business action** | A deployment-topology decision (VPC, peering, IP allowlists) made by whoever operates the Hub, not by this codebase. `hub/DEPLOYMENT.md` is where a deployment's own posture belongs. |

## 2. Data governance and privacy

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 2.1 | Indefinite retention; no configurable retention or legal hold | **Done** | `hub/retention.py`: per-org policies by **object type and status**; a plan that deletes nothing and an apply that requires the plan's digest; legal holds that outrank every policy and are counted rather than silently skipped; per-type floors refused rather than clamped. CLI: `set-retention`, `clear-retention`, `retention-plan`, `retention-apply`, `legal-hold`, `release-hold`, `holds`. Tests: `hub/tests/test_retention.py` (34) plus CLI coverage in `hub/tests/test_manage.py`. Documented in [`DATA_RETENTION.md`](DATA_RETENTION.md) §2. |
| 2.2 | Subject deletion and export | **Partial** | Whole-account deletion exists (self-service and `purge-org`), and `export-assignments` exports an org's experiment data. `search_trace_content` (MCP tool) / `hub.manage search-content` now *locates* traces for a subject-erasure request — a literal or POSIX-regex scan of title/context/solution text, including quarantined traces, with no relevance ranking. This closes "there is no field this system could search on to do it for them": there was no structured field to search on, so this searches the free text directly instead. **Not done, and cannot honestly be claimed done:** automated, provably-complete per-*person* deletion. A match proves text is present; a non-match is not proof of absence — free text can misspell, abbreviate, or split an identifier this cannot reassemble. `search-content` finds candidates for a human to review and then `delete_trace`; it is not, and does not claim to be, an automated "subject has no data here" certification. That claim would need a structured subject-id column this schema does not have. Tests: `hub/tests/test_search_content.py` (15) plus CLI coverage in `hub/tests/test_manage.py` (6). |
| 2.3 | Unknown data residency | **Requires business action** | Residency is a property of where an operator runs Postgres. This repository cannot assert a region. What it *can* say, and does in `DATA_RETENTION.md`, is exactly which bytes exist and where the code puts them. A residency commitment needs a hosting decision and a contract. |
| 2.4 | No DPA or subprocessor list | **Requires business action** | Legal documents. There is one processor the code actually implies — Stripe, for self-serve billing (`hub/billing.py`), which holds card data entirely; the Hub stores only customer and subscription ids. That fact is documented, but a DPA is not a file in a repository. |

## 3. Measurement integrity (the product's core claim)

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 3.1 | Aggregate value statistics were unsound (double-counted occasions, no joint CI, post-selection bias) | **Done** | `commontrace/value.py`: unique-occasion accounting with an explicit overlap record, a joint confidence interval by quadrature, and a post-selection (winner's-curse) correction. The aggregate is **withheld** rather than reported when the inputs cannot support it, with the reason. Tests: `tests/test_value_aggregation.py` (28). |
| 3.2 | Repeated looks at a running experiment inflate false positives | **Done** | `commontrace/experiment.py` adds alpha-spending (O'Brien-Fleming, Pocock) and an anytime-valid confidence sequence. Tests: `tests/test_sequential_testing.py` (19) pin the mechanics; unit tests cannot answer *how often does this call a useless memory a winner*, so `commons/eval/sequential_error_rates.py` measures it by simulation and is the source of the numbers here — **re-run it to check them**. At 500 occasions/arm, a 50% baseline, looking every 25 occasions and stopping on the first significant look: false-positive rate under the null **14.7% → 0.7%**. The cost, stated rather than omitted: power at a real +10pp effect is **65.3%**, because a procedure that will not cry wolf at an accumulating random walk also waits longer to call a true effect. An underpowered null is reported as UNDERPOWERED, never as "no effect". |
| 3.3 | Results could be settled after seeing them | **Done** | `commontrace/prereg.py` records the primary outcome, minimum practical effect, holdout rate, planned occasions and stopping rule *before* the run, and diffs the actual run against it. `start-experiment` pre-registers automatically. Tests: `tests/test_prereg_and_export.py` (32). NULL means "not pre-registered" and is reported as a finding, never backfilled. |
| 3.4 | A customer cannot independently re-derive the number they are billed on | **Done** | `commontrace/raw_export.py` + `export-assignments` emit every arm decision as CSV — including assigned-but-never-reported rows, which are the attrition question — and print the digest the value ledger's HMAC signature commits to. An analyst can re-run the comparison and check it against the signed invoice. Tests: `tests/test_prereg_and_export.py`. |
| 3.5 | Evidence decay: an effect measured long ago was billed as though current (§6.4 row 10, and the wedge the audit names in both its Executive Summary and Conclusion) | **Done** | `commontrace/decay.py`. Past a 180-day horizon a HELPS stops being billed; a stale HURTS **keeps** counting, because expiring it would raise the invoice — a vendor deleting its own harms by waiting. Both rules move the figure down: when evidence decays it resolves against the party who benefits from the doubt. Undated evidence is treated exactly as expired. The Hub's working-set horizon and the ledger's are now one constant, not two that must agree. Tests: `tests/test_evidence_decay.py` (25) and `hub/tests/test_holdout.py::TestEvidenceDecayReachesTheInvoice` (6), which go through `crud.value_delivered` because a horizon is only worth anything if it is reachable from the call that produces an invoice. **Not done:** automatic *retirement* of the lesson itself — decay withholds the figure and names what to re-measure; changing a lesson's status stays a human decision. |
| 3.6 | A crowded-out lesson was logged as treated | **Done** (found while building 5.1) | Holdout arms are now assigned *after* the dosage budget, over only the lessons that will actually be administered. Previously a lesson the budget dropped was logged as TREATED on an occasion it was never present for, pulling the measured effect toward zero — silently, and worse the tighter the budget. Tests: `tests/test_mcp_server.py::test_a_lesson_the_budget_crowds_out_is_never_logged_as_treated`. |

## 4. Deployment, releases and change control

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 4.1 | No immutable releases; no atomic promotion or rollback | **Done** | `commontrace/release.py` + `commontrace release cut\|list\|show\|diff\|rollback`: content-addressed, append-only snapshots of exactly which (lesson, revision) pairs were active together. Stale-base rejection on cut; a rewritten lesson is its own diff category; rollback refuses to restore a lesson whose text has changed since, and appends rather than rewinds. Tests: `tests/test_release.py` (25). |
| 4.2 | No dev/stage/prod environments, canary/ring targeting, scheduled activation, approval chain UI | **Not done** | Releases (4.1) give the identity these features would target, and two-person approval (1.4) gives the gate. Ring targeting, scheduling and a chain UI are not built. Listed here rather than omitted because 4.1 makes it tempting to imply more than exists. |
| 4.3 | No signed release artifacts, SBOM, or build provenance | **Requires business action** *(with an engineering half)* | Needs a signing identity and a key-custody decision first. The mechanical part (cosign/SLSA attestation in `.github/workflows/ci.yml`) is a small change once that exists; today there is neither, and the repository publishes no signed artifact. |
| 4.4 | No canonical product identity: owner, legal entity, trademark/use rights, support contact | **Requires business action** | `LICENSE` is MIT, copyright "CommonTrace" — which is a name, not a legal counterparty. There is no registered entity, no named owner, no trademark position and no support contact. A procurement reviewer cannot trace a running build to a company today, and no code change alters that. |

## 5. Retrieval and the agent-facing surface

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 5.1 | No dosage control: `top_k` bounds count, not context cost | **Done** | `commontrace/dosage.py` admits against a budget in characters as well as count; `retrieve` returns a `budget` gauge and names everything that did not fit under `not_injected`. Always-on lessons (`core: true`) are admitted ahead of the matched set and are still budgeted. Tests: `tests/test_dosage_and_receipts.py` (31) plus end-to-end coverage through `MCPServer.call_tool` in `tests/test_mcp_server.py`. |
| 5.2 | Nothing recorded what the agent could have seen, or what it used | **Done** | `commontrace/receipts.py` records per occasion: the **visible** candidate set (pinned to revisions, with a digest), the **admitted** subset, and — as a separate later line — what the agent says it **used**. Before this, "the memory did not help" and "the memory was never offered" were indistinguishable, and every reuse figure was counting injections. |
| 5.3 | Hybrid retrieval fusion was benchmarked against a private copy of the formula | **Done** | `commons/eval/hybrid_fusion.py` now calls the shipped `commontrace.retrieval.reciprocal_rank_fusion`, which gained the per-arm weights the sweep needs. It could previously have tuned one implementation and shipped another. |
| 5.4 | Retrieval picked one arm instead of fusing (§4.1 "Hybrid Retrieval with Budgets") | **Done** | `commontrace retrieval --fusion rrf` runs the lexical and semantic arms together and fuses them by rank. Opt-in, because fusion changes which lessons are **eligible** — the denominator of every causal number here — so the arm composition is carried inside the scorer label an assignment records (`rrf(idf-v2+semantic)`) and `integrity.check_scorer_drift` invalidates a run whose arms changed mid-flight, with no new column and no second check. Tests: `tests/test_hybrid_retrieval.py` (31). **Scope stated plainly:** two arms (lexical + semantic), not the four the audit sketches; graph and temporal arms are not built, and per §7.4 they belong behind a replaceable adapter rather than in this codebase. |

## 6. Integration and ecosystem

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 6.1 | No supported imports for LangSmith / Langfuse / Braintrust | **Done** | `commontrace/adapters.py` + `commontrace import --source`. Each adapter reads the vendor's own nested export shape and picks up the outcome that system already knows. Tests: `tests/test_adapters.py` (45). **Scope stated plainly:** these read an export *file*, not a live API. That is deliberate (no third-party credentials, no egress during onboarding, works air-gapped) and is not the same thing as an authenticated connector. |
| 6.2 | No OpenTelemetry support | **Partial** | Spans under the GenAI semantic conventions import via `--source otel`, in both OTLP-JSON and flat-attribute shapes. **Not done:** auto-instrumentation, runtime wrappers, or a collector — CommonTrace consumes OTel, it does not emit it. |
| 6.3 | No webhooks or event bus | **Done** | `hub/events.py`: a versioned event set, a durable at-least-once delivery queue with backoff and a visible dead letter, and HMAC signatures whose secret is *derived* rather than stored. Events carry ids, counts and verdicts and **never** trace content — enforced by a per-type field whitelist, with a test asserting it across the whole registry so it fails on the next event type someone adds with a body in it. Tests: `hub/tests/test_events.py` (39). |
| 6.4 | No mobile / JVM / .NET SDKs | **Partial** | `sdk/typescript` (`@commontrace/hub-client`): a thin, typed TypeScript client over the official `@modelcontextprotocol/sdk`, with typed convenience methods for the tools an integration reaches for first (`search_traces`, `contribute_trace`, `get_trace`, `vote_trace`, `amend_trace`, `list_tags`, `account_usage`) and a generic `call(name, args)` for the rest of the 26-tool surface, including anything added later. Retries a `rate_limited` tool-level refusal (with the server's own `retry_after`) the same way `commontrace/hub_client.py`'s own documented incident says is worth doing by default; any other tool-level error is raised immediately, never retried. Deliberately thinner than the Python client — no bulk-sync reconciliation, idempotency bookkeeping, or adaptive pacing; a project needing that still wants the Python CLI. Built and tested in CI against a real install of the official MCP SDK (`sdk-typescript` job, `.github/workflows/ci.yml`). Tests: 8, in `sdk/typescript/test/`. **Not done:** mobile (iOS/Android), JVM, and .NET clients — Python only, plus MCP and now TypeScript, is the whole client surface. |

## 7. Assurance, operations and support

Everything in this section is **Requires business action**. None of it is
blocked on code, and none of it is partially satisfied by code that exists.

| # | Finding | Status | Why no code closes it |
|---|---|---|---|
| 7.1 | No SOC 2 Type II | **Requires business action** | Needs an audit firm, an observation window, and staffed controls. The audit log, RLS, scoped keys and retention controls in this repo are *evidence a future audit could use*; they are not an attestation and must never be presented as one. |
| 7.2 | No annual penetration test or remediation letter | **Requires business action** | Needs a paid third party. `SECURITY.md` describes private vulnerability reporting, which is a disclosure channel, not a test. |
| 7.3 | No trust center with control status | **Requires business action** | A trust center publishes *attested* status. Publishing one that sourced its status from this repository would assert third-party verification that does not exist. |
| 7.4 | No production SLO, status page, on-call or escalation | **Requires business action** | Requires a running production deployment and a staffed rota. There is none — see the standing caveat in `DATA_RETENTION.md` that `hub/` is implemented but not deployed anywhere. |
| 7.5 | No RPO/RTO, restore or deletion drills | **Requires business action** | A drill is an exercise performed against a real deployment, not a feature. Backup and restore are the operator's responsibility today and `hub/DEPLOYMENT.md` says so. |
| 7.6 | No support SLA, incident or vulnerability response SLA | **Requires business action** | Commitments made by a company to a customer. |
| 7.7 | No published p95/p99 under a stated corpus, tenant and concurrency envelope | **Partial** | A local-tier retrieval latency gate runs in CI (`perf gate` in `.github/workflows/ci.yml`) and `hub/bench_scaling.py` / `hub/bench_concurrency.py` exist. **Not done:** a published envelope measured on a real multi-tenant deployment, which 7.4 blocks. |
| 7.8 | No accessibility audit | **Requires business action** | Needs an auditor and a UI large enough to audit; the surfaces today are a CLI, an MCP tool layer and a small operator console. |

## 8. Product surface and collaboration

| # | Finding | Status | Evidence / what remains |
|---|---|---|---|
| 8.1 | No reviewer queue, comments, assignments, notification inbox, ownership | **Done** | `hub/collab.py`, built on 1.2's human users. `add_comment`/`list_comments` let a customer's own team discuss one of their own traces; `assign_trace`/`unassign_trace` give it one owner at a time; `list_my_notifications`/`mark_notification_read` are a per-person inbox ("you were assigned a trace", "someone commented on one assigned to you") with no delivery beyond the table itself (no email/push/webhook). All six require a signed-in person, refused as `person_required` for an API-key-only caller, and are gated by the same scope+capability layers as every other tool (`CAP_CURATE` to write, `CAP_VIEW` to read). Distinct from `hub/manage.py`'s pre-existing Knowledge Base review queue, which remains an operator-only, cross-tenant surface. Tests: `hub/tests/test_collab.py` (28). **Not done:** any UI to render this — the surface is MCP tools only, same as the rest of the Hub. |
| 8.2 | No span/waterfall timeline, session replay, token-by-span view, saved views | **Not applicable** | These are observability-product features. CommonTrace is not an observability product and should not become one — §7.4 of the audit itself argues the opposite, that trace capture belongs behind a replaceable adapter. Integrating with the systems that do this well (6.1, 6.2) is the intended answer. |
| 8.3 | No alerting, scheduled reports, BI export | **Partial** | `hub/alerts.py`, built on the webhook pipeline (6.3): `create-alert-rule`/`list-alert-rules`/`delete-alert-rule` define a threshold on a closed, named set of metrics (`quarantine_rate`, `commons_queries_used_pct`, `traces_used_pct` — unknown metrics refused at creation, not silently skipped); `check-alerts` evaluates them and fires `alert.triggered` through an org's existing webhook endpoint(s), respecting a per-rule cooldown; `generate-report` emits a `report.generated` usage summary the same way. `export-assignments` remains the BI-shaped export for experiment data. **Not done:** an in-process scheduler — `check-alerts`/`generate-report` are operator-CLI commands meant for an external cron, the same shape as `webhook-deliver`'s existing redelivery sweep, not a built-in scheduling surface. Tests: `hub/tests/test_alerts.py` (23) plus CLI coverage in `hub/tests/test_manage.py` (13). |
| 8.4 | Two incompatible trust models and an undisclosed-search-shaped egress | **Done** *(documentation)* | The boundary is stated explicitly: there is no org-to-org sharing anywhere in the system, and the Knowledge Base is a separately opted-in, operator-curated layer — see `README.md` "The CommonTrace Knowledge Base" and `hub/README.md` "Tenant isolation vs. the CommonTrace Knowledge Base". Marked Done as a *disclosure*, which is what the finding asked for; the architecture it discloses was already the implemented one. |

---

## What a reader should take from this

**The measurement claims are the ones that got the most work**, because they
are the ones this product is ultimately selling and the ones where being
wrong is least visible. A billing figure derived from a double-counted
occasion, an effect size inflated by repeated looks, or a "treated" arm that
was never actually treated all produce a number that looks exactly like a
correct one. Sections 3 and 5 are that work.

**The security and governance findings are largely closed; the *assurance*
findings are entirely open.** Row-level security that actually bites,
least-privilege tokens, separation of duties, memory-admission guardrails
and retention with legal holds all exist and are tested. None of that is
SOC 2, a pen test report, or an SLA, and this register does not let the
first list be read as progress on the second.

**Identity (1.2) moved from open to partial, and unblocked 8.1.** A `User`
row is now a person, distinct from the org's workload API key, with a
named role (RBAC, checked as a second gate alongside the existing scopes)
and OIDC bearer-JWT sign-in — no auto-provisioning, and deprovisioning
blocks access on the person's very next call. SAML, SCIM, and a login UI
remain undone on 1.2. With a person to attribute a comment to or assign
work to, 8.1's collaboration surface (comments, assignment, a
notification inbox, built in `hub/collab.py`) is now done. This still
does not close 2.2 by itself: subject-level deletion is about a
customer's *own* end users named inside trace content, not about who can
log into this Hub. 2.2 has since gained its own partial closure
separately — `search_trace_content` locates candidates by exact
identifier for a human to review and delete, which is as far as this
schema can honestly go without a structured subject-id column it does
not have.

**Four items cannot be closed from a repository at all**: a legal
counterparty (4.4), an attestation (7.1), an independent test (7.2), and a
staffed operation (7.4–7.6). They are listed at full weight rather than
softened, because a buyer discovering them after a "compliant" answer is a
worse outcome than a buyer reading them here.
