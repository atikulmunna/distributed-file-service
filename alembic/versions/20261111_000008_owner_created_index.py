"""uploads owner and creation index

Revision ID: 20261111_000008
Revises: 20261110_000007
Create Date: 2026-11-11 10:00:00

Index for GET /v1/uploads: one owner's uploads, newest first, paged by (created_at, id).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261111_000008"
down_revision: str | Sequence[str] | None = "20261110_000007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_uploads_owner_created", "uploads", ["owner_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_uploads_owner_created", table_name="uploads")
