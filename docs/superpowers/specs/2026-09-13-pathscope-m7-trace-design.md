# PathScope M7: firmware trace-buffer scope

Status: approved design (requirements interview 2026-09-13).
Supersedes the M6 polling scope acquisition path.

## 1. Problem and decision

A debug probe reads target memory transaction by transaction, so a
polling scope can never guarantee that two channels' values were
sampled at the same instant - on a slow probe the spread is
milliseconds, which voids cross-channel reasoning. The M6 polling
scope was validated on hardware and then retired by this decision:

**The scope page acquires data ONLY from a firmware-side trace
buffer.** Sampling happens in the target's own timer ISR, where a
whole record is captured in one instant by construction. The probe
merely transports records; transport speed bounds the sample rate but
can never break coherence. Targets without the firmware module get a
clear inline error on the scope page, not a degraded mode. The Data
Path page's register polling (flows/anomalies) is unaffected.

## 2. Firmware reference module

The repo ships `firmware/ps_trace/ps_trace.c` + `ps_trace.h`:

- Pure C99, zero HAL/vendor dependencies, static allocation only,
  Doxygen comments on every public declaration.
- Integration contract: firmware calls `ps_trace_sample()` from one
  periodic timer ISR and links the module. Nothing else.
- The Blackpill demo firmware integrates it (1 kHz timer ISR) as the
  living example and the hardware-validation vehicle.
- The integrator provides the address whitelist (see section 6) and
  the sample period; both are compile-time configuration in
  `ps_trace.h`.

## 3. Memory contract

All structures live in target RAM and are written by firmware in its
NATIVE endianness; the tool detects byte order from the magic (see
section 7). All layout is fixed-width and 4-byte aligned.

### 3.1 Descriptor (`ps_trace_desc`)

| offset | field        | type      | meaning |
|--------|--------------|-----------|---------|
| 0      | magic        | u32       | 'PSCP' (0x50435350 LE view) |
| 4      | version      | u16       | contract version, starts at 1 |
| 6      | max_ch       | u8        | PS_MAX_CH, fixed 10 in v1 |
| 7      | status       | u8        | see 3.4 |
| 8      | period_us    | u32       | ISR period - the time axis |
| 12     | record_size  | u16       | 8 + 4*max_ch (= 48) |
| 14     | ring_count   | u16       | records in the ring (v1: 256) |
| 16     | ring_addr    | u32       | ring buffer base address |
| 20     | wr_seq       | u32       | seq of the NEXT record to write |
| 24     | watch table  | see 3.3   | |

The tool locates `ps_trace_desc` via the ELF symbol (ELF-only
discovery, section 7), reads it, verifies magic and version, and
refuses with a specific inline error otherwise.

### 3.2 Record (fixed width)

| offset | field    | type          | meaning |
|--------|----------|---------------|---------|
| 0      | seq      | u32           | monotonically increasing, never reset |
| 4      | gen      | u8            | watch-table generation (3.3) |
| 5      | pad[3]   | u8[3]         | alignment |
| 8      | slot[i]  | u32 * max_ch  | `*(u32*)watch_addr[i]`, unused slots 0 |

Record `seq` lives at `ring_addr + (seq % ring_count) * record_size`.
Time reconstruction: `t = seq * period_us`. A gap in seq numbers is a
real sampling loss (drain fell behind) and is rendered as a broken
line, never interpolated - same honesty rule as M6's gap handling.

Slots are raw 32-bit reads; interpretation (u16.lo, i8.2, f32, ...)
is display-side in the tool, reusing the M6 11-type decode.

### 3.3 Watch table and update protocol

```
u32 watch_addr[PS_MAX_CH];
u8  watch_count;     /* the gate: 0 = table update in progress */
u8  generation;      /* incremented by firmware on each accepted table */
u8  reserved[2];
```

Single-buffer with a count gate (single-word probe writes are atomic
with respect to the ISR; multi-word table writes are not):

1. tool writes `watch_count = 0`
2. ISR sees 0, skips table sampling (still emits seq/gen records with
   empty slots so the time axis never stalls)
3. tool writes `watch_addr[0..N-1]`
4. tool writes `watch_count = N`
5. firmware validates the table (section 6); on acceptance increments
   `generation` and resumes sampling; on rejection sets `status` and
   keeps count at 0

Records carry `gen`, so the tool knows exactly which record onward
uses the new layout. The few-millisecond gate window appears as an
honest gap on every channel. Double-buffered tables are a documented
upgrade option, not v1.

### 3.4 Status codes

0 = OK; 1 = BAD_ADDR (table rejected, offending addresses outside the
whitelist); 2 = BAD_COUNT (> max_ch). Nonzero status is shown as an
inline scope-page error naming the code.

