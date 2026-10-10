"""Deterministic lexical completeness grading for benchmark contexts.

The existing `complete` column in conversation_bench is evidence-id based: it
asks whether the gold evidence turn *refs* survived into the assembled context.
This grader asks the complementary lexical question on the retrieved context
itself: does the context actually contain the content of the gold evidence?

The pure logic lives in `commontrace.completeness`, so runtime recall grades
its contexts the same way; this module re-exports it unchanged.

Buckets:
  COMPLETE     -- every evidence element is present in the context
  PARTIAL      -- some elements are present, some are missing
  INSUFFICIENT -- no element is present
Each element is reported under `present` or `missing` by the same key.
"""
from __future__ import annotations

from commontrace.completeness import (  # noqa: F401 - the benchmark's public names
    COMPLETE,
    INSUFFICIENT,
    PARTIAL,
    PRESENCE_THRESHOLD,
    _key_tokens,
    _tokens,
    bucket_counts,
    grade_context_completeness,
)
