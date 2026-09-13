/**
 * @file ps_trace.h
 * @brief PathScope firmware trace-buffer sampler: reference module.
 * @details Publishes a fixed-layout descriptor plus a ring buffer of
 *          register-value records that the PathScope host tool reads
 *          directly out of target memory over a debug probe. Pure
 *          C99, zero vendor/CMSIS includes - the integrator supplies
 *          a periodic timer tick (calling ps_trace_sample() from
 *          exactly one ISR) and a whitelist of addresses that are
 *          safe to read (ps_trace_whitelist / ps_trace_whitelist_len,
 *          defined by the integration, e.g. main.c).
 *
 *          Layout mirrors the host-side contract (core/trace/
 *          contract.py) exactly: ps_trace_desc_t is 68 bytes,
 *          ps_trace_record_t is 48 bytes, both packed and 4-byte
 *          aligned, field order matching the spec byte-for-byte.
 */
#ifndef PS_TRACE_H
#define PS_TRACE_H

#include <stdint.h>

/** @brief Maximum number of watched channels (contract section 3.1). */
#define PS_TRACE_MAX_CH      10u
/** @brief Ring buffer depth, in records.
 *  @details CONTRACT-VALUE CHANGE (T11 hardware gate, fix round 1):
 *           256 -> 1024. A real probe's per-command latency (tens of
 *           ms of fixed overhead plus real transfer time for a large
 *           block read) made the 256-deep/256ms-span ring too small
 *           to ever hold a stably-readable window once a host is more
 *           than a command or two behind - every refresh() raced the
 *           ring wrapping under it and lost the whole batch, every
 *           cycle, on hardware (never reproduced by the same-thread
 *           sim, where wr_seq is frozen for the whole read). 1024
 *           records is 49152 bytes (1024 * PS_TRACE_RECORD_SIZE) of
 *           the target's 128KB SRAM - comfortable alongside this
 *           firmware's other statics (see main.c). This constant is
 *           validated against core/trace/contract.py's RING_COUNT
 *           (parse_desc rejects a mismatch) - both sides were bumped
 *           together; the sim (core/trace/sim.py) and every test that
 *           depends on the ring's size follow contract.py's constant. */
#define PS_TRACE_RING_COUNT  1024u
/** @brief Sample period in microseconds; the integrator's timer tick
 *         must fire at this rate for the wall-clock time axis to be
 *         correct on the host side. */
#define PS_TRACE_PERIOD_US   1000u
/** @brief Wire size of one ps_trace_record_t, in bytes (contract 3.3). */
#define PS_TRACE_RECORD_SIZE 48u

/** @brief Descriptor magic: 'PSCP' read as a little-endian u32. */
#define PS_TRACE_MAGIC       0x50435350u
/** @brief Descriptor/contract version this module implements. */
#define PS_TRACE_VERSION     1u

/** @brief Descriptor status: table accepted, sampling normally. */
#define PS_TRACE_STATUS_OK        0u
/** @brief Descriptor status: a watched address failed the whitelist. */
#define PS_TRACE_STATUS_BAD_ADDR  1u
/** @brief Descriptor status: watch_count exceeded PS_TRACE_MAX_CH. */
#define PS_TRACE_STATUS_BAD_COUNT 2u

/**
 * @brief One half-open [lo, hi) address range that ps_trace_sample()
 *        is allowed to read from.
 */
typedef struct {
    uint32_t lo; /**< Range start, inclusive. */
    uint32_t hi; /**< Range end, exclusive. */
} ps_trace_range_t;

/**
 * @brief Integrator-supplied whitelist of ranges ps_trace_sample()
 *        may read from when accepting a new watch table.
 * @details Defined by the integration (e.g. main.c), not by this
 *          module, so the whitelist always matches the target's
 *          actual memory map.
 */
extern const ps_trace_range_t ps_trace_whitelist[];
/** @brief Number of entries in ps_trace_whitelist. */
extern const uint32_t ps_trace_whitelist_len;

