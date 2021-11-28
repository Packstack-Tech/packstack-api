"""image thumb filename

Revision ID: 1149ff60d9c5
Revises: afc19309c361
Create Date: 2021-11-17 20:54:58.179713

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1149ff60d9c5'
down_revision = 'afc19309c361'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('image', sa.Column('s3_url_thumb', sa.String(), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('image', 's3_url_thumb')
    # ### end Alembic commands ###