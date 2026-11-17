"""Four-state vector arithmetic used to evaluate RTL control predicates.

A ``Vec`` is a fixed-width string of ``0``, ``1``, ``x``, ``z`` with the declared
left (most significant) bit first, matching ``contracts.LogicValue``. Nothing
here converts an unknown bit to an integer: an operation whose result depends on
an unknown bit yields ``x`` for the affected bits, following IEEE 1800 operator
semantics. Operand widths and signedness are supplied by the caller, because the
elaborated AST has already applied the context-dependent sizing rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final


class Tri(Enum):
    """Three-valued truth of a control predicate."""

    FALSE = 0
    TRUE = 1
    UNKNOWN = 2


def tri_not(value: Tri) -> Tri:
    if value is Tri.UNKNOWN:
        return Tri.UNKNOWN
    return Tri.FALSE if value is Tri.TRUE else Tri.TRUE


def tri_and(*values: Tri) -> Tri:
    if any(value is Tri.FALSE for value in values):
        return Tri.FALSE
    if any(value is Tri.UNKNOWN for value in values):
        return Tri.UNKNOWN
    return Tri.TRUE


def tri_or(*values: Tri) -> Tri:
    if any(value is Tri.TRUE for value in values):
        return Tri.TRUE
    if any(value is Tri.UNKNOWN for value in values):
        return Tri.UNKNOWN
    return Tri.FALSE


class UnaryOp(StrEnum):
    PLUS = "+"
    MINUS = "-"
    BIT_NOT = "~"
    LOGIC_NOT = "!"
    RED_AND = "&"
    RED_NAND = "~&"
    RED_OR = "|"
    RED_NOR = "~|"
    RED_XOR = "^"
    RED_XNOR = "~^"


class BinaryOp(StrEnum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"
    POW = "**"
    AND = "&"
    OR = "|"
    XOR = "^"
    XNOR = "~^"
    EQ = "=="
    NE = "!="
    CASE_EQ = "==="
    CASE_NE = "!=="
    WILD_EQ = "==?"
    WILD_NE = "!=?"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    LOGIC_AND = "&&"
    LOGIC_OR = "||"
    IMPLIES = "->"
    EQUIV = "<->"
    SHL = "<<"
    SHR = ">>"
    ASHL = "<<<"
    ASHR = ">>>"


class CaseFlavor(StrEnum):
    NORMAL = "case"
    CASEZ = "casez"
    CASEX = "casex"


_UNKNOWN: Final = frozenset("xz")
_ARITHMETIC: Final = frozenset(
    {BinaryOp.ADD, BinaryOp.SUB, BinaryOp.MUL, BinaryOp.DIV, BinaryOp.MOD}
)
_BITWISE: Final = frozenset({BinaryOp.AND, BinaryOp.OR, BinaryOp.XOR, BinaryOp.XNOR})
_EQUALITY: Final = frozenset(
    {
        BinaryOp.EQ,
        BinaryOp.NE,
        BinaryOp.CASE_EQ,
        BinaryOp.CASE_NE,
        BinaryOp.WILD_EQ,
        BinaryOp.WILD_NE,
    }
)
_RELATIONAL: Final = frozenset({BinaryOp.LT, BinaryOp.LE, BinaryOp.GT, BinaryOp.GE})
_LOGICAL: Final = frozenset(
    {BinaryOp.LOGIC_AND, BinaryOp.LOGIC_OR, BinaryOp.IMPLIES, BinaryOp.EQUIV}
)
_SHIFT: Final = frozenset({BinaryOp.SHL, BinaryOp.SHR, BinaryOp.ASHL, BinaryOp.ASHR})


@dataclass(frozen=True, slots=True)
class Vec:
    """A four-state vector; ``bits[0]`` is the most significant bit."""

    bits: str
    signed: bool = False

    def __post_init__(self) -> None:
        if not self.bits or any(bit not in "01xz" for bit in self.bits):
            raise ValueError(f"invalid four-state bits: {self.bits!r}")

    @property
    def width(self) -> int:
        return len(self.bits)

    @property
    def is_known(self) -> bool:
        return "x" not in self.bits and "z" not in self.bits

    def to_unsigned(self) -> int:
        """Unsigned integer value; raises if any bit is x/z."""
        if not self.is_known:
            raise ValueError(f"cannot convert {self.bits!r} to an integer: contains x/z")
        return int(self.bits, 2)

    def to_int(self) -> int:
        """Integer value honoring ``signed``; raises if any bit is x/z."""
        value = self.to_unsigned()
        if self.signed and self.bits[0] == "1":
            value -= 1 << self.width
        return value


def unknown(width: int, signed: bool = False) -> Vec:
    return Vec("x" * width, signed)


def from_int(value: int, width: int, signed: bool = False) -> Vec:
    """Two's-complement encoding of ``value`` truncated to ``width`` bits."""
    return Vec(format(value & ((1 << width) - 1), f"0{width}b"), signed)


