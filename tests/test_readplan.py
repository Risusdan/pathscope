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


def test_duplicate_address_shares_one_read():
    # Two keys at the SAME address (a register channel plus an
    # addr-watch on it, a T5 hardware finding) must land on one op as
    # two targets - separate ops read the word at different instants,
    # so a live counter shows two different values for one address.
    plan = build_read_plan({"DMA2.S0NDTR": 0x40026414,
                            "@40026414": 0x40026414})
    assert len(plan) == 1
    assert plan[0].count == 1
    assert sorted(k for k, _i in plan[0].targets) == \
        ["@40026414", "DMA2.S0NDTR"]
    assert all(idx == 0 for _k, idx in plan[0].targets)


def test_address_inside_merged_span_adds_target_not_op():
    # An address already covered by the current op's merged span gets
    # a target at the right word index, not a second transaction.
    plan = build_read_plan({"DMA2.S0CR": 0x40026410,
                            "DMA2.S0NDTR": 0x40026418,
                            "@40026414": 0x40026414})
    assert len(plan) == 1
    assert plan[0].addr == 0x40026410 and plan[0].count == 3
    assert ("@40026414", 1) in plan[0].targets


def test_duplicate_forbidden_address_not_merged():
    # Guarded addresses keep their isolated-op rule even when
    # duplicated - the covered-span shortcut must not apply to them.
    plan = build_read_plan({"ADC1.DR": 0x4001204C,
                            "@4001204C": 0x4001204C},
                           forbidden_addrs={0x4001204C})
    assert len(plan) == 2
    assert all(op.count == 1 for op in plan)
