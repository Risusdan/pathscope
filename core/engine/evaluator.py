"""ast-based sandboxed evaluation of rule expressions. The AST whitelist
IS the language definition - anything not matched below is rejected at
compile time, so no runtime surprise is possible."""
import ast
import operator
from typing import Any, Callable, Dict, Optional, Set

from ..target.registers import RegisterModel, RegRef, SvdError
from .history import History
from .snapshot import Snapshot


class ExprError(Exception):
    pass


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.BitAnd: operator.and_,
        ast.BitOr: operator.or_, ast.BitXor: operator.xor,
        ast.LShift: operator.lshift, ast.RShift: operator.rshift}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne,
        ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}
_BUILTINS = ("stalled", "changed")


class _Missing(Exception):
    """A referenced register has no value in this snapshot."""


def _attr_chain(node: ast.AST) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class CompiledExpr:
    def __init__(self, fn: Callable[[Snapshot], Any], refs: Set[str]):
        self._fn = fn
        self.refs = refs

    def eval(self, snap: Snapshot):
        try:
            return self._fn(snap)
        except _Missing:
            return None


class Evaluator:
    def __init__(self, model: RegisterModel, history: History):
        self.model = model
        self.history = history

    def compile(self, expr: str) -> CompiledExpr:
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ExprError("syntax error in %r: %s" % (expr, e))
        refs: Set[str] = set()
        fn = self._build(tree.body, refs)
        return CompiledExpr(fn, refs)

    def _reg(self, node: ast.AST, refs: Set[str]) -> RegRef:
        chain = _attr_chain(node)
        if chain is None:
            raise ExprError("expected a register reference")
        try:
            rr = self.model.resolve(chain)
        except SvdError as e:
            raise ExprError(str(e))
        refs.add(rr.reg_key)
        return rr

    def _build(self, node: ast.AST, refs: Set[str]):
        if isinstance(node, ast.Constant) and isinstance(node.value,
                                                         (int, bool)):
            v = node.value
            return lambda s: v
        if isinstance(node, ast.Attribute):
            rr = self._reg(node, refs)
            mask = (1 << (rr.msb - rr.lsb + 1)) - 1
            lsb = rr.lsb
            key = rr.reg_key

            def read(s: Snapshot):
                v = s.value(key)
                if v is None:
                    raise _Missing()
                return (v >> lsb) & mask
            return read
        if isinstance(node, ast.BoolOp):
            subs = [self._build(x, refs) for x in node.values]
            if isinstance(node.op, ast.And):
                return lambda s: all(f(s) for f in subs)
            return lambda s: any(f(s) for f in subs)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sub = self._build(node.operand, refs)
            return lambda s: not sub(s)
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or type(node.ops[0]) not in _CMP:
                raise ExprError("unsupported comparison")
            op = _CMP[type(node.ops[0])]
            left = self._build(node.left, refs)
            right = self._build(node.comparators[0], refs)
            return lambda s: op(left(s), right(s))
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN:
                raise ExprError("unsupported operator")
            op = _BIN[type(node.op)]
            left = self._build(node.left, refs)
            right = self._build(node.right, refs)
            return lambda s: op(left(s), right(s))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) \
                    or node.func.id not in _BUILTINS or node.keywords:
                raise ExprError("unknown function call")
            name = node.func.id
            rr = self._reg(node.args[0] if node.args else ast.Constant(0),
                           refs)
            hist = self.history
            if name == "stalled":
                if len(node.args) != 2 \
                        or not isinstance(node.args[1], ast.Constant):
                    raise ExprError("stalled(REF, ms) expects a constant ms")
                ms = float(node.args[1].value)

                def stalled(s: Snapshot):
                    age = hist.last_change_age(rr.reg_key, s.t)
                    return age is not None and age * 1000.0 > ms
                return stalled
            if len(node.args) != 1:
                raise ExprError("changed(REF) takes one argument")
            return lambda s: hist.changed_on_last(rr.reg_key)
        raise ExprError("disallowed construct: %s"
                        % type(node).__name__)
