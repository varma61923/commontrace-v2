"""How a natural-language task description becomes a Postgres tsquery.

This module exists because the answer used to be one function call,
`plainto_tsquery`, and `plainto_tsquery` ANDs every lexeme it extracts:

    plainto_tsquery('english', 'customer charged twice for one order')
      -> 'custom' & 'charg' & 'twice' & 'one' & 'order'

A trace had to contain ALL of those to come back. For the query shape this
product exists to serve -- an agent describing, in its own words, the task
it is about to attempt -- that conjunction is essentially never satisfied.
Measured by `hub/bench_retrieval.py` on the shipped substrate corpus
against held-out paraphrased probes: **0.0% recall@1 and a 100%
zero-result rate**, where the local file tier scored 84.8% recall@1 on the
same 46 records. After this module: 95.7% recall@1, 0% zero-result.

STRATEGY.md §12.7 measured the local tier and concluded "any single shared
content word puts a lesson on the list"; §13.2 records link 2 -- *retrieval
finds the right memory when a task is described in the operator's own
words* -- as **measured, holds**; §13.3 says link 2 is one half of the only
thing that makes this more than a good DevTools business. All three
statements were true of `commontrace/retrieval.py`. None of them had ever
been about `hub/crud.py:search_traces`, which is the only retrieval path a
Hub customer has.

THE CONTRACT DECIDES THE OPERATOR
---------------------------------
§12.7's finding was that the commons matcher and the per-org tier disagree
because they emit different things, and each made the right trade for what
it emits:

    a quoted coverage percentage  must not over-claim -> buy precision
    a ranked top-k list to skim   must not hide the answer -> buy recall

`search_traces` emits the second, so it takes the second trade: match ANY
lexeme, rank, return top-k, no threshold. A weak match costs the reader a
glance; a conjunctive miss costs them the answer, silently, with HTTP 200.

RELAXING THE OPERATOR IS HALF THE JOB
-------------------------------------
The other half is that a disjunction of every word in a sentence matches
almost everything, and both consequences are bad:

  *Cost.* On a 64,000-trace org, ONE query term present in every trace
  produced a 64,000-row match set, and `ts_rank` over it was 188ms of a
  233ms query. `hub/bench_scaling.py` fitted the path at alpha=0.87 --
  STRATEGY.md §13.2's link-3 failure mode, serving cost that grows with the
  customer's own corpus.

  *Quality.* That term cannot rank anything: it scores every document
  identically. It only drags in traces whose sole connection to the query
  is one corpus-wide word. An agent injects what it is handed, so that is
  not a weak answer to skim past -- it is context poisoning with this
  product's name on it.

So `choose_terms` picks which lexemes are matched on: a term appearing in
more than `RANK_BUDGET` of the org's traces is dropped as
non-discriminating, and the rest are taken rarest first while their
frequencies still sum to the budget. Rarest first is the order of
information content; the sum bounds the ranker, because a union is never
larger than it. Same sweep afterwards: alpha=0.32, 66ms.

The two halves are one decision, not a fix and a mitigation. Deciding which
terms carry information is what makes matching on any of them safe.

THREE TRAPS, ALL OF WHICH FAIL SILENTLY
---------------------------------------
Each of these looked right, was wrong, and raised nothing:

1. `replace(plainto_tsquery(...)::text, '&', '|')` -- the one-line way to
   turn the conjunction into a disjunction. Lexemes can CONTAIN '&'
   (`'a.com/x&y=1'`, from a URL), so the replace edits the lexeme rather
   than the operator. It stays inside the quoted literal, so nothing is
   injected; the term simply stops matching.

2. Splitting the query on a word regex in Python and OR-ing one
   `plainto_tsquery` per word. Postgres's parser keeps `user@example.com`
   as ONE lexeme and does not also emit `user`, so a Python split asks for
   `user`, `example`, `com` and matches none of them -- wrong on exactly
   the identifiers engineers search by. The document side is tokenized by
   `to_tsvector`; doing the query side with the same function is the only
   way the two cannot drift.

3. `to_tsquery` over already-extracted lexemes. It runs the dictionary over
   what it parses, quoted literals included, so it stems a second time: the
   lexeme `stamped` becomes `stamp`, and the two never match. This one
   fails on ordinary English rather than on exotic input, and only on
   lexemes that are themselves still stemmable -- so a corpus looks fine on
   most terms while quietly losing others. `::tsquery` runs no dictionary.

A fourth is `quote_literal`; see `tsquery_literal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Float, cast, func, literal, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.sql import ColumnElement

from hub.models import TEXT_SEARCH_CONFIG, Trace

# ts_rank normalization flags, bit-ORed (Postgres docs, "Ranking Search
# Results"). 1 = divide by 1 + log(document length).
#
# Without it, ranking rewards verbosity: a trace whose solution_text runs to
# three paragraphs has more chances to contain a query lexeme than a
# two-line one that is exactly on point, and outranks it for that reason
# alone. That is the wrong bias for a corpus whose best entries are often
# its tersest. Logarithmic rather than linear (2) because length does carry
# some real signal -- a longer trace genuinely can be more informative --
# and dividing it out entirely over-corrects.
RANK_NORMALIZATION = 1


# The most documents `search_traces` will ever rank to return one page.
#
# Not a latency target -- a bound on a quantity that would otherwise track
# the customer's own success. Measured on a 64,000-trace org: with no bound,
# one query term present in every trace produced a 64,000-row match set and
# `ts_rank` over it cost 188ms of a 233ms query, and hub/bench_scaling.py
# fitted the natural-language read path at alpha=0.87 -- STRATEGY.md
# §13.2's link-3 failure mode, cost to serve growing with corpus size. With
# it, the same sweep fits alpha=0.32 and the query runs in 66ms.
#
# 8,000 rather than a smaller number because the bound must not throw away
# genuinely selective terms: in that same 64,000-trace org the useful terms
# of a realistic query sat at 3,200-6,400 documents (5-10% of the corpus),
# and a budget under those would have dropped the very terms the query was
# about and returned nothing.
RANK_BUDGET = 8_000

# The most lexemes one query is probed and matched on. A task description
# does not reach this; a pasted stack trace or log excerpt does, and every
# lexeme past the first few is noise that also costs a scan.
MAX_QUERY_TERMS = 40


def tsquery_literal(lexeme: str) -> str:
    """One lexeme, quoted so `tsquery`'s parser reads it back as exactly
    itself.

    Inside a quoted tsquery lexeme, `''` denotes a quote and `\\X` denotes a
    literal X -- so a lexeme is encoded by doubling its backslashes and then
    its quotes, backslashes first (the ones this adds must not then be
    quote-escaped).

    NOT Postgres's `quote_literal`, which is the obvious choice and is wrong
    here: `quote_literal('a\\b')` returns `E'a\\\\b'`, SQL's escape-string
    form, and `tsquery` does not know that syntax -- it reads the whole
    thing as the lexeme `E'a\\b'`, and the term stops matching. Postgres's
    parser happens never to emit a lexeme containing a backslash today (a
    backslash splits the token: `C:\\Users\\app.log` becomes `c`, `user`,
    `app.log`), which is exactly why that bug would have sat here
    indefinitely.
    """
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def tsquery_for(lexemes: Sequence[str]) -> ColumnElement:
    """SQL expression: a tsquery ORing `lexemes`, which must already be
    lexemes (`to_tsvector` output), not raw words.

    CAST, NOT `to_tsquery`. `to_tsquery` runs the text search dictionary
    over whatever it parses, INCLUDING a quoted literal, so feeding it
    lexemes that `to_tsvector` already stemmed stems them a second time:

        to_tsvector('english', 'Cache stampede')  ->  ... 'stamped' ...
        to_tsquery('english', '''stamped''')      ->  'stamp'

    'stamped' and 'stamp' never match, so the query silently misses the
    document it was built from. It only bites lexemes that are themselves
    still stemmable, which is why a corpus can look fine on most terms while
    quietly losing others. `::tsquery` runs no dictionary at all: the
    lexemes go in exactly as `to_tsvector` produced them, which is the only
    thing that can be right when both sides come from the same function.

    Sorted, so identical inputs compile to one bound parameter value rather
    than several. OR is commutative so the results are identical either way,
    but the tsquery text is what appears in `pg_stat_statements`, in
    slow-query logs and in `EXPLAIN` output, and a value that varies run to
    run makes all three useless for the path most likely to need them.
    """
    return cast(literal(" | ".join(tsquery_literal(x) for x in sorted(lexemes))), TSQUERY)


def relevance(vector: ColumnElement, tsquery: ColumnElement) -> ColumnElement:
    """SQL expression: the ranking score for one row.

    `ts_rank` sums the weights of the query lexemes a document matches, so a
    trace matching five of eight query terms outranks one matching one --
    which is what makes a relaxed match usable rather than a firehose. The
    ordering does the work a threshold would do, without a threshold's
    failure mode of discarding the answer at a cutoff (STRATEGY.md §12.7).
    """
    return func.ts_rank(vector, tsquery, RANK_NORMALIZATION, type_=Float)


def term_frequency_stmt(org_id: str, query: str, budget: int | None = None):
    """One statement returning `(lexeme, document_frequency)` for every
    lexeme in `query`, within one org's corpus.

    The count is deliberately CAPPED at `budget + 1`. Nothing downstream
    needs to know how far past the budget a term goes -- only whether it is
    past it -- and an exact count of a term appearing in every trace is
    precisely the scan this whole mechanism exists to avoid paying for.
    With the cap, the probe's cost is bounded by
    `budget x lexemes` rows regardless of how large the corpus is:
    measured at 10ms against a 64,000-trace org, unchanged as that org grows.

    The `tags` filter is deliberately NOT applied. Document frequency is a
    property of the corpus, not of one filtered request; applying the filter
    would make the same term look rare in a tag-scoped search and common in
    an unscoped one, and the budget below is a bound on work, which the
    unfiltered count over-estimates and therefore never under-bounds.

    At most `MAX_QUERY_TERMS` lexemes are probed, longest first. Without a
    cap, one pasted stack trace is a query with hundreds of distinct
    lexemes, and this statement runs one bounded scan PER lexeme -- an
    amplification a single caller could aim at the database. Longest first
    rather than alphabetically because at this point no frequency is known
    yet and word length is the only signal available; it is a weak proxy for
    specificity, but a query long enough to hit this cap is far outside
    anything a task description produces, and the alternative is truncating
    on the first letter.
    """
    # Read at call time, not bound as a default argument, so the module
    # constant stays the single knob rather than a value frozen at import.
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
        # Correlated to the outer lexeme rather than joined: without this
        # SQLAlchemy adds the unnest to the inner FROM as well, which turns
        # a per-lexeme count into a cross join over every lexeme and reports
        # the same (wrong, larger) frequency for all of them.
        .correlate(lex)
        .limit(budget + 1)
        .subquery()
    )
    df = select(func.count()).select_from(capped).correlate(lex).scalar_subquery()
    return select(lex.c.lexeme.label("lexeme"), df.label("df")).select_from(lex)


def _sql_escape(lexeme: ColumnElement) -> ColumnElement:
    """`tsquery_literal`'s escaping, as SQL, for the one place the lexeme
    exists only inside the database (the frequency probe, which derives its
    own lexemes). Same two substitutions in the same order."""
    return func.replace(
        func.replace(lexeme, literal("\\"), literal("\\\\")), literal("'"), literal("''")
    )


@dataclass(frozen=True)
class ChosenTerms:
    """Which of a query's lexemes will actually be matched on."""

    #: Lexemes the tsquery is built from, rarest first.
    used: tuple[str, ...]
    #: Lexemes discarded as too common to discriminate.
    ignored: tuple[str, ...]
    #: Every lexeme the query reduced to, in the order Postgres emits them.
    all_terms: tuple[str, ...]


