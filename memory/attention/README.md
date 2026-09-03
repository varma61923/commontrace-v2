# Attention layer (v2.3)

Fast pre-filter for Alpha (Phase 0) based on local semantic embeddings.
Allows Alpha to scale (100+ lessons) without degrading retrieval quality
or latency.

## Role in the Pipeline

Alpha (Phase 0) reads the memory base to identify lessons relevant
to the incoming task. Without a pre-filter, Alpha must read the frontmatter of
all lessons at increasing volume. With attention:

1. `query.py "task"` returns ~10 candidates by cosine similarity + all
   lessons with `importance >= 4` (safety override)
2. Alpha reads these candidates in depth (frontmatter + body), applies the
   semantic filter `applies_when` / `do_not_apply_when` and the ranking by
   `score = importance x tag_match`.

The cosine score is a **complement** to the qualitative ranking, not a replacement.
Alpha continues to judge applicability + quality.

## Model Used

`multi-qa-mpnet-base-dot-v1` (sentence-transformers, ~420 MB, 768 dim).
Optimized for Q&A retrieval. Downloaded once from HuggingFace into
`~/.cache/huggingface/`, then run **strictly locally** — no runtime API
calls, no telemetry. Strictly local after the first model download.

## `index.npz` Format

Stored under `memory/attention/index.npz`. Stable contract:

| Field           | Type             | Semantics                                          |
|-----------------|------------------|----------------------------------------------------|
| `slugs`         | `np.ndarray[str]`| Lesson identifiers, ordered                        |
| `embeddings`    | `np.ndarray[N,D]`| L2-normalized embeddings (cosine = dot product)    |
| `model_name`    | `str`            | `"multi-qa-mpnet-base-dot-v1"`                     |
| `encoded_field` | `str`            | Human-readable schema of encoded fields            |
| `timestamp`     | `str`            | ISO-8601 build time                                |
| `n_lessons`     | `int`            | Number of active lessons indexed                   |

Text encoded per lesson: concatenation of `description + domain + tags +
applies_when + do_not_apply_when + rule` (separator ` | `, explicit labels).

## Safety Override importance >= 4

Frozen design decision v2.3: any active lesson with `importance >= 4` is
**always included** in the set returned by `query.py`, even if absent from the
top-K cosine. Guarantees that a critical / showstopper lesson is never
silently excluded by an orthogonal query. Floor configurable via
`--include-importance-floor=N`.

## Trigger Rebuild

- **Automatic (Phase 11)**: the orchestrator launches `build_index.py` at the
  end of Phase 11 if Lambda has applied at least one lesson creation / update /
  revision (addition, status change, or modified rule).
- **Manual**: `python build_index.py --force` (systematic rebuild, useful
  after manual editing, archival, or lesson merging).

The `--force` mode ignores the freshness check (mtime).

## Staleness Detection at Query Time

`build_index.py`'s freshness check only protects the *next* `build_index.py`
invocation — it does nothing if a lesson changes and nobody remembers to
rebuild. `query.py` therefore runs its own, independent staleness check on
every invocation, before returning results:

1. **Slug-set drift**: the set of currently-active lesson slugs on disk is
   compared against the slugs actually embedded in `index.npz`. Catches
   lessons added, archived, or deleted since the last build — including
   below the importance floor, where the existing safety override wouldn't
   otherwise surface the gap.
2. **mtime drift**: any *active* `lesson_*.md` file newer than `index.npz`
   itself (an edit that keeps the same slug, e.g. reworded `rule` or
   `applies_when`, which the slug-set check alone can't see). Archived or
   malformed lessons are deliberately excluded from this signal — editing
   one of those doesn't change what got embedded, so it must not trigger a
   false warning.

Either signal firing prints a `[WARN]` to stderr and adds a
`# WARNING: index may be stale -- ...` comment line to the `query.py`
stdout brief itself, so Alpha sees it inline rather than only in a log it
may not be reading. The query still runs and returns its best-effort
result — a stale index is a **degraded** signal, not a fatal one, since the
alternative (refusing to answer) would make retrieval unavailable exactly
when a rebuild hasn't caught up yet. `alpha_telemetry.jsonl` records an
`index_stale` boolean per invocation so staleness frequency is visible in
aggregate, not just per-call.

## Dreamer Hooks (v2.4, NOT implemented here)

The `index.npz` format is intentionally exposed for reuse by a future
**Dreamer** agent (v2.4). Anticipated use case: Dreamer detects fusion
candidates among existing lessons via **inter-lesson** cosine similarity
(sim > 0.85 = fusion candidate). Concretely:

```python
import numpy as np
data = np.load("memory/attention/index.npz", allow_pickle=False)
emb = data["embeddings"]                    # already L2-normalized
sim_matrix = emb @ emb.T                    # pairwise cosine (N x N)
candidates = np.argwhere((sim_matrix > 0.85) & (sim_matrix < 1.0))
# Dreamer then proposes the fusions to Lambda for validation
```

No Dreamer code is shipped in v2.3 — only the `index.npz` contract is
stable and documented to allow future evolution.

## Anti-patterns to Avoid

- Do not use cosine as a replacement for Alpha judgment (only as a
  pre-filter + ranking complement)
- Do not degrade `query.py` to a cosine dump without the importance context
- Do not forget to rebuild after manually editing a lesson (otherwise
  Alpha uses a stale index — `--force` to the rescue)
- Do not substitute an external dependency (FAISS, hnswlib, chroma) for
  numpy: numpy is sufficient up to 10k+ lessons. KISS.
