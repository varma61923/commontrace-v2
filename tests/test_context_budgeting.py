"""Tests for CommonTrace context management and budgeting modules."""

from __future__ import annotations

import pytest

from commontrace import budgeting, context


class TestTokenEstimation:
    """Tests for token estimation functions."""

    def test_estimate_startup_context_tokens_empty(self):
        """Empty text should return 0 tokens."""
        assert context.estimate_startup_context_tokens("") == 0
        assert context.estimate_startup_context_tokens(None) == 0  # type: ignore

    def test_estimate_startup_context_tokens_basic(self):
        """Basic token estimation with 4 chars/token."""
        # 8 chars = 2 tokens
        assert context.estimate_startup_context_tokens("abcdefgh") == 2
        # 10 chars = 3 tokens (ceiling)
        assert context.estimate_startup_context_tokens("abcdefghij") == 3
        # 4 chars = 1 token
        assert context.estimate_startup_context_tokens("abcd") == 1

    def test_estimate_tokens_custom_ratio(self):
        """Custom chars-per-token ratio."""
        # 10 chars with 5 chars/token = 2 tokens
        assert context.estimate_tokens("abcdefghij", chars_per_token=5) == 2
        # 10 chars with 3 chars/token = 4 tokens (ceiling)
        assert context.estimate_tokens("abcdefghij", chars_per_token=3) == 4

    def test_fits_within_budget(self):
        """Budget fitting check."""
        # 8 chars = 2 tokens, fits in 3 token budget
        assert context.fits_within_budget("abcdefgh", token_limit=3) is True
        # 8 chars = 2 tokens, does not fit in 1 token budget
        assert context.fits_within_budget("abcdefgh", token_limit=1) is False

    def test_validate_context_size(self):
        """Context size validation."""
        # Fits
        fits, estimated = context.validate_context_size("abcdefgh", token_limit=3)
        assert fits is True
        assert estimated == 2

        # Does not fit
        fits, estimated = context.validate_context_size("abcdefgh", token_limit=1)
        assert fits is False
        assert estimated == 2

    def test_validate_context_size_raises(self):
        """Validation raises when over budget with raise_on_exceed=True."""
        with pytest.raises(ValueError, match="Context exceeds budget"):
            context.validate_context_size("abcdefgh", token_limit=1, raise_on_exceed=True)


class TestContextBudget:
    """Tests for ContextBudget class."""

    def test_initialization(self):
        """ContextBudget initialization."""
        budget = context.ContextBudget(token_limit=100, chars_per_token=4)
        assert budget.token_limit == 100
        assert budget.chars_per_token == 4
        assert budget.char_limit == 400

    def test_initialization_with_char_limit(self):
        """ContextBudget with explicit char limit."""
        budget = context.ContextBudget(token_limit=100, char_limit=500, chars_per_token=4)
        assert budget.char_limit == 500  # Override the calculated value

    def test_estimate_tokens(self):
        """ContextBudget.estimate_tokens uses its own ratio."""
        budget = context.ContextBudget(token_limit=100, chars_per_token=5)
        assert budget.estimate_tokens("abcdefghij") == 2  # 10 chars / 5 = 2

    def test_fits(self):
        """ContextBudget.fits check."""
        budget = context.ContextBudget(token_limit=2, chars_per_token=4)
        assert budget.fits("abcd") is True  # 1 token
        assert budget.fits("abcdefgh") is True  # 2 tokens (at limit, still fits)

    def test_remaining_chars(self):
        """ContextBudget.remaining_chars calculation."""
        budget = context.ContextBudget(token_limit=10, chars_per_token=4)
        assert budget.remaining_chars("abcd") == 36  # 40 - 4
        assert budget.remaining_chars("abcdefgh") == 32  # 40 - 8

    def test_truncate_to_fit_no_truncation(self):
        """Truncate when text already fits."""
        budget = context.ContextBudget(token_limit=10, chars_per_token=4)
        text = "abcd"
        assert budget.truncate_to_fit(text) == text

    def test_truncate_to_fit_simple(self):
        """Simple truncation without structure preservation."""
        budget = context.ContextBudget(token_limit=2, chars_per_token=4)
        text = "abcdefgh"
        # Should truncate to 8 chars (2 * 4)
        result = budget.truncate_to_fit(text, preserve_structure=False)
        assert result == "abcdefgh"
        assert len(result) <= budget.char_limit

    def test_truncate_to_fit_with_structure(self):
        """Truncation with structure preservation."""
        budget = context.ContextBudget(token_limit=10, chars_per_token=4)
        text = "<parent_memory>\nabcdefgh\n</parent_memory>"
        result = budget.truncate_to_fit(text, preserve_structure=True)
        # Should preserve tags if possible
        assert len(result) <= budget.char_limit


