"""The CommonTrace Knowledge Base: signature helpers and input validation.

WHAT THIS IS FOR
----------------
Every other read path in this Hub is unconditionally scoped to the calling
org (see hub/crud.py's module docstring). That is correct, and it stays
correct: a fleet's own experience compounds for that fleet, on its own
infrastructure, and no other customer ever reads it.

This module is the one deliberate, additive exception -- and it is NOT
org-to-org sharing. An earlier design routed it that way (customer A opts a
trace in, customer B's queries can match it) and that design is retired.
The reason is adverse selection, and it does not have a fix: why would an
org contribute knowledge that might help a competitor? The most valuable
lessons are the most proprietary, so voluntary contribution biases toward
generic filler and withholds anything that would actually matter, and no
amount of incentive design changes that a customer is being asked to trust
a stranger with their own trace text.

So instead: the Knowledge Base is a single corpus the OPERATOR authors and
curates -- substrate knowledge ("Stripe webhook handlers need idempotency
keys", "React 19 hydrates Date differently than 18") that isn't anyone's
trade secret, shipped and maintained the way a vendor maintains
documentation or a team maintains an internal wiki, not the way two
competitors would maintain a joint one. `hub/manage.py:commons_seed`
(bulk load) and `hub/crud.py:review_kb_submission` (one community
submission at a time, called only from `hub/manage.py review-submission`)
are the only two things that ever write a `commons_source == "seed"` row --
both operator-run, neither reachable from a customer's own API key.
A customer may *propose* an entry (`submit_kb_entry`), but proposing is
not publishing: the row that creates lives in a separate table
(`KnowledgeBaseSubmission`) that no commons query ever reads, and it stays
there, invisible to every other org, unless and until an operator's own
review-submission call accepts it. No customer's trace is ever visible to
another customer through this system, because no customer's trace enters
this corpus without a human at the operator deciding it should.

`commons_access` (hub/plans.py) is the "optional" half: a plan controls
whether an org may consult the Knowledge Base, not whether it must expose
anything to unlock that access. An org that never queries it, or a
deployment with `HUB_COMMONS_ENABLED=false`, loses nothing about its own
per-org memory -- see hub/DEPLOYMENT.md's single-org deployment mode.

WHY SIGNATURES AND NOT TEXT
---------------------------
Even though there is no other customer to protect data from, the query
itself still never needs to send failure text to ask "has this been seen
before" -- so it doesn't. The client MinHashes its own failure locally and
sends only a signature; text cannot be reconstructed from one. What comes
*back* is drawn only from the operator's curated corpus, so the exchange is
signature-in, operator-curated-content-out.

Stated limitation, same as commontrace/overlap.py's: this is not a
cryptographic privacy guarantee. A party who can guess a candidate string
can test whether it is present. Defeating that needs private set
intersection, which is a real follow-up and not quietly assumed here.

WHY THE ALGORITHM IS IMPORTED, NOT REIMPLEMENTED
------------------------------------------------
MinHash signatures are only comparable if both sides draw the *same*
permutations. A near-copy of the hashing here that drifted from the
client's by one constant would not fail loudly -- it would silently return
plausible, wrong similarity numbers, which is the worst possible failure
mode for a number this product intends to quote to customers. So the Hub
imports commontrace.overlap rather than reimplementing it, and
hub/tests/test_commons.py pins client/server signature identity.
commontrace/overlap.py is pure stdlib, so this costs the Hub no new
dependency.
"""

from __future__ import annotations

from commontrace import overlap

# Signature width. Must match what clients use, or estimate_jaccard is
# meaningless -- so it is taken from the shared module rather than restated.
COMMONS_NUM_PERM = overlap.DEFAULT_NUM_PERM

# A commons trace counts as "already solves" a submitted failure at or above
# this estimated Jaccard similarity. Deliberately conservative: this number
# gets quoted to customers, so it should under-claim.
DEFAULT_COMMONS_THRESHOLD = overlap.DEFAULT_MATCH_THRESHOLD

# Bounds on client-submitted input. commons_overlap is an untrusted,
# unauthenticated-shaped surface in the sense that the *content* is
# arbitrary caller data, and its cost is O(submitted x corpus) -- so the
# submitted side is capped rather than trusted to be reasonable.
MAX_SUBMITTED_FAILURES = 500
MAX_LABEL_CHARS = 200

