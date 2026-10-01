"""Thin, per-framework middleware that derives the occasion id
commontrace/measure.py's `CausalMemory` needs from a framework's OWN
per-run identifier, instead of a caller inventing one.

None of these modules build a second retrieval, ranking, or holdout
implementation -- that already exists exactly once, in `CausalMemory`, and
stays there. Each submodule here is deliberately small: given an
already-configured `CausalMemory` (wrapping whatever memory the caller's
own application uses -- this store's lessons, Mem0, a plain dict, anything
with a retrieval call), it answers one question -- "what is this
framework's own identifier for the current run?" -- and wraps a framework
hook so retrieval/outcome-reporting happens automatically instead of by
hand at every call site.

Importing a submodule here never requires anything beyond that
framework's own package (already an application dependency if it is using
that framework at all); no submodule is imported by `commontrace/__init__.py`
or any core module, so a customer using none of these frameworks pays
nothing for their existence.
"""
