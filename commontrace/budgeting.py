"""CommonTrace Context Budgeting Module.

Provides intelligent truncation that preserves filesystem tree structure
and other structured content formats.

Designed to work with the context.py module to manage context budgets
while maintaining readability of truncated content.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Structure-preserving truncation
# ---------------------------------------------------------------------------


def truncate_preserving_structure(text: str, max_chars: int) -> str:
    """Truncate text while preserving XML-like structure (filesystem trees, etc.).

    This function attempts to truncate at tag boundaries to keep the structure
    valid. If the text contains a filesystem tree section, it prioritizes
    preserving that structure over other content.

    Args:
        text: Text to truncate
        max_chars: Maximum character limit

    Returns:
        Truncated text that fits within max_chars while preserving structure
    """
    if len(text) <= max_chars:
        return text

    # Try to identify and preserve filesystem tree structure
    tree_match = re.search(r"<memory_filesystem>[\s\S]*?</memory_filesystem>", text)
    if tree_match:
        return _truncate_with_tree(text, max_chars, tree_match)

    # Try to preserve any XML-like structure
    return _truncate_preserving_tags(text, max_chars)


def _truncate_with_tree(text: str, max_chars: int, tree_match: re.Match) -> str:
    """Truncate text while preserving the filesystem tree section.

    Args:
        text: Full text
        max_chars: Maximum character limit
        tree_match: Regex match object for the tree section

    Returns:
        Truncated text with tree preserved if possible
    """
    tree = tree_match.group(0)

    # If the tree alone fits, return just the tree with a notice
    if len(tree) <= max_chars:
        notice = "\n[Context truncated: only filesystem tree preserved]"
        if len(tree) + len(notice) <= max_chars:
            return tree + notice[: max_chars - len(tree)]
        return tree[: max_chars]

    # Try to truncate the tree itself while preserving structure
    truncated_tree = _truncate_tree_structure(tree, max_chars)
    if truncated_tree:
        return truncated_tree

    # Fallback: preserve as much of the tree as possible
    return tree[: max_chars]


def _truncate_tree_structure(tree: str, max_chars: int) -> str | None:
    """Truncate a filesystem tree while preserving its directory structure.

    Args:
        tree: Tree text (between <memory_filesystem> tags)
        max_chars: Maximum character limit

    Returns:
        Truncated tree preserving structure, or None if not possible
    """
    lines = tree.split("\n")

    # Keep opening tag
    if not lines or not lines[0].startswith("<memory_filesystem>"):
        return None

    closing = "</memory_filesystem>"
    minimal = f"{lines[0]}\n{closing}"
    if len(minimal) > max_chars:
        return None

    result = [lines[0]]

    # Try to add lines until we hit the limit
    for line in lines[1:]:
        if line.strip() == closing:
            break

        candidate = "\n".join(result + [line, closing])
        if len(candidate) <= max_chars:
            result.append(line)
        else:
            break

    result.append(closing)
    return "\n".join(result)


def _truncate_preserving_tags(text: str, max_chars: int) -> str:
    """Truncate text while preserving XML-like tag structure.

    Args:
        text: Text to truncate
        max_chars: Maximum character limit

    Returns:
        Truncated text with tags preserved
    """
    # Find all tag positions
    tag_pattern = re.compile(r"</?[\w_]+>")
    tags = list(tag_pattern.finditer(text))

    if not tags:
        # No tags, simple truncation
        return text[: max_chars]

    # Try to truncate at a tag boundary
    for tag_match in reversed(tags):
        if tag_match.end() <= max_chars:
            # Truncate at this tag boundary
            truncated = text[: tag_match.end()]
            # Try to close any open tags
            closed = _close_open_tags(truncated, tag_match.end(), text)
            if len(closed) <= max_chars:
                return closed

    # Fallback: simple truncation
    return text[: max_chars]


def _close_open_tags(truncated: str, truncate_pos: int, original: str) -> str:
    """Close any unclosed XML tags in truncated text.

    Args:
        truncated: Truncated text
        truncate_pos: Position where truncation occurred
        original: Original full text

    Returns:
        Text with unclosed tags closed
    """
    tag_pattern = re.compile(r"<(/)?([\w_]+)(?:\s+[^>]*)?(/)?>")
    tag_stack: list[str] = []

    for match in tag_pattern.finditer(truncated):
        is_closing, tag_name, is_self_closing = match.groups()
        if is_self_closing or match.group(0).endswith("/>"):
            continue
        if is_closing:
            if tag_stack and tag_stack[-1] == tag_name:
                tag_stack.pop()
            elif tag_name in tag_stack:
                while tag_stack and tag_stack[-1] != tag_name:
                    tag_stack.pop()
                if tag_stack:
                    tag_stack.pop()
        else:
            tag_stack.append(tag_name)

    result = truncated
    for tag in reversed(tag_stack):
        result += f"</{tag}>"

    return result


# ---------------------------------------------------------------------------
# Section-aware truncation
# ---------------------------------------------------------------------------


def truncate_section(
    text: str,
    section_name: str,
    max_chars: int,
    preserve_structure: bool = True,
) -> str:
    """Truncate a specific section within text.

    Args:
        text: Full text containing the section
        section_name: Name of the section (e.g., "parent_memory")
        max_chars: Maximum character limit for the section
        preserve_structure: Whether to preserve structure within the section

    Returns:
        Text with the specified section truncated
    """
    # Match the section
    pattern = rf"<{section_name}>[\s\S]*?</{section_name}>"
    match = re.search(pattern, text)

    if not match:
        # Section not found, return original
        return text

    section = match.group(0)
    start = match.start()
    end = match.end()

    # Truncate the section
    if preserve_structure:
        truncated_section = truncate_preserving_structure(section, max_chars)
    else:
        truncated_section = section[: max_chars]

    # Reconstruct the text
    return text[: start] + truncated_section + text[end:]


def build_minimal_section(section_name: str, notice: str | None = None) -> str:
    """Build a minimal section with just opening/closing tags and notice.

    Args:
        section_name: Name of the section
        notice: Optional notice text to include

    Returns:
        Minimal section string
    """
    if notice:
        return f"<{section_name}>\n{notice}\n</{section_name}>"
    return f"<{section_name}>\n</{section_name}>"


# ---------------------------------------------------------------------------
# Parent memory truncation (reflection-specific)
# ---------------------------------------------------------------------------


def truncate_parent_memory(
    text: str,
    max_chars: int,
    notice: str | None = None,
) -> str:
    """Truncate parent memory section while preserving filesystem tree.

    This is specialized for reflection subagent operations where the parent
    memory section contains a filesystem tree that should be preserved if possible.

    Args:
        text: Full text containing parent_memory section
        max_chars: Maximum character limit
        notice: Optional truncation notice

    Returns:
        Text with parent_memory truncated appropriately
    """
    if notice is None:
        from commontrace.context import get_reflection_startup_notice

        notice = get_reflection_startup_notice()

    # Match the parent_memory section
    pattern = r"<parent_memory>[\s\S]*?</parent_memory>"
    match = re.search(pattern, text)

    if not match:
        # No parent_memory section, return original
        return text

    section = match.group(0)
    start = match.start()
    end = match.end()

    # If the section fits, return as-is
    if len(section) <= max_chars:
        return text

    # Try to preserve filesystem tree
    tree_match = re.search(r"<memory_filesystem>[\s\S]*?</memory_filesystem>", section)
    if tree_match:
        tree = tree_match.group(0)
        prefix = "<parent_memory>\n"
        suffix = "\n</parent_memory>"

        # Try to fit tree + notice
        candidate = f"{prefix}{tree}\n{notice}{suffix}"
        if len(candidate) <= max_chars:
            return text[: start] + candidate + text[end:]

        # Try to fit just tree
        candidate = f"{prefix}{tree}{suffix}"
        if len(candidate) <= max_chars:
            return text[: start] + candidate + text[end:]

    # Fallback: minimal section with notice
    minimal = build_minimal_section("parent_memory", notice)
    if len(minimal) <= max_chars:
        return text[: start] + minimal + text[end:]

    # Truncate minimal section while preserving tags
    truncated = truncate_preserving_structure(minimal, max_chars)
    return text[: start] + truncated + text[end:]


# ---------------------------------------------------------------------------
# Smart truncation with priority
# ---------------------------------------------------------------------------


def truncate_with_priority(
    text: str,
    max_chars: int,
    priority_sections: list[str] | None = None,
) -> str:
    """Truncate text with priority given to certain sections.

    Args:
        text: Text to truncate
        max_chars: Maximum character limit
        priority_sections: List of section names to preserve (in order of priority)

    Returns:
        Truncated text with priority sections preserved as much as possible
    """
    if len(text) <= max_chars:
        return text

    if not priority_sections:
        return truncate_preserving_structure(text, max_chars)

    # Try to preserve priority sections
    for section_name in priority_sections:
        pattern = rf"<{section_name}>[\s\S]*?</{section_name}>"
        match = re.search(pattern, text)

        if match:
            section = match.group(0)
            # If this section fits, preserve it and truncate the rest
            if len(section) <= max_chars:
                # Calculate budget for other content
                other_budget = max_chars - len(section)
                # Get content before and after the section
                before = text[: match.start()]
                after = text[match.end():]

                # Truncate before and after
                truncated_before = truncate_preserving_structure(before, other_budget // 2) if before else ""
                remaining_budget = max_chars - len(section) - len(truncated_before)
                truncated_after = truncate_preserving_structure(after, remaining_budget) if after else ""

                return truncated_before + section + truncated_after

    # Fallback to structure-preserving truncation
    return truncate_preserving_structure(text, max_chars)