## 4. Sample rate

- The rate is `1 / period_us`, declared by firmware - the tool never
  infers timing from transport arrival times.
- Design target: 1 kHz at 10 channels on the Blackpill. The actual
  sustainable ceiling is transport-bound and MUST be measured, not
  promised: the implementation plan starts with a throughput spike
  measuring large-block-read speed on the real probe, and the measured
  ceiling goes into the README. Slow links (e.g. a UART-based debug
  bridge) simply set a longer period; coherence is unaffected.
- Ring depth 256 records (~12 KB with v1 sizes) gives 256 ms of drain
  slack at 1 kHz; overflow shows as a seq gap.

## 5. Tool-side pipeline

- Drain: the poller thread reads new records with block reads
  (`read_block32`), interleaved with the existing flows sweep; table
  writes go through the same command queue via `write32` (already on
  the adapter ABC). No adapter changes.
- Decode: records are parsed in bulk into numpy arrays
  (`np.frombuffer` + strided views); per-channel samples live in a
  numpy-backed `TraceStore` (ring per channel), separate from the
  polling `History`. numpy is already a hard dependency of pyqtgraph,
  so the `ui` extra gains nothing new and `core` keeps zero deps
  (TraceStore placement decided at plan time under that constraint).
- Render: 10 s default window; pyqtgraph auto-downsampling
  (peak mode) + clip-to-view. The 10/30/60 s window selector stays in
  the backlog and becomes trivial on this path.

## 6. Safety (defense in depth)

The watch table is read by the TARGET CPU inside an ISR - a bad
address is a bus fault (target crash), and an address with a read
side effect fires that side effect at the sample rate. Three layers:

1. Tool refuses to add guarded addresses (SVD readAction + flows.yaml
   `guarded`) or addresses outside known-valid ranges - same chain as
   M5/M6.
2. Firmware validates every submitted address against the
   integrator's compile-time whitelist of readable ranges and rejects
   the whole table otherwise (status = BAD_ADDR, old behavior: keeps
   sampling nothing until a valid table arrives).
3. The status field closes the loop: the tool surfaces rejections
   inline immediately.

## 7. Discovery and portability

- Discovery is ELF-only: Load ELF -> find `ps_trace_desc` -> read and
  verify magic + version. Wrong/missing module -> specific inline
  error. (ELF is the linker's native output on every relevant
  toolchain; .bin/.hex are derived from it.)
- Endianness: firmware writes natively; the tool detects byte order
  from the magic's byte pattern and decodes the whole contract with
  that order. Big-endian targets (e.g. SPARC-family cores) need no
  firmware byte swapping.

## 8. Scope page UX (v1)

- Channel table: FIXED 10 rows, one per watch-table slot, always
  visible (constant footprint). Row i is slot i; the 10-color palette
  binds to slots (slot 0 is always blue, ...). Empty slots render as
  grey placeholders.
- Columns: [-][swatch][Name][Address][Type][Value][Scale][Offset].
  The Hz column is gone - the rate is a page property shown once in
  the header ("1000 Hz (firmware)"). Value/hover cursor, Auto-lane,
  y-axis-follows-selection carry over from M6 unchanged.
- Add/remove channel = watch-table update via the gate protocol;
  effect lands at a generation boundary; a full table refuses with an
  inline "table full" error.
- Run/Stop freezes the DISPLAY only; sampling and drain never stop,
  so UI state can never cause data loss.
- Retired with the polling path: per-channel Hz, the probe-reads
  budget label, and the sweep-skew meter - coherence is now
  guaranteed by construction, not measured.
- Header shows a drain-health note only when samples were lost
  (seq gap in the last window); silent when healthy.

## 9. Out of scope (backlog)

- Triggered capture (condition + pre/post window, FreeMaster-style).
- Variable-width records (bandwidth optimization).
- Manual descriptor-address fallback for ELF-less workflows.
- Double-buffered watch tables (gapless table swaps).
- 10/30/60 s window selector (unblocked by the numpy path).
- Restore-previous-table on firmware rejection (currently a rejected
  edit stops all channels until the next valid table).
- Drain status message could state that sampling halted table-wide.
- Probe-adaptive read-cap/overhead tuning (current constants were
  measured on one probe).
- Organic concurrent-write simulation modeling.

## 10. Validation

- T0 spike: measured block-read throughput on real hardware defines
  the rate ceiling (goes into README).
- Unit: contract encode/decode round-trips both endiannesses; gate
  protocol state machine; seq-gap detection; whitelist rejection.
- Hardware gate: Blackpill demo at 1 kHz - coherent multi-channel
  waveforms, live table edits with generation-marked gaps, unplug/
  replug recovery, README results.
