"""Browser console pages."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.ui import ui_html

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@router.get("/ui", response_class=HTMLResponse)
@router.get("/console", response_class=HTMLResponse)
def web_console() -> HTMLResponse:
    return HTMLResponse(content=ui_html())
