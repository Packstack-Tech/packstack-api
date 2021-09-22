"""user categories

Revision ID: 4fd131fb290c
Revises: b3ee99d6ed17
Create Date: 2021-02-20 13:07:51.840405

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4fd131fb290c'
down_revision = 'b3ee99d6ed17'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('category', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_unique_constraint('_user_category_uc', 'category', ['user_id', 'name'])
    op.create_foreign_key(None, 'category', 'user', ['user_id'], ['id'])
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'category', type_='foreignkey')
    op.drop_constraint('_user_category_uc', 'category', type_='unique')
    op.drop_column('category', 'user_id')
    # ### end Alembic commands ###
