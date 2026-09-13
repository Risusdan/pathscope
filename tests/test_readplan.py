from core.engine.readplan import build_read_plan


def test_adjacent_registers_merge():
    # DMA2.S0CR @ +0x10 and DMA2.S1CR @ +0x28: gap 5 words -> one op
    plan = build_read_plan({"DMA2.S0CR": 0x40026410,
                            "DMA2.S1CR": 0x40026428})
    assert len(plan) == 1
    op = plan[0]
    assert op.addr == 0x40026410
    assert op.count == 7                      # 0x10..0x28 inclusive
    assert ("DMA2.S0CR", 0) in op.targets
    assert ("DMA2.S1CR", 6) in op.targets


def test_distant_registers_split():
    plan = build_read_plan({"ADC1.SR": 0x40012000,
                            "DMA2.S0CR": 0x40026410})
    assert len(plan) == 2
    assert plan[0].addr == 0x40012000         # sorted by address


def test_merge_gap_zero_never_merges():
    plan = build_read_plan({"DMA2.S0CR": 0x40026410,
                            "DMA2.S1CR": 0x40026428},
                           merge_gap_words=0)
    assert len(plan) == 2


def test_merge_refused_across_forbidden_address():
    # S0CR @ +0x10 and S1CR @ +0x28 normally merge into one op; forbid an
    # address inside the gap and the merge must split.
    plan = build_read_plan({"DMA2.S0CR": 0x40026410,
                            "DMA2.S1CR": 0x40026428},
                           forbidden_addrs={0x40026414})   # S0NDTR
    assert len(plan) == 2
    assert plan[0].count == 1 and plan[1].count == 1


def test_forbidden_target_still_readable_alone():
    # A force-polled guarded register is a legitimate target; it must get
    # its own op, and neighbours must not merge across it.
    plan = build_read_plan({"DMA2.S0CR": 0x40026410,
                            "DMA2.S0NDTR": 0x40026414,
                            "DMA2.S1CR": 0x40026428},
                           forbidden_addrs={0x40026414})
    addrs = sorted(op.addr for op in plan)
    assert 0x40026414 in addrs                # own op exists
    for op in plan:
        if op.addr != 0x40026414:
            span = range(op.addr, op.addr + 4 * op.count, 4)
            assert 0x40026414 not in span     # never swept incidentally
