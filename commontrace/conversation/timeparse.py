"""Time in conversation: when a message was said, what its relative time words
point at, and which window a question asks about. Deterministic, no model."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
MONTH_NUMBER = {**{m: i for i, m in enumerate(MONTHS, 1)},
                **{m[:3]: i for i, m in enumerate(MONTHS, 1)}, "sept": 9}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
                "a couple of": 2, "a couple": 2, "couple of": 2, "a few": 3, "few": 3, "several": 3}
SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11), "winter": (12, 2)}

_MONTH_RE = "|".join(sorted(MONTH_NUMBER, key=len, reverse=True))
_NUM_RE = r"\d{1,3}|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(WEEKDAYS)


def label(day: dt.date) -> str:
    return f"{day.day} {MONTHS[day.month - 1].capitalize()} {day.year}"


def month_label(year: int, month: int) -> str:
    return f"{MONTHS[month - 1].capitalize()} {year}"


def _month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - dt.timedelta(days=1)


def _add_months(day: dt.date, months: int) -> tuple[int, int]:
    index = day.year * 12 + day.month - 1 + months
    return index // 12, index % 12 + 1


def _number(word: str) -> int:
    word = word.lower().strip()
    return int(word) if word.isdigit() else NUMBER_WORDS.get(word, 0)


# --- when a message was said ---------------------------------------------------

_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?"
                  r"\s*(Z|[+-]\d{2}:?\d{2})?\s*$")
_SLASHED = re.compile(r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})(?:\s*\(\w+\))?(?:\s+(\d{1,2}):(\d{2}))?\s*$")
_SPOKEN = re.compile(
    rf"^\s*(?:(\d{{1,2}})(?::(\d{{2}}))?\s*([ap])\.?m\.?\s*(?:on\s+)?)?"
    rf"(?:(?:{_WEEKDAY_RE}),?\s+)?(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?|({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?),?\s+(\d{{4}})\s*$",
    re.I)


def parse_moment(text: str | None) -> dt.datetime | None:
    """A wall-clock moment from the formats conversations carry, or None."""
    if not text:
        return None
    text = str(text)
    m = _ISO.match(text)
    if m:
        y, mo, d, hh, mm, ss, tz = m.groups()
        try:
            moment = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
        except ValueError:
            return None
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            moment -= sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return moment
    m = _SLASHED.match(text)
    if m:
        y, mo, d, hh, mm = m.groups()
        try:
            return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
        except ValueError:
            return None
    m = _SPOKEN.match(text)
    if m:
        hh, mm, ampm, d1, mon1, mon2, d2, y = m.groups()
        hour = int(hh or 0) % 12 + (12 if (ampm or "").lower() == "p" else 0)
        try:
            return dt.datetime(int(y), MONTH_NUMBER[(mon1 or mon2).lower().rstrip(".")],
                               int(d1 or d2), hour, int(mm or 0))
        except ValueError:
            return None
    return None


# --- what a message's relative time words point at -----------------------------

@dataclass(frozen=True)
class Grounding:
    start: int
    end: int
    phrase: str
    label: str
    lo: dt.date
    hi: dt.date


def _weekday_before(anchor: dt.date, weekday: int, strictly: bool = True) -> dt.date:
    back = (anchor.weekday() - weekday) % 7
    if back == 0 and strictly:
        back = 7
    return anchor - dt.timedelta(days=back)


def _season(year: int, name: str) -> tuple[dt.date, dt.date]:
    first, last = SEASONS[name]
    if first > last:
        return dt.date(year, first, 1), _month_bounds(year + 1, last)[1]
    return dt.date(year, first, 1), _month_bounds(year, last)[1]


def _unit_span(anchor: dt.date, unit: str, n: int, sign: int) -> tuple[str, dt.date, dt.date]:
    if unit == "day":
        day = anchor + dt.timedelta(days=sign * n)
        return label(day), day, day
    if unit == "week":
        day = anchor + dt.timedelta(weeks=sign * n)
        return f"the week of {label(day)}", day - dt.timedelta(days=3), day + dt.timedelta(days=3)
    if unit == "month":
        y, m = _add_months(anchor, sign * n)
        lo, hi = _month_bounds(y, m)
        return month_label(y, m), lo, hi
    year = anchor.year + sign * n
    return str(year), dt.date(year, 1, 1), dt.date(year, 12, 31)


_RELATIVE = re.compile(
    r"\b(?P<dbyest>the day before yesterday)\b"
    r"|\b(?P<dafter>the day after tomorrow)\b"
    r"|\b(?P<today>today|tonight|this (?:morning|afternoon|evening))\b"
    r"|\b(?P<yest>yesterday|last night)\b"
    r"|\b(?P<tom>tomorrow)\b"
    rf"|\b(?P<agon>{_NUM_RE})\s+(?P<agou>day|week|month|year)s?\s+ago\b"
    rf"|\bin\s+(?P<inn>{_NUM_RE})\s+(?P<inu>day|week|month|year)s?\b"
    r"|\b(?P<rel>last|this|next|past|coming)\s+(?P<relu>week|weekend|month|year)\b"
    rf"|\b(?P<wrel>last|this|next|on|this past|this coming)\s+(?P<wday>{_WEEKDAY_RE})\b"
    r"|\b(?P<srel>last|this|next)\s+(?P<season>spring|summer|fall|autumn|winter)\b"
    r"|\b(?P<lately>recently|the other day|lately)\b",
    re.I)


_FUTURE = re.compile(r"\b(?:will|'ll|going to|gonna|plan(?:ning)?|hope to|want to|can't wait|is|are|am|'s|'re"
                     r"|starts?|begins?|tomorrow|next|upcoming|coming)\b", re.I)
_PAST = re.compile(r"\b(?:was|were|went|had|did|got|made|saw|took|came|ran|ate|bought|met|said|told"
                   r"|[a-z]+ed)\b", re.I)


def _future_tense(text: str, start: int, end: int) -> bool:
    lo = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start)) + 1
    hi = min([i for i in (text.find(".", end), text.find("!", end), text.find("?", end)) if i >= 0] or [len(text)])
    clause = text[lo:hi]
    return bool(_FUTURE.search(clause)) and not _PAST.search(clause)


def ground(text: str, anchor: dt.date | dt.datetime | None) -> list[Grounding]:
    """Absolute dates for the relative time words in `text`, said on `anchor`."""
    if not text or anchor is None:
        return []
    if isinstance(anchor, dt.datetime):
        anchor = anchor.date()
    out: list[Grounding] = []
    for m in _RELATIVE.finditer(text):
        g = m.groupdict()
        lo = hi = None
        if g["dbyest"]:
            lo = hi = anchor - dt.timedelta(days=2)
            text_label = label(lo)
        elif g["dafter"]:
            lo = hi = anchor + dt.timedelta(days=2)
            text_label = label(lo)
        elif g["today"]:
            lo = hi = anchor
            text_label = label(lo)
        elif g["yest"]:
            lo = hi = anchor - dt.timedelta(days=1)
            text_label = label(lo)
        elif g["tom"]:
            lo = hi = anchor + dt.timedelta(days=1)
            text_label = label(lo)
        elif g["agon"]:
            n = _number(g["agon"])
            if not n:
                continue
            text_label, lo, hi = _unit_span(anchor, g["agou"].lower(), n, -1)
            if n >= 3 and not g["agon"].isdigit():
                text_label = f"about {text_label}"
        elif g["inn"]:
            n = _number(g["inn"])
            if not n:
                continue
            text_label, lo, hi = _unit_span(anchor, g["inu"].lower(), n, +1)
        elif g["rel"]:
            which, unit = g["rel"].lower(), g["relu"].lower()
            sign = {"last": -1, "past": -1, "this": 0, "next": 1, "coming": 1}[which]
            if unit == "week":
                if which == "past":
                    lo, hi = anchor - dt.timedelta(days=7), anchor
                    text_label = f"the week before {label(anchor)}"
                elif sign < 0:
                    lo = anchor - dt.timedelta(days=7 + anchor.weekday())
                    hi = anchor - dt.timedelta(days=anchor.weekday() + 1)
                    text_label = f"the week before {label(anchor)}"
                elif sign > 0:
                    lo = anchor + dt.timedelta(days=7 - anchor.weekday())
                    hi = lo + dt.timedelta(days=6)
                    text_label = f"the week after {label(anchor)}"
                else:
                    lo = anchor - dt.timedelta(days=anchor.weekday())
                    hi = lo + dt.timedelta(days=6)
                    text_label = f"the week of {label(anchor)}"
            elif unit == "weekend":
                weekend = anchor.weekday() >= 5
                if sign < 0 or which == "past":
                    saturday = _weekday_before(anchor, 5) - dt.timedelta(days=7 if weekend else 0)
                elif sign == 0 and weekend:
                    saturday = anchor - dt.timedelta(days=anchor.weekday() - 5)
                else:
                    saturday = anchor + dt.timedelta(days=(5 - anchor.weekday()) % 7 or 7)
                lo, hi = saturday, saturday + dt.timedelta(days=1)
                text_label = f"the weekend of {label(saturday)}"
            elif unit == "month":
                y, mo = _add_months(anchor, -1 if sign < 0 else sign)
                lo, hi = _month_bounds(y, mo)
                text_label = month_label(y, mo)
            else:
                year = anchor.year + (-1 if sign < 0 else sign)
                lo, hi = dt.date(year, 1, 1), dt.date(year, 12, 31)
                text_label = str(year)
        elif g["wrel"]:
            which, weekday = g["wrel"].lower(), WEEKDAYS.index(g["wday"].lower())
            ahead = which in ("next", "this coming") or (
                which in ("this", "on") and _future_tense(text, m.start(), m.end()))
            if ahead:
                day = anchor + dt.timedelta(days=(weekday - anchor.weekday()) % 7 or 7)
            else:
                day = _weekday_before(anchor, weekday, strictly=which != "this")
            lo = hi = day
            text_label = f"{WEEKDAYS[weekday].capitalize()} {label(day)}"
        elif g["srel"]:
            which, season = g["srel"].lower(), g["season"].lower()
            spans = [_season(y, season) for y in range(anchor.year - 2, anchor.year + 2)]
            if which == "last":
                lo, hi = [sp for sp in spans if sp[1] < anchor][-1]
            elif which == "next":
                lo, hi = next(sp for sp in spans if sp[0] > anchor)
            else:
                lo, hi = next((sp for sp in spans if sp[0] <= anchor <= sp[1]),
                              next(sp for sp in spans if sp[0] > anchor))
            text_label = f"{season} {lo.year}"
        elif g["lately"]:
            lo, hi = anchor - dt.timedelta(days=14), anchor
            text_label = f"shortly before {label(anchor)}"
        if lo is None:
            continue
        out.append(Grounding(m.start(), m.end(), m.group(0), text_label, lo, hi))
    return out


def annotate(text: str, groundings: list[Grounding]) -> str:
    """`text` with each grounded phrase followed by its absolute date."""
    if not groundings:
        return text
    pieces, cursor = [], 0
    for g in groundings:
        pieces.append(text[cursor:g.end])
        pieces.append(f" [{g.label}]")
        cursor = g.end
    pieces.append(text[cursor:])
    return "".join(pieces)


# --- which window a question asks about ----------------------------------------

_Q_EXACT = re.compile(
    rf"\b(?:on\s+)?(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})|({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?),?"
    rf"\s+(\d{{4}})\b", re.I)
_Q_MONTH = re.compile(rf"\b(?:in\s+|during\s+)?({_MONTH_RE})(?:\s+of)?,?\s+(\d{{4}})\b", re.I)
_Q_YEAR = re.compile(r"\b(?:in|during|of|since|before|after)\s+((?:19|20)\d{2})\b", re.I)
_Q_PAST = re.compile(rf"\b(?:in|over|during)\s+the\s+(?:past|last)\s+(?:({_NUM_RE})\s+)?(day|week|month|year)s?\b",
                     re.I)


def question_window(question: str, now: dt.date | dt.datetime | None) -> tuple[dt.date, dt.date, str] | None:
    """The date range a question restricts itself to, if it names one."""
    if not question:
        return None
    if isinstance(now, dt.datetime):
        now = now.date()
    m = _Q_EXACT.search(question)
    if m:
        d1, mon1, mon2, d2, y = m.groups()
        try:
            day = dt.date(int(y), MONTH_NUMBER[(mon1 or mon2).lower()], int(d1 or d2))
        except ValueError:
            day = None
        if day:
            return day, day, label(day)
    m = _Q_MONTH.search(question)
    may_verb = m and m.group(1).lower() == "may" and not re.search(
        r"\b(?:in|during|of)\s+may\b|\bmay\s+(?:of\s+)?\d{4}", question, re.I)
    if m and not may_verb:
        y, mo = int(m.group(2)), MONTH_NUMBER[m.group(1).lower()]
        lo, hi = _month_bounds(y, mo)
        return lo, hi, month_label(y, mo)
    m = _Q_YEAR.search(question)
    if m:
        year = int(m.group(1))
        return dt.date(year, 1, 1), dt.date(year, 12, 31), str(year)
    if now is None:
        return None
    m = _Q_PAST.search(question)
    if m:
        n = _number(m.group(1)) if m.group(1) else 1
        unit = m.group(2).lower()
        days = {"day": 1, "week": 7, "month": 31, "year": 366}[unit] * max(n, 1)
        return now - dt.timedelta(days=days), now, f"the {m.group(0).split('the ', 1)[1]}"
    for g in ground(question, now):
        if g.hi <= now or g.lo <= now:
            return g.lo, g.hi, g.label
    return None
