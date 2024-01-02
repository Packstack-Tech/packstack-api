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