def resize(value: Vec, width: int, signed: bool | None = None) -> Vec:
    """Truncate to the low bits or extend; extension follows ``value.signed``."""
    out_signed = value.signed if signed is None else signed
    current = value.width
    if width == current:
        bits = value.bits
    elif width < current:
        bits = value.bits[current - width :]
    else:
        fill = value.bits[0] if value.signed else "0"
        bits = fill * (width - current) + value.bits
    return Vec(bits, out_signed)


def truth(value: Vec) -> Tri:
    """Integral truth: any 1 is true, all 0 is false, otherwise unknown."""
    if "1" in value.bits:
        return Tri.TRUE
    if value.is_known:
        return Tri.FALSE
    return Tri.UNKNOWN


def from_tri(value: Tri, width: int = 1) -> Vec:
    bit = {Tri.TRUE: "1", Tri.FALSE: "0", Tri.UNKNOWN: "x"}[value]
    return Vec("0" * (width - 1) + bit)


def _and_bit(a: str, b: str) -> str:
    if a == "0" or b == "0":
        return "0"
    return "1" if a == "1" and b == "1" else "x"


def _or_bit(a: str, b: str) -> str:
    if a == "1" or b == "1":
        return "1"
    return "0" if a == "0" and b == "0" else "x"


def _xor_bit(a: str, b: str) -> str:
    if a in _UNKNOWN or b in _UNKNOWN:
        return "x"
    return "1" if a != b else "0"


def _not_bit(a: str) -> str:
    if a in _UNKNOWN:
        return "x"
    return "0" if a == "1" else "1"


def _reduce(bits: str, op: UnaryOp) -> str:
    if op in (UnaryOp.RED_AND, UnaryOp.RED_NAND):
        result = "1"
        for bit in bits:
            result = _and_bit(result, bit)
        return result if op is UnaryOp.RED_AND else _not_bit(result)
    if op in (UnaryOp.RED_OR, UnaryOp.RED_NOR):
        result = "0"
        for bit in bits:
            result = _or_bit(result, bit)
        return result if op is UnaryOp.RED_OR else _not_bit(result)
    result = "0"
    for bit in bits:
        result = _xor_bit(result, bit)
    return result if op is UnaryOp.RED_XOR else _not_bit(result)


def unary(op: UnaryOp, operand: Vec, width: int, signed: bool) -> Vec:
    """Evaluate a unary operator; the result has ``width`` bits."""
    if op is UnaryOp.LOGIC_NOT:
        return from_tri(tri_not(truth(operand)), width)
    if op in (
        UnaryOp.RED_AND,
        UnaryOp.RED_NAND,
        UnaryOp.RED_OR,
        UnaryOp.RED_NOR,
        UnaryOp.RED_XOR,
        UnaryOp.RED_XNOR,
    ):
        return resize(Vec(_reduce(operand.bits, op)), width, signed)
    value = resize(operand, width)
    if op is UnaryOp.BIT_NOT:
        return Vec("".join(_not_bit(bit) for bit in value.bits), signed)
    if op is UnaryOp.PLUS:
        return Vec(value.bits, signed)
    if not value.is_known:
        return unknown(width, signed)
    return from_int(-value.to_unsigned(), width, signed)


