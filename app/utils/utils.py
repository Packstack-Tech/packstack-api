from itertools import groupby


def enum_to_dict(enumeration):
    return {enum.name: enum.value for enum in enumeration}


def list_intersection(list1, list2):
    return list(set(list1) & set(list2))


def list_difference(list1, list2):
    return list(list(set(list1) - set(list2)) + list(set(list2) - set(list1)))


def group_by_category(pack_items):
    category_list = []
    category_key = lambda x: x.item.category
    for key, group in groupby(pack_items, category_key):
        category_list.append({
            "category": key,
            "items": list(group)
        })

    return category_list
