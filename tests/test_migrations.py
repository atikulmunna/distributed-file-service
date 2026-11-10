"""Alembic migrations on Postgres, run in a subprocess against a throwaway database.

A subprocess keeps Alembic's logging setup (fileConfig) from disabling the app's loggers
for the rest of the test session.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.db import engine

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="column type migrations are Postgres-specific"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DB = "dfs_migration_test"
BIG_FILE = 3 * 1024**3  # over the 32-bit INTEGER limit


def _admin_execute(statement: str) -> None:
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(statement))
    finally:
        admin.dispose()


@pytest.fixture
def migration_db_url():
    _admin_execute(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)')
    _admin_execute(f'CREATE DATABASE "{MIGRATION_DB}"')
    yield make_url(settings.database_url).set(database=MIGRATION_DB).render_as_string(hide_password=False)
    _admin_execute(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)')


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url, "DFS_ENV_FILE": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]


def _integer_types(url: str) -> dict[str, str]:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            columns = {
                f"{table}.{column}": conn.scalar(
                    text("SELECT data_type FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
                    {"t": table, "c": column},
                )
                for table, column in [
                    ("uploads", "file_size"),
                    ("uploads", "chunk_size"),
                    ("chunks", "id"),
                    ("chunk_request_idempotency", "id"),
                ]
            }
            sequences = {
                f"seq:{table}.id": conn.scalar(
                    text(
                        "SELECT data_type FROM information_schema.sequences"
                        " WHERE sequence_name = split_part(pg_get_serial_sequence(:t, 'id'), '.', 2)"
                    ),
                    {"t": table},
                )
                for table in ("chunks", "chunk_request_idempotency")
            }
    finally:
        eng.dispose()
    return columns | sequences


def _execute(url: str, statement: str, **params) -> object:
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            result = conn.execute(text(statement), params)
            return result.scalar() if result.returns_rows else None
    finally:
        eng.dispose()


INSERT_UPLOAD = (
    "INSERT INTO uploads (id, owner_id, file_name, file_size, chunk_size, total_chunks, status, created_at, updated_at)"
    " VALUES (:id, 'u', 'f.bin', :size, 5, 2, 'IN_PROGRESS', now(), now())"
)


def test_migration_head_uses_64_bit_sizes_and_ids(migration_db_url) -> None:
    _alembic(migration_db_url, "upgrade", "head")
    assert set(_integer_types(migration_db_url).values()) == {"bigint"}
    _execute(migration_db_url, INSERT_UPLOAD, id="big", size=BIG_FILE)
    assert _execute(migration_db_url, "SELECT file_size FROM uploads WHERE id = 'big'") == BIG_FILE


def test_migration_round_trip(migration_db_url) -> None:
    _alembic(migration_db_url, "upgrade", "head")
    _execute(migration_db_url, INSERT_UPLOAD, id="small", size=10)
    _execute(
        migration_db_url,
        "INSERT INTO chunks (upload_id, chunk_index, size_bytes, s3_key, status, retry_count, created_at, updated_at)"
        " VALUES ('small', 0, 5, 'k', 'UPLOADED', 0, now(), now())",
    )

    _alembic(migration_db_url, "downgrade", "20260220_000004")  # the revision before 64-bit sizes
    assert set(_integer_types(migration_db_url).values()) == {"integer"}
    assert _execute(migration_db_url, "SELECT count(*) FROM chunks WHERE upload_id = 'small'") == 1

    _alembic(migration_db_url, "upgrade", "head")
    assert set(_integer_types(migration_db_url).values()) == {"bigint"}
    assert _execute(migration_db_url, "SELECT file_size FROM uploads WHERE id = 'small'") == 10


def test_every_migration_downgrades_to_base_and_back(migration_db_url) -> None:
    _alembic(migration_db_url, "upgrade", "head")
    _alembic(migration_db_url, "downgrade", "base")
    _alembic(migration_db_url, "upgrade", "head")
    assert set(_integer_types(migration_db_url).values()) == {"bigint"}


def test_last_activity_backfilled_from_updated_at(migration_db_url) -> None:
    _alembic(migration_db_url, "upgrade", "20261109_000006")
    _execute(
        migration_db_url,
        "INSERT INTO uploads (id, owner_id, file_name, file_size, chunk_size, total_chunks, status, created_at, updated_at)"
        " VALUES ('old', 'u', 'f.bin', 10, 5, 2, 'IN_PROGRESS', now(), TIMESTAMPTZ '2026-01-01 00:00:00+00')",
    )
    _alembic(migration_db_url, "upgrade", "head")
    assert _execute(migration_db_url, "SELECT last_activity_at = updated_at FROM uploads WHERE id = 'old'") is True
    assert (
        _execute(
            migration_db_url,
            "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'uploads' AND column_name = 'last_activity_at'",
        )
        == "NO"
    )
