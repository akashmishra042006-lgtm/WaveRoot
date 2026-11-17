"""Expression IR for the Track B dependency graph.

``ast_graph`` lowers elaborated ``pyslang`` expressions into these immutable
nodes so that the slicer can evaluate control predicates against waveform
values without holding on to the compiler's objects. Every node records the
width and signedness the elaborated AST assigned to it; evaluation relies on
that instead of re-deriving SystemVerilog sizing rules.

Evaluation is four-state. A signal that the trace cannot supply evaluates to
all-``x``, which makes every predicate that depends on it unknown and therefore
keeps every branch it guards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from src.causal import fourstate as fs
from src.causal.fourstate import BinaryOp, CaseFlavor, Tri, UnaryOp, Vec
from src.contracts import EdgeRole, SourceSpan

Lookup = Callable[[str], Vec | None]
"""Returns a signal's value at the observation being evaluated, or None."""


@dataclass(frozen=True, slots=True)
class Expr:
    width: int
    signed: bool


@dataclass(frozen=True, slots=True)
class Const(Expr):
    bits: str


@dataclass(frozen=True, slots=True)
class SignalRef(Expr):
    """A read of a whole signal, named relative to the analyzed top instance."""

    signal: str


@dataclass(frozen=True, slots=True)
class PartSelect(Expr):
    """A constant select: ``width`` bits starting ``offset`` bits from the left."""

    value: Expr
    offset: int
    label: str


class SelectMode(StrEnum):
    ELEMENT = "element"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class IndexedSelect(Expr):
    """A select whose position depends on a run-time index.

    ``left`` and ``right`` are the declared range of ``value``'s outer packed
    dimension and ``elem_width`` is the width of one element of it.
    """

    value: Expr
    index: Expr
    left: int
    right: int
    elem_width: int
    mode: SelectMode


@dataclass(frozen=True, slots=True)
class ArrayElement(Expr):
    """An element of an unpacked array. Its value is not reconstructed yet."""

    array: Expr
    index: Expr


@dataclass(frozen=True, slots=True)
class Unary(Expr):
    op: UnaryOp
    operand: Expr


@dataclass(frozen=True, slots=True)
class Binary(Expr):
    op: BinaryOp
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class Conditional(Expr):
    """``predicate ? if_true : if_false``; ``decision`` identifies the choice."""

    decision: int
    predicate: Expr
    if_true: Expr
    if_false: Expr
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class Concat(Expr):
    parts: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class Replicate(Expr):
    count: int
    operand: Expr


@dataclass(frozen=True, slots=True)
class Convert(Expr):
    """A width or signedness conversion of ``operand`` to this node's type."""

    operand: Expr


@dataclass(frozen=True, slots=True)
class Unsupported(Expr):
    """A construct the lowering does not model. It evaluates to all-``x``.

    ``operands`` keeps every signal the construct reads, so the dependency is
    retained conservatively even though its value is not computed.
    """

    what: str
    operands: tuple[Expr, ...]


def evaluate(expr: Expr, lookup: Lookup) -> Vec:
    """Evaluate ``expr`` with four-state semantics; the result has ``expr.width`` bits."""
    result = _evaluate(expr, lookup)
    if result.width != expr.width:
        return fs.unknown(expr.width, expr.signed)
    return result if result.signed == expr.signed else Vec(result.bits, expr.signed)


def _evaluate(expr: Expr, lookup: Lookup) -> Vec:
    if isinstance(expr, Const):
        return Vec(expr.bits, expr.signed)
    if isinstance(expr, SignalRef):
        value = lookup(expr.signal)
        if value is None or value.width != expr.width:
            return fs.unknown(expr.width, expr.signed)
        return Vec(value.bits, expr.signed)
    if isinstance(expr, PartSelect):
        bits = evaluate(expr.value, lookup).bits[expr.offset : expr.offset + expr.width]
        return Vec(bits, expr.signed) if len(bits) == expr.width else fs.unknown(expr.width)
    if isinstance(expr, IndexedSelect):
        return _indexed_select(expr, lookup)
    if isinstance(expr, Unary):
        return fs.unary(expr.op, evaluate(expr.operand, lookup), expr.width, expr.signed)
    if isinstance(expr, Binary):
        left, right = evaluate(expr.left, lookup), evaluate(expr.right, lookup)
        return fs.binary(expr.op, left, right, expr.width, expr.signed)
    if isinstance(expr, Conditional):
        chosen = fs.truth(evaluate(expr.predicate, lookup))
        if chosen is Tri.TRUE:
            return fs.resize(evaluate(expr.if_true, lookup), expr.width, expr.signed)
        if chosen is Tri.FALSE:
            return fs.resize(evaluate(expr.if_false, lookup), expr.width, expr.signed)
        return fs.merge(
            evaluate(expr.if_true, lookup),
            evaluate(expr.if_false, lookup),
            expr.width,
            expr.signed,
        )
    if isinstance(expr, Concat):
        return Vec("".join(evaluate(part, lookup).bits for part in expr.parts), expr.signed)
    if isinstance(expr, Replicate):
        return Vec(evaluate(expr.operand, lookup).bits * expr.count, expr.signed)
    if isinstance(expr, Convert):
        return fs.resize(evaluate(expr.operand, lookup), expr.width, expr.signed)
    return fs.unknown(expr.width, expr.signed)