# Ceiling on how many ranked candidates one commons_search returns.
# Measured against the held-out probes (commons/eval/search_modes.py),
# recall@10 is 100% and recall@5 is 95.7%, so a caller asking for more than
# this is paying scan and payload cost for results past the point where the
# answer is essentially always already present. Also bounds the response
# size, since each candidate carries full solution text.
MAX_SEARCH_CANDIDATES = 25
DEFAULT_SEARCH_CANDIDATES = 5

# Hard ceiling on how many hits ONE Knowledge Base entry can accrue from ONE
# commons_overlap call. commons_hits is the content-quality signal
# hub/manage.py:kb_stats reports (which entries are actually earning their
# query traffic) -- incremented once per submitted failure that
# best-matches an entry, deliberately, so a fleet that genuinely hits the
# same substrate failure across several distinct tasks in one batch counts
# for each. But nothing about the wire format stops a caller from
# submitting the identical signature MAX_SUBMITTED_FAILURES times in a
# single request, and without this cap that would count whichever entry it
# matches once per repetition -- one submission, counted as if it were
# hundreds. This bound is set well above any plausible legitimate
# multi-task batch (hub/tests/test_commons.py's own such test uses 2)
# while keeping the count a single call can inflate for an entry small.
MAX_HITS_PER_TRACE_PER_QUERY = 20

# Hard ceiling on how many commons traces one query will compare against.
#
# This is not a guess. Measured on this codebase: the comparison is
# O(submitted x corpus) and costs ~4.6us per pair in pure Python, so
# 500 failures x 50,000 traces is ~114 seconds -- a request that ties up a
# worker until it times out. numpy vectorizes it ~24x (used below when
# available), which moves the same case to ~5s: better, still not a
# synchronous request budget.
#
# So the query is bounded rather than left to grow with the corpus. When
# the cap bites, the result says so explicitly (`corpus_truncated`) and the
# coverage number is reported as a LOWER BOUND -- a silently-truncated
# scan would under-report the one number this product's strategy rests on,
# which is exactly the failure mode to avoid.
#
# The real fix past this ceiling is an approximate-nearest-neighbour index
# (pgvector, or a dedicated ANN service). Deliberately not built yet:
# classic MinHash LSH banding was measured first and rejected on evidence
# -- at this module's 0.30 match threshold, r=2 filters almost nothing
# (77% candidate rate) and r=4 loses 36% of true matches. Low thresholds
# are simply where LSH stops paying.
MAX_COMMONS_CORPUS = 20_000

try:  # pragma: no cover - exercised by whichever path the environment has
    import numpy as _np
except ImportError:  # listed in hub/requirements.txt, but not required for correctness
    _np = None

# Without numpy the same bounded worst case takes ~46s instead of ~1.6s
# (measured), which is not a request budget. Rather than let a Hub that
# happens to be missing an optional accelerator hang, the scan bound scales
# down with the implementation actually in use. The result stays correct
# either way -- it is just reported as a lower bound sooner, which
# `corpus_truncated` and the accompanying note make explicit.
MAX_COMMONS_CORPUS_NO_NUMPY = 2_000


def max_corpus_scan() -> int:
    """The per-query corpus ceiling for the matcher this host will use."""
    return MAX_COMMONS_CORPUS if _np is not None else MAX_COMMONS_CORPUS_NO_NUMPY


class CommonsInputError(ValueError):
    """A submitted overlap request was malformed or oversized. Surfaced as a
    clean invalid_request rather than a 500 or an unbounded scan."""


def matchable_text(title: str, context_text: str, tags: list[str] | None) -> str:
    """The text a commons trace is signed on.

    Deliberately title + context + tags, NOT solution_text: the match being
    estimated is "does this trace describe the same *situation* as the
    failure I'm hitting", which is what title/context carry. The solution is
    what you get back once it matches, not part of deciding whether it
    matches. This mirrors how commontrace/commands/overlap_cmd.py signs a
    recurring failure, so the two sides of the comparison are symmetric.
    """
    return " ".join([title or "", context_text or "", " ".join(tags or [])])


