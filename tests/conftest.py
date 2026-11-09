"""Test harness that isolates every run from developer config and real services.

The environment is fixed at the top of this file, before any `app` module is imported,
because the app builds its settings, DB engine, and storage backend at import time.

Database: SQLite in a temp dir by default. Set TEST_DATABASE_URL (Postgres) to run the
suite against Postgres; the database is created if it does not exist.
"""

import os
import shutil
import tempfile
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
    }
)


def _ensure_postgres_database(url: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database})
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


if _TEST_DATABASE_URL.startswith("postgresql"):
    _ensure_postgres_database(_TEST_DATABASE_URL)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.auth import api_key_rate_limiter  # noqa: E402
from app.db import Base, engine  # noqa: E402
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


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
