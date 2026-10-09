"""Deterministic operator hints; exceptions and credentials are never auto-logged."""
from __future__ import annotations


def hint(error: BaseException) -> str:
    from commontrace.exceptions import CommonTraceError

    if isinstance(error, CommonTraceError):
        return error.remediation
    if isinstance(error, ImportError):
        return "Install the documented optional extra in the same Python environment."
    if isinstance(error, PermissionError):
        return "Check the credential's principal and scopes; do not retry with elevated caller data."
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "Check connectivity and retry within a bounded deadline."
    if isinstance(error, ValueError):
        return "Check field types, limits, timestamps and configuration."
    return "Inspect the request ID in operator logs."
