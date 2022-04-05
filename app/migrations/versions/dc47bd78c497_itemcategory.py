"""itemcategory

Revision ID: dc47bd78c497
Revises: 3cbfae9d3e32
Create Date: 2022-04-03 13:03:36.602527

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'dc47bd78c497'
down_revision = '3cbfae9d3e32'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('itemcategory',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('consumable', sa.Boolean(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['category.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_itemcategory_id'), 'itemcategory', ['id'], unique=False)
    op.drop_column('category', 'consumable')
    op.drop_column('category', 'sort_order')
    op.drop_constraint('item_category_id_fkey', 'item', type_='foreignkey')
    op.create_foreign_key(None, 'item', 'itemcategory', ['category_id'], ['id'])
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'item', type_='foreignkey')
    op.create_foreign_key('item_category_id_fkey', 'item', 'category', ['category_id'], ['id'])
    op.add_column('category', sa.Column('sort_order', sa.INTEGER(), autoincrement=False, nullable=True))
    op.add_column('category', sa.Column('consumable', sa.BOOLEAN(), autoincrement=False, nullable=True))
    op.drop_index(op.f('ix_itemcategory_id'), table_name='itemcategory')
    op.drop_table('itemcategory')
    # ### end Alembic commands ###