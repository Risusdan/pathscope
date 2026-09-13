"""ELF symbol lookup for scope channels, per the M6 scope-view plan.

Loads every defined OBJECT symbol with a nonzero size from an ELF's
.symtab via pyelftools - both STB_GLOBAL and STB_LOCAL bindings, since
a C `static` array (e.g. firmware/blackpill_adc_dma/main.c's adc_buf)
still gets a full symtab entry, it is just LOCAL-bound rather than
missing - the brief is explicit both must appear.

Word-size filtering (a scope channel always reads one 32-bit word,
regardless of the symbol's declared byte size) happens at ADD time in
ScopePage, not here: this module reports exactly what the ELF has, so
the picker can show every candidate and let ScopePage's tooltip
explain the caveat at the point where it matters. The consumer is the
scope's trace watch table (ScopePage.add_address_slot() occupies a
slot and pushes it to TraceReader.set_watch()), not the retired
per-address polling path.

pyelftools import is lazy (inside load_symbols), matching
scope_page.py's pyqtgraph-imported-only-when-first-needed pattern, so
importing this module costs nothing until a caller actually loads an
ELF.
"""
from collections import namedtuple
from typing import List

Symbol = namedtuple("Symbol", "name addr size")


def load_symbols(path: str) -> List[Symbol]:
    """Return every defined OBJECT symbol with size > 0 from path's ELF
    .symtab, sorted by name. A symbol is "defined" if its section index
    is not SHN_UNDEF (SHN_ABS and ordinary section indices both count -
    the brief calls out "SHN_ABS/defined" explicitly). Both global and
    local (static) bindings are included. Returns [] if the ELF has no
    .symtab (e.g. stripped)."""
    from elftools.elf.elffile import ELFFile

    out = []
    with open(path, "rb") as f:
        elf = ELFFile(f)
        symtab = elf.get_section_by_name(".symtab")
        if symtab is None:
            return out
        for sym in symtab.iter_symbols():
            if not sym.name:
                continue
            if sym["st_info"]["type"] != "STT_OBJECT":
                continue
            size = sym["st_size"]
            if size <= 0:
                continue
            if sym["st_shndx"] == "SHN_UNDEF":
                continue
            out.append(Symbol(name=sym.name, addr=sym["st_value"],
                               size=size))
    out.sort(key=lambda s: s.name)
    return out