def signature_for(title: str, context_text: str, tags: list[str] | None) -> list[int]:
    """MinHash signature for a trace entering the commons."""
    return overlap.minhash(matchable_text(title, context_text, tags), COMMONS_NUM_PERM)


def validate_submitted_failures(failures: object) -> list[tuple[str, list[int]]]:
    """Coerce and bound a client-submitted failure list.

    Returns [(label, signature), ...]. Raises CommonsInputError on anything
    malformed -- a caller sending garbage should get a clean 400-shaped
    answer, never a traceback and never an unbounded comparison loop.
    """
    if not isinstance(failures, (list, tuple)):
        raise CommonsInputError("failures must be a list")
    if len(failures) > MAX_SUBMITTED_FAILURES:
        raise CommonsInputError(
            f"too many failures submitted ({len(failures)}); "
            f"the maximum is {MAX_SUBMITTED_FAILURES} per request"
        )

    out: list[tuple[str, list[int]]] = []
    for i, item in enumerate(failures):
        if not isinstance(item, dict):
            raise CommonsInputError(f"failures[{i}] must be an object with 'label' and 'signature'")
        raw_sig = item.get("signature")
        if not isinstance(raw_sig, (list, tuple)):
            raise CommonsInputError(f"failures[{i}].signature must be a list of integers")
        if len(raw_sig) != COMMONS_NUM_PERM:
            # A different width is not a comparison that can be salvaged --
            # estimate_jaccard would compare mismatched positions and return
            # a confident, meaningless number.
            raise CommonsInputError(
                f"failures[{i}].signature has {len(raw_sig)} values; "
                f"this Hub's commons uses num_perm={COMMONS_NUM_PERM}. "
                "Re-sign with a matching width."
            )
        sig = _coerce_signature(raw_sig, f"failures[{i}].signature")
        label = str(item.get("label") or f"failure-{i}")[:MAX_LABEL_CHARS]
        out.append((label, sig))
    return out


def _coerce_signature(raw_sig: object, field: str) -> list[int]:
    """Validate one MinHash signature. Shared by every entry point that
    accepts one, so the width and range rules cannot drift between them --
    the same reason this module imports the hashing rather than
    reimplementing it. A signature that passes one surface and fails
    another is exactly the silent, confidently-wrong-number failure this
    file's docstring exists to prevent.
    """
    if not isinstance(raw_sig, (list, tuple)):
        raise CommonsInputError(f"{field} must be a list of integers")
    if len(raw_sig) != COMMONS_NUM_PERM:
        # A different width is not a comparison that can be salvaged --
        # estimate_jaccard would compare mismatched positions and return
        # a confident, meaningless number.
        raise CommonsInputError(
            f"{field} has {len(raw_sig)} values; this Hub's commons uses "
            f"num_perm={COMMONS_NUM_PERM}. Re-sign with a matching width."
        )
    sig: list[int] = []
    for v in raw_sig:
        # bool is an int subclass; a signature of booleans is a client bug.
        # Range-checked to the uint64 domain MinHash signatures actually
        # live in, not just type-checked: a value outside it still
        # "is an int" but breaks the numpy path (`_np.array(..., dtype=
        # uint64)` raises OverflowError on a negative or >2**64-1 value,
        # surfacing as an unhandled 500) and silently wraps on the
        # pure-Python path instead, so the two implementations would
        # disagree on the exact same input depending on which one this
        # host happens to run.
        if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 2**64 - 1):
            raise CommonsInputError(f"{field} must contain only integers in [0, 2**64 - 1]")
        sig.append(v)
    return sig


def validate_query_signature(signature: object) -> list[int]:
    """Coerce and bound the single signature `commons_search` compares
    against the corpus. Same rules as a submitted failure's signature,
    enforced by the same code."""
    return _coerce_signature(signature, "query_signature")


