"""add multipart columns

Revision ID: 20260218_000002
Revises: 20260218_000001
Create Date: 2026-02-18 21:05:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260218_000002"
down_revision: Union[str, Sequence[str], None] = "20260218_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("multipart_upload_id", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("s3_etag", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "s3_etag")
    op.drop_column("uploads", "multipart_upload_id")
