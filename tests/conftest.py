"""Test harness that isolates every run from developer config and real services.

The environment is fixed at the top of this file, before any `app` module is imported,
because the app builds its settings, DB engine, and storage backend at import time.

Database: SQLite in a temp dir by default. Set TEST_DATABASE_URL (Postgres) to run the
suite against Postgres; the database is created if it does not exist.
"""

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="dfs-tests-"))
STORAGE_ROOT = _TMP_ROOT / "storage"
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
DATABASE_URL = _TEST_DATABASE_URL or f"sqlite:///{(_TMP_ROOT / 'test.db').as_posix()}"

os.environ.update(
    {
        "DFS_ENV_FILE": "",
        "DATABASE_URL": DATABASE_URL,
        "STORAGE_BACKEND": "local",
        "STORAGE_ROOT": str(STORAGE_ROOT),
        "QUEUE_BACKEND": "memory",
        "AUTH_MODE": "api_key",
        "API_KEY_MAPPINGS": "dev-key:dev-user,other-key:other-user",
        "ADMIN_USER_IDS": "dev-user",
        "CLEANUP_ENABLED": "false",
        "AUTOSCALE_ENABLED": "false",
        "TRACING_ENABLED": "false",
        "API_RATE_LIMIT_PER_MINUTE": "0",
        # Tests drive verification explicitly; the end-to-end test turns the in-process worker on.
        "FINALIZER_IN_PROCESS": "false",
    }
)


if _TEST_DATABASE_URL.startswith("postgresql"):
    from scripts.bench_init import ensure_postgres_database

    ensure_postgres_database(_TEST_DATABASE_URL)

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.auth import api_key_rate_limiter  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.jobs import job_queue  # noqa: E402
from app.main import app  # noqa: E402
from app.storage import storage  # noqa: E402


def _empty_tables() -> None:
    tables = Base.metadata.sorted_tables
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            names = ", ".join(f'"{table.name}"' for table in tables)
            conn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        else:
            for table in reversed(tables):
                conn.execute(table.delete())


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    _empty_tables()
    shutil.rmtree(STORAGE_ROOT, ignore_errors=True)
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    api_key_rate_limiter.reset()
    job_queue.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "dev-key"}


@pytest.fixture
def slow_storage(monkeypatch):
    """Call with a delay in seconds; every storage chunk write then sleeps that long first."""

    def _apply(delay_s: float) -> None:
        real_write = storage.write_chunk

        def slow_write(*args, **kwargs):
            time.sleep(delay_s)
            return real_write(*args, **kwargs)

        monkeypatch.setattr(storage, "write_chunk", slow_write)

    return _apply


@pytest.fixture
def small_pool():
    """Bind sessions to a 2-connection pool with a 3 s checkout timeout for one test."""
    small = create_engine(settings.database_url, pool_size=2, max_overflow=0, pool_timeout=3)
    SessionLocal.configure(bind=small)
    try:
        yield small
    finally:
        SessionLocal.configure(bind=engine)
        small.dispose()


@pytest.fixture
def live_server():
    """Run the app on a real uvicorn server (TestClient hands bodies over without yielding)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
