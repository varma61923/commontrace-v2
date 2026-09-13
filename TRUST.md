# Trust and Provenance

A buyer evaluating this should be able to trace one running build to who
made it, what licence governs it, who to contact when it breaks, what
third parties are involved, and — the question that actually decides
adoption — **which bytes leave the machine it runs on**.

This document answers what the repository can answer, and names what it
cannot. Where the honest answer is "there is no such thing yet", that is
what it says. The related documents are [`SECURITY.md`](SECURITY.md)
(reporting a vulnerability), [`DATA_RETENTION.md`](DATA_RETENTION.md) (what
is stored and for how long), and [`AUDIT_RESPONSE.md`](AUDIT_RESPONSE.md)
(a third-party audit answered finding by finding, including the open ones).

---

## 1. What this is, and what it is not

| | |
|---|---|
| **Licence** | MIT (see [`LICENSE`](LICENSE)) |
| **Copyright holder** | "CommonTrace" — **a name, not a legal entity** (see §6) |
| **Security contact** | GitHub Security Advisories on this repository — [`SECURITY.md`](SECURITY.md) |
| **Support contact** | **None.** There is no support channel and no SLA (§6) |
| **Attestations** | **None.** No SOC 2, no penetration test, no certification (§6) |
| **Production deployment** | **None known.** The Hub is implemented; this project does not operate one |

That last row matters more than it looks. Everything below describes what
the *code* does. If someone is offering you a hosted CommonTrace, none of
the operational claims here transfer to it automatically — they are
properties of this source, and a deployment has to be verified on its own.

## 2. The two tiers, and the boundary between them

CommonTrace has two conformance tiers (`protocol/PROTOCOL.md` §5), and the
trust question is completely different for each.

### Local tier — no network, at all

The CLI and the MCP server (`commontrace/`) read and write flat Markdown
files under `memory/` in whatever directory you ran `commontrace init` in.

**There is exactly one module in the client that can open a network
connection: `commontrace/hub_client.py`.** Nothing else imports an HTTP
client, and you can check that in one command:

```bash
grep -rn "import httpx\|import urllib\|import requests" commontrace/
```

`hub_client` is reached only by `commontrace sync` and the Hub-backed
commands, and only when you have supplied a Hub URL and API key — via
`--hub-url`/`--hub-api-key` or `COMMONTRACE_HUB_URL`/
`COMMONTRACE_HUB_API_KEY`. Without those, there is nothing to connect to
and no code path that tries.

So: **retrieval, capture, distillation, approval, the randomized holdout,
the value calculation and every import adapter run entirely locally.** The
import adapters (§3) are deliberately file-based for this reason — an
importer that authenticated to a vendor would mean this product holding a
third party's credentials and making egress calls on your behalf during
onboarding, before any trust exists.

### Hub tier — multi-tenant, and what that costs

The Hub (`hub/`) is a Postgres-backed multi-tenant service. If you run or
use one, the honest list of what it holds is in
[`DATA_RETENTION.md`](DATA_RETENTION.md) §1. The isolation story:

- Every row that can carry org-specific content has a non-nullable,
  indexed `org_id`, and read paths filter on it **in the SQL WHERE
  clause** (`hub/crud.py`).
- Underneath that, Postgres row-level security (`FORCE ROW LEVEL
  SECURITY`) so a forgotten predicate returns zero rows rather than
  another tenant's data.
- The service connects as a **NOSUPERUSER, NOBYPASSRLS** role
  (`hub/postgres-init/10-runtime-role.sql`). This is the part that is
  easy to get wrong and invisible when you do: a superuser silently
  bypasses every policy, so RLS *appears* configured and enforces
  nothing. `HUB_REQUIRE_RLS=1` makes startup **fail closed** if the
  connected role can bypass.
- Known limits of RLS are stated rather than glossed, in the migration
  itself (`d5c8b3a91e77`): constraint checks run outside the policy and
  remain a side channel; RLS does not sanitise query logs; logical
  replication does not respect it.

## 3. What leaves the boundary, exhaustively

This is the section to read if you read only one.

| Path | Leaves? | What, exactly |
|---|---|---|
| `commontrace` CLI / MCP, local store | **No** | No network call exists on these paths. |
| `commontrace import --source ...` | **No** | Reads a file you already have. No API key, no hostname, no socket. |
| `commontrace sync` → Hub | **Yes, on request** | Traces you explicitly sync, to the Hub URL you configured. |
| Hub → other organizations | **No** | There is no org-to-org sharing anywhere in this system. |
| Hub → the Knowledge Base | **Only with `shared_with_commons`** | A per-trace opt-in flag, default false. A trace is readable cross-org only when it is explicitly marked shared **and** not quarantined **and** not retracted. |
| Hub → your webhook endpoint | **Yes, if you configure one** | Ids, counts, verdicts and timestamps — **never trace content.** Enforced by a per-event-type field whitelist (`hub/events.py`), not by convention. |
| Hub → Stripe | **Only with self-serve billing** | A customer id and a subscription id. Card data never touches the Hub. |

