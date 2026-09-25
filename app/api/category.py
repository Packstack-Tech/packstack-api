import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import func, or_

from models.base import User, Category, Item, ItemCategory
from utils.auth import authenticate
from utils.item_category import (
    ensure_item_category, find_category_by_name, find_item_category, forked_shared_ids,
    get_visible_category, merge_categories, move_benchmark, normalize_category_name,
    remove_category,
)

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])


def _commit(message: str):
    try:
        db.session.commit()
    except Exception:
        logger.exception(message)
        db.session.rollback()
        raise HTTPException(400, message)


def _name_taken(existing: Category, user_id: int) -> HTTPException:
    """409 carrying the colliding category so the client can offer a merge."""
    ic = find_item_category(db.session, existing.id, user_id)
    return HTTPException(409, {
        "code": "category_exists",
        "message": f'A category named "{existing.name}" already exists.',
        "category_id": existing.id,
        "item_category_id": ic.id if ic else None,
        "name": existing.name,
    })


class CategoryType(BaseModel):
    name: str
    consumable: bool = False


@route.post("", status_code=201)
def create(payload: CategoryType, response: Response, user: User = Depends(authenticate)):
    """Add a category to the user's list. Idempotent by name: an existing
    category (shared or theirs) with the same name is adopted, not duplicated."""
    name = normalize_category_name(payload.name)
    category = find_category_by_name(db.session, name, user.id)
    if category:
        already_listed = find_item_category(db.session, category.id, user.id)
        if already_listed:
            response.status_code = 200
            return already_listed
    else:
        category = Category(user_id=user.id, name=name)
        db.session.add(category)
        db.session.flush()

    item_category = ensure_item_category(db.session, category.id, user.id)
    _commit("Unable to create category.")
    db.session.refresh(item_category)
    return item_category


@route.get("")
def fetch(user: User = Depends(authenticate)):
    """Every category the user can pick: their own plus the shared ones.

    Used by the item form's category picker in shipped app versions. Shared
    categories the user has replaced (renamed, or merged into one of theirs)
    are left out, so they don't come back as look-alike duplicates.
    """
    hidden = forked_shared_ids(db.session, user.id)
    q = db.session.query(Category).filter(
        or_(Category.user_id == user.id, Category.user_id.is_(None)))
    if hidden:
        q = q.filter(Category.id.notin_(hidden))
    return q.order_by(Category.name).all()


@route.get("/mine")
def fetch_mine(user: User = Depends(authenticate)):
    """The user's category list, in their order, including empty categories,
    plus shared categories they haven't used yet as suggestions.

    `id` is the ItemCategory id (what Item.category_id and the sort endpoint
    use); `category_id` is the Category id (what item create/update, rename,
    delete and merge take).
    """
    counts = dict(
        db.session.query(
            Item.category_id,
            func.count(Item.id),
        ).filter(
            Item.user_id == user.id, Item.deleted.is_(False), Item.removed.isnot(True),
        ).group_by(Item.category_id).all()
    )
    archived = dict(
        db.session.query(
            Item.category_id,
            func.count(Item.id),
        ).filter(
            Item.user_id == user.id, Item.deleted.is_(False), Item.removed.is_(True),
        ).group_by(Item.category_id).all()
    )

    rows = db.session.query(ItemCategory).filter_by(user_id=user.id).order_by(
        ItemCategory.sort_order.nulls_last(), ItemCategory.id).all()

    categories = [{
        "id": ic.id,
        "category_id": ic.category_id,
        "name": ic.category.name,
        "shared": ic.category.user_id is None,
        "sort_order": ic.sort_order,
        "item_count": counts.get(ic.id, 0),
        "archived_count": archived.get(ic.id, 0),
    } for ic in rows if ic.category]

    used = {c["category_id"] for c in categories} | forked_shared_ids(db.session, user.id)
    suggested = db.session.query(Category).filter(
        Category.user_id.is_(None), Category.id.notin_(list(used) or [-1]),
    ).order_by(Category.name).all()

    return {
        "categories": categories,
        "suggested": [{"category_id": c.id, "name": c.name} for c in suggested],
    }


class CategoryUpdateType(BaseModel):
    name: Optional[str] = None


@route.put("/{category_id}")
def update(category_id: int, payload: CategoryUpdateType, user: User = Depends(authenticate)):
    """Rename a category in the user's list.

    Renaming a shared category gives the user their own copy (shared names
    are the same for everyone) and remembers which one it replaced.
    A name already used by another category the user can see is refused
    with 409 `category_exists`; the client should offer to merge instead.
    """
    category = get_visible_category(db.session, category_id, user.id)
    if payload.name is None:
        return category

    name = normalize_category_name(payload.name)
    if name == category.name:
        return category

    clash = find_category_by_name(db.session, name, user.id, exclude_id=category.id)
    if clash:
        raise _name_taken(clash, user.id)

    if category.user_id is None:
        item_category = find_item_category(db.session, category.id, user.id)
        if not item_category:
            raise HTTPException(404, "Category is not in your list.")

        fork = Category(user_id=user.id, name=name, forked_from_id=category.id)
        db.session.add(fork)
        db.session.flush()
        item_category.category_id = fork.id
        move_benchmark(db.session, user.id, category.name, name)
        _commit("Unable to update category.")
        db.session.refresh(fork)
        return fork

    old_name = category.name
    category.name = name
    move_benchmark(db.session, user.id, old_name, name)
    _commit("Unable to update category.")
    db.session.refresh(category)
    return category


class CategoryMergeType(BaseModel):
    into_category_id: int


@route.post("/{category_id}/merge")
def merge(category_id: int, payload: CategoryMergeType, user: User = Depends(authenticate)):
    """Move every item in this category into another and remove this one.

    The fix for duplicates: works across shared and own categories in either
    direction. Returns the target's ItemCategory.
    """
    source = get_visible_category(db.session, category_id, user.id)
    target = get_visible_category(db.session, payload.into_category_id, user.id)

    if source.user_id is None and not find_item_category(db.session, source.id, user.id):
        raise HTTPException(404, "Category is not in your list.")

    target_ic = merge_categories(db.session, user.id, source, target)
    _commit("Unable to merge categories.")
    db.session.refresh(target_ic)
    return target_ic


@route.delete("/{category_id}", status_code=204)
def delete(category_id: int, user: User = Depends(authenticate)):
    """Remove a category from the user's list; its items become uncategorized.

    Shared categories are unlinked (they return to suggestions); the user's
    own are deleted outright, including ones no item uses.
    """
    category = get_visible_category(db.session, category_id, user.id)
    if category.user_id is None and not find_item_category(db.session, category.id, user.id):
        raise HTTPException(404, "Category is not in your list.")

    remove_category(db.session, user.id, category)
    _commit("Unable to delete category.")
