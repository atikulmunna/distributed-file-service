from app.config import settings
from app.db import engine_options


def test_postgres_engine_uses_configured_pool(monkeypatch) -> None:
    monkeypatch.setattr(settings, "db_pool_size", 12)
    monkeypatch.setattr(settings, "db_max_overflow", 8)
    monkeypatch.setattr(settings, "db_pool_timeout_s", 7)
    options = engine_options("postgresql+psycopg://u:p@db:5432/dfs")
    assert options["pool_size"] == 12
    assert options["max_overflow"] == 8
    assert options["pool_timeout"] == 7
    assert options["pool_pre_ping"] is True  # drop connections the server or a proxy closed


def test_sqlite_engine_keeps_its_own_options() -> None:
    options = engine_options("sqlite:///./x.db")
    assert options["connect_args"] == {"check_same_thread": False}
    assert "pool_size" not in options
