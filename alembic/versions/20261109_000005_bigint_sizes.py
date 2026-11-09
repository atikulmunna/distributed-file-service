"""64-bit file sizes and row ids

Revision ID: 20261109_000005
Revises: 20260220_000004
Create Date: 2026-11-09 12:00:00

uploads.file_size and uploads.chunk_size become BIGINT so files over 2 GiB can be stored
(the spec requires files up to 5 GB). chunks.id and chunk_request_idempotency.id become
BIGINT together with their sequences: altering a serial column's type does not widen the
sequence behind it, which would otherwise still stop at 2^31 - 1.

SQLite needs no change: its INTEGER storage class is already 64-bit.
On Postgres, ALTER COLUMN ... TYPE rewrites the table under an exclusive lock.
Downgrade fails if any stored value or sequence position no longer fits in 32 bits.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20261109_000005"
down_revision: Union[str, Sequence[str], None] = "20260220_000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ("uploads", "file_size"),
    ("uploads", "chunk_size"),
    ("chunks", "id"),
    ("chunk_request_idempotency", "id"),
]
SERIAL_TABLES = ("chunks", "chunk_request_idempotency")


def _set_integer_width(new_type: sa.types.TypeEngine, old_type: sa.types.TypeEngine, sequence_type: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, column in COLUMNS:
        op.alter_column(table, column, type_=new_type, existing_type=old_type, existing_nullable=False)
    for table in SERIAL_TABLES:
        sequence = bind.scalar(sa.text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})
        op.execute(sa.text(f"ALTER SEQUENCE {sequence} AS {sequence_type}"))


def upgrade() -> None:
    _set_integer_width(sa.BigInteger(), sa.Integer(), "bigint")


def downgrade() -> None:
    _set_integer_width(sa.Integer(), sa.BigInteger(), "integer")
