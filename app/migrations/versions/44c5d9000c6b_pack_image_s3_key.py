"""pack image s3 key

Revision ID: 44c5d9000c6b
Revises: 15f992354be3
Create Date: 2021-03-17 21:05:10.149573

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '44c5d9000c6b'
down_revision = '15f992354be3'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('packimage', sa.Column('s3_key', sa.String(), nullable=True))
    op.add_column('packimage', sa.Column('s3_url', sa.String(), nullable=True))
    op.drop_column('packimage', 'url')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('packimage', sa.Column('url', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.drop_column('packimage', 's3_url')
    op.drop_column('packimage', 's3_key')
    # ### end Alembic commands ###
