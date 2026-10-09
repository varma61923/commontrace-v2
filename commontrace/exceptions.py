"""Public error categories with safe, actionable HTTP/CLI diagnostics."""
from __future__ import annotations


class CommonTraceError(Exception):
    status_code = 500
    code = "commontrace_error"
    retryable = False
    default_remediation = "Inspect the server's request ID and configuration."

    def __init__(self, message: str, *, remediation: str | None = None):
        super().__init__(message)
        self.remediation = remediation or self.default_remediation

    def public(self) -> dict:
        from commontrace.telemetry import redact

        return {"code": self.code, "message": redact(str(self)), "retryable": self.retryable,
                "remediation": redact(self.remediation)}


class DomainError(CommonTraceError, ValueError):
    status_code = 400
    code = "domain_error"
    default_remediation = "Check the request against /v1/openapi.json."


class InfrastructureError(CommonTraceError, RuntimeError):
    status_code = 503
    code = "infrastructure_error"
    retryable = True
    default_remediation = "Retry with backoff after checking provider and database availability."


class CapabilityError(CommonTraceError, RuntimeError):
    status_code = 503
    code = "capability_error"
    default_remediation = "Install or enable the optional capability before retrying."


class ConfigurationError(CommonTraceError, RuntimeError):
    code = "configuration_error"
    default_remediation = "Correct the server configuration; repeating this request will not fix it."
