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
_DIRECTIVE_VERBS = (r"(?:use|format|include|add|give|show|write|respond|reply|answer|explain|provide|keep|mention"
                    r"|make|ask|suggest|recommend|list|start|end|put|wrap|label|highlight|cite|send|call|refer"
                    r"|address|avoid|stick|limit|break|number|summari[sz]e|structure|present|check|remind)")
_PATTERNS = [
    # standing instructions to the assistant: kept and shown with every recall
    ("instruction", re.compile(
        r"(?:^|[.!?]\s+|\b(?:please|and|also)\s+)(?P<what>(?:always|never|don't ever|do not ever)\s+"
        + _DIRECTIVE_VERBS + r"\b.+)", re.I)),
    ("instruction", re.compile(
        r"\b(?P<what>(?:from now on|going forward|in (?:all )?future (?:answers|responses|replies)|whenever I ask"
        r"|every time I ask|each time I ask|when(?:ever)? you (?:answer|respond|reply|explain|suggest|write))\b.+)",
        re.I)),
    ("instruction", re.compile(
        r"\b(?P<what>(?:I (?:want|need|would like|'d like) you to|make sure (?:to|you)|be sure to|remember to"
        r"|please (?:don't|do not|avoid|stop|always|never|keep|make sure))\b.+)", re.I)),
    ("preference", re.compile(
        r"\bI(?:'d| would)\s+(?:much\s+|really\s+)?(?:prefer|rather|like it if|like to keep)\b(?P<what>.+)", re.I)),
    ("preference", re.compile(
        r"\bI(?:'m| am)\s+(?:more\s+)?(?:comfortable with|keen on|leaning towards?|partial to)\b(?P<what>.+)", re.I)),
    ("preference", re.compile(
        r"\bI(?:'m| am| do)?\s+(?:really\s+|absolutely\s+|also\s+|totally\s+|just\s+)?"
        r"(?:love|like|enjoy|prefer|adore|into|a (?:big |huge )?fan of|obsessed with|appreciate"
        r"|tend to prefer)\b(?P<what>.+)", re.I)),
    ("preference", re.compile(r"\bwhich I (?:enjoy|love|like|prefer)\b(?P<what>.*)", re.I)),
    ("dislike", re.compile(
        r"\bI(?:'m| am| do)?\s+(?:really\s+)?(?:hate|dislike|can't stand|cannot stand|don't (?:really )?like"
        r"|not (?:really )?like|avoid|not a fan of|allergic to)\b(?P<what>.+)", re.I)),
    ("favorite", re.compile(r"\bmy (?:all-time )?fav(?:ou|o)rite (?P<what>.+)", re.I)),
    ("identity", re.compile(  # a self-introduction: "I'm Craig, a 44-year-old colour technologist"
        r"\b(?:I(?:'m| am)|my name is|this is) [A-Z][a-z]+(?: [A-Z][a-z]+)?,? (?:an?|the) (?P<what>[^,.;!?]{3,80})")),
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


_NAME = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s+(?:[A-Z][a-z]+|of|de|van|von))*(?:\s+[A-Z][a-z]+)?")
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_QUOTED = re.compile(r"[\"“]([^\"”]{3,60})[\"”]")
_NOT_NAMES = frozenset("""i im ive id ill hey hi hello oh ok okay yes no wow thanks thank sure yeah great nice cool well
so also maybe sorry please congrats congratulations lol haha what when where who why how which that this
these those there here it its my your our their his her mr mrs ms dr""".split())


def entities(text: str) -> set[str]:
    """Names and titles a message mentions, lower-cased: proper nouns (a capitalised word
    that opens a sentence counts only when it is not an ordinary word) and quoted titles."""
    out: set[str] = set()
    for m in _NAME.finditer(text or ""):
        name = re.sub(r"'s$", "", m.group(0)).strip()
        words = name.lower().split()
        while words and (words[0] in _NOT_NAMES or words[0] in STOPWORDS):
            words = words[1:]
        if not words or len(" ".join(words)) < 3:
            continue
        start = m.start()
        opens_sentence = start == 0 or re.search(r"[.!?]\s*$", text[:start]) is not None
        if opens_sentence and len(words) == 1 and words[0] in _COMMON:
            continue
        out.add(" ".join(words))
    for m in _QUOTED.finditer(text or ""):
        out.add(re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip().lower()))
    out.update(a.lower() for a in _ACRONYM.findall(text or "") if a not in ("OK", "TV", "LOL", "OMG"))
    return {e for e in out if e not in STOPWORDS}


_COMMON = frozenset("""today yesterday tomorrow tonight just really actually honestly anyway everyone someone
nothing something anything everything going got good bad love like thinking last next first one two three
lately recently definitely absolutely totally speaking talking looking working planning trying hoping
tell show give describe list name explain summarize summarise recommend suggest remind find help can could
would will did does do have has had is are was were let please any some many much every each after before
since during because although though while if then than""".split())
