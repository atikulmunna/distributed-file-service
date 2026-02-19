"""add upload owner

Revision ID: 20260220_000003
Revises: 20260218_000002
Create Date: 2026-02-20 02:35:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260220_000003"
down_revision: Union[str, Sequence[str], None] = "20260218_000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("uploads")}
    if "owner_id" not in column_names:
        op.add_column("uploads", sa.Column("owner_id", sa.String(length=128), nullable=False, server_default="legacy"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("uploads")}
    if "owner_id" in column_names:
        op.drop_column("uploads", "owner_id")