class TestReflectionContextBudget:
    """Tests for ReflectionContextBudget class."""

    def test_reflection_defaults(self):
        """ReflectionContextBudget uses reflection-specific defaults."""
        budget = context.ReflectionContextBudget()
        assert budget.token_limit == context.REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT
        assert budget.char_limit == context.REFLECTION_STARTUP_CONTEXT_CHAR_LIMIT
        assert budget.chars_per_token == context.STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN


class TestStructurePreservingTruncation:
    """Tests for structure-preserving truncation functions."""

    def test_truncate_preserving_structure_no_truncation(self):
        """No truncation when text fits."""
        text = "<test>content</test>"
        result = budgeting.truncate_preserving_structure(text, max_chars=100)
        assert result == text

    def test_truncate_preserving_structure_simple(self):
        """Simple truncation without structure."""
        text = "abcdefgh"
        result = budgeting.truncate_preserving_structure(text, max_chars=4)
        assert result == "abcd"

    def test_truncate_preserving_structure_with_tree(self):
        """Preserve filesystem tree structure."""
        text = "<memory_filesystem>\n/file1.txt\n/file2.txt\n</memory_filesystem>"
        result = budgeting.truncate_preserving_structure(text, max_chars=60)
        # Should preserve the tree structure
        assert "<memory_filesystem>" in result
        assert "</memory_filesystem>" in result

    def test_truncate_tree_structure(self):
        """Truncate tree while preserving directory structure."""
        tree = "<memory_filesystem>\n/file1.txt\n/file2.txt\n/file3.txt\n</memory_filesystem>"
        result = budgeting._truncate_tree_structure(tree, max_chars=60)
        # Should preserve opening and closing tags
        assert result.startswith("<memory_filesystem>")
        assert result.endswith("</memory_filesystem>")
        assert len(result) <= 60

    def test_truncate_preserving_tags(self):
        """Preserve XML-like tag structure."""
        text = "<section>content here</section>"
        result = budgeting._truncate_preserving_tags(text, max_chars=10)
        # Should try to preserve tag boundaries
        assert len(result) <= 10

    def test_close_open_tags(self):
        """Close unclosed XML tags."""
        truncated = "<section><subsection>"
        original = "<section><subsection>content</subsection></section>"
        result = budgeting._close_open_tags(truncated, len(truncated), original)
        # Should close the open tags
        assert "</subsection>" in result
        assert "</section>" in result


class TestSectionTruncation:
    """Tests for section-aware truncation."""

    def test_truncate_section_not_found(self):
        """Return original when section not found."""
        text = "no sections here"
        result = budgeting.truncate_section(text, "parent_memory", max_chars=10)
        assert result == text

    def test_truncate_section_found(self):
        """Truncate specific section."""
        text = "<parent_memory>\nabcdefgh\n</parent_memory>"
        result = budgeting.truncate_section(text, "parent_memory", max_chars=20)
        # Should truncate the section
        assert len(result) <= 20

    def test_build_minimal_section(self):
        """Build minimal section with notice."""
        result = budgeting.build_minimal_section("test", "truncated")
        assert result == "<test>\ntruncated\n</test>"

    def test_build_minimal_section_no_notice(self):
        """Build minimal section without notice."""
        result = budgeting.build_minimal_section("test")
        assert result == "<test>\n</test>"


