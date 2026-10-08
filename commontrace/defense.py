"""Pluggable memory-defense screen for secret and PII detection.

Adapted from Hindsight and Supermemory architecture:
- 40+ pattern catalog across AI/LLM keys, cloud providers, CI/CD tokens, payment gateways,
  messaging bots, database URLs, PEM private keys, JWTs, and PII.
- Length-aware fingerprinted previews (prefix...suffix) that never leak raw secrets.
- Luhn-validated credit card detection with UUID-neighborhood suppression.
- Flexible policy actions: ALLOW, REDACT, BLOCK.
- Pluggable extension contract with audit-ready structured findings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DefenseAction(str, Enum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


_VALID_ACTIONS = {a.value for a in DefenseAction}


@dataclass(frozen=True)
class PolicyRule:
    on: str
    action: DefenseAction


@dataclass(frozen=True)
class DefensePolicy:
    enabled: bool = True
    rules: tuple[PolicyRule, ...] = (
        PolicyRule(on="sensitive_data", action=DefenseAction.REDACT),
    )


@dataclass
class DefenseDecision:
    action: DefenseAction
    detector: str | None = None
    message: str = ""
    redacted_content: str | None = None
    matched_types: list[str] = field(default_factory=list)
    hits: list[dict[str, str]] = field(default_factory=list)


@dataclass
class RedactionResult:
    content: str
    matched_types: list[str]
    hits: list[dict[str, str]] = field(default_factory=list)


def _fingerprint_value(value: str) -> str:
    """Return a redaction-identifiable preview of a matched value.

    The preview keeps the prefix and a short suffix so a SIEM operator can
    correlate against their credential inventory without the raw secret crossing
    the wire. Length-aware so short values don't leak material:
    - Length < 6: return fixed-length mask '[redacted]'.
    - Length 6-15: keep first 2 + last 2 around an ellipsis.
    - Length > 15: keep first 4 + last 4 around an ellipsis.
    """
    n = len(value)
    if n < 6:
        return "[redacted]"
    if n <= 15:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


def parse_policy(raw: dict | None) -> DefensePolicy:
    """Parse a raw config dict into a frozen DefensePolicy."""
    if raw is None:
        return DefensePolicy()
    if not isinstance(raw, dict):
        raise ValueError("defense policy must be an object")

    raw_rules = raw.get("rules", []) or []
    if not isinstance(raw_rules, (list, tuple)):
        raise ValueError("defense policy rules must be a list")
    rules: list[PolicyRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError("each defense policy rule must be an object")
        on_raw = item.get("on")
        if not isinstance(on_raw, str) or not on_raw:
            raise ValueError(f"invalid on {on_raw!r}; must be a non-empty string")
        action_raw = item.get("action")
        if action_raw not in _VALID_ACTIONS:
            raise ValueError(f"invalid action {action_raw!r}; must be one of {sorted(_VALID_ACTIONS)}")
        rules.append(PolicyRule(on=on_raw, action=DefenseAction(action_raw)))

    return DefensePolicy(
        enabled=bool(raw.get("enabled", True)),
        rules=tuple(rules),
    )


# ASCII token boundaries preventing CJK character misinterpretation
_ASCII_TOKEN_START = r"(?<![A-Za-z0-9_])"
_ASCII_TOKEN_END = r"(?![A-Za-z0-9_])"


def _ascii_token_pattern(body: str) -> str:
    """Wrap an ASCII token pattern without treating non-ASCII/CJK letters as token chars."""
    return f"{_ASCII_TOKEN_START}{body}{_ASCII_TOKEN_END}"


# Comprehensive 40+ pattern catalog
_REDACTION_PATTERNS: list[tuple[str, str]] = [
    # AI / LLM providers
    ("anthropic_key", _ascii_token_pattern(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_project_key", _ascii_token_pattern(r"sk-proj-[A-Za-z0-9_-]{48,}")),
    ("openai_admin_key", _ascii_token_pattern(r"sk-admin-[A-Za-z0-9_-]{40,}")),
    ("openai_key", _ascii_token_pattern(r"sk-[A-Za-z0-9_-]{20,}")),
    ("google_api_key", _ascii_token_pattern(r"AIza[0-9A-Za-z_-]{35}")),
    ("google_oauth_token", _ascii_token_pattern(r"ya29\.[0-9A-Za-z_-]{20,}")),
    ("xai_key", _ascii_token_pattern(r"xai-[A-Za-z0-9]{40,}")),
    ("groq_key", _ascii_token_pattern(r"gsk_[A-Za-z0-9]{20,}")),
    ("commontrace_key", _ascii_token_pattern(r"ctk_(?:sys_)?[0-9a-f]{32}(?:_[0-9a-f]{8,})?")),
    ("hindsight_key", _ascii_token_pattern(r"hsk_(?:sys_)?[0-9a-f]{32}(?:_[0-9a-f]{8,})?")),
    ("huggingface_token", _ascii_token_pattern(r"hf_[A-Za-z0-9]{30,}")),
    ("replicate_token", _ascii_token_pattern(r"r8_[A-Za-z0-9]{30,}")),
    ("perplexity_key", _ascii_token_pattern(r"pplx-[A-Za-z0-9]{40,}")),
    ("databricks_token", _ascii_token_pattern(r"dapi[A-Za-z0-9]{32}")),
    # Cloud providers
    ("aws_access_key", _ascii_token_pattern(r"AKIA[0-9A-Z]{16}")),
    ("aws_session_token", _ascii_token_pattern(r"ASIA[0-9A-Z]{16}")),
    (
        "aws_secret_key",
        r"(?i)aws(.{0,20})?(secret|private)?[\s_-]?access[\s_-]?key[\s_-]?[:=][\s\"']*([A-Za-z0-9/+=]{40})",
    ),
    ("digitalocean_token", _ascii_token_pattern(r"dop_v1_[a-f0-9]{64}")),
    # Source control & CI
    ("github_fg_pat", _ascii_token_pattern(r"github_pat_[A-Za-z0-9_]{60,}")),
    ("github_token", _ascii_token_pattern(r"ghp_[A-Za-z0-9]{36}")),
    ("github_app_token", _ascii_token_pattern(r"ghs_[A-Za-z0-9]{36}")),
    ("github_user_token", _ascii_token_pattern(r"ghu_[A-Za-z0-9]{36}")),
    ("github_refresh", _ascii_token_pattern(r"ghr_[A-Za-z0-9]{36}")),
    ("github_oauth", _ascii_token_pattern(r"gho_[A-Za-z0-9]{36}")),
    ("gitlab_pat", _ascii_token_pattern(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("npm_token", _ascii_token_pattern(r"npm_[A-Za-z0-9]{30,}")),
    ("pypi_token", _ascii_token_pattern(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}")),
    # Payment processors
    ("stripe_secret", _ascii_token_pattern(r"sk_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("stripe_restricted", _ascii_token_pattern(r"rk_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("square_token", _ascii_token_pattern(r"sq0[a-z]{3}-[A-Za-z0-9_-]{22,}")),
    ("braintree_token", _ascii_token_pattern(r"access_token\$production\$[a-z0-9]{16}\$[a-f0-9]{32}")),
    # Communication & messaging
    ("slack_token", _ascii_token_pattern(r"xox[abpr]-[0-9A-Za-z-]{10,}")),
    ("slack_webhook", r"https://hooks\.slack\.com/services/T[A-Za-z0-9_]{8,}/B[A-Za-z0-9_]{8,}/[A-Za-z0-9_]{20,}"),
    ("twilio_api_key", _ascii_token_pattern(r"SK[0-9a-fA-F]{32}")),
    ("twilio_account_sid", _ascii_token_pattern(r"AC[0-9a-fA-F]{32}")),
    ("sendgrid_key", _ascii_token_pattern(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}")),
    ("mailgun_key", _ascii_token_pattern(r"key-[A-Za-z0-9]{32}")),
    ("discord_bot", _ascii_token_pattern(r"[MNO][A-Za-z0-9]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}")),
    ("telegram_bot", _ascii_token_pattern(r"[0-9]{8,10}:[A-Za-z0-9_-]{35}")),
    # Commerce
    ("shopify_token", _ascii_token_pattern(r"shpat_[a-fA-F0-9]{32}")),
    # Databases
    ("db_url_postgres", r"postgres(?:ql)?://[^\s:/@]+:[^\s/@]+@[^\s]+"),
    ("db_url_mysql", r"mysql://[^\s:/@]+:[^\s/@]+@[^\s]+"),
    ("db_url_mongodb", r"mongodb(?:\+srv)?://[^\s:/@]+:[^\s/@]+@[^\s]+"),
    # Private keys & generic tokens
    ("private_key_pem",
     r"-----BEGIN (?P<pem_kind>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?)-----"
     r"(?:[ \t]*\r?\n[\s\S]*?(?:-----END (?P=pem_kind)-----|\Z))?"),
    ("jwt", _ascii_token_pattern(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # PII
    (
        "credit_card",
        _ascii_token_pattern(r"(?<!\d)(?<!\d\.)(?:\d{4}[ -]?){3}\d{1,4}(?!\d)(?!\.\d)"),
    ),
    ("ssn_us", _ascii_token_pattern(r"\d{3}-\d{2}-\d{4}")),
]

_COMPILED_REDACTIONS: list[tuple[str, re.Pattern]] = [
    (label, re.compile(pattern)) for label, pattern in _REDACTION_PATTERNS
]
_UUID_PATTERN = re.compile(_ascii_token_pattern(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"))


def _is_luhn_card(value: str) -> bool:
    """Validate a decimal-digit candidate using the Luhn checksum."""
    digits = [int(char) for char in value if char not in " -"]
    if len(set(digits)) == 1:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def apply_redaction(content: str) -> RedactionResult:
    """Scrub known secret/PII patterns from content replacing with [REDACTED:type]."""
    matched: list[str] = []
    hits: list[dict[str, str]] = []

    for label, pattern in _COMPILED_REDACTIONS:
        if label == "credit_card":
            parts: list[str] = []
            search_start = 0
            copied_until = 0
            while match := pattern.search(content, search_start):
                search_start = match.start() + 1
                value = match.group()
                if not _is_luhn_card(value):
                    continue
                # Suppress UUID matches passing Luhn
                if any(
                    identifier.start() < match.end() and identifier.end() > match.start()
                    for identifier in _UUID_PATTERN.finditer(
                        content, max(0, match.start() - 35), min(len(content), match.end() + 36)
                    )
                ):
                    continue
                if "credit_card" not in matched:
                    matched.append("credit_card")
                hits.append({"detector": "credit_card", "preview": _fingerprint_value(value)})
                parts.extend((content[copied_until:match.start()], "[REDACTED:credit_card]"))
                copied_until = match.end()
                search_start = match.end()

            parts.append(content[copied_until:])
            content = "".join(parts)
            continue

        raw_hits = pattern.findall(content)
        if not raw_hits:
            continue
        if label not in matched:
            matched.append(label)
        for raw in raw_hits:
            if isinstance(raw, tuple):
                non_empty = [g for g in raw if g]
                raw_str = max(non_empty, key=len) if non_empty else ""
            else:
                raw_str = raw
            if not raw_str:
                continue
            hits.append({"detector": label, "preview": _fingerprint_value(raw_str)})
        content = pattern.sub(f"[REDACTED:{label}]", content)

    # Keep the standalone defense API aligned with capture's credential rules
    # (opaque bearer values, modern Hub keys, and generic assignments).
    from commontrace.memory_guard import redact_secrets

    content, extra_labels = redact_secrets(content)
    for label in extra_labels:
        detector = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        if detector not in matched:
            matched.append(detector)
        hits.append({"detector": detector, "preview": "[redacted]"})
    return RedactionResult(content=content, matched_types=matched, hits=hits)


class MemoryDefenseScreen:
    """Pluggable memory-defense screen orchestrator."""

    def __init__(self, default_policy: DefensePolicy | None = None) -> None:
        self.default_policy = default_policy or DefensePolicy()

    def screen(
        self,
        content: str,
        *,
        policy: DefensePolicy | None = None,
        context: dict[str, Any] | None = None,
    ) -> DefenseDecision:
        """Screen content against active policy and return action decision."""
        pol = policy or self.default_policy
        if not pol.enabled:
            return DefenseDecision(action=DefenseAction.ALLOW)

        rule = next((r for r in pol.rules if r.on in ("sensitive_data", "secrets")), None)
        if rule is None or rule.action is DefenseAction.ALLOW:
            return DefenseDecision(action=DefenseAction.ALLOW)

        result = apply_redaction(content)
        if not result.matched_types:
            return DefenseDecision(action=DefenseAction.ALLOW)

        return DefenseDecision(
            action=rule.action,
            detector="sensitive_data",
            message=f"Sensitive data pattern matched: {', '.join(result.matched_types)}",
            redacted_content=result.content if rule.action is DefenseAction.REDACT else None,
            matched_types=result.matched_types,
            hits=result.hits,
        )


_DEFAULT_SCREEN = MemoryDefenseScreen()


def screen_content(
    content: str,
    policy: DefensePolicy | None = None,
) -> DefenseDecision:
    """Module-level screening helper."""
    return _DEFAULT_SCREEN.screen(content, policy=policy)
