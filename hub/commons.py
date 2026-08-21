"""The cross-org knowledge commons: signature helpers and input validation.

WHAT THIS IS FOR
----------------
Every other read path in this Hub is unconditionally scoped to the calling
org (see hub/crud.py's module docstring). That is correct, and it stays
correct. But it also means the product is per-org memory: a fleet's own
experience compounds for that fleet and nobody else, so value grows
linearly with customers and there is no network effect.

The commons is the opt-in exception, and it is deliberately narrow:

    Substrate failures are shared. Business logic stays private.

"Stripe webhook handlers need idempotency keys" is not a trade secret and
every fleet on earth rediscovers it at full cost. Your pricing rules and
escalation policy are yours and always should be. The Hub cannot tell those
apart -- that judgment is the contributing org's, made explicitly per trace
(crud.share_trace), recorded with a rationale, and revocable
(crud.unshare_trace).

WHY SIGNATURES AND NOT TEXT
---------------------------
The question a prospect wants answered before contributing anything is
"of the failures my fleet keeps hitting, how many has someone else already
solved?" Answering it must not require them to upload their failures.

So the client MinHashes its own failures locally and sends only signatures.
Text cannot be reconstructed from a MinHash signature. What comes *back*
is drawn only from traces their owners explicitly placed in the commons, so
the exchange is signature-in, consented-content-out.

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
        sig: list[int] = []
        for v in raw_sig:
            # bool is an int subclass; a signature of booleans is a client bug.
            if not isinstance(v, int) or isinstance(v, bool):
                raise CommonsInputError(f"failures[{i}].signature must contain only integers")
            sig.append(v)
        label = str(item.get("label") or f"failure-{i}")[:MAX_LABEL_CHARS]
        out.append((label, sig))
    return out


def estimate(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimated Jaccard similarity between two signatures of equal width."""
    return overlap.estimate_jaccard(sig_a, sig_b)
