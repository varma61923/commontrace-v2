# Cognee raw-source benchmark adapter audit

**Decision: no fair public raw-source adapter is available in the inspected Cognee
checkout. No native score was measured.**

## Source inspected

- Local competitor checkout: `/root/Test/cognee`
- Revision: `0ec7a9fa61c9ff04bf7e02e0d57af363a993a5e1`
- Raw ingestion API: `cognee/api/v1/add/add.py`
- Processing API: `cognee/api/v1/cognify/cognify.py`
- Search API and retrieval types: `cognee/api/v1/search/search.py`,
  `cognee/modules/search/types/SearchType.py`
- LLM-free lexical retriever: `cognee/modules/retrieval/lexical_retriever.py`,
  `cognee/modules/retrieval/bm25_retriever.py`
- Direct-chunk integration fixture: `cognee/tests/integration/retrieval/test_bm25_retriever.py`
- No-key demo path: `cognee/cli/commands/demo_command.py`

## Why the public path does not fit

Cognee's public `add()` stores raw items without making an LLM call, but its API
docs direct callers to `cognify()` before search (`add.py`, lines 151–167).
`cognify()` chunks the source, extracts entities and relationships, constructs a
graph, and creates embeddings (`cognify.py`, lines 138–168). The public search
contract likewise lists a built knowledge graph as a prerequisite
(`search.py`, lines 80–85). Using this path would benchmark an extraction and
indexing pipeline, not raw-source recall.

`SearchType.CHUNKS_LEXICAL` is a useful native retrieval mode: it applies BM25
to `DocumentChunk` nodes without an LLM. However, Cognee's test fixture that
uses it without cognify manually constructs `TextDocument` and `DocumentChunk`
objects, persists them through internal `tasks.storage.add_data_points`, then
instantiates the internal `BM25ChunksRetriever` directly
(`test_bm25_retriever.py`, lines 33–63). That is a valid internal retriever
test, but it is not a public raw-source retain/search contract suitable for this
cross-system profile.

The CLI demo uses public `remember()` and `search(CHUNKS_LEXICAL)` without a
live LLM or embedding provider, but restores a bundled COGX archive that was
cognified before packaging (`demo_command.py`, lines 57–64 and 119–133). It
does not accept arbitrary raw source turns as the archive contents.

The benchmark environment also has no importable `cognee` package. No package
installation or model download was attempted. This is a second blocker to a
native run, independent of the public API mismatch.

## Revisit condition

Add a profile only if Cognee exposes or documents a supported local path that
accepts raw source turns and returns bounded native recall without entity
extraction, graph generation, or hidden model downloads. A specifically
labelled internal BM25-only experiment could be designed later, but it would
need separate review and should not be reported as Cognee's public raw-source
benchmark profile.
