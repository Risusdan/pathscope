"""Data model passed from engine to consumers. Values carry their own
timestamps because a sweep is not atomic (spec 6.4)."""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Sample:
    value: int
    t: float


@dataclass
class Snapshot:
    values: Dict[str, Sample]
    t: float
    rate_hz: float

    def value(self, reg_key: str) -> Optional[int]:
        s = self.values.get(reg_key)
        return None if s is None else s.value
