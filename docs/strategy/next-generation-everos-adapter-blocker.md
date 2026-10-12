# EverOS benchmark adapter assessment

**Decision (2026-10-12): do not add an EverOS profile in this environment.**
EverOS is the next local competitor to assess: `/root/Test/EverOS` includes an
official LoCoMo benchmark runner and a keyword-only search configuration. The
available path cannot currently provide a fair raw-source retrieval run through
CommonTrace's shared reader, judge, and context budget.

The repository's `benchmarks/config_keyword.toml` selects native `keyword`
search, but its benchmark still performs EverOS's normal add/flush path. The
config labels `backbone_model` as extracting every memory. In
`src/everos/service/memorize.py`, ordinary adds run `prepare_cells` with the
configured LLM before the extraction pipelines; the environment has no
`EVEROS_LLM__API_KEY` or `EVEROS_LLM__BASE_URL` configured. Selecting keyword
search avoids an LLM in retrieval, but does not remove the LLM-backed write
stage.

EverOS does expose `defer_extraction`, but its implementation writes messages to
the unprocessed buffer and skips boundary detection and extraction. That buffer
is only included in search responses when the caller filters to a session; it
is not a ranked, cross-session raw-turn index. Searching it would therefore
change the retrieval task. Directly inserting fabricated episode rows into
EverOS's internal index would bypass its supported write path and would not
resolve that mismatch.

The current CommonTrace virtual environment also lacks `everos`, `lancedb`,
`pyarrow`, `sqlmodel`, and `fastapi`. The local source requires Python 3.12 or
newer (available here), and declares those packages plus the Everalgo packages
as core dependencies. The blocker is thus not the Python version: it is the
absent runtime stack and, more importantly, the absence of a configured
extraction service for EverOS's normal conversation ingestion path. EverOS's
benchmark runner also owns separate answer and judge stages; it is not an
adapter for the shared CommonTrace reader/judge.

To reopen this assessment, choose one of two comparable contracts: allow a
declared, pinned extraction model for every compared system, or provide an
EverOS-supported way to index raw source turns and search them across sessions
without extraction. Then install the pinned local EverOS package and run it
through `benchmarks/conversation_bench.py` so the same reader, judge, and token
budget are applied to the returned source turns. Until then, no EverOS accuracy
number from this harness is reproducible or comparable.
