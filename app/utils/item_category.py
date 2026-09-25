"""A user's categories.

Two tables are involved:

- ``Category`` is a name. ``user_id IS NULL`` marks the shared, normalized
  categories every account can use; anything else is one user's own.
- ``ItemCategory`` puts a Category into one user's closet, with that user's
  sort order. ``Item.category_id`` points at the ItemCategory, not the
  Category.

A user's category list is their ItemCategory rows, and that is what every
screen should show, empty ones included. Shared categories the user has not
adopted are only suggestions.

Invariants (enforced by migrations/category_management.sql plus the code
here):

- at most one ItemCategory per (user, category);
- category names are unique, case- and whitespace-insensitively, among the
  shared categories and within each user's own ones;
- a user's own category never shadows a shared one of the same name: name
  lookups resolve to the shared row, and renames that would collide are
  refused so the client can offer a merge instead.
"""

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.base import Category, CategoryBenchmark, Item, ItemCategory

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 50


def normalize_category_name(name: Optional[str]) -> str:
    """Trim and collapse internal whitespace. Raises 400 on an unusable name."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(400, "Category name is required.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise HTTPException(400, f"Category name must be {MAX_NAME_LENGTH} characters or fewer.")
    return cleaned


def _visible(query, user_id: int):
    return query.filter(or_(Category.user_id == user_id, Category.user_id.is_(None)))


def find_category_by_name(session: Session, name: str, user_id: int,
                          exclude_id: Optional[int] = None) -> Optional[Category]:
    """The category visible to this user with this name, ignoring case.

    Shared wins over the user's own: shared categories feed analytics, and
    after migrations/category_management.sql the two never coexist anyway.
    """
    q = _visible(session.query(Category), user_id).filter(
        func.lower(Category.name) == name.lower())
    if exclude_id is not None:
        q = q.filter(Category.id != exclude_id)
    return q.order_by(Category.user_id.isnot(None), Category.id).first()


def get_visible_category(session: Session, category_id: int, user_id: int) -> Category:
    category = _visible(session.query(Category), user_id).filter(
        Category.id == category_id).first()
    if not category:
        raise HTTPException(404, "Category does not exist.")
    return category


def resolve_category(session: Session, category_name: str, user_id: int) -> int:
    """Category id for a typed name, creating the user's own category if new."""
    name = normalize_category_name(category_name)
    existing = find_category_by_name(session, name, user_id)
    if existing:
        return existing.id

    try:
        with session.begin_nested():
            category = Category(name=name, user_id=user_id)
            session.add(category)
        return category.id
    except IntegrityError:
        # A concurrent request created it first (uq_category_user_name).
        existing = find_category_by_name(session, name, user_id)
        if existing:
            return existing.id
        raise


def _next_sort_order(session: Session, user_id: int) -> int:
    current = session.query(func.max(ItemCategory.sort_order)).filter(
        ItemCategory.user_id == user_id).scalar()
    return 0 if current is None else current + 1


def find_item_category(session: Session, category_id: int, user_id: int) -> Optional[ItemCategory]:
    return session.query(ItemCategory).filter_by(
        category_id=category_id, user_id=user_id).order_by(ItemCategory.id).first()


def ensure_item_category(session: Session, category_id: int, user_id: int) -> ItemCategory:
    """The user's ItemCategory for a Category, creating it at the end of their list.

    Race-safe: two items saved at once used to create two ItemCategory rows
    for the same category, which showed up as duplicate groups in the closet.
    uq_itemcategory_user_category now rejects the second insert and we return
    the winner's row.
    """
    existing = find_item_category(session, category_id, user_id)
    if existing:
        return existing

    get_visible_category(session, category_id, user_id)

    try:
        with session.begin_nested():
            item_category = ItemCategory(
                category_id=category_id, user_id=user_id,
                sort_order=_next_sort_order(session, user_id))
            session.add(item_category)
        return item_category
    except IntegrityError:
        existing = find_item_category(session, category_id, user_id)
        if existing:
            return existing
        logger.exception("Failed to create item category")
        raise HTTPException(400, "An error occurred while creating category.")


