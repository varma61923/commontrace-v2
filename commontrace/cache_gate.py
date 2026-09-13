"""Turns that cannot be worth a retrieval.

Retrieval is not free. `hub/crud.py`'s working-set header measures it: one
search page costs roughly 1,160 tokens, paid again on every call, and paid
whether or not the corpus had anything to say. On the local tier the cost is
not tokens but the whole corpus -- every `retrieve` re-reads and re-ranks the
active lesson set.

A meaningful share of an agent's turns cannot possibly match a lesson, because
they carry no content to match on. "ok". "thanks". "lgtm". "go ahead". Ranking
a corpus against "ok" is guaranteed to return either nothing or noise, and the
noise is the expensive outcome: a lesson retrieved against a contentless turn
is a lesson injected on an occasion it had nothing to do with, which is
exactly the marginal-eligibility contamination `integrity.check_marginal_
eligibility` exists to catch.

Adapted from Nous Hermes Agent's `TRIVIAL_PROMPT_RE` (`agent/memory_provider.py`),
which uses the same trick to skip provider round-trips on low-entropy turns.

TWO PROPERTIES THIS HAS TO HAVE, and the second is the one worth stating:

1. **It must never swallow a real query.** The pattern is anchored at BOTH
   ends, so a turn only counts as trivial when the whole thing is an
   acknowledgement. "no" is trivial; "no results come back when the token
   expires" is not, and shares its first word. Everything below is tested
   against that class of near-miss rather than only against the words it is
   meant to catch.

2. **It runs BEFORE randomization, never after.** A skipped turn is not an
   occasion: nothing is retrieved, so no arm is assigned and no row is logged.
   That ordering is what makes the gate safe for the experiment. A filter
   applied before assignment merely narrows which turns become occasions,
   and both arms are drawn from the survivors on identical terms. The same
   filter applied AFTER assignment -- dropping occasions once their arm is
   known -- would select on something downstream of the treatment, which is
   the one thing the holdout cannot survive.
"""

from __future__ import annotations

import re

# Acknowledgements, greetings and go-aheads: turns whose entire content is a
# signal to proceed. Deliberately a closed list rather than a length or entropy
# heuristic -- "restart" and "rerun" are short and low-entropy too, and both
# are real queries about real failure modes.
_TRIVIAL_WORDS = (
    r"y|n|yes|no|yep|nope|yeah|nah|ok|okay|k|kk|sure|fine|"
    r"thanks|thank you|ty|cheers|"
    r"hi|hey|hello|yo|morning|good morning|"
    r"continue|carry on|go ahead|go on|proceed|do it|"
    r"got it|understood|makes sense|sounds good|agreed|"
    r"cool|nice|great|perfect|awesome|good|done|next|lgtm|ship it|"
    r"please|please do|now|again"
)

# Trailing punctuation, emphasis and emoji-ish noise that carries no query of
# its own. A turn is trivial when it is one of the words above and then only
# this.
_TRAILING = r"""[\s!?.:;,'"~…()\[\]{}<>*&^%$#@+=`\-_/\\|]*"""

TRIVIAL_PROMPT_RE = re.compile(
    rf"^(?:{_TRIVIAL_WORDS}){_TRAILING}$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """Is this turn incapable of matching a lesson?

    True for an empty or whitespace-only turn as well: there is nothing to
    rank a corpus against, so the honest answer is the same one.

    Conservative by construction -- when in doubt this returns False and the
    retrieval happens. A missed skip costs one ranking pass; a wrong skip
    costs the agent a lesson it needed, and only one of those is recoverable
    by the agent noticing.
    """
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))
