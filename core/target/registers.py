"""CMSIS-SVD subset parser: peripherals, registers, fields, readAction,
derivedFrom, dim arrays. Deliberately minimal - extend only on need."""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dfield
from typing import Dict, Optional


class SvdError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    lsb: int
    msb: int


@dataclass
class Register:
    name: str
    address: int
    read_action: Optional[str] = None
    fields: Dict[str, Field] = dfield(default_factory=dict)


@dataclass
class Peripheral:
    name: str
    base: int
    registers: Dict[str, Register] = dfield(default_factory=dict)


@dataclass(frozen=True)
class RegRef:
    ref: str
    reg_key: str
    address: int
    lsb: int
    msb: int
    read_action: Optional[str]


def _int(text: str) -> int:
    return int(text, 0)


def _parse_register(rnode, base: int, out: Dict[str, Register]) -> None:
    name_t = rnode.findtext("name")
    offset = _int(rnode.findtext("addressOffset"))
    read_action = rnode.findtext("readAction")
    fields: Dict[str, Field] = {}
    for fnode in rnode.iter("field"):
        fname = fnode.findtext("name")
        lsb = _int(fnode.findtext("bitOffset"))
        width = _int(fnode.findtext("bitWidth"))
        fields[fname] = Field(fname, lsb, lsb + width - 1)
    dim = rnode.findtext("dim")
    if dim is None:
        out[name_t] = Register(name_t, base + offset, read_action, fields)
        return
    inc = _int(rnode.findtext("dimIncrement"))
    for i in range(_int(dim)):
        n = name_t.replace("%s", str(i))
        out[n] = Register(n, base + offset + i * inc, read_action, fields)


class RegisterModel:
    def __init__(self, peripherals: Dict[str, Peripheral]):
        self.peripherals = peripherals

    @classmethod
    def from_svd(cls, path: str) -> "RegisterModel":
        root = ET.parse(path).getroot()
        peripherals: Dict[str, Peripheral] = {}
        deferred = []
        for pnode in root.iter("peripheral"):
            name = pnode.findtext("name")
            base = _int(pnode.findtext("baseAddress"))
            parent = pnode.get("derivedFrom")
            if parent is not None:
                deferred.append((name, base, parent))
                continue
            regs: Dict[str, Register] = {}
            regs_node = pnode.find("registers")
            if regs_node is not None:
                for rnode in regs_node.findall("register"):
                    _parse_register(rnode, base, regs)
            peripherals[name] = Peripheral(name, base, regs)
        for name, base, parent in deferred:
            if parent not in peripherals:
                raise SvdError("derivedFrom unknown peripheral: %s" % parent)
            src = peripherals[parent]
            regs = {
                rn: Register(rn, base + (r.address - src.base),
                             r.read_action, dict(r.fields))
                for rn, r in src.registers.items()
            }
            peripherals[name] = Peripheral(name, base, regs)
        return cls(peripherals)

    def resolve(self, ref: str) -> RegRef:
        parts = ref.split(".")
        if len(parts) not in (2, 3):
            raise SvdError("bad register reference: %r" % ref)
        pname, rname = parts[0], parts[1]
        periph = self.peripherals.get(pname)
        if periph is None:
            raise SvdError("unknown peripheral: %s" % pname)
        reg = periph.registers.get(rname)
        if reg is None:
            raise SvdError("unknown register: %s.%s" % (pname, rname))
        reg_key = "%s.%s" % (pname, rname)
        if len(parts) == 2:
            return RegRef(ref, reg_key, reg.address, 0, 31, reg.read_action)
        f = reg.fields.get(parts[2])
        if f is None:
            raise SvdError("unknown field: %s" % ref)
        return RegRef(ref, reg_key, reg.address, f.lsb, f.msb,
                      reg.read_action)
