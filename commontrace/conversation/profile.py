"""What a user says about themselves: preferences, identity, habits, plans and
possessions, kept as the user's own sentences so nothing is paraphrased."""
from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = frozenset("""a about above after again against all am an and any are aren as at be because been
before being below between both but by can cannot could did didn do does doesn doing don down during each
few for from further had hadn has hasn have haven having he her here hers herself him himself his how i if
in into is isn it its itself just let me more most my myself no nor not now of off on once only or other
our ours ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up very was wasn we were weren what when where
which while who whom why will with won would you your yours yourself yourselves s t ll ve re d m""".split())

MAX_STATEMENT = 300

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_PATTERNS = [
    ("preference", re.compile(
        r"\bI(?:'m| am| do)?\s+(?:really\s+|absolutely\s+|also\s+|totally\s+|just\s+)?"
        r"(?:love|like|enjoy|prefer|adore|into|a (?:big |huge )?fan of|obsessed with|appreciate"
        r"|tend to prefer)\b(?P<what>.+)", re.I)),
    ("preference", re.compile(r"\bwhich I (?:enjoy|love|like|prefer)\b(?P<what>.*)", re.I)),
    ("dislike", re.compile(
        r"\bI(?:'m| am| do)?\s+(?:really\s+)?(?:hate|dislike|can't stand|cannot stand|don't (?:really )?like"
        r"|not (?:really )?like|avoid|not a fan of|allergic to)\b(?P<what>.+)", re.I)),
    ("favorite", re.compile(r"\bmy (?:all-time )?fav(?:ou|o)rite (?P<what>.+)", re.I)),
    ("identity", re.compile(
        r"\b(?:I(?:'m| am)|as) (?:a|an) (?P<what>(?:[a-z-]+ ){0,3}(?:user|fan|lover|owner|parent|mom|dad"
        r"|student|teacher|engineer|developer|nurse|doctor|artist|writer|designer|vegetarian|vegan"
        r"|comedian|photographer|runner|musician|player|scientist|researcher|manager|chef|cook|athlete"
        r"|beginner|enthusiast|professional|freelancer|volunteer)\b.*)", re.I)),
    ("identity", re.compile(
        r"\bI(?:'m| am|'ve| have| just)? (?:work(?:ing)? (?:as|in|at|on|for)|live in|moved to|grew up in|study"
        r"|studied|majored in"
        r"|graduated (?:from|with))\b(?P<what>.+)", re.I)),
    ("habit", re.compile(r"\bI (?:usually|always|often|normally|typically|never|regularly)\b(?P<what>.+)", re.I)),
    ("plan", re.compile(
        r"\bI(?:'m| am|'ve been| have been)? (?:planning|hoping|going|trying|learning|looking) (?:to|on|for)?\b"
        r"(?P<what>.+)", re.I)),
    ("possession", re.compile(
        r"\b(?:I (?:have|own|got|bought|adopted|recently bought|just bought)|my new|my current)\b(?P<what>.+)", re.I)),
]


@dataclass(frozen=True)
class Fact:
    kind: str
    subject: str
    statement: str
    slot: str | None = None


_SLOTS = [
    (re.compile(r"\bwork(?:ing)? (?:as|at|for)\b", re.I), "job"),
    (re.compile(r"\blive in\b|\bmoved to\b", re.I), "home"),
    (re.compile(r"\b(?:study|studied|majored in)\b", re.I), "study"),
]


def slot_of(kind: str, sentence: str, what: str) -> str | None:
    """What a newer statement of the same kind replaces: one job, one home, one favourite X."""
    if kind == "favorite":
        first = subject_of(what, limit=1)
        return f"favorite:{first}" if first else None
    if kind == "identity":
        for pattern, slot in _SLOTS:
            if pattern.search(sentence):
                return slot
    return None


def subject_of(text: str, limit: int = 6) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", text.lower()) if w not in STOPWORDS]
    return " ".join(words[:limit])


def extract(text: str) -> list[Fact]:
    """Self-descriptive sentences from one user message, at most one fact per sentence."""
    out: list[Fact] = []
    for sentence in _SENTENCES.split(text or ""):
        sentence = sentence.strip()
        if len(sentence) < 8 or sentence.endswith("?") and not re.search(r"\bI\b|\bmy\b", sentence):
            continue
        for kind, pattern in _PATTERNS:
            m = pattern.search(sentence)
            if not m:
                continue
            subject = subject_of(m.group("what"))
            if subject:
                out.append(Fact(kind, subject, sentence[:MAX_STATEMENT], slot_of(kind, sentence, m.group("what"))))
            break
    return out
