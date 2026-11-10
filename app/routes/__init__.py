"""HTTP routers. Shared OpenAPI error responses live here."""

from typing import Any

from app.schemas import ErrorResponse, ValidationErrorResponse

COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing API key"},
    403: {"model": ErrorResponse, "description": "Invalid API key, or admin access required"},
    422: {"model": ValidationErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Throttled request"},
    500: {"model": ErrorResponse, "description": "Internal error (details are logged under request_id)"},
    503: {"model": ErrorResponse, "description": "Temporarily unavailable; retry after Retry-After seconds"},
}
