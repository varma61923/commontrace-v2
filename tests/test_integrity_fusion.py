"""The marginal-eligibility check judges only rows a relevance floor decided.

A fused ranking logs its rank-fusion score (never above 2/61) beside the
lexical arm's floor (0.04 by default). Compared against that floor, every
fused assignment looked like a lesson that barely matched, so every store
that opted into fusion had its experiment declared invalid: no causal
numbers, and no harm withdrawal, whatever the data said.
"""
from __future__ import annotations

from commontrace import integrity, retrieval

# The end-to-end case -- a store retrieving through MCP with fusion on -- is
# tests/test_mcp_fusion.py::test_a_fused_experiment_is_not_called_marginal.


def _row(i, lesson, relevance, scorer, floor=0.04):
    return integrity.Assignment(
        lesson=lesson, occasion_id=f"o{i}", injected=bool(i % 2), salt="s",
        relevance=relevance, scorer=scorer, floor=floor,
    )


def test_lexical_rows_are_still_judged_against_their_floor():
    weak = [_row(i, "barely", 0.05, "idf-v2") for i in range(20)]
    assert integrity.check_marginal_eligibility(weak).severity == integrity.SEVERITY_INVALIDATES


def test_in_a_mixed_log_only_the_lexical_rows_are_judged():
    fused = [_row(i, "fused", 0.03, "rrf(idf-v2+semantic)") for i in range(20)]
    semantic = [_row(i, "semantic", 0.5, "semantic") for i in range(20)]
    weak = [_row(i, "barely", 0.05, "idf-v2") for i in range(20)]
    finding = integrity.check_marginal_eligibility(fused + semantic + weak)
    assert finding.severity == integrity.SEVERITY_INVALIDATES
    assert set(finding.numbers["marginal_share_by_lesson"]) == {"barely"}


def test_unlabelled_rows_predate_fusion_and_are_lexical():
    weak = [_row(i, "old", 0.05, None) for i in range(20)]
    assert integrity.check_marginal_eligibility(weak).severity == integrity.SEVERITY_INVALIDATES


def test_the_floor_gated_labels_are_the_lexical_scorers():
    assert integrity._FLOOR_GATED_SCORERS == set(retrieval.LEXICAL_SCORERS)