def _indexed_select(expr: IndexedSelect, lookup: Lookup) -> Vec:
    base = evaluate(expr.value, lookup)
    index = evaluate(expr.index, lookup)
    if not index.is_known:
        return fs.unknown(expr.width, expr.signed)
    start = index.to_int()
    count = expr.width // expr.elem_width
    if expr.mode is SelectMode.ELEMENT:
        elements = range(start, start + 1)
    elif expr.mode is SelectMode.UP:
        elements = range(start, start + count)
    else:
        elements = range(start - count + 1, start + 1)
    descending = expr.left >= expr.right
    total = abs(expr.left - expr.right) + 1
    positions = sorted((expr.left - e) if descending else (e - expr.left) for e in elements)
    chunks: list[str] = []
    for position in positions:
        if 0 <= position < total:
            chunks.append(base.bits[position * expr.elem_width : (position + 1) * expr.elem_width])
        else:
            chunks.append("x" * expr.elem_width)
    return Vec("".join(chunks), expr.signed)


def children(expr: Expr) -> tuple[Expr, ...]:
    """Direct operands of ``expr`` in source order."""
    if isinstance(expr, PartSelect):
        return (expr.value,)
    if isinstance(expr, IndexedSelect):
        return (expr.value, expr.index)
    if isinstance(expr, ArrayElement):
        return (expr.array, expr.index)
    if isinstance(expr, (Unary, Replicate, Convert)):
        return (expr.operand,)
    if isinstance(expr, Binary):
        return (expr.left, expr.right)
    if isinstance(expr, Conditional):
        return (expr.predicate, expr.if_true, expr.if_false)
    if isinstance(expr, Concat):
        return expr.parts
    if isinstance(expr, Unsupported):
        return expr.operands
    return ()


def signals_of(expr: Expr) -> tuple[str, ...]:
    """Every signal ``expr`` reads, in first-read order without duplicates."""
    found: dict[str, None] = {}
    stack = [expr]
    while stack:
        current = stack.pop()
        if isinstance(current, SignalRef):
            found.setdefault(current.signal)
        stack.extend(reversed(children(current)))
    return tuple(found)


def is_supported(expr: Expr) -> bool:
    """False when ``expr`` contains a construct whose value is not computed."""
    if isinstance(expr, (Unsupported, ArrayElement)):
        return False
    return all(is_supported(child) for child in children(expr))


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


class GuardKind(StrEnum):
    IF = "if"
    TERNARY = "ternary"
    CASE_ITEM = "case_item"
    CASE_DEFAULT = "case_default"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class Guard:
    """One enclosing branch decision on the path to a statement or operand.

    ``decision`` identifies the ``if``, ``case``, or ``?:`` construct, so the
    two arms of one ``if`` share it. For a case item, ``items`` are the
    expressions of this arm and ``earlier`` those of every arm before it, which
    encodes first-match priority. A default arm has no ``items`` and lists
    every arm in ``earlier``.
    """

    decision: int
    kind: GuardKind
    predicate: Expr
    text: str
    polarity: bool = True
    flavor: CaseFlavor = CaseFlavor.NORMAL
    items: tuple[Expr, ...] = ()
    earlier: tuple[Expr, ...] = ()
    span: SourceSpan | None = None


def evaluate_guard(guard: Guard, lookup: Lookup) -> Tri:
    """Whether ``guard`` selects its branch: true, false, or unknown."""
    if guard.kind is GuardKind.UNSUPPORTED:
        return Tri.UNKNOWN
    if guard.kind in (GuardKind.IF, GuardKind.TERNARY):
        outcome = fs.truth(evaluate(guard.predicate, lookup))
        return outcome if guard.polarity else fs.tri_not(outcome)
    selector = evaluate(guard.predicate, lookup)
    earlier = fs.tri_or(
        *(fs.case_match(selector, evaluate(item, lookup), guard.flavor) for item in guard.earlier)
    )
    if guard.kind is GuardKind.CASE_DEFAULT:
        return fs.tri_not(earlier)
    this = fs.tri_or(
        *(fs.case_match(selector, evaluate(item, lookup), guard.flavor) for item in guard.items)
    )
    return fs.tri_and(fs.tri_not(earlier), this)


