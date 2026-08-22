"""Error types shared across the MCP server.

Tools never let a raw exception escape to the client (the MCP SDK folds
an uncaught exception's message straight into the client-visible
ToolError — verified against mcp==2.0.0 — so anything with a file path,
SQL fragment, or internal detail in its `str()` would leak). Every tool
catches `KGError` (and anything unexpected) at its boundary, logs the
full detail server-side via `logging.exception`, and returns a small
sanitized `{"error": {...}}` payload instead of raising.
"""


class KGError(Exception):
    """Base class for errors safe to summarize back to the client."""

    code = "internal_error"

    def __init__(self, message, code=None):
        super().__init__(message)
        if code:
            self.code = code


class DatabaseNotFoundError(KGError):
    code = "db_not_found"


class DatabaseUnavailableError(KGError):
    code = "db_unavailable"


class QueryTimeoutError(KGError):
    code = "query_timeout"


class InvalidInputError(KGError):
    code = "invalid_input"


class DepthLimitExceededError(KGError):
    code = "depth_limit_exceeded"


class NotFoundError(KGError):
    """Requested node/service/API genuinely doesn't exist in the graph —
    distinct from a system error. Callers surface this as a normal
    ``not_found`` result rather than an ``error`` payload."""

    code = "not_found"


def to_error_payload(exc):
    """Render any exception as the sanitized shape every tool returns.
    Unrecognized exceptions collapse to a generic message — their real
    text is never forwarded, only logged server-side by the caller.
    """
    if isinstance(exc, KGError):
        return {"code": exc.code, "message": str(exc)}
    return {"code": "internal_error", "message": "An internal error occurred. See server logs for details."}
