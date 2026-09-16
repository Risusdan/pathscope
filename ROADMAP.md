# Roadmap

Future work, loosely ordered within each area. Checked items are
done and get folded into CHANGELOG.md at the next release; nothing
here is a commitment.

## Scope

- [ ] Triggered capture: arm on a condition, keep a pre/post window
      (FreeMaster-style)
- [ ] Window selector (10 / 30 / 60 s)
- [ ] CSV export of the visible window
- [ ] Restore the previous watch table when firmware rejects an edit
      (today a rejected edit stops all channels until the next valid
      table)
- [ ] Double-buffered watch tables (gapless table swaps)
- [ ] Variable-width records (bandwidth optimization at high rates)
- [ ] Manual descriptor-address fallback for ELF-less workflows
- [ ] Probe-adaptive read-cap/overhead tuning (current constants were
      measured on one probe)

## Layout editor

- [ ] Structural editing in the GUI (add/remove blocks and edges)
- [ ] Multi-select group drag
- [ ] Draggable port anchors
- [ ] Redo
- [ ] Interior waypoints re-route around a moved block (endpoints
      already track; interior points stay put by design today)

## Targets and transports

- [ ] A second debug adapter beyond pyOCD SWD (the TargetAdapter ABC
      is the seam; UART/other bridge transports mainly change the
      trace drain rate, not the contract)
- [ ] Big-endian target validation on real hardware (the trace
      contract already detects byte order from the magic)
- [ ] Loader-side validation that an explicit edge path has >= 2
      points (today only the editor enforces it)

## Diagnostics

- [ ] Fault snapshot: dump registers + trace ring to a file when an
      anomaly rule fires
- [ ] Drain-status message naming table-wide sampling halts
      explicitly

## Engineering (deferred by ruling - do on the next natural touch)

- [ ] scope_page.py: helper extraction + ChannelSlot dataclass +
      error-label owner helper (next scope_page feature/fix)
- [ ] MainWindow: extract the layout-editor controller into
      ui/layout_edit.py (next layout-editor feature)
