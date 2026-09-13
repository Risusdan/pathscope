/**
 * @file ps_trace.c
 * @brief PathScope firmware trace-buffer sampler: reference implementation.
 * @details See ps_trace.h for the public interface and the contract
 *          this module implements. No vendor/CMSIS includes - the
 *          only platform coupling is the raw addresses the
 *          integrator writes into ps_trace_whitelist and passes in
 *          via ps_trace_desc.watch_addrs.
 */
#include "ps_trace.h"

/** @brief Ring buffer backing store: PS_TRACE_RING_COUNT records of
 *         PS_TRACE_RECORD_SIZE bytes each. */
static volatile uint8_t ps_trace_ring[PS_TRACE_RING_COUNT * PS_TRACE_RECORD_SIZE];

/** @brief The descriptor instance; layout must match ps_trace_desc_t
 *         exactly (see ps_trace.h). */
volatile ps_trace_desc_t ps_trace_desc;

/** @brief watch_count last successfully ACCEPTED by ps_trace_sample(),
 *         used by the level-based validation gate to detect a real
 *         change in the requested table (see ps_trace_sample()). */
static uint8_t ps_trace_accepted_count;
/** @brief watch_addrs snapshot last successfully ACCEPTED, compared
 *         against the live table on every tick where watch_count != 0
 *         to decide whether re-validation is needed. */
static uint32_t ps_trace_accepted_addrs[PS_TRACE_MAX_CH];

/**
 * @brief Check one address against ps_trace_whitelist.
 * @param addr Address to check.
 * @return 1 if addr is 4-byte aligned and the full 4-byte word at addr
 *         falls inside some [lo, hi) whitelist range, 0 otherwise.
 */
static int ps_trace_addr_ok(uint32_t addr)
{
    uint32_t i;

    if ((addr & 3u) != 0u) {
        return 0;
    }
    for (i = 0; i < ps_trace_whitelist_len; i++) {
        if (addr >= ps_trace_whitelist[i].lo
            && addr <= ps_trace_whitelist[i].hi - 4u) {
            return 1;
        }
    }
    return 0;
}

void ps_trace_init(void)
{
    uint32_t i;

    for (i = 0; i < PS_TRACE_RING_COUNT * PS_TRACE_RECORD_SIZE; i++) {
        ps_trace_ring[i] = 0u;
    }

    ps_trace_desc.magic = PS_TRACE_MAGIC;
    ps_trace_desc.version = PS_TRACE_VERSION;
    ps_trace_desc.max_ch = (uint8_t)PS_TRACE_MAX_CH;
    ps_trace_desc.status = PS_TRACE_STATUS_OK;
    ps_trace_desc.period_us = PS_TRACE_PERIOD_US;
    ps_trace_desc.record_size = (uint16_t)PS_TRACE_RECORD_SIZE;
    ps_trace_desc.ring_count = (uint16_t)PS_TRACE_RING_COUNT;
    ps_trace_desc.ring_addr = (uint32_t)ps_trace_ring;
    ps_trace_desc.wr_seq = 0u;
    for (i = 0; i < PS_TRACE_MAX_CH; i++) {
        ps_trace_desc.watch_addrs[i] = 0u;
    }
    ps_trace_desc.watch_count = 0u;
    ps_trace_desc.generation = 0u;
    ps_trace_desc.reserved[0] = 0u;
    ps_trace_desc.reserved[1] = 0u;

    ps_trace_accepted_count = 0u;
    for (i = 0; i < PS_TRACE_MAX_CH; i++) {
        ps_trace_accepted_addrs[i] = 0u;
    }
}

void ps_trace_sample(void)
{
    uint8_t count = ps_trace_desc.watch_count;
    uint32_t i;
    volatile ps_trace_record_t *rec;

    /* Table protocol: host writes watch_count=0, then the addresses,
       then watch_count=N. Validation is LEVEL-based, not edge-based:
       whenever count != 0 and the live table differs from the last
       ACCEPTED snapshot, (re-)validate it. This covers the same
       0 -> N transition an edge detector would, but also covers a
       host whose count=0 write and count=N write both land within
       one tick (the edge would be invisible to an edge detector,
       leaving an UNVALIDATED table in place) - here the live table
       simply still differs from what was last accepted, so it gets
       validated exactly the same. A table equal to the last accepted
       one is never re-validated (and never re-bumps generation) on a
       later tick where nothing changed. */
    if (count != 0u) {
        int differs = (count != ps_trace_accepted_count);

        for (i = 0; !differs && i < count; i++) {
            if (ps_trace_desc.watch_addrs[i] != ps_trace_accepted_addrs[i]) {
                differs = 1;
            }
        }

        if (differs) {
            if (count > PS_TRACE_MAX_CH) {
                ps_trace_desc.status = PS_TRACE_STATUS_BAD_COUNT;
                ps_trace_desc.watch_count = 0u;
                ps_trace_accepted_count = 0u;
            } else {
                int ok = 1;

                for (i = 0; i < count; i++) {
                    if (!ps_trace_addr_ok(ps_trace_desc.watch_addrs[i])) {
                        ok = 0;
                        break;
                    }
                }
                if (ok) {
                    ps_trace_accepted_count = count;
                    for (i = 0; i < count; i++) {
                        ps_trace_accepted_addrs[i] = ps_trace_desc.watch_addrs[i];
                    }
                    ps_trace_desc.generation++;
                    ps_trace_desc.status = PS_TRACE_STATUS_OK;
                } else {
                    ps_trace_desc.status = PS_TRACE_STATUS_BAD_ADDR;
                    ps_trace_desc.watch_count = 0u;
                    ps_trace_accepted_count = 0u;
                }
            }
        }
    } else {
        /* Gate closed (or already closed): forget whatever was
           accepted before, so the NEXT nonzero count is always fully
           (re-)validated, even if it happens to numerically match a
           table from before the gate closed - closing the gate is
           the host's own signal that it is about to change
           something. */
        ps_trace_accepted_count = 0u;
    }

    /* Emit one record every tick, even while count == 0, so the
       host's time axis never stalls. */
    rec = (volatile ps_trace_record_t *)
        &ps_trace_ring[(ps_trace_desc.wr_seq % PS_TRACE_RING_COUNT) * PS_TRACE_RECORD_SIZE];

    rec->seq = ps_trace_desc.wr_seq;
    rec->gen = ps_trace_desc.generation;
    rec->pad[0] = 0u;
    rec->pad[1] = 0u;
    rec->pad[2] = 0u;

    count = ps_trace_desc.watch_count;
    for (i = 0; i < PS_TRACE_MAX_CH; i++) {
        if (i < count) {
            rec->slots[i] = *(volatile uint32_t *)ps_trace_desc.watch_addrs[i];
        } else {
            rec->slots[i] = 0u;
        }
    }

    /* Publish barrier: every other field of this record has been
       written above; wr_seq is the host's torn-read guard, so it must
       be the last store this function makes. The DMB below is
       required alongside the ordering: the debug probe is an
       independent bus master (ARM AN321), so without it the record
       writes could still sit in the CPU's write buffer, undrained to
       SRAM, at the moment wr_seq becomes visible over SWD/DAP. */
    __asm volatile ("dmb" ::: "memory");
    ps_trace_desc.wr_seq++;
}
