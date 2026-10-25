import sys
from dataclasses import dataclass
from componentize_py_types import Err
from wit_world.exports.api import Person
from .formatting import salutation
from .operations import total

assert sys.platform == 'wasi', 'application modules must initialize inside guest Python'


@dataclass
class Greeting:
    name: str


def greet(name: str) -> str:
    return salutation(Greeting(name).name)


def birthday(value: Person) -> Person:
    return Person(value.name, value.age + 1)


def checked(value: int) -> int:
    if value < 0:
        raise Err('value must be nonnegative')
    return value * 2
