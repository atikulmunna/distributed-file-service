"""Browser smoke test (D5): the UI uploads through a real server in a real browser.

Run with RUN_E2E=1 (needs `pip install -r requirements-e2e.txt` and a browser:
`python -m playwright install chromium`, or E2E_BROWSER_CHANNEL=chrome for an installed Chrome).
Each test starts its own server on a free port with SQLite and local storage in a temp directory.
Waits use locators, never page-evaluated strings: the page's Content-Security-Policy forbids eval.
"""

import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.skipif(os.getenv("RUN_E2E") != "1", reason="Set RUN_E2E=1 (see module docstring)."),
    pytest.mark.timeout(240),  # a server start, a browser and an upload; the suite default is 60 s
]

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "e2e-key"


def _free_port() -> int:
    # Chrome refuses some ports (5060, 6000, ...); every port it blocks is at or below 10080.
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port > 10080:
            return port


@contextmanager
def running_server(tmp_path: Path, **settings: str):
    port = _free_port()
    env = {
        **os.environ,
        "DFS_ENV_FILE": "",
        "APP_ENV": "development",
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        "STORAGE_BACKEND": "local",
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "QUEUE_BACKEND": "memory",
        "FINALIZER_IN_PROCESS": "true",
        "API_KEY_MAPPINGS": f"{API_KEY}:e2e-user",
        "ADMIN_USER_IDS": "e2e-user",
        "API_RATE_LIMIT_PER_MINUTE": "0",
        **settings,
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{base}/ready", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the server did not become ready"
            time.sleep(0.3)
        yield base
    finally:
        server.terminate()
        server.wait(timeout=30)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(channel=os.getenv("E2E_BROWSER_CHANNEL") or None)
        yield launched
        launched.close()


def _open(browser, base: str):
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + "/")
    page.fill("#api-key", API_KEY)
    page.click("#key-form button[type=submit]")
    page.locator("#key-status[data-state=ok]").wait_for(timeout=10_000)
    return context, page, errors


def _upload(page, path: Path, parallelism: int = 4) -> None:
    page.set_input_files("#file-input", str(path))
    page.locator("#plan:not([hidden])").wait_for()
    page.click("#advanced summary")
    page.fill("#parallelism", str(parallelism))
    page.click("#start-upload")
    page.locator("#events li span", has_text="Upload completed").wait_for(timeout=120_000)


def test_upload_completes_and_downloads(browser, tmp_path) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(os.urandom(12 * 1000 * 1000))
    with running_server(tmp_path) as base:
        context, page, errors = _open(browser, base)
        _upload(page, source)
        page.locator("#upload-detail .status[data-status=COMPLETED]").wait_for(timeout=10_000)
        with page.expect_download() as info:
            page.click("#detail-actions >> text=Download")
        target = tmp_path / "downloaded.bin"
        info.value.save_as(target)
        context.close()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()
    assert errors == []


def test_whole_file_checksum_is_verified(browser, tmp_path) -> None:
    source = tmp_path / "verified.bin"
    source.write_bytes(os.urandom(3 * 1000 * 1000))
    with running_server(tmp_path) as base:
        context, page, errors = _open(browser, base)
        page.set_input_files("#file-input", str(source))
        page.click("#advanced summary")
        page.check("#whole-checksum")
        page.click("#start-upload")
        page.locator("#events li span", has_text="Upload completed").wait_for(timeout=120_000)
        page.locator("#upload-detail dd", has_text="Matched the declared SHA-256").wait_for(timeout=10_000)
        context.close()
    assert errors == []


