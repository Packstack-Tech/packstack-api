from itertools import groupby


def enum_to_dict(enumeration):
    return {enum.name: enum.value for enum in enumeration}


def list_intersection(list1, list2):
    return list(set(list1) & set(list2))


def list_difference(list1, list2):
    return list(list(set(list1) - set(list2)) + list(set(list2) - set(list1)))


def group_by_category(pack_items):
    category_list = []
    def category_key(x): return x.item.category
    for key, group in groupby(pack_items, category_key):
        category_list.append({
            "category": key,
            "items": list(group)
        })

    return category_list


def clone_model(model, ignore_keys=[]):
    """Clone an arbitrary sqlalchemy model object without its primary key values."""

    table = model.__table__
    non_pk_columns = [
        k for k in table.columns.keys() if k not in table.primary_key]
    columns = [c for c in non_pk_columns if c not in ignore_keys]
    data = {c: getattr(model, c) for c in columns}
    return data
