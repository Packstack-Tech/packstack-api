def convert_weight(value, source_unit, target_unit):
    if source_unit == target_unit:
        return value

    float_value = float(value)

    # Define conversion factors
    conversion_factors = {
        ("g", "kg"): 0.001,
        ("g", "oz"): 0.03527396,
        ("g", "lb"): 0.00220462,
        ("kg", "g"): 1000,
        ("kg", "oz"): 35.27396,
        ("kg", "lb"): 2.20462,
        ("oz", "g"): 28.34952,
        ("oz", "kg"): 0.02834952,
        ("oz", "lb"): 0.0625,
        ("lb", "g"): 453.59237,
        ("lb", "kg"): 0.45359237,
        ("lb", "oz"): 16
    }

    # Ensure the source and target units are valid
    if (source_unit, target_unit) in conversion_factors:
        conversion_factor = conversion_factors[(source_unit, target_unit)]
        converted_value = float_value * conversion_factor
        return converted_value
    else:
        raise Exception("Invalid source or target unit")


def standardize_weight_unit(unit: str):
    unit = unit.lower().strip()
    if unit == 'gram' or unit == 'grams':
        unit = 'g'

    if unit == 'kilogram' or unit == 'kilograms':
        unit = 'kg'

    if unit == 'ounce' or unit == 'ounces':
        unit = 'oz'

    if unit == 'lbs' or unit == 'pound' or unit == 'pounds':
        unit = 'lb'

    if unit not in ['g', 'kg', 'oz', 'lb']:
        raise Exception("Invalid unit. Must be one of: g, kg, oz, lb")

    return unit