The webhook row is worth expanding, because a webhook is the classic place
for a boundary to erode: it is configured once and then nobody looks at it
again. Every event type declares its exact permitted fields and `emit`
**refuses** a payload carrying anything else. It is a whitelist rather than
a denylist of dangerous field names, because a denylist fails the moment
somebody adds a field nobody thought to ban — and there is a test asserting
no event type in the whole registry declares a content-shaped field, so it
fails on the *next* one somebody adds too.

## 4. Provenance of a running build

| Question | Answer |
|---|---|
| Where does the source live? | This repository. There is no other canonical copy. |
| What runs in CI? | `.github/workflows/ci.yml`: tests on Python 3.10/3.11/3.12 (core install and dev extra), hub tests against a real Postgres, `alembic upgrade head`, ruff, bandit, pip-audit, mypy, a retrieval-latency perf gate, a Docker image build, and a compose stack serving real MCP traffic. |
| Are dependencies pinned? | Yes for the Hub — `hub/requirements-lock.txt`, checked by `hub/tests/test_requirements_lock.py`. |
| Are release artifacts signed? | **No.** No cosign, no SLSA provenance, no SBOM. See §6. |
| Can I verify the build I am running? | **Not cryptographically.** You can read the source and run the tests. That is the whole of it today. |

## 5. Verifying the claims yourself

Every claim above is meant to be checkable without trusting this file.

```bash
# The only network-capable module in the client
grep -rn "import httpx\|import urllib\|import requests" commontrace/

# Every org-scoped table has row-level security (fails on the next one that doesn't)
pytest hub/tests/test_retention.py -k row_level_security

# No event type can carry trace content
pytest hub/tests/test_events.py -k content

# The service role cannot bypass RLS
pytest hub/tests/test_deployment_roles.py

# What repeated looks at an experiment actually cost
python -m commons.eval.sequential_error_rates

# Everything
pytest tests/ hub/tests
```

## 6. What does not exist

Stated plainly, because a buyer who finds these after a reassuring answer
is worse off than one who reads them here. Most are blocked on a business
decision rather than on code — a certification, a signed contract, a
staffed rota — and no amount of building closes them. Where code *has*
since narrowed one, the bullet says so explicitly and names what is still
missing, rather than dropping the bullet; see
[`AUDIT_RESPONSE.md`](AUDIT_RESPONSE.md) §7 for the full treatment.

- **No legal entity.** The copyright line names a project, not a
  counterparty. You cannot sign a contract with it, and procurement
  cannot diligence it.
- **No SOC 2, no ISO certification, no HIPAA posture.** The audit log,
  RLS, scoped tokens and retention controls in this repository are
  *evidence a future audit could use*. They are not an attestation and
  must not be read as one.
- **No independent penetration test.** `SECURITY.md` describes a
  disclosure channel. A channel is not a test.
- **No DPA, and no residency commitment from this project.** A DPA needs
  a legal entity to sign it, and there is none. Residency is a property
  of where an operator runs Postgres; this source cannot assert a region
  on its own behalf — though an operator who *has* decided theirs can now
  publish it, along with their legal name and support contact, at
  `GET /disclosure` (`hub/disclosure.py`), which reports
  `"not disclosed by this deployment's operator"` for anything left
  unset rather than guessing. The **subprocessor list does now exist**:
  [`SUBPROCESSORS.md`](SUBPROCESSORS.md) names the one processor this
  code actually implies (Stripe, for self-serve billing).
- **No support SLA, no incident or vulnerability response SLA, no status
  page, no on-call rota.**
- **No SAML, and no browser-based login.** OIDC, SCIM and human user
  accounts now exist — a `User` row is a person with a named role
  (`hub/rbac.py`), `hub/sso.py` verifies a bearer JWT their IdP issued,
  and `hub/scim.py` serves `/scim/v2/Users` and `/scim/v2/Groups` for
  auto-provisioning and immediate deprovisioning. What is still absent is
  the *interactive* half: no OAuth2 Authorization Code/PKCE redirect flow
  and no SAML, so a person arrives holding a token rather than being sent
  to an IdP and back. A scoped API key remains a *workload* credential,
  distinct from a person.
- **No signed artifacts, SBOM, or build provenance.**

## 7. How to read this document

If a future version of this file starts claiming something in §6, ask for
the artifact — the auditor's report, the entity registration, the signed
attestation — rather than accepting the sentence. That is the standard this
document is trying to hold itself to, and the reason it lists its own gaps
at full weight instead of describing them as roadmap.
