"""Shared word tokenization for the local, ephemeral lexical-scoring paths
(commontrace/retrieval.py's lesson ranking, commontrace/distill.py's trace
clustering). Both re-derive their token sets on every call from whatever is
on disk right now -- nothing here is ever persisted -- so unlike
commontrace/overlap.py's separate, intentionally-pinned copy (its
tokenization feeds MinHash signatures that ARE persisted as
Trace.commons_signature; changing it would silently stop matching every
already-stored signature), there is no compatibility reason for these two
to disagree, and previously they had quietly drifted from each other in
places (see CHANGELOG). One shared copy instead of two that can drift again.
"""

from __future__ import annotations

import re

# \w with re.UNICODE, not [a-z0-9]: the ASCII-only class silently mutilates
# any non-English text. "résumé" tokenized to ['sum'] (the accented letters
# split the word and the fragments were dropped by the len>1 filter), and
# CJK/Cyrillic/Arabic text tokenized to nothing at all.
WORD_RE = re.compile(r"\w+", re.UNICODE)

# Cheap English stopword list -- filtering these out keeps scores from being
# dominated by words that carry no discriminating signal ("the", "to", "a", ...).
STOPWORDS = frozenset(
    """
    a an the of to in on for with and or but is are was were be been being
    this that these those it its as at by from into over under again
    further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too
    very can will just don should now i you he she we they them his her
    """.split()
)
