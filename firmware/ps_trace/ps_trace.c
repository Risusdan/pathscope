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

/** @brief watch_count as observed on the previous ps_trace_sample()
 *         call, used to detect the host's 0 -> N table-write
 *         transition exactly once per change. */
static uint8_t ps_trace_last_count;

/**
 * @brief Check one address against ps_trace_whitelist.
 * @param addr Address to check.
 * @return 1 if addr falls inside some [lo, hi) whitelist range, 0
 *         otherwise.
 */
static int ps_trace_addr_ok(uint32_t addr)
{
    uint32_t i;

    for (i = 0; i < ps_trace_whitelist_len; i++) {
        if (addr >= ps_trace_whitelist[i].lo && addr < ps_trace_whitelist[i].hi) {
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

    ps_trace_last_count = 0u;
}

void ps_trace_sample(void)
{
    uint8_t count = ps_trace_desc.watch_count;
    uint32_t i;
    volatile ps_trace_record_t *rec;

    /* Table protocol: host writes watch_count=0, then the addresses,
       then watch_count=N. Validate exactly once, on the 0 -> N edge. */
    if (ps_trace_last_count == 0u && count != 0u) {
        if (count > PS_TRACE_MAX_CH) {
            ps_trace_desc.status = PS_TRACE_STATUS_BAD_COUNT;
            ps_trace_desc.watch_count = 0u;
        } else {
            int ok = 1;

            for (i = 0; i < count; i++) {
                if (!ps_trace_addr_ok(ps_trace_desc.watch_addrs[i])) {
                    ok = 0;
                    break;
                }
            }
            if (ok) {
                ps_trace_desc.generation++;
                ps_trace_desc.status = PS_TRACE_STATUS_OK;
            } else {
                ps_trace_desc.status = PS_TRACE_STATUS_BAD_ADDR;
                ps_trace_desc.watch_count = 0u;
            }
        }
    }
    ps_trace_last_count = ps_trace_desc.watch_count;

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
       be the last store this function makes. */
    ps_trace_desc.wr_seq++;
}
