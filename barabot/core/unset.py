"""`UNSET`: "leave this field as it is" in partial updates, distinct from None."""

from __future__ import annotations

import enum
from typing import Final


class Unset(enum.Enum):
    UNSET = enum.auto()

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = Unset.UNSET
