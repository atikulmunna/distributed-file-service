"""The browser UI: one static page (app/static) that talks to the JSON API, with no build step."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# The page loads only its own script and stylesheet and talks only to this origin. No inline code is
# allowed, so injected markup could not run even if some text were ever inserted as HTML.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

router = APIRouter()


@router.get("/", include_in_schema=True)
@router.get("/ui", include_in_schema=True)
@router.get("/console", include_in_schema=True)
def web_console() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY, "Cache-Control": "no-cache"},
    )