def _equality(left: str, right: str) -> Tri:
    """``==``: a known mismatch is false; otherwise an unknown bit is unknown."""
    ambiguous = False
    for a, b in zip(left, right):
        if a in _UNKNOWN or b in _UNKNOWN:
            ambiguous = True
        elif a != b:
            return Tri.FALSE
    return Tri.UNKNOWN if ambiguous else Tri.TRUE


def _wildcard_equality(left: str, right: str) -> Tri:
    """``==?``: x/z in the right operand are wildcards."""
    ambiguous = False
    for a, b in zip(left, right):
        if b in _UNKNOWN:
            continue
        if a in _UNKNOWN:
            ambiguous = True
        elif a != b:
            return Tri.FALSE
    return Tri.UNKNOWN if ambiguous else Tri.TRUE


def _trunc_div(a: int, b: int) -> int:
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


def _power(base: int, exponent: int, width: int) -> int | None:
    if exponent >= 0:
        # Modular, so a large constant exponent cannot build an enormous integer.
        return pow(base, exponent, 1 << width)
    if base == 0:
        return None
    if base == 1:
        return 1
    if base == -1:
        return 1 if exponent % 2 == 0 else -1
    return 0


def _arithmetic(op: BinaryOp, a: Vec, b: Vec, width: int, signed: bool) -> Vec:
    if not (a.is_known and b.is_known):
        return unknown(width, signed)
    both_signed = a.signed and b.signed
    x = a.to_int() if both_signed else a.to_unsigned()
    y = b.to_int() if both_signed else b.to_unsigned()
    if op is BinaryOp.ADD:
        return from_int(x + y, width, signed)
    if op is BinaryOp.SUB:
        return from_int(x - y, width, signed)
    if op is BinaryOp.MUL:
        return from_int(x * y, width, signed)
    if y == 0:
        return unknown(width, signed)
    quotient = _trunc_div(x, y)
    if op is BinaryOp.DIV:
        return from_int(quotient, width, signed)
    return from_int(x - y * quotient, width, signed)


def _shift(op: BinaryOp, value: Vec, amount: Vec, width: int, signed: bool) -> Vec:
    if not amount.is_known:
        return unknown(width, signed)
    count = min(amount.to_unsigned(), width)
    bits = value.bits
    if op in (BinaryOp.SHL, BinaryOp.ASHL):
        return Vec(bits[count:] + "0" * count, signed)
    fill = bits[0] if op is BinaryOp.ASHR and signed else "0"
    return Vec(fill * count + bits[: width - count], signed)


def _compare(op: BinaryOp, left: Vec, right: Vec) -> Tri:
    size = max(left.width, right.width)
    a, b = resize(left, size), resize(right, size)
    if op in (BinaryOp.CASE_EQ, BinaryOp.CASE_NE):
        equal = Tri.TRUE if a.bits == b.bits else Tri.FALSE
        return equal if op is BinaryOp.CASE_EQ else tri_not(equal)
    if op in (BinaryOp.EQ, BinaryOp.NE):
        equal = _equality(a.bits, b.bits)
        return equal if op is BinaryOp.EQ else tri_not(equal)
    if op in (BinaryOp.WILD_EQ, BinaryOp.WILD_NE):
        equal = _wildcard_equality(a.bits, b.bits)
        return equal if op is BinaryOp.WILD_EQ else tri_not(equal)
    if not (a.is_known and b.is_known):
        return Tri.UNKNOWN
    both_signed = a.signed and b.signed
    x = a.to_int() if both_signed else a.to_unsigned()
    y = b.to_int() if both_signed else b.to_unsigned()
    outcome = {
        BinaryOp.LT: x < y,
        BinaryOp.LE: x <= y,
        BinaryOp.GT: x > y,
        BinaryOp.GE: x >= y,
    }[op]
    return Tri.TRUE if outcome else Tri.FALSE


