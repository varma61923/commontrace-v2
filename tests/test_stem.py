"""commontrace/_stem.py is the Porter (1980) stemmer, exactly.

The expected stems are the vocabulary of Porter's paper plus the words
retrieval most needs to conflate, each cross-checked against NLTK's
PorterStemmer in ORIGINAL_ALGORITHM mode (the whole LoCoMo, field-fixture
and commons vocabulary -- 12,281 words -- agreed with it when this was
written). NLTK is not a dependency; these values are pinned instead.
"""
from __future__ import annotations

import pytest

from commontrace._stem import stem

EXPECTED = [
    ("caresses", "caress"), ("ponies", "poni"), ("ties", "ti"), ("caress", "caress"),
    ("cats", "cat"), ("feed", "feed"), ("agreed", "agre"), ("plastered", "plaster"),
    ("bled", "bled"), ("motoring", "motor"), ("sing", "sing"), ("conflated", "conflat"),
    ("troubled", "troubl"), ("sized", "size"), ("hopping", "hop"), ("tanned", "tan"),
    ("falling", "fall"), ("hissing", "hiss"), ("fizzed", "fizz"), ("failing", "fail"),
    ("filing", "file"), ("happy", "happi"), ("sky", "sky"), ("relational", "relat"),
    ("conditional", "condit"), ("rational", "ration"), ("valenci", "valenc"),
    ("hesitanci", "hesit"), ("digitizer", "digit"), ("conformabli", "conform"),
    ("radicalli", "radic"), ("differentli", "differ"), ("vileli", "vile"),
    ("analogousli", "analog"), ("vietnamization", "vietnam"), ("predication", "predic"),
    ("operator", "oper"), ("feudalism", "feudal"), ("decisiveness", "decis"),
    ("hopefulness", "hope"), ("callousness", "callous"), ("formaliti", "formal"),
    ("sensitiviti", "sensit"), ("sensibiliti", "sensibl"), ("triplicate", "triplic"),
    ("formative", "form"), ("formalize", "formal"), ("electriciti", "electr"),
    ("electrical", "electr"), ("hopeful", "hope"), ("goodness", "good"),
    ("revival", "reviv"), ("allowance", "allow"), ("inference", "infer"),
    ("airliner", "airlin"), ("gyroscopic", "gyroscop"), ("adjustable", "adjust"),
    ("defensible", "defens"), ("irritant", "irrit"), ("replacement", "replac"),
    ("adjustment", "adjust"), ("dependent", "depend"), ("adoption", "adopt"),
    ("homologou", "homolog"), ("communism", "commun"), ("activate", "activ"),
    ("angulariti", "angular"), ("homologous", "homolog"), ("effective", "effect"),
    ("bowdlerize", "bowdler"), ("probate", "probat"), ("rate", "rate"), ("cease", "ceas"),
    ("controlling", "control"), ("roll", "roll"), ("generalizations", "gener"),
    ("oscillators", "oscil"), ("resets", "reset"), ("resetting", "reset"),
    ("retried", "retri"), ("retries", "retri"), ("uploads", "upload"),
]


@pytest.mark.parametrize("word,expected", EXPECTED)
def test_porter(word, expected):
    assert stem(word) == expected


@pytest.mark.parametrize("token", ["http2", "e2e", "0x80070005", "ab", "k8s", "ünïcode", "日本語"])
def test_tokens_that_are_not_plain_english_words_are_left_exact(token):
    """Identifiers, error codes and non-ASCII words are matched exactly:
    stemming them could only merge things that differ."""
    assert stem(token) == token
