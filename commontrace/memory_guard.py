from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_SECRET = "secret"
CATEGORY_PII = "pii"
CATEGORY_INJECTION = "injection"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

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

_SECRET_PATTERNS_MEDIUM: tuple[tuple[str, re.Pattern], ...] = (
    ("possible credential assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|"
        r"private[_-]?key|passwd|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}['\"]?"
    )),
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
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

_HIDDEN_CODEPOINTS = (
    0x200B,
    0x200C,
    0x200D,
    0x2060,
    0xFEFF,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
)
_HIDDEN_CHARS_RE = re.compile("[" + "".join(chr(cp) for cp in _HIDDEN_CODEPOINTS) + "]")


@dataclass(frozen=True)
class Finding:
    category: str
    confidence: str
    label: str
    field: str
    start: int
    end: int
    excerpt: str

    @property
    def blocking(self) -> bool:
        if self.category == CATEGORY_PII:
            return False
        return self.confidence == CONFIDENCE_HIGH


def _redact(text: str, start: int, end: int) -> str:
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
    found: list[str] = []
    if not text:
        return text, found
    for label, pattern in _SECRET_PATTERNS_HIGH:
        def _mark(match, label=label):
            found.append(label)
            return f"[REDACTED {label}]"
        text = pattern.sub(_mark, text)
    return text, found


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Actively mask PII in *text*, returning ``(masked_text, labels)``.

    Unlike :func:`scan_text` which only reports findings, this function
    replaces PII in the string with placeholder tokens so the original data
    never touches storage.  Adapted from the Supermemory / Mem0 ingestion
    protection pattern.

    Masked categories:
    - Email addresses → ``[REDACTED email]``
    - Phone numbers → ``[REDACTED phone]``
    - US SSN-shaped numbers → ``[REDACTED ssn]``
    - Luhn-valid card numbers → ``[REDACTED card]``
    """
    if not text:
        return text, []
    found: list[str] = []

    def _sub(pattern: re.Pattern, label: str, text: str) -> str:
        def _mark(m: re.Match) -> str:
            found.append(label)
            return f"[REDACTED {label}]"
        return pattern.sub(_mark, text)

    text = _sub(_EMAIL_RE, "email", text)
    text = _sub(_PHONE_RE, "phone", text)
    text = _sub(_SSN_RE, "ssn", text)

    # Card numbers need Luhn validation before redacting.
    def _card_mark(m: re.Match) -> str:
        digits = re.sub(r"[ -]", "", m.group())
        if _luhn_ok(digits):
            found.append("card")
            return "[REDACTED card]"
        return m.group()

    text = _CARD_CANDIDATE_RE.sub(_card_mark, text)
    return text, found


def scan_text(text: str, field: str = "") -> list[Finding]:
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

    findings.extend(scan_injection(text, field))
    return findings


def scan_injection(text: str, field: str = "") -> list[Finding]:
    if not text:
        return []
    findings: list[Finding] = []
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
        if not self.findings:
            return ""
        by_category: dict[str, int] = {}
        for f in self.findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1
        parts = [f"{n} {cat}" + ("s" if n != 1 else "") for cat, n in sorted(by_category.items())]
        return "content safety scan flagged: " + ", ".join(parts)


def scan_fields(fields: dict) -> GuardReport:
    findings: list[Finding] = []
    for name, value in fields.items():
        if isinstance(value, str) and value:
            findings.extend(scan_text(value, field=name))
    return GuardReport(findings=findings)
