def enum_to_dict(enumeration):
    return {enum.name: enum.value for enum in enumeration}


def list_intersection(list1, list2):
    return list(set(list1) & set(list2))


def list_difference(list1, list2):
    return list(list(set(list1)-set(list2)) + list(set(list2)-set(list1)))
