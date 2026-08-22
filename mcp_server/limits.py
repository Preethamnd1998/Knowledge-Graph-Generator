"""Small input-clamping helpers shared by every tool — Rules 5/6: result
limits and traversal depth are always bounded, regardless of what a
caller asks for.
"""
from .errors import InvalidInputError

VALID_DIRECTIONS = ("outgoing", "incoming", "both")


def clamp_limit(requested, config, default=None):
    default = default or config.max_results
    if requested is None:
        return default
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        raise InvalidInputError("limit must be an integer")
    if requested < 1:
        raise InvalidInputError("limit must be at least 1")
    return min(requested, config.max_results)


def clamp_depth(requested, config, default=1):
    if requested is None:
        return default
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        raise InvalidInputError("depth must be an integer")
    if requested < 1:
        raise InvalidInputError("depth must be at least 1")
    return min(requested, config.max_depth)


def validate_direction(direction):
    direction = (direction or "outgoing").lower()
    if direction not in VALID_DIRECTIONS:
        raise InvalidInputError(f"direction must be one of {VALID_DIRECTIONS}")
    return direction


def require_non_empty(value, field_name):
    if not value or not str(value).strip():
        raise InvalidInputError(f"'{field_name}' is required")
    return str(value).strip()
