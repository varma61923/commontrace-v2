# Bounded benchmark inference

Problem: nominal retrieval budgets do not bound a full-history prompt or a
multi-call rubric judge. A preflight average estimate can silently understate
cost, and cached historical charges can look like new spend.

Design: the existing conversation harness dispatches through a benchmark-only
HTTP client. Before every actual reader or judge call it reserves the configured
input/output price times a UTF-8 byte input bound plus 1024 framing tokens and an
explicit output cap. A successful response settles validated integer usage;
failure or missing/overbound usage keeps the reservation and stops the run.
There are no automatic retries or hidden provider completion caches. Cached
results have zero current spend and separately recorded historical cost.

The input bound and output cap are a declared provider contract. A malicious or
incompatible provider, additional fees, incorrect configured pricing or extra
unreported tokens cannot be converted into a billing guarantee. Live support is
Anthropic/OpenAI-compatible HTTP; unsupported cloud SDK dispatch is refused.
Pricing must be known, including explicit zero prices for a free local model.

Optional `--tokenizer tiktoken:<encoding>` counts emitted context text using the
chosen encoding. The retrieval packer still uses its existing estimate; token
targets require a subsequent tokenizer-aware packer and budget validation. A
reader's provider-reported input usage includes instructions and framing.

Tests use a real isolated HTTP server to verify pre-dispatch refusal, output caps,
long-history reservations, multi-call rubric accounting, unknown-price rejection,
no retries, uncertain charges and cache-hit cost separation. Existing judge and
source-bound benchmark tests protect behavior. No assignment, protocol, canonical
storage or approval semantics change. Rollback is the prior harness with a fresh
run directory; preserve original reports and cache tables.

Acceptance is functional safety under the declared provider contract, not a paid
accuracy result. Before publishing a comparison, verify provider price/token
assumptions and obtain a completed matched run with a manifest.
