"""Add is_leftover to meals

Revision ID: 7a3f2c9d1b8e
Revises: 44f85c07519a
Create Date: 2026-07-21 07:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a3f2c9d1b8e"
down_revision: Union[str, Sequence[str], None] = "44f85c07519a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "meals",
        sa.Column("is_leftover", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column(
        "meals",
        "is_leftover",
        existing_type=sa.Boolean(),
        server_default=None,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("meals", "is_leftover")
