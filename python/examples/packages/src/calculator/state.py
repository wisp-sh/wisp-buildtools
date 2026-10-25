_calls = 0


def next_value() -> int:
    global _calls
    _calls += 1
    return _calls
