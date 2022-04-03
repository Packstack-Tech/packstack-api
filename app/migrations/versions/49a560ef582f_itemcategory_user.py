"""itemcategory user

Revision ID: 49a560ef582f
Revises: dc47bd78c497
Create Date: 2022-04-03 13:07:30.808250

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '49a560ef582f'
down_revision = 'dc47bd78c497'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('itemcategory', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'itemcategory', 'user', ['user_id'], ['id'])
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'itemcategory', type_='foreignkey')
    op.drop_column('itemcategory', 'user_id')
    # ### end Alembic commands ###
