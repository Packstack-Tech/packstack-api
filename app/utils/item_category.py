import logging

from fastapi import HTTPException
from sqlalchemy import or_
from models.base import Category, ItemCategory

logger = logging.getLogger(__name__)


def get_or_create_item_category(session, category_id, user_id):
    item_category = session.query(ItemCategory).filter_by(
        category_id=category_id, user_id=user_id).first()

    if item_category:
        return item_category.id

    category = session.query(Category).filter(
        Category.id == category_id,
        or_(Category.user_id == user_id, Category.user_id.is_(None)),
    ).first()

    if not category:
        raise HTTPException(404, "Category does not exist.")

    position = session.query(
        ItemCategory).filter_by(user_id=user_id).count()
    new_item_category = ItemCategory(
        category_id=category_id, user_id=user_id, sort_order=position)

    try:
        session.add(new_item_category)
        session.flush()
    except Exception:
        logger.exception("Failed to create item category")
        raise HTTPException(400, "An error occurred while creating category.")

    return new_item_category.id
