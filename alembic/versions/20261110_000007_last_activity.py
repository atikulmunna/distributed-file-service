"""upload last activity

Revision ID: 20261110_000007
Revises: 20261109_000006
Create Date: 2026-11-10 10:00:00

Adds uploads.last_activity_at: when the upload last made progress (a chunk recorded or a status
change). Cleanup judges staleness by it instead of created_at, so a long upload that keeps sending
chunks is never deleted mid-transfer. Existing rows are backfilled from updated_at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261110_000007"
down_revision: str | Sequence[str] | None = "20261109_000006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE uploads SET last_activity_at = updated_at")
    with op.batch_alter_table("uploads") as batch:  # batch mode: SQLite cannot ALTER COLUMN in place
        batch.alter_column(
            "last_activity_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),  # writers outside the ORM get a value too
        )
    op.create_index("ix_uploads_status_last_activity", "uploads", ["status", "last_activity_at"])


def downgrade() -> None:
    op.drop_index("ix_uploads_status_last_activity", table_name="uploads")
    with op.batch_alter_table("uploads") as batch:
        batch.drop_column("last_activity_at")
