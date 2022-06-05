"""packitem-checked

Revision ID: ab0d1398e0f2
Revises: 5d935cc52800
Create Date: 2022-06-05 12:54:11.660289

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ab0d1398e0f2'
down_revision = '5d935cc52800'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('packitem', sa.Column('checked', sa.Boolean(), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('packitem', 'checked')
    # ### end Alembic commands ###