def _logical(op: BinaryOp, left: Vec, right: Vec) -> Tri:
    a, b = truth(left), truth(right)
    if op is BinaryOp.LOGIC_AND:
        return tri_and(a, b)
    if op is BinaryOp.LOGIC_OR:
        return tri_or(a, b)
    if op is BinaryOp.IMPLIES:
        return tri_or(tri_not(a), b)
    return tri_or(tri_and(a, b), tri_and(tri_not(a), tri_not(b)))


def binary(op: BinaryOp, left: Vec, right: Vec, width: int, signed: bool) -> Vec:
    """Evaluate a binary operator; the result has ``width`` bits."""
    if op in _LOGICAL:
        return from_tri(_logical(op, left, right), width)
    if op in _EQUALITY or op in _RELATIONAL:
        return from_tri(_compare(op, left, right), width)
    if op in _SHIFT:
        return _shift(op, resize(left, width), right, width, signed)
    if op is BinaryOp.POW:
        base = resize(left, width)
        if not (base.is_known and right.is_known):
            return unknown(width, signed)
        result = _power(
            base.to_int() if base.signed else base.to_unsigned(),
            right.to_int() if right.signed else right.to_unsigned(),
            width,
        )
        return unknown(width, signed) if result is None else from_int(result, width, signed)
    a, b = resize(left, width), resize(right, width)
    if op in _ARITHMETIC:
        return _arithmetic(op, a, b, width, signed)
    if op not in _BITWISE:
        raise ValueError(f"unsupported binary operator {op!r}")
    if op is BinaryOp.AND:
        bits = "".join(_and_bit(x, y) for x, y in zip(a.bits, b.bits))
    elif op is BinaryOp.OR:
        bits = "".join(_or_bit(x, y) for x, y in zip(a.bits, b.bits))
    elif op is BinaryOp.XOR:
        bits = "".join(_xor_bit(x, y) for x, y in zip(a.bits, b.bits))
    else:
        bits = "".join(_not_bit(_xor_bit(x, y)) for x, y in zip(a.bits, b.bits))
    return Vec(bits, signed)


def merge(if_true: Vec, if_false: Vec, width: int, signed: bool) -> Vec:
    """Result of ``c ? a : b`` when ``c`` is unknown: agreeing known bits survive."""
    a, b = resize(if_true, width), resize(if_false, width)
    bits = "".join(x if x == y and x in "01" else "x" for x, y in zip(a.bits, b.bits))
    return Vec(bits, signed)


def case_match(selector: Vec, item: Vec, flavor: CaseFlavor) -> Tri:
    """Conservative match of one case item.

    ``casez`` treats ``z`` in either operand as a wildcard and ``casex`` treats
    ``x`` and ``z`` that way. A remaining unknown bit makes the match unknown
    rather than a literal four-state comparison, so the caller keeps every
    branch that is feasible once the unknown bit resolves.
    """
    size = max(selector.width, item.width)
    a, b = resize(selector, size).bits, resize(item, size).bits
    wildcards = {CaseFlavor.NORMAL: "", CaseFlavor.CASEZ: "z", CaseFlavor.CASEX: "xz"}[flavor]
    ambiguous = False
    for x, y in zip(a, b):
        if x in wildcards or y in wildcards:
            continue
        if x in _UNKNOWN or y in _UNKNOWN:
            ambiguous = True
        elif x != y:
            return Tri.FALSE
    return Tri.UNKNOWN if ambiguous else Tri.TRUE
