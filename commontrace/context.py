"""CommonTrace Context Management Module.

Provides conservative character-based token estimation and context management.
Designed for minimal overhead with no external tokenizer dependencies.

Uses 4 chars/token as a conservative estimate for English/Markdown content.
This is a safety guard before sending prompts to the API; actual tokenization
happens at the LLM provider level.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Token estimation constants
# ---------------------------------------------------------------------------

STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN = 4
REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT = 16_000
REFLECTION_STARTUP_CONTEXT_CHAR_LIMIT = (
    REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT * STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN
)

# Leave room for the reflection subagent system prompt and launch boilerplate.
# The final guard in subagent manager enforces the full system+prompt budget.
REFLECTION_PARENT_MEMORY_SNAPSHOT_CHAR_LIMIT = 40_000


# ---------------------------------------------------------------------------
# Token estimation functions
# ---------------------------------------------------------------------------


def estimate_startup_context_tokens(text: str) -> int:
    """Estimate token count using conservative 4 chars/token heuristic.

    Args:
        text: Input text to estimate

    Returns:
        Estimated number of tokens (ceiling of chars / 4)
    """
    if not text:
        return 0
    return (len(text) + STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN - 1) // STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """Estimate token count with configurable chars-per-token ratio.

    Args:
        text: Input text to estimate
        chars_per_token: Characters per token (default: 4 for conservative estimate)

    Returns:
        Estimated number of tokens
    """
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be positive")
    if not text:
        return 0
    return (len(text) + chars_per_token - 1) // chars_per_token


def fits_within_budget(text: str, token_limit: int, chars_per_token: int = 4) -> bool:
    """Check if text fits within a token budget.

    Args:
        text: Input text to check
        token_limit: Maximum allowed tokens
        chars_per_token: Characters per token (default: 4)

    Returns:
        True if estimated tokens <= limit, False otherwise
    """
    estimated = estimate_tokens(text, chars_per_token)
    return estimated <= token_limit


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------


class ContextBudget:
    """Manages context budget for a specific operation or subagent."""

    def __init__(
        self,
        token_limit: int = REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT,
        char_limit: int | None = None,
        chars_per_token: int = STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN,
    ):
        """Initialize a context budget.

        Args:
            token_limit: Maximum allowed tokens
            char_limit: Optional character limit (overrides token_limit * chars_per_token)
            chars_per_token: Characters per token for estimation
        """
        self.token_limit = token_limit
        self.chars_per_token = chars_per_token
        self.char_limit = char_limit if char_limit is not None else (token_limit * chars_per_token)

    def estimate_tokens(self, text: str) -> int:
        """Estimate tokens for text using this budget's ratio."""
        return estimate_tokens(text, self.chars_per_token)

    def fits(self, text: str) -> bool:
        """Check if text fits within this budget."""
        if not text:
            return True
        if len(text) > self.char_limit:
            return False
        return fits_within_budget(text, self.token_limit, self.chars_per_token)

    def remaining_chars(self, text: str) -> int:
        """Calculate remaining character budget after accounting for text.

        Args:
            text: Text to account for

        Returns:
            Remaining characters (may be negative if over budget)
        """
        return self.char_limit - len(text)

    def truncate_to_fit(self, text: str, preserve_structure: bool = True) -> str:
        """Truncate text to fit within budget, optionally preserving structure.

        Args:
            text: Text to truncate
            preserve_structure: If True, try to preserve XML-like structure

        Returns:
            Truncated text that fits within budget
        """
        if len(text) <= self.char_limit:
            return text

        if preserve_structure:
            # Try to preserve XML-like structure by truncating at tag boundaries
            from commontrace.budgeting import truncate_preserving_structure

            return truncate_preserving_structure(text, self.char_limit)

        # Simple hard truncation
        return text[: self.char_limit]


class ReflectionContextBudget(ContextBudget):
    """Specialized budget for reflection subagent operations."""

    def __init__(self):
        """Initialize with reflection-specific defaults."""
        super().__init__(
            token_limit=REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT,
            char_limit=REFLECTION_STARTUP_CONTEXT_CHAR_LIMIT,
            chars_per_token=STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN,
        )


# ---------------------------------------------------------------------------
# Context validation
# ---------------------------------------------------------------------------


def validate_context_size(
    text: str | None,
    token_limit: int,
    chars_per_token: int = 4,
    raise_on_exceed: bool = False,
) -> tuple[bool, int]:
    """Validate that context fits within token budget.

    Args:
        text: Text to validate
        token_limit: Maximum allowed tokens
        chars_per_token: Characters per token for estimation
        raise_on_exceed: If True, raise ValueError when over budget

    Returns:
        Tuple of (fits, estimated_tokens)

    Raises:
        ValueError: If text exceeds budget and raise_on_exceed is True
    """
    safe_text = text or ""
    estimated = estimate_tokens(safe_text, chars_per_token)
    fits = estimated <= token_limit

    if not fits and raise_on_exceed:
        raise ValueError(
            f"Context exceeds budget: {estimated} tokens > {token_limit} limit "
            f"({len(safe_text)} chars at {chars_per_token} chars/token)"
        )

    return fits, estimated


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def get_reflection_startup_notice() -> str:
    """Get the standard notice for reflection context truncation."""
    return (
        f"[Reflection startup context truncated: system prompt + initial message "
        f"are capped at ~{REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT:,} estimated tokens. "
        f"Some parent memory preview content was omitted; read files directly from "
        f"MEMORY_DIR if needed.]"
    )
