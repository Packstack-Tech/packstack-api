from fastapi import HTTPException
from models.base import ItemCategory

def get_or_create_item_category(session, category_id, user_id):
    if not category_id:
        return None

    item_category = session.query(ItemCategory).filter_by(
        category_id=category_id, user_id=user_id).first()

    if item_category:
        return item_category.id

    # Create Item Category
    position = session.query(
        ItemCategory).filter_by(user_id=user_id).count()
    new_item_category = ItemCategory(
        category_id=category_id, user_id=user_id, sort_order=position)

    try:
        session.add(new_item_category)
        session.commit()
        session.refresh(new_item_category)
    except:
        raise HTTPException(400, "An error occurred while creating category.")

    return new_item_category.id
    