def get_or_create_item_category(session: Session, category_id: int, user_id: int) -> int:
    """ItemCategory id for a Category id. Kept for existing callers."""
    return ensure_item_category(session, category_id, user_id).id


def resolve_import_category(session: Session, category_name: str, user_id: int,
                            category_cache: dict) -> Optional[int]:
    """ItemCategory id for a category name in an import file, cached per import."""
    try:
        name = normalize_category_name(category_name)
    except HTTPException:
        return None

    key = name.lower()
    if key not in category_cache:
        category_id = resolve_category(session, name, user_id)
        category_cache[key] = get_or_create_item_category(session, category_id, user_id)
    return category_cache[key]


def forked_shared_ids(session: Session, user_id: int) -> set[int]:
    """Shared categories this user has replaced with their own, by renaming
    one or merging it into one of theirs. Not offered as suggestions."""
    rows = session.query(Category.forked_from_id).filter(
        Category.user_id == user_id, Category.forked_from_id.isnot(None)).all()
    # Picking the shared category again (e.g. typing its name) brings it back.
    adopted = session.query(ItemCategory.category_id).filter(ItemCategory.user_id == user_id)
    return {r[0] for r in rows} - {r[0] for r in adopted}


def move_benchmark(session: Session, user_id: int, old_name: str, new_name: str) -> None:
    """Lifespan overrides are keyed by category name; carry one across a rename or merge.

    If the destination already has an override, it wins and the old one is dropped.
    """
    if old_name == new_name:
        return
    old = session.query(CategoryBenchmark).filter_by(user_id=user_id, category_name=old_name).first()
    if not old:
        return
    target = session.query(CategoryBenchmark).filter_by(user_id=user_id, category_name=new_name).first()
    if target:
        session.delete(old)
    else:
        old.category_name = new_name


def _delete_if_unused(session: Session, category: Category, user_id: int) -> None:
    """Delete one of the user's own categories once nothing points at it."""
    if category.user_id != user_id:
        return
    session.flush()
    if not session.query(ItemCategory.id).filter_by(category_id=category.id).first():
        session.delete(category)


def merge_categories(session: Session, user_id: int, source: Category, target: Category) -> ItemCategory:
    """Move all of the user's items from source into target and remove source.

    Returns the target ItemCategory. Target keeps its own sort position.
    """
    if source.id == target.id:
        raise HTTPException(400, "Cannot merge a category into itself.")

    target_ic = ensure_item_category(session, target.id, user_id)
    source_ics = session.query(ItemCategory).filter_by(
        category_id=source.id, user_id=user_id).all()

    for source_ic in source_ics:
        session.query(Item).filter(
            Item.user_id == user_id, Item.category_id == source_ic.id
        ).update({Item.category_id: target_ic.id}, synchronize_session="fetch")
        session.delete(source_ic)

    # Merging a shared category into one of the user's own says the user's
    # replaces it, the same as renaming it would have; stop suggesting it.
    if source.user_id is None and target.user_id == user_id and target.forked_from_id is None:
        target.forked_from_id = source.id

    move_benchmark(session, user_id, source.name, target.name)
    _delete_if_unused(session, source, user_id)
    return target_ic


def remove_category(session: Session, user_id: int, category: Category) -> int:
    """Take a category out of the user's list. Its items become uncategorized.

    Shared categories are only unlinked (they stay available as suggestions);
    the user's own are deleted. Returns how many items were uncategorized.
    """
    ics = session.query(ItemCategory).filter_by(category_id=category.id, user_id=user_id).all()
    moved = 0
    for ic in ics:
        moved += session.query(Item).filter(
            Item.user_id == user_id, Item.category_id == ic.id
        ).update({Item.category_id: None}, synchronize_session="fetch")
        session.delete(ic)

    if category.user_id == user_id:
        session.delete(category)
    return moved