def estimate(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimated Jaccard similarity between two signatures of equal width."""
    return overlap.estimate_jaccard(sig_a, sig_b)


def best_matches(
    submitted: list[tuple[str, list[int]]],
    corpus_signatures: list[list[int]],
) -> list[tuple[int, float]]:
    """For each submitted signature, the (corpus_index, similarity) of its
    single best match. Returns (-1, 0.0) when the corpus is empty.

    Two implementations, identical results. numpy compares one signature
    against the entire corpus as a single vectorized operation (~24x faster,
    measured); the pure-Python path is the exact same arithmetic in a loop
    and is what runs when numpy is absent, since numpy is deliberately not a
    Hub dependency. hub/tests/test_commons.py asserts the two agree, so the
    fast path can never quietly diverge from the reference one.
    """
    if not corpus_signatures:
        return [(-1, 0.0) for _ in submitted]

    if _np is not None:
        corpus = _np.array(corpus_signatures, dtype=_np.uint64)
        out: list[tuple[int, float]] = []
        for _label, sig in submitted:
            sims = (corpus == _np.array(sig, dtype=_np.uint64)).sum(axis=1) / COMMONS_NUM_PERM
            idx = int(sims.argmax())
            sim = float(sims[idx])
            # argmax returns 0 for an all-zero row, but "nothing matched at
            # all" is -1 in the reference implementation, and the two must
            # agree: `threshold` is caller-controlled and clamps to 0.0, so
            # a zero-similarity "match" is reachable, and the paths would
            # then disagree only on hosts that happen to have numpy.
            out.append((idx, sim) if sim > 0.0 else (-1, 0.0))
        return out

    out = []
    for _label, sig in submitted:
        best_idx, best_sim = -1, 0.0
        for i, candidate in enumerate(corpus_signatures):
            sim = estimate(sig, candidate)
            if sim > best_sim:
                best_idx, best_sim = i, sim
        out.append((best_idx, best_sim))
    return out


def rank_candidates(
    query_signature: list[int],
    corpus_signatures: list[list[int]],
    top_k: int,
) -> list[tuple[int, float]]:
    """The SAME comparison as best_matches, ranked and truncated instead of
    thresholded: [(corpus_index, similarity), ...] best first, zero-scored
    records dropped.

    This is the whole difference between a coverage meter and a knowledge
    base, and it is worth being precise about why it is not a tuning change.
    Measured on the shipped corpus and the held-out probes
    (commons/eval/search_modes.py, recorded in commons/eval/RESULTS.md):

        thresholded coverage   10.9% recall,  0% false positives
        ranked candidates      89.1% recall@1, 95.7%@5, 100%@10

    Same signatures, same estimator, same corpus, same privacy properties --
    the caller still sends only a MinHash signature and no failure text.
    What the threshold was discarding was nine of every ten real answers.

    The reason this may never be reported as coverage: on those same
    probes, a failure the corpus does NOT contain still returns a non-empty
    ranked list 100% of the time, and the score distributions overlap (true
    match median 0.148, absent median 0.078, both ranging down to 0.039).
    The score separates on average, not case by case. So these are
    candidates for a human or agent to judge, exactly like a search
    engine's results -- and `commons_overlap`'s conservative, thresholded
    percentage remains the only thing this system will call coverage.

    Ordering: similarity first, then measured usefulness (commons_hits) and
    trust as tie-breaks, applied by the caller. Popularity never overrides
    relevance -- a well-corroborated answer to a different question
    outranking the right answer is the specific failure a naive
    "rank by votes" blend produces.
    """
    if not corpus_signatures or top_k <= 0:
        return []

    if _np is not None:
        corpus = _np.array(corpus_signatures, dtype=_np.uint64)
        sims = (corpus == _np.array(query_signature, dtype=_np.uint64)).sum(axis=1) / COMMONS_NUM_PERM
        # argpartition would be cheaper asymptotically, but top_k is small
        # and bounded (MAX_SEARCH_CANDIDATES) while the corpus scan above it
        # is already bounded too; a full argsort here is simpler to reason
        # about and never the hot spot.
        order = _np.argsort(-sims, kind="stable")[:top_k]
        return [(int(i), float(sims[i])) for i in order if float(sims[i]) > 0.0]

    scored = [(i, estimate(query_signature, c)) for i, c in enumerate(corpus_signatures)]
    scored = [(i, s) for i, s in scored if s > 0.0]
    # Stable sort on the negated score keeps corpus order as the tie-break,
    # matching numpy's kind="stable" above so the two paths agree exactly.
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
