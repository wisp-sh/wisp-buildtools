from vendor_math import sum_values


def total(values: list[int]) -> int:
    # Resolve a local module lazily and prove it remains cached between imports.
    from . import state
    first = state.next_value()
    from . import state as same_state
    assert same_state.next_value() == first + 1
    return sum_values(values)
