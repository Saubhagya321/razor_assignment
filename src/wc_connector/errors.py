"""The connector's error model (SPEC.md section 6).

Every tool failure becomes a ConnectorError, which tools.py turns into an
isError: true result. Never raise raw httpx exceptions or upstream bodies
past the client layer.
"""

from __future__ import annotations

import json


class ConnectorError(Exception):
    """A structured, model-safe error. Never carries secrets or raw upstream bodies.

    str(error) is the exact JSON the MCP tool result's text content must
    carry (SPEC.md section 6), since FastMCP turns an uncaught exception's
    str() into that text content for an isError: true result.
    """

    def __init__(self, code: str, message: str, retryable: bool, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            error["retry_after_seconds"] = self.retry_after_seconds
        return {"error": error}

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


def auth_failed() -> ConnectorError:
    return ConnectorError(
        code="AUTH_FAILED",
        message="Credentials missing, invalid, or lacking permission.",
        retryable=False,
    )


def forbidden() -> ConnectorError:
    return ConnectorError(
        code="FORBIDDEN",
        message="The request was blocked (for example by a proxy or WAF) rather than by the store's API.",
        retryable=False,
    )


def not_found(what: str = "resource") -> ConnectorError:
    return ConnectorError(code="NOT_FOUND", message=f"The requested {what} was not found.", retryable=False)


def bad_request(summary: str = "The request was invalid.") -> ConnectorError:
    return ConnectorError(code="BAD_REQUEST", message=summary, retryable=False)


def rate_limited(retry_after_seconds: float | None = None) -> ConnectorError:
    return ConnectorError(
        code="RATE_LIMITED",
        message="The store is rate-limiting requests. Try again later.",
        retryable=True,
        retry_after_seconds=retry_after_seconds,
    )


def upstream_unavailable() -> ConnectorError:
    return ConnectorError(
        code="UPSTREAM_UNAVAILABLE",
        message="The store did not respond successfully after retrying.",
        retryable=True,
    )


def input_error(message: str) -> ConnectorError:
    return ConnectorError(code="BAD_REQUEST", message=message, retryable=False)


def config_error(message: str) -> ConnectorError:
    """A local misconfiguration (e.g. bad WC_CA_BUNDLE), not an upstream failure.

    Not retryable: retrying without a human fixing the config will fail the
    same way every time.
    """
    return ConnectorError(code="CONFIG_ERROR", message=message, retryable=False)
