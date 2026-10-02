"""How a natural-language task description becomes a Postgres tsquery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Float, cast, func, literal, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.sql import ColumnElement

from hub.models import TEXT_SEARCH_CONFIG, Trace

RANK_NORMALIZATION = 1


RANK_BUDGET = 8_000

MAX_QUERY_TERMS = 40


def tsquery_literal(lexeme: str) -> str:
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def tsquery_for(lexemes: Sequence[str]) -> ColumnElement:
    return cast(literal(" | ".join(tsquery_literal(x) for x in sorted(lexemes))), TSQUERY)


def relevance(vector: ColumnElement, tsquery: ColumnElement) -> ColumnElement:
    """SQL expression: the ranking score for one row."""
    return func.ts_rank(vector, tsquery, RANK_NORMALIZATION, type_=Float)


def term_frequency_stmt(org_id: str, query: str, budget: int | None = None):
    budget = RANK_BUDGET if budget is None else budget
    every = (
        func.unnest(func.tsvector_to_array(func.to_tsvector(TEXT_SEARCH_CONFIG, query)))
        .table_valued("lexeme")
        .render_derived(name="every_lexeme", with_types=False)
    )
    lex = (
        select(every.c.lexeme)
        .select_from(every)
        .order_by(func.length(every.c.lexeme).desc(), every.c.lexeme)
        .limit(MAX_QUERY_TERMS)
        .subquery("lex")
    )
    capped = (
        select(literal(1))
        .select_from(Trace)
        .where(
            Trace.org_id == org_id,
            Trace.quarantined.is_(False),
            Trace.search_vector.op("@@")(
                cast(literal("'") + _sql_escape(lex.c.lexeme) + literal("'"), TSQUERY)
            ),
        )
        .correlate(lex)
        .limit(budget + 1)
        .subquery()
    )
    df = select(func.count()).select_from(capped).correlate(lex).scalar_subquery()
    return select(lex.c.lexeme.label("lexeme"), df.label("df")).select_from(lex)


def _sql_escape(lexeme: ColumnElement) -> ColumnElement:
    return func.replace(
        func.replace(lexeme, literal("\\"), literal("\\\\")), literal("'"), literal("''")
    )


@dataclass(frozen=True)
class ChosenTerms:
    """Which of a query's lexemes will actually be matched on."""

    used: tuple[str, ...]
    ignored: tuple[str, ...]
    all_terms: tuple[str, ...]


def choose_terms(frequencies: Sequence[tuple[str, int]], budget: int | None = None) -> ChosenTerms:
    budget = RANK_BUDGET if budget is None else budget
    if budget < 0:
        budget = 0
    ranked = sorted(frequencies, key=lambda pair: (pair[1], pair[0]))
    used: list[str] = []
    ignored: list[str] = []
    spent = 0
    for lexeme, df in ranked:
        if df > budget:
            ignored.append(lexeme)
        elif spent + df <= budget:
            used.append(lexeme)
            spent += df
        else:
            ignored.append(lexeme)
    return ChosenTerms(
        used=tuple(used),
        ignored=tuple(sorted(ignored)),
        all_terms=tuple(lexeme for lexeme, _ in frequencies),
    )
