"""Groups the registers a sweep needs into as few block reads as the
address map allows."""
from dataclasses import dataclass
from typing import FrozenSet, List, Set, Tuple

from ..target.registers import RegisterModel


@dataclass
class ReadOp:
    addr: int
    count: int
    targets: List[Tuple[str, int]]


def build_read_plan(reg_keys: Set[str], model: RegisterModel,
                    merge_gap_words: int = 8,
                    forbidden_addrs: FrozenSet[int] = frozenset()
                    ) -> List[ReadOp]:
    entries = sorted((model.resolve(k).address, k) for k in reg_keys)
    forbidden = sorted(forbidden_addrs)
    plan: List[ReadOp] = []
    for addr, key in entries:
        if addr in forbidden_addrs:
            # guarded target: isolated single-word op, never merged
            plan.append(ReadOp(addr=addr, count=1, targets=[(key, 0)]))
            continue
        if plan:
            cur = plan[-1]
            cur_end = cur.addr + 4 * cur.count
            gap = (addr - cur_end) // 4
            spans_forbidden = any(cur_end <= f < addr for f in forbidden)
            merged_would_span = any(cur.addr <= f <= addr
                                    for f in forbidden)
            if (0 <= gap <= merge_gap_words and not spans_forbidden
                    and not merged_would_span
                    and cur.addr not in forbidden_addrs):
                cur.count = (addr - cur.addr) // 4 + 1
                cur.targets.append((key, (addr - cur.addr) // 4))
                continue
        plan.append(ReadOp(addr=addr, count=1, targets=[(key, 0)]))
    return plan
