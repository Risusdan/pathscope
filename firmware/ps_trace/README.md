# ps_trace - firmware trace-buffer sampler

The firmware half of PathScope's scope view: a timer ISR captures up
to 10 watched 32-bit values into one record per tick, so every value
in a record is from the same instant by construction. The host tool
drains the ring over the debug probe and plots it.

Pure C99, no HAL or vendor dependencies, static allocation only.
`ps_trace.h` carries the full Doxygen contract; this file is the
five-minute integration recipe.

## Wiring it into your firmware

1. Add `ps_trace.c` and `ps_trace.h` to the build.

2. Define the read whitelist somewhere in your firmware - the address
   ranges the ISR is allowed to dereference (RAM holding your
   variables, plus any peripheral registers that are safe to read at
   the sample rate; keep read-to-clear / FIFO-pop registers out):

       #include "ps_trace.h"

       const ps_trace_range_t ps_trace_whitelist[] = {
           {0x20000000u, 0x20020000u},   /* SRAM */
           {0x40026400u, 0x40026800u},   /* DMA2 */
       };
       const uint32_t ps_trace_whitelist_len =
           sizeof ps_trace_whitelist / sizeof ps_trace_whitelist[0];

3. Call `ps_trace_init()` once at startup, then `ps_trace_sample()`
   from exactly one periodic timer ISR. The period must match
   `PS_TRACE_PERIOD_US` in `ps_trace.h` (default 1000 us = 1 kHz) -
   the host reads that field for its time axis, so a mismatch skews
   every plot.

4. Keep the ELF. The host discovers the module through the
   `ps_trace_desc` symbol - in the app: scope tab, Load ELF, add
   channels. `.bin`/`.hex` alone are not enough.

That is the whole integration. Which addresses get sampled is decided
at runtime by the host writing the watch table; changing channels
never needs a rebuild or reflash.

## Sizing and rate

- RAM cost: about 48 KB with the defaults (1024 records x 48 bytes)
  plus the descriptor. Shrink `PS_TRACE_RING_COUNT` on small parts -
  the ring only needs to cover the probe's drain latency.
- The sample rate is transport-bound end to end: the ring must drain
  faster than it fills. `pathscope bench-read` measures your probe's
  ceiling (65.8 KB/s = 1403 Hz at 48-byte records on the reference
  clone ST-Link). A slower link (for example a UART-based debug
  bridge) just needs a longer `PS_TRACE_PERIOD_US`; coherence is
  unaffected.
- Big-endian targets need no byte swapping - the host detects byte
  order from the descriptor magic.

The Blackpill demo (`firmware/blackpill_adc_dma`) is the living
example: TIM-driven 1 kHz sampling next to an ADC+DMA workload.
