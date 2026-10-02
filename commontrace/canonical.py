"""Alias canonicalizer with guard rails.

Maps entity aliases to canonical names (case-insensitive) while refusing
to rewrite risky words (modal verbs, deontic language, etc.).
"""
from __future__ import annotations

from typing import Any

#: Words that must never be rewritten as aliases, even if present in the map.
#: Modal verbs, deontic / normative language, and common stopwords.
DEFAULT_RISKY_WORDS: frozenset[str] = frozenset({
    # modal verbs
    "must", "shall", "should", "will", "would", "could", "can",
    "may", "might", "ought", "need", "needs", "required", "require",
    "requires", "mandatory", "forbidden", "prohibited",
    # normative / logical glue
    "not", "no", "never", "always", "all", "any", "none",
    "if", "then", "else", "and", "or", "true", "false",
})


def _normalize_alias_map(aliases: dict[str, Any]) -> dict[str, str]:
    """Normalize an alias map to {alias_lower: canonical}.

    Accepts two shapes:
      - {alias: canonical}
      - {canonical: [alias, ...]}  (values that are list/tuple/set)
    """
    norm: dict[str, str] = {}
    for key, value in (aliases or {}).items():
        if isinstance(value, (list, tuple, set)):
            canonical = str(key)
            norm[canonical.strip().lower()] = canonical
            for alias in value:
                norm[str(alias).strip().lower()] = canonical
        else:
            norm[str(key).strip().lower()] = str(value)
    return norm


def canonicalize_alias(
    name: str,
    aliases: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Map ``name`` to its canonical form.

    Returns ``(canonical, was_rewritten)``. Matching is case-insensitive.
    Names in :data:`DEFAULT_RISKY_WORDS` are never rewritten.

    Args:
        name: the raw alias or entity name.
        aliases: alias map in either {alias: canonical} or
            {canonical: [aliases]} shape.
    """
    raw = str(name or "")
    key = raw.strip().lower()
    if not key:
        return raw, False
    if key in DEFAULT_RISKY_WORDS:
        return raw, False
    norm = _normalize_alias_map(aliases or {})
    if key not in norm:
        return raw, False
    canonical = norm[key]
    if str(canonical).strip().lower() in DEFAULT_RISKY_WORDS:
        return raw, False
    if canonical == raw:
        return raw, False
    # Same text modulo case still counts as a rewrite only if spelling differs
    # beyond case? Treat pure case-fold matches as rewritten when the stored
    # canonical differs in case from input.
    if canonical.lower() == key and canonical == raw:
        return raw, False
    return canonical, True
