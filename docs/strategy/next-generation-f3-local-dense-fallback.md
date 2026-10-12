# F3: Preserve lexical retrieval when local dense inference fails

`indexed_search` already starts with SQLite FTS5 candidates and exposes a
`dense_fallback` field through its `explain` dictionary. Its optional local
projection search and embedding-score calls previously caught only a fixed
set of built-in exception types. This change lets backend-specific ordinary
exceptions from those two local dense stages fall back to the existing FTS
candidate list and records the exception class in `explain["dense_fallback"]`.
The message and traceback are not copied into the report. Caller-supplied
`vector_search` remains outside this fallback boundary.

The local model loader makes the offline boundary explicit. It still tries
`SentenceTransformer(..., local_files_only=True)` first. If that cache-only
load fails while `COMMONTRACE_OFFLINE` is enabled, it raises `OfflineError`
before the second constructor call that omits `local_files_only` and may fetch
from Hugging Face. With offline mode disabled, that existing online retry is
preserved. The retrieval layer catches the offline error and returns the FTS
results with `dense_fallback` set to `OfflineError`.

The adaptation is informed by Cognee's explicit lexical path: in checkout
`/root/Test/cognee`, revision
`0ec7a9fa61c9ff04bf7e02e0d57af363a993a5e1`,
`cognee/cli/commands/demo_command.py` lines 61–64 describes
`CHUNKS_LEXICAL` as BM25 keyword search that needs neither an LLM nor
embeddings; lines 127–132 select that search type. That demo searches a
pre-cognified bundled archive, so it does not establish a general Cognee
failure-recovery contract. The narrow transferable point is that lexical
search can be a usable retrieval arm independent of dense inference.

Focused contract tests inject a backend-specific exception into the local
dense stages and simulate a missing local-model cache. They check that the
FTS match still returns with a BM25 signal, no fabricated dense signal, and
only the exception class in the explanation; the offline test also asserts
that the download constructor is never called. A separate mocked test keeps
the prior online cache-miss retry. These tests use a temporary SQLite store
and load no embedding model. They validate continuity and the network boundary,
not retrieval quality or latency. If FTS finds no candidates, this change
cannot create results; if FTS itself fails, the error still propagates. A
recorded fallback class identifies the last failed local dense stage, not the
model or cache root cause.

## Cross-encoder offline continuation

The same cache-miss boundary now applies to `rerank_arm._load()`. Graphify
identified the shared loader used by `rerank()` and `ready()`; source inspection
found that its second `CrossEncoder` constructor omitted `local_files_only`
even when CommonTrace offline mode was enabled. A regression failed first:
the fake runtime observed two constructor calls, including the download-capable
retry. After the guard, it observes only the cache-only call and reports
`OfflineError`; retrieval retains the identical first-stage rows at a fixed
reference time. Cached cross-encoders remain usable offline, and online cache
misses preserve the existing retry.

Offline scoring also bypasses the optional warm reranker worker. Its socket
identity does not distinguish offline mode, so a reused process can retain an
earlier online environment. A regression first proved that offline scoring
still called that worker. It now loads cache-only in the requesting process;
cached scoring remains available with either worker setting. This can add
local model startup cost in offline mode; no latency improvement is claimed.

Validation: `python -m pytest tests/test_dense_retrieval_fallback.py
tests/test_rerank.py tests/test_retrieval_rerank_stage.py
tests/test_reranker_registry.py tests/test_offline_mode.py -q` covers 82 tests.
The larger run adding envelope/policy/extension-vector tests and
`tests/test_warm.py` passes 178 tests with 2 optional skips.
The constructor and scoring runtime are mocked in the new tests, so this is
network-boundary and fallback evidence, not real-model quality or latency.
This reuses CommonTrace's existing offline policy and fallback stage; no new
reranking implementation or competitor model-loading contract is assumed.
