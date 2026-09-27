"""Screen free-text lesson/trace content for the three things that turn a
memory store into an attack surface, before it is stored or injected.

WHY THIS EXISTS
---------------
OWASP's Agentic Top 10 names Memory & Context Poisoning (ASI06): an agent
that reads its own past experience back into a live decision is exposed to
whatever got written into that experience, whether it arrived by an
honest mistake or a deliberate attempt to steer a future agent. CommonTrace
is exactly that shape of system on both sides of the boundary --
`commontrace/import_data.py`/`failure_import.py` capture text from
wherever a fleet's logs came from, and an `active` lesson is injected into
every later retrieval **verbatim** (`commontrace/mcp_server.py:
approve_lesson`'s own docstring). Nothing between those two points ever
looked at what the text actually said.

This module closes that gap with three narrow, independent checks:

  1. **Secrets.** A captured trace can carry a credential that leaked into
     a log line the same way it leaks into any other log -- an AWS key, a
     GitHub token, a private key block. Storing it turns a memory corpus
     into a second place that credential now lives, readable by every
     later agent and every operator with corpus access.
  2. **PII.** An email address, phone number, or something that Luhn-checks
     as a card number does not belong in a *generalized rule* -- CommonTrace's
     whole pitch is that a lesson is a reusable procedure, not a stored
     episode, and a specific person's details are exactly the episode this
     product exists to abstract away from.
  3. **Prompt injection.** Text engineered to be read by a future *agent*,
     not a future *person* -- "ignore previous instructions", a forged
     system-prompt block, characters chosen to be invisible in a UI but
     present in the string an LLM reads. A lesson is the one kind of
     content in this product that a live decision reads unmediated; this is
     the category ASI06 is actually about.

WHAT THIS IS NOT
----------------
Not a moderation system and not exhaustive, the same limitation
`hub/abuse.py:suspicion_reason` states about its own spam heuristic: these
are pattern matches, not semantic understanding, and an adversary who knows
the patterns can phrase around them. What it changes is the *default* --
today nothing here is checked at all, so a well-known credential prefix or
a textbook injection phrase currently passes uninspected all the way to
`active`. Catching the well-known shapes is a real reduction in blast
radius even though it is not a ceiling on what a targeted attacker could
still get through, and every finding is named specifically enough that a
human reviewing a quarantined trace or a refused lesson can see exactly
what tripped it and judge the false-positive cases for themselves.

Every pattern below is intentionally conservative about SECRET and
INJECTION findings (the two categories callers may use to block or refuse
outright) -- optimized for "if this matches, something is really wrong"
over "catch every possible secret", because the cost of a false positive
here is refusing a legitimate lesson or quarantining a legitimate trace,
and this module has no way to tell the difference between that and a
correct refusal on its own. PII findings are deliberately never used to
block anything in this module -- see Finding.confidence and `blocking`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_SECRET = "secret"
CATEGORY_PII = "pii"
CATEGORY_INJECTION = "injection"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

# --- Secrets -----------------------------------------------------------
#
# HIGH confidence: a structured, provider-specific token shape. These are
# vanishingly unlikely to occur in ordinary prose by chance -- the false
# positive this module has to weigh is "a real credential", not "text that
# happens to look like one" -- so findings in this tier are safe to BLOCK
# on, not just flag.
_SECRET_PATTERNS_HIGH: tuple[tuple[str, re.Pattern], ...] = (
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe secret key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{24,}\b")),
    ("Stripe webhook signing secret", re.compile(r"\bwhsec_[A-Za-z0-9]{32,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("PEM private key block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
    )),
    ("JSON Web Token", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    )),
)

# MEDIUM confidence: a keyword=value assignment shape that is genuinely
# common in innocent contexts too (a code snippet showing where a key
# GOES, a redacted example, a variable name that merely mentions "token").
# Flagged, never used to block on its own -- see `blocking` below.
_SECRET_PATTERNS_MEDIUM: tuple[tuple[str, re.Pattern], ...] = (
    ("possible credential assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|"
        r"private[_-]?key|passwd|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}['\"]?"
    )),
)

# --- PII -----------------------------------------------------------------
#
# Never used to block -- see `blocking`. A support lesson legitimately
# discusses email fields, phone fields, and payment flows; the point is to
# surface a specific PERSON'S details ending up in a generalized rule for a
# human to judge, not to refuse every lesson that mentions the concept.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Candidate card numbers: 13-19 digits, optionally grouped with spaces/dashes.
# Luhn-checked below before being reported at all -- an ungrouped 16-digit
# number that fails Luhn is far more likely a trace/session/order id than a
# card, and reporting it anyway would make this category noise.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# --- Prompt injection ------------------------------------------------------
#
# HIGH confidence only, deliberately: every pattern here is a phrase or a
# character class with essentially no legitimate reason to appear in a
# lesson written for a human co-author, only for an agent that will read
# the raw string. Findings in this category BLOCK, same as high-confidence
# secrets, for the same reason: an active lesson is injected verbatim into
# every later retrieval, so this is the one category that is dangerous
# specifically BECAUSE this product's own injection step exists.
_INJECTION_PHRASE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("instruction-override phrasing", re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?"
        r"(?:previous|prior|above|earlier|the\s+above)\s+"
        r"(?:instructions?|rules?|prompts?|context)\b"
    )),
    ("forged system-role block", re.compile(
        r"(?i)(?:^|\n)\s*(?:system|assistant)\s*(?:prompt)?\s*[:=]"
    )),
    ("fabricated new-instructions block", re.compile(
        r"(?i)\bnew\s+instructions\s*:"
    )),
    ("jailbreak-style persona override", re.compile(
        r"(?i)\byou\s+are\s+now\b[^.\n]{0,60}\b"
        r"(?:dan|jailbreak(?:ed)?|unrestricted|no\s+rules|without\s+restrictions|"
        r"free\s+from\s+(?:all\s+)?restrictions?)\b"
    )),
    ("smuggled instruction comment", re.compile(
        r"(?i)<!--\s*(?:system|instruction|ignore)[^>]{0,200}-->", re.DOTALL
    )),
)

# Characters with no legitimate reason to appear in lesson prose: zero-width
# spacers and joiners used to hide text from a human reviewer while leaving
# it intact for the model reading the raw string, and bidirectional-override
# controls used the same way text-spoofing attacks use them elsewhere.
#
# LISTED AS NUMERIC CODEPOINTS, NOT AS LITERAL CHARACTERS, and that is not a
# style preference. A source file holding these characters literally is
# unreviewable in exactly the way this check exists to catch: they are
# invisible in a diff, so no reviewer could see which codepoints the class
# actually held, or notice one being added or quietly dropped. It is also
# the finding bandit's B613 (trojansource, CWE-838) raises against any
# source file carrying bidirectional controls -- and suppressing that on the
# one module whose whole job is to find them would be precisely backwards.
# The compiled pattern is identical to the literal form; only the source
# spelling changes.
_HIDDEN_CODEPOINTS = (
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x2060,  # WORD JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (byte-order mark)
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x2066,  # LEFT-TO-RIGHT ISOLATE
    0x2067,  # RIGHT-TO-LEFT ISOLATE
    0x2068,  # FIRST STRONG ISOLATE
    0x2069,  # POP DIRECTIONAL ISOLATE
)
_HIDDEN_CHARS_RE = re.compile("[" + "".join(chr(cp) for cp in _HIDDEN_CODEPOINTS) + "]")


@dataclass(frozen=True)
class Finding:
    category: str        # CATEGORY_SECRET / CATEGORY_PII / CATEGORY_INJECTION
    confidence: str       # CONFIDENCE_HIGH / CONFIDENCE_MEDIUM
    label: str             # human-readable name of what matched
    field: str             # which input field this was found in
    start: int
    end: int
    excerpt: str            # REDACTED preview -- never the raw matched text

    @property
    def blocking(self) -> bool:
        """Whether this single finding, on its own, is severe enough to
        refuse the content outright. High-confidence secrets and every
        injection finding qualify; PII and medium-confidence secrets never
        do -- they are surfaced for a human to weigh, not acted on alone."""
        if self.category == CATEGORY_PII:
            return False
        return self.confidence == CONFIDENCE_HIGH


def _redact(text: str, start: int, end: int) -> str:
    """A preview safe to print in a UI, a log line, or an approval refusal
    -- never the raw matched text. Long enough to let a reviewer recognize
    what kind of thing matched (a key, an email, a phrase), short enough
    and masked enough that the excerpt itself cannot leak the secret it is
    reporting on."""
    matched = text[start:end]
    if len(matched) <= 8:
        body = matched[:2] + "..." if len(matched) > 2 else "..."
    else:
        body = f"{matched[:4]}...{matched[-2:]}"
    lo = max(0, start - 12)
    hi = min(len(text), end + 12)
    prefix = ("..." if lo > 0 else "") + text[lo:start]
    suffix = text[end:hi] + ("..." if hi < len(text) else "")
    return f"{prefix}{body}{suffix}".replace("\n", " ")


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """`text` with every HIGH-confidence credential replaced by a marker
    naming its kind, and the kinds found (in order, one per match).

    For raw experience (a captured or imported trace), where refusing the
    whole text would lose the rest of what happened: the credential is the
    one part nobody needs to keep, and keeping it makes the memory store a
    second place it now lives -- readable by every later agent, and pushed
    to the Hub by `sync`. Only the high-confidence shapes are redacted
    (AWS/GitHub/Slack/Stripe/Google/Anthropic keys, PEM blocks, JWTs), so a
    false positive costs a few characters, never the trace."""
    found: list[str] = []
    if not text:
        return text, found
    for label, pattern in _SECRET_PATTERNS_HIGH:
        def _mark(match, label=label):
            found.append(label)
            return f"[REDACTED {label}]"
        text = pattern.sub(_mark, text)
    return text, found


def scan_text(text: str, field: str = "") -> list[Finding]:
    """Every finding in one string. Overlap between patterns is expected
    and left in -- a JWT-shaped string inside a "token=..." assignment is
    legitimately two findings, and de-duplicating them would hide exactly
    the case where two independent checks agree something is wrong."""
    if not text:
        return []
    findings: list[Finding] = []

    for label, pattern in _SECRET_PATTERNS_HIGH:
        for m in pattern.finditer(text):
            findings.append(Finding(
                CATEGORY_SECRET, CONFIDENCE_HIGH, label, field,
                m.start(), m.end(), _redact(text, m.start(), m.end()),
            ))
    for label, pattern in _SECRET_PATTERNS_MEDIUM:
        for m in pattern.finditer(text):
            findings.append(Finding(
                CATEGORY_SECRET, CONFIDENCE_MEDIUM, label, field,
                m.start(), m.end(), _redact(text, m.start(), m.end()),
            ))

    for m in _EMAIL_RE.finditer(text):
        findings.append(Finding(
            CATEGORY_PII, CONFIDENCE_HIGH, "email address", field,
            m.start(), m.end(), _redact(text, m.start(), m.end()),
        ))
    for m in _PHONE_RE.finditer(text):
        findings.append(Finding(
            CATEGORY_PII, CONFIDENCE_MEDIUM, "phone number", field,
            m.start(), m.end(), _redact(text, m.start(), m.end()),
        ))
    for m in _SSN_RE.finditer(text):
        findings.append(Finding(
            CATEGORY_PII, CONFIDENCE_MEDIUM, "US SSN-shaped number", field,
            m.start(), m.end(), _redact(text, m.start(), m.end()),
        ))
    for m in _CARD_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if _luhn_ok(digits):
            findings.append(Finding(
                CATEGORY_PII, CONFIDENCE_HIGH, "card number (Luhn-valid)", field,
                m.start(), m.end(), _redact(text, m.start(), m.end()),
            ))

    for label, pattern in _INJECTION_PHRASE_PATTERNS:
        for m in pattern.finditer(text):
            findings.append(Finding(
                CATEGORY_INJECTION, CONFIDENCE_HIGH, label, field,
                m.start(), m.end(), _redact(text, m.start(), m.end()),
            ))
    for m in _HIDDEN_CHARS_RE.finditer(text):
        findings.append(Finding(
            CATEGORY_INJECTION, CONFIDENCE_HIGH,
            "hidden/bidi-override Unicode character", field,
            m.start(), m.end(), _redact(text, m.start(), m.end()),
        ))

    return findings


@dataclass(frozen=True)
class GuardReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def should_block(self) -> bool:
        return bool(self.blocking_findings)

    def summary(self) -> str:
        """One short line naming what was found, in the same register as
        `hub/abuse.py:suspicion_reason`'s return value -- suitable to store
        directly as a quarantine reason or surface in a refusal message."""
        if not self.findings:
            return ""
        by_category: dict[str, int] = {}
        for f in self.findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1
        parts = [f"{n} {cat}" + ("s" if n != 1 else "") for cat, n in sorted(by_category.items())]
        return "content safety scan flagged: " + ", ".join(parts)


def scan_fields(fields: dict) -> GuardReport:
    """Scan every string value in `fields` (e.g. {"title": ..., "context_text":
    ..., "rule": ...}) and return one combined report. Non-string values
    (None, missing keys) are skipped rather than raising -- callers pass
    whatever subset of a trace/lesson's text fields they have on hand."""
    findings: list[Finding] = []
    for name, value in fields.items():
        if isinstance(value, str) and value:
            findings.extend(scan_text(value, field=name))
    return GuardReport(findings=findings)