/**
 * @brief Trace descriptor: contract section 3.1 layout, exactly 68
 *        bytes, packed and 4-byte aligned. Field order must not
 *        change without updating core/trace/contract.py to match.
 */
typedef struct {
    uint32_t magic;                        /**< PS_TRACE_MAGIC. */
    uint16_t version;                      /**< PS_TRACE_VERSION. */
    uint8_t  max_ch;                       /**< PS_TRACE_MAX_CH. */
    uint8_t  status;                       /**< PS_TRACE_STATUS_*. */
    uint32_t period_us;                    /**< PS_TRACE_PERIOD_US. */
    uint16_t record_size;                  /**< PS_TRACE_RECORD_SIZE. */
    uint16_t ring_count;                   /**< PS_TRACE_RING_COUNT. */
    uint32_t ring_addr;                    /**< Absolute address of the ring. */
    uint32_t wr_seq;                       /**< Publish barrier: bumped last, after every other field of the current record is written. */
    uint32_t watch_addrs[PS_TRACE_MAX_CH]; /**< Host-written watch table. */
    uint8_t  watch_count;                  /**< Host protocol: 0, then addrs, then N. */
    uint8_t  generation;                   /**< Bumped by firmware on table accept. */
    uint8_t  reserved[2];                  /**< Padding to 68 bytes; always 0. */
} __attribute__((packed, aligned(4))) ps_trace_desc_t;

/**
 * @brief One ring record: contract section 3.3 layout, exactly 48
 *        bytes, packed and 4-byte aligned.
 */
typedef struct {
    uint32_t seq;                     /**< Record's sequence number, equals desc.wr_seq at publish time. */
    uint8_t  gen;                     /**< desc.generation at sample time. */
    uint8_t  pad[3];                  /**< Padding; always 0. */
    uint32_t slots[PS_TRACE_MAX_CH];  /**< Sampled values; slots beyond watch_count are 0. */
} __attribute__((packed, aligned(4))) ps_trace_record_t;

/**
 * @brief The descriptor instance: the ELF symbol the PathScope host
 *        tool locates via the target's symbol table.
 */
extern volatile ps_trace_desc_t ps_trace_desc;

/**
 * @brief One-time setup. Zeroes the descriptor and ring, then
 *        publishes the static contract fields (magic, version,
 *        max_ch, period_us, record_size, ring_count, ring_addr).
 * @details Call once at startup, before enabling the periodic timer
 *          that drives ps_trace_sample().
 * @return None.
 */
void ps_trace_init(void);

/**
 * @brief Sample one record into the ring and publish it.
 * @details Call from exactly one periodic timer ISR, at
 *          PS_TRACE_PERIOD_US. On each call: whenever watch_count != 0
 *          AND the live table (count plus watch_addrs[0..count-1])
 *          differs from the last ACCEPTED table, validates every
 *          watch_addrs[0..count-1] against ps_trace_whitelist - accept
 *          bumps generation and sets status OK, reject sets status
 *          (BAD_ADDR or BAD_COUNT if count > PS_TRACE_MAX_CH) and
 *          resets watch_count to 0. This gate is LEVEL-based rather
 *          than edge-triggered on the watch_count == 0 -> N transition,
 *          so a host whose count=0 write and count=N write both land
 *          within a single sample period is still validated correctly
 *          (an edge-triggered gate could miss that transition entirely
 *          and start dereferencing an unvalidated table). Hosts
 *          SHOULD still dwell at least one sample period after writing
 *          count=0 before writing the new addresses, for a clean gap
 *          in the record stream at the edit - the level-based gate
 *          removes the correctness dependency on that dwell, not the
 *          benefit of it. Then writes one record at
 *          wr_seq % PS_TRACE_RING_COUNT (seq, gen, watched slot
 *          values; unused slots 0) and increments wr_seq last, as
 *          the publish barrier the host's torn-read guard depends on.
 *          Emits a record even while watch_count == 0 so the host's
 *          time axis never stalls.
 * @return None.
 */
void ps_trace_sample(void);

#endif /* PS_TRACE_H */
