"""Deterministic concurrency for tests: run an action inside the server's own DB transaction,
right before a matching SQL statement executes."""

from datetime import UTC, datetime

from sqlalchemy import event

from app.db import engine


class before_statement:  # lowercase on purpose: used as a context manager, reads like a phrase
    """Run `action(conn, cursor, parameters)` once, right before the first statement whose SQL starts
    with `prefix` (case-insensitive) and for which `match(parameters)` is true."""

    def __init__(self, prefix: str, action, match=lambda parameters: True) -> None:
        self.prefix = prefix.upper()
        self.action = action
        self.match = match
        self.fired = False

    def _hook(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if not self.fired and statement.lstrip().upper().startswith(self.prefix) and self.match(parameters):
            self.fired = True
            self.action(conn, cursor, parameters)

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(engine, "before_cursor_execute", self._hook)


def raw_insert(conn, cursor, table: str, columns: list[str], values: tuple) -> None:
    """Insert on the server's own DBAPI connection (same transaction), bypassing SQLAlchemy."""
    mark = "?" if conn.dialect.name == "sqlite" else "%s"
    raw = cursor.connection.cursor()
    raw.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join([mark] * len(columns))})", values)
    raw.close()


def db_now(conn):
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%d %H:%M:%S.%f") if conn.dialect.name == "sqlite" else now
