"""What one API key is allowed to do, as distinct from which org it speaks for."""

from __future__ import annotations

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
SCOPE_SCIM = "scim"

ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN, SCOPE_SCIM)

_LEGACY_IMPLIED_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)

DEFAULT_SCOPES: tuple[str, ...] = _LEGACY_IMPLIED_SCOPES


class ScopeError(ValueError):
    ...


def parse(raw: str | list | tuple | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_SCOPES
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    requested = {str(item).strip().lower() for item in items if str(item).strip()}
    if not requested:
        raise ScopeError(
            "no scopes given. Pass at least one of "
            f"{', '.join(ALL_SCOPES)} -- a key with no scopes can do nothing at all."
        )
    unknown = sorted(requested - set(ALL_SCOPES))
    if unknown:
        raise ScopeError(
            f"unknown scope(s): {', '.join(unknown)}. "
            f"Valid scopes are {', '.join(ALL_SCOPES)}."
        )
    return tuple(scope for scope in ALL_SCOPES if scope in requested)


def satisfies(granted: object, required: str) -> bool:
    if granted is None:
        return required in _LEGACY_IMPLIED_SCOPES
    return required in set(granted)


def describe(granted: object) -> str:
    """Short human-readable rendering for CLI output and audit lines."""
    if granted is None:
        return (
            f"{','.join(_LEGACY_IMPLIED_SCOPES)} (legacy key, issued before "
            "scopes existed)"
        )
    scopes = tuple(granted)
    if not scopes:
        return "none"
    return ",".join(scope for scope in ALL_SCOPES if scope in set(scopes))
