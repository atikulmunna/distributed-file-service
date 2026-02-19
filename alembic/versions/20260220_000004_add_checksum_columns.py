"""add checksum columns

Revision ID: 20260220_000004
Revises: 20260220_000003
Create Date: 2026-02-20 10:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260220_000004"
down_revision: Union[str, Sequence[str], None] = "20260220_000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    upload_columns = {column["name"] for column in inspector.get_columns("uploads")}
    chunk_columns = {column["name"] for column in inspector.get_columns("chunks")}
    if "file_checksum_sha256" not in upload_columns:
        op.add_column("uploads", sa.Column("file_checksum_sha256", sa.String(length=64), nullable=True))
    if "chunk_checksum_sha256" not in chunk_columns:
        op.add_column("chunks", sa.Column("chunk_checksum_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    upload_columns = {column["name"] for column in inspector.get_columns("uploads")}
    chunk_columns = {column["name"] for column in inspector.get_columns("chunks")}
    if "chunk_checksum_sha256" in chunk_columns:
        op.drop_column("chunks", "chunk_checksum_sha256")
    if "file_checksum_sha256" in upload_columns:
        op.drop_column("uploads", "file_checksum_sha256")
