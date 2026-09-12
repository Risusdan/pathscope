"""Groups the registers a sweep needs into as few block reads as the
address map allows."""
from dataclasses import dataclass
from typing import List, Set, Tuple

from ..target.registers import RegisterModel


@dataclass
class ReadOp:
    addr: int
    count: int
    targets: List[Tuple[str, int]]


def build_read_plan(reg_keys: Set[str], model: RegisterModel,
                    merge_gap_words: int = 8) -> List[ReadOp]:
    entries = sorted((model.resolve(k).address, k) for k in reg_keys)
    plan: List[ReadOp] = []
    for addr, key in entries:
        if plan:
            cur = plan[-1]
            gap = (addr - (cur.addr + 4 * cur.count)) // 4
            if 0 <= gap <= merge_gap_words:
                cur.count = (addr - cur.addr) // 4 + 1
                cur.targets.append((key, (addr - cur.addr) // 4))
                continue
        plan.append(ReadOp(addr=addr, count=1, targets=[(key, 0)]))
    return plan