def test_throttled_chunks_are_retried(browser, tmp_path) -> None:
    # The server takes one chunk of an upload at a time (the fair-share limit, which /v1/limits does
    # not report, so the page sends 4 at once). Chrome hashes chunks one after another, which
    # staggers its requests, so the test holds the first four chunk requests and releases them
    # together: they reach the real server at the same moment, the extra ones are answered 429 with
    # Retry-After, and the page must retry them until the upload completes.
    source = tmp_path / "throttled.bin"
    source.write_bytes(os.urandom(12 * 1024 * 1024))
    limits = {"MAX_FAIR_INFLIGHT_CHUNKS_PER_UPLOAD": "1", "CHUNK_SIZE_BYTES": str(1024 * 1024)}
    with running_server(tmp_path, **limits) as base:
        context, page, errors = _open(browser, base)
        held = []

        def gate(route):
            if route.request.method != "PUT" or len(held) >= 4:
                route.continue_()
                return
            held.append(route)
            if len(held) == 4:
                for waiting in held:
                    waiting.continue_()

        page.route("**/v1/uploads/*/chunks/*", gate)
        _upload(page, source, parallelism=4)
        throttled = page.locator("#events li span", has_text="throttled")
        assert throttled.count() >= 1
        assert "per-upload fair-share limit reached" in throttled.first.text_content()
        assert page.locator("#events li span", has_text="Retried (attempt").count() >= 1
        page.locator("#upload-detail .status[data-status=COMPLETED]").wait_for(timeout=10_000)
        context.close()
    assert errors == []


def test_resume_after_reload_is_offered_and_finishes(browser, tmp_path) -> None:
    source = tmp_path / "reload.bin"
    source.write_bytes(os.urandom(12 * 1024 * 1024))
    with running_server(tmp_path, CHUNK_SIZE_BYTES=str(1024 * 1024)) as base:
        context, page, errors = _open(browser, base)

        def delayed(route):
            time.sleep(0.3)
            route.continue_()

        page.route("**/v1/uploads/*/chunks/*", delayed)
        page.set_input_files("#file-input", str(source))
        page.click("#advanced summary")
        page.fill("#parallelism", "1")
        page.click("#start-upload")
        page.locator("#chunk-map .cell.confirmed").nth(2).wait_for(timeout=30_000)
        page.close()  # the tab is closed mid-upload; this browser's storage remains

        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(base + "/")
        page.fill("#api-key", API_KEY)
        page.click("#key-form button[type=submit]")
        page.locator("#key-status[data-state=ok]").wait_for(timeout=10_000)
        page.set_input_files("#file-input", str(source))
        offer = page.locator("#resume-upload:not([hidden])")
        offer.wait_for(timeout=10_000)
        assert "chunks already on server" in offer.text_content()
        offer.click()
        page.locator("#events li span", has_text="Upload completed").wait_for(timeout=120_000)
        assert page.locator("#upload-detail .status[data-status=COMPLETED]").count() == 1
        context.close()
    assert errors == []


def test_resuming_with_a_different_file_is_refused(browser, tmp_path) -> None:
    mib = 1024 * 1024
    original = tmp_path / "original" / "same-name.bin"
    impostor = tmp_path / "impostor" / "same-name.bin"
    original.parent.mkdir()
    impostor.parent.mkdir()
    original.write_bytes(os.urandom(4 * mib))
    impostor.write_bytes(os.urandom(4 * mib))  # same name and size, different content
    with running_server(tmp_path) as base:
        init = httpx.post(
            f"{base}/v1/uploads/init",
            json={"file_name": "same-name.bin", "file_size": 4 * mib, "chunk_size": mib},
            headers={"X-API-Key": API_KEY},
        )
        upload_id = init.json()["upload_id"]
        chunk = original.read_bytes()[:mib]
        put = httpx.put(
            f"{base}/v1/uploads/{upload_id}/chunks/0",
            content=chunk,
            headers={"X-API-Key": API_KEY, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        assert put.status_code == 202, put.text

        context, page, errors = _open(browser, base)
        page.set_input_files("#file-input", str(impostor))
        page.locator("#uploads-body tr", has_text="same-name.bin").first.click()
        page.click("#detail-actions >> text=Resume")
        page.locator("#events li span", has_text="does not match this upload").wait_for(timeout=30_000)
        detail = httpx.get(f"{base}/v1/uploads/{upload_id}", headers={"X-API-Key": API_KEY}).json()
        assert detail["uploaded_chunks"] == 1  # nothing from the other file was sent
        context.close()
    assert errors == []
