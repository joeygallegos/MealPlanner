"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


def upgrade():
    # Rename columns in the mealdays table
    with op.batch_alter_table("meal_days") as batch_op:
        batch_op.alter_column("is_sammy_home", new_column_name="is_starred")
        batch_op.alter_column("is_work_day", new_column_name="is_sammy_working")


def downgrade():
    # Revert the column names if rolling back
    with op.batch_alter_table("meals") as batch_op:
        batch_op.alter_column("is_starred", new_column_name="is_sammy_home")
        batch_op.alter_column("is_sammy_working", new_column_name="is_work_day")