def guard_operands(guard: Guard) -> tuple[Expr, ...]:
    """Expressions whose values decide the guard."""
    return (guard.predicate, *guard.earlier, *guard.items)


def describe_guard(guard: Guard) -> str:
    """Human-readable form of the condition under which the guard holds."""
    if guard.kind is GuardKind.UNSUPPORTED:
        return f"unsupported construct ({guard.text})"
    if guard.kind in (GuardKind.IF, GuardKind.TERNARY):
        return guard.text if guard.polarity else f"!({guard.text})"
    if guard.kind is GuardKind.CASE_DEFAULT:
        return f"{guard.flavor.value} ({guard.text}) default"
    items = ", ".join(to_sv(item) for item in guard.items)
    return f"{guard.flavor.value} ({guard.text}) matches {items}"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Read:
    """One signal read, with its role and the ``?:`` arms it sits inside."""

    signal: str
    role: EdgeRole
    guards: tuple[Guard, ...]


def collect_reads(
    expr: Expr, role: EdgeRole = EdgeRole.DATA, guards: tuple[Guard, ...] = ()
) -> tuple[Read, ...]:
    """Signals read by ``expr``.

    Operands of a ``?:`` predicate and of a run-time select index are control
    reads. Operands inside a ``?:`` arm carry a guard for that arm.
    """
    found: dict[Read, None] = {}
    _collect(expr, role, guards, found)
    return tuple(found)


def _collect(
    expr: Expr, role: EdgeRole, guards: tuple[Guard, ...], found: dict[Read, None]
) -> None:
    if isinstance(expr, SignalRef):
        found.setdefault(Read(expr.signal, role, guards))
    elif isinstance(expr, Conditional):
        _collect(expr.predicate, EdgeRole.CONTROL, guards, found)
        text = to_sv(expr.predicate)
        for arm, polarity in ((expr.if_true, True), (expr.if_false, False)):
            guard = Guard(
                decision=expr.decision,
                kind=GuardKind.TERNARY,
                predicate=expr.predicate,
                text=text,
                polarity=polarity,
                span=expr.span,
            )
            _collect(arm, role, (*guards, guard), found)
    elif isinstance(expr, IndexedSelect):
        _collect(expr.value, role, guards, found)
        _collect(expr.index, EdgeRole.CONTROL, guards, found)
    elif isinstance(expr, ArrayElement):
        _collect(expr.array, role, guards, found)
        _collect(expr.index, EdgeRole.CONTROL, guards, found)
    else:
        for child in children(expr):
            _collect(child, role, guards, found)


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------


def literal(bits: str, signed: bool = False) -> str:
    """A sized SystemVerilog binary literal for ``bits``."""
    return f"{len(bits)}'{'s' if signed else ''}b{bits}"


def to_sv(expr: Expr) -> str:
    """Approximate SystemVerilog text for messages. It is not re-parsed."""
    if isinstance(expr, Const):
        return literal(expr.bits, expr.signed)
    if isinstance(expr, SignalRef):
        return expr.signal
    if isinstance(expr, PartSelect):
        return f"{_atom(expr.value)}{expr.label}"
    if isinstance(expr, IndexedSelect):
        index = to_sv(expr.index)
        count = expr.width // expr.elem_width
        if expr.mode is SelectMode.ELEMENT:
            return f"{_atom(expr.value)}[{index}]"
        sign = "+:" if expr.mode is SelectMode.UP else "-:"
        return f"{_atom(expr.value)}[{index} {sign} {count}]"
    if isinstance(expr, ArrayElement):
        return f"{_atom(expr.array)}[{to_sv(expr.index)}]"
    if isinstance(expr, Unary):
        return f"{expr.op.value}{_atom(expr.operand)}"
    if isinstance(expr, Binary):
        return f"{_atom(expr.left)} {expr.op.value} {_atom(expr.right)}"
    if isinstance(expr, Conditional):
        return f"{_atom(expr.predicate)} ? {_atom(expr.if_true)} : {_atom(expr.if_false)}"
    if isinstance(expr, Concat):
        return "{" + ", ".join(to_sv(part) for part in expr.parts) + "}"
    if isinstance(expr, Replicate):
        return "{" + f"{expr.count}{{{to_sv(expr.operand)}}}" + "}"
    if isinstance(expr, Convert):
        return to_sv(expr.operand)
    if isinstance(expr, Unsupported):
        return f"<{expr.what}>"
    return "<expr>"


def _atom(expr: Expr) -> str:
    text = to_sv(expr)
    if isinstance(expr, Convert):
        return _atom(expr.operand)
    return f"({text})" if isinstance(expr, (Binary, Conditional)) else text
