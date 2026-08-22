"""Per-tool logging + the error boundary every tool is wrapped in.

Applied as the innermost decorator (before @server.tool()) so it wraps
the plain function the MCP SDK introspects for its schema — functools.wraps
sets __wrapped__, which inspect.signature() follows, so the SDK still
sees the original parameter names/types/docstring.

Logs tool name, duration, and result count on every call (Section 22);
never logs raw exception text or full results — only counts.
"""
import functools
import logging
import time

from .errors import KGError, to_error_payload

_CONFIGURED = False


def configure_logging(level="INFO"):
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _CONFIGURED = True


def _result_count(result):
    if isinstance(result, dict):
        if "error" in result:
            return 0
        total, found_list = 0, False
        for key in ("results", "dependencies", "dependents", "chain", "functions", "relationships"):
            value = result.get(key)
            if isinstance(value, list):
                found_list = True
                total += len(value)
        return total if found_list else 1
    if isinstance(result, list):
        return len(result)
    return 1 if result is not None else 0


def tool_boundary(name):
    logger = logging.getLogger(f"kg_mcp.tools.{name}")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.info("tool=%s duration_ms=%s result_count=%d", name, duration_ms, _result_count(result))
                return result
            except KGError as exc:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.warning("tool=%s duration_ms=%s error_code=%s", name, duration_ms, exc.code)
                return {"error": to_error_payload(exc)}
            except Exception:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.exception("tool=%s duration_ms=%s unexpected_error", name, duration_ms)
                return {"error": to_error_payload(RuntimeError("internal"))}
        return wrapper
    return decorator
