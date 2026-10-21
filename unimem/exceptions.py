"""Public error categories with safe, actionable HTTP/CLI diagnostics."""
from __future__ import annotations


class UniMemError(Exception):
    status_code = 500
    code = "unimem_error"
    retryable = False
    default_remediation = "Inspect the server's request ID and configuration."

    def __init__(self, message: str, *, remediation: str | None = None):
        super().__init__(message)
        self.remediation = remediation or self.default_remediation

    def public(self) -> dict:
        from unimem.telemetry import redact

        return {"code": self.code, "message": redact(str(self)), "retryable": self.retryable,
                "remediation": redact(self.remediation)}


class DomainError(UniMemError, ValueError):
    status_code = 400
    code = "domain_error"
    default_remediation = "Check the request against /v1/openapi.json."


class InfrastructureError(UniMemError, RuntimeError):
    status_code = 503
    code = "infrastructure_error"
    retryable = True
    default_remediation = "Retry with backoff after checking provider and database availability."


class CapabilityError(UniMemError, RuntimeError):
    status_code = 503
    code = "capability_error"
    default_remediation = "Install or enable the optional capability before retrying."


class ConfigurationError(UniMemError, RuntimeError):
    code = "configuration_error"
    default_remediation = "Correct the server configuration; repeating this request will not fix it."


def remediation_hint(error: BaseException) -> str:
    """Return a deterministic, non-secret hint for a generic operator error."""
    if isinstance(error, UniMemError):
        return error.remediation
    if isinstance(error, ImportError):
        return "Install the documented optional extra in the same Python environment."
    if isinstance(error, PermissionError):
        return "Check the credential's principal and scopes; do not retry with elevated caller data."
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "Check connectivity and retry within a bounded deadline."
    if isinstance(error, ValueError):
        return "Check field types, limits, timestamps and configuration."
    if isinstance(error, OSError):
        return "Check the path, permissions, or local service named in the error."
    return "Review the supplied values and local logs for details."