class TestParentMemoryTruncation:
    """Tests for parent memory truncation."""

    def test_truncate_parent_memory_not_found(self):
        """Return original when parent_memory not found."""
        text = "no parent memory"
        result = budgeting.truncate_parent_memory(text, max_chars=10)
        assert result == text

    def test_truncate_parent_memory_fits(self):
        """Return original when section fits."""
        text = "<parent_memory>\nabcd\n</parent_memory>"
        result = budgeting.truncate_parent_memory(text, max_chars=50)
        assert result == text

    def test_truncate_parent_memory_with_tree(self):
        """Preserve filesystem tree in parent memory."""
        text = "<parent_memory>\n<memory_filesystem>\n/file.txt\n</memory_filesystem>\n</parent_memory>"
        result = budgeting.truncate_parent_memory(text, max_chars=40)
        # Should try to preserve the tree
        assert len(result) <= 40

    def test_truncate_parent_memory_minimal(self):
        """Fall back to minimal section."""
        text = "<parent_memory>\nvery long content here that exceeds budget\n</parent_memory>"
        result = budgeting.truncate_parent_memory(text, max_chars=30)
        # Should return minimal section
        assert "<parent_memory>" in result
        assert len(result) <= 30


class TestPriorityTruncation:
    """Tests for priority-based truncation."""

    def test_truncate_with_priority_no_sections(self):
        """Simple truncation when no priority sections."""
        text = "abcdefgh"
        result = budgeting.truncate_with_priority(text, max_chars=4, priority_sections=None)
        assert result == "abcd"

    def test_truncate_with_priority_preserves_section(self):
        """Preserve priority section if it fits."""
        text = "<important>content</important>\nother content"
        result = budgeting.truncate_with_priority(
            text, max_chars=30, priority_sections=["important"]
        )
        # Should preserve the important section
        assert "<important>" in result
        assert len(result) <= 30

    def test_truncate_with_priority_multiple_sections(self):
        """Try multiple priority sections in order."""
        text = "<first>a</first>\n<second>b</second>\nother"
        result = budgeting.truncate_with_priority(
            text, max_chars=20, priority_sections=["first", "second"]
        )
        # Should try to preserve first, then second
        assert len(result) <= 20


class TestConstants:
    """Tests for module constants."""

    def test_startup_context_constants(self):
        """Verify startup context constants are set correctly."""
        assert context.STARTUP_CONTEXT_ESTIMATED_CHARS_PER_TOKEN == 4
        assert context.REFLECTION_STARTUP_CONTEXT_TOKEN_LIMIT == 16_000
        assert context.REFLECTION_STARTUP_CONTEXT_CHAR_LIMIT == 64_000  # 16_000 * 4
        assert context.REFLECTION_PARENT_MEMORY_SNAPSHOT_CHAR_LIMIT == 40_000

    def test_get_reflection_startup_notice(self):
        """Reflection startup notice contains expected information."""
        notice = context.get_reflection_startup_notice()
        assert "16,000" in notice
        assert "truncated" in notice.lower()

    def test_char_limit_zero(self):
        """ContextBudget preserves char_limit=0 without falling back to token_limit."""
        budget = context.ContextBudget(token_limit=100, char_limit=0)
        assert budget.char_limit == 0
        assert budget.fits("a") is False
        assert budget.fits("") is True

    def test_negative_chars_per_token(self):
        """Invalid chars_per_token raises ValueError."""
        with pytest.raises(ValueError, match="chars_per_token must be positive"):
            context.estimate_tokens("test", chars_per_token=0)

    def test_validate_context_size_none(self):
        """None text is safely handled as empty string."""
        fits, est = context.validate_context_size(None, token_limit=10)
        assert fits is True
        assert est == 0

    def test_close_open_tags_sequential_nesting(self):
        """Sequential and nested tags are properly closed in LIFO order."""
        text = "<a><b></b><c>"
        result = budgeting._close_open_tags(text, len(text), text)
        assert result == "<a><b></b><c></c></a>"
