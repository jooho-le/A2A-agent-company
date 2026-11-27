"""JSON contract scalars, including integral numbers from Protobuf Struct."""

import math
from typing import Annotated

from pydantic import BeforeValidator


def require_json_integer(value: object) -> int:
    """Accept JSON integers, not bool/string coercion or fractional numbers.

    ProtoJSON Struct represents JSON numbers as floats, so finite integral
    floats are accepted. Native ints do not take an overflowing float detour.
    """
    if isinstance(value, bool):
        raise ValueError("Contract integer fields must be JSON numbers")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError("Contract integer fields must be finite integral JSON numbers")


JSONInteger = Annotated[int, BeforeValidator(require_json_integer)]
