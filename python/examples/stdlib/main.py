"""Use Python control flow, classes, comprehensions and guest standard library."""
import collections
import functools
import itertools
import json
import math
import re


class Numbers:
    def __init__(self, values):
        self.values = values

    def total(self):
        counts = collections.Counter(self.values)
        return functools.reduce(lambda total, item: total + item[0] * item[1], counts.items(), 0)


def add(a: int, b: int) -> int:
    serialized = json.dumps([a, b])
    values = [int(value) for value in json.loads(serialized)]
    assert math.isfinite(a) and re.fullmatch(r"\[.*\]", serialized)
    return Numbers(list(itertools.chain(values, []))).total()
