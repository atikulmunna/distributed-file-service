"""upload failure reason

Revision ID: 20261109_000006
Revises: 20261109_000005
Create Date: 2026-11-09 18:00:00

Adds uploads.failure_reason, a human-readable reason for ABORTED and FAILED uploads.
The new statuses COMPLETING and VERIFYING need no migration: status is a plain string column.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20261109_000006"
down_revision: Union[str, Sequence[str], None] = "20261109_000005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("failure_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("uploads", "failure_reason")