def choose_terms(frequencies: Sequence[tuple[str, int]], budget: int | None = None) -> ChosenTerms:
    """Pick the lexemes worth matching on, rarest first, within a budget on
    how many documents the ranker will have to score.

    WHY THIS EXISTS, WHICH IS TWO REASONS THAT HAPPEN TO AGREE.

    *Quality.* A term that appears in nearly every trace an org has stored
    cannot rank anything: it contributes the same score to every document.
    Left in, it does worse than nothing -- it drags in every trace that
    shares only that one word, so a query about a checkout timeout comes
    back with twenty unrelated incidents whose only connection is the word
    "retry". For an agent, which injects what it gets, that is not a weak
    answer to skim past. It is context poisoning with the product's name on
    it.

    *Cost.* Measured on a 64,000-trace org, one such term (`retri`, present
    in 100% of the corpus) was the sole cause of a 64,000-row match set, and
    `ts_rank` over those rows was 188ms of the query's 233ms. Nine of the
    ten lexemes in that query matched nothing at all. Dropping the one that
    matched everything is the entire difference between a bounded read and
    STRATEGY.md §13.2's link-3 failure mode -- serving cost that grows with
    the customer's own corpus.

    THE RULE. A term matching more than `budget` documents is dropped
    outright: it cannot discriminate, and ranking that many documents to
    return twenty is not what makes a memory useful. The rest are taken
    rarest first while their frequencies still sum to `budget` or less --
    rarest first because that is the order of information content, and the
    sum because a union is never larger than it, so the ranker provably
    never scores more than `budget` rows however many terms are kept.

    Zero-frequency lexemes are kept. They match nothing, so they cost
    nothing and change no result -- but dropping them would make `used`
    lie about which terms the search ran with.

    Ties break alphabetically so the same query always produces the same
    tsquery; without that, four equally common terms would be selected in
    whatever order the probe happened to return them.
    """
    # Read at call time, not bound as a default argument: the module
    # constants are the knobs an operator or a test turns, and a default
    # argument would freeze whatever they were at import.
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
