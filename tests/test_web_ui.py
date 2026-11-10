"""The browser UI: static files served by the API (D4: no build step), and the rules it must keep."""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
# Built from code points so this file itself contains no dash characters.
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


@pytest.mark.parametrize("path", ["/", "/ui", "/console"])
def test_console_serves_index_with_landmarks(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for landmark in ("upload", "transfer", "uploads", "service"):
        assert f'id="{landmark}"' in response.text, landmark
    assert '<script type="module" src="/static/app.js"></script>' in response.text


def test_console_page_has_a_strict_content_security_policy(client) -> None:
    policy = client.get("/").headers["content-security-policy"]
    for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'"):
        assert directive in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


@pytest.mark.parametrize(
    ("asset", "content_type"),
    [("/static/app.css", "text/css"), ("/static/app.js", "javascript")],
)
def test_static_assets_served_with_types(client, asset, content_type) -> None:
    response = client.get(asset)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_app_js_has_no_innerhtml_assignment() -> None:
    # UI rule 8: dynamic text goes in with textContent or DOM nodes, never parsed as HTML.
    for script in STATIC.rglob("*.js"):
        source = script.read_text(encoding="utf-8")
        found = re.findall(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b", source)
        assert found == [], f"{script.name} parses strings as HTML: {sorted(set(found))}"


def test_ui_copy_has_no_em_or_en_dash() -> None:
    for asset in STATIC.rglob("*"):
        if asset.is_file() and asset.suffix in {".html", ".css", ".js"}:
            text = asset.read_text(encoding="utf-8")
            assert EM_DASH not in text, f"em dash in {asset.name}"
            assert EN_DASH not in text, f"en dash in {asset.name}"


def test_page_has_no_inline_script_or_style() -> None:
    # The content security policy forbids inline code; keep the page compatible with it.
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page) is None
    assert "<style" not in page
    assert " style=" not in page
    assert re.search(r"\son[a-z]+=", page) is None  # no onclick= and similar


def test_version_route_available(client) -> None:
    payload = client.get("/version").json()
    assert {"app_name", "app_version"} <= set(payload)


def test_app_version_header_present(client) -> None:
    assert client.get("/health").headers.get("X-DFS-App-Version") is not None
