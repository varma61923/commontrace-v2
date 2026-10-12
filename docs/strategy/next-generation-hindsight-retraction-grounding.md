# Hindsight-inspired retraction grounding

Hindsight source reviewed at `a9eeed77f9bce99d8f97d39a30d3281747300824`:

- `hindsight-api-slim/hindsight_api/engine/memory_engine.py` documents curation as moving an invalidated world/experience fact out of the live recall store, pruning dependent links, and re-deriving observations; observations themselves cannot be curated.
- `hindsight-api-slim/hindsight_api/engine/reflect/retractions.py` detects missing IDs in a mental model's `based_on` evidence, and `hindsight-system-tests/tests/test_36_page_maintenance.py` verifies that withdrawing cited evidence schedules a page refresh. `hindsight-system-tests/tests/test_31_mental_model_staleness.py` separately verifies prior model content remains in history.
- `hindsight-system-tests/tests/test_15_memory_curation.py` covers both recall exclusion and operator access to the invalidated fact's audit record.

CommonTrace already provides the relevant fail-closed read behavior. `commontrace/memory_journal.py::_grounded` recursively rejects a derived observation whose source is no longer active; `tests/test_memory_journal.py::test_derivatives_hide_when_grounding_retracted` now also checks direct current lookup and retained observation history. Source-bound pages call `wiki.source_state` from `knowledge_pages.get_page` and `_binding_live`, so invalidating a grounding fact removes the page from get/list/search immediately. The immutable page revision remains available through the explicit `page_history(..., _check_sources=False)` audit path; ordinary source-checked history remains hidden. A focused test now pins both paths in `tests/test_report_features.py::test_withdrawn_wiki_source_hides_current_page_but_keeps_audit_history`.

This adapts Hindsight's separation of live retrieval from audit history without adding a model-driven refresh dependency to recall. Explicit forgetting still follows CommonTrace's existing erasure path and removes dependent journal history; this behavior applies to withdrawal/invalidation only.
