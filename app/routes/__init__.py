"""HTTP routers. Shared OpenAPI error responses live here."""

from app.schemas import ErrorResponse

COMMON_ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing API key"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    429: {"model": ErrorResponse, "description": "Throttled request"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}
