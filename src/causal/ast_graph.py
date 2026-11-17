"""Structural driver-to-load graph of one elaborated SystemVerilog design.

``build_design_graph`` elaborates SystemVerilog with ``pyslang`` and lowers one
top instance, and every module instance below it, into a ``DesignGraph``:

* every declared signal, with its width, declared range, and source line;
* for every driven signal, its ``Driver`` records: the guarded assignments of
  one continuous assignment or procedural block, in execution order, each with
  the ``if`` / ``case`` decisions that enclose it;
* a flat list of ``StructuralEdge`` records (source signal to dependent signal)
  labeled ``combinational`` or ``sequential``, ``data`` or ``control``, with the
  guards under which the dependence is active and its source span.

The graph is static. It names signals by their path relative to the analyzed
top instance (``wptr``, ``u_core.q``, ``u_core.u_fifo.wptr``) and knows nothing
about time; ``backward_slicer`` unrolls it over real clock edges and prunes it
with waveform values.

Module instances are lowered by recursion over each elaborated instance body,
so two instances that differ by parameter are lowered separately. Each port
connection becomes a combinational ``Driver``: an input port drives the child's
internal signal from the connection expression, and an output port drives the
parent's signals from the child's internal signal. An input port connected to
a plain whole-signal reference also records an alias, which lets a clock be
recognized through the hierarchy.

Scope of this version: ``assign``, ``always_comb``, ``always_ff`` /
``always @(posedge clk)``, ``if``, ``case`` / ``casez`` / ``casex``, ``?:``,
packed vectors, part-selects, packed struct fields, and module instances with
input and output ports. Constructs outside that scope are never dropped: they
are recorded as diagnostics and as drivers or operands marked unsupported, so
the slicer can report where its result is incomplete.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pyslang
from pyslang import ast, syntax

from src.causal import expr as ir
from src.causal import fourstate as fs
from src.causal.fourstate import BinaryOp, CaseFlavor, UnaryOp, Vec
from src.contracts import BitRange, Diagnostic, EdgeRole, EdgeType, SourceFile, SourceSpan


class DesignError(Exception):
    """The RTL could not be read, elaborated, or matched to a top module."""


class SignalKind(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"
    INTERNAL = "internal"


class ProcessKind(StrEnum):
    CONTINUOUS = "assign"
    ALWAYS_COMB = "always_comb"
    ALWAYS_FF = "always_ff"
    ALWAYS = "always"
    ALWAYS_LATCH = "always_latch"
    PORT = "port"
    INSTANCE = "instance"


@dataclass(frozen=True, slots=True)
class SignalInfo:
    """A declared signal. ``name`` is its path relative to the analyzed top instance.

    Only ports of the top instance have kind ``input``, ``output``, or
    ``inout``. Ports of nested instances are ``internal`` signals driven by
    their port connections.
    """

    name: str
    width: int
    bit_range: BitRange | None
    signed: bool
    kind: SignalKind
    span: SourceSpan | None
    is_aggregate: bool = False


@dataclass(frozen=True, slots=True)
class ClockSpec:
    signal: str
    rising: bool


@dataclass(frozen=True, slots=True)
class GuardedAssign:
    """One assignment to ``target`` and the decisions that must hold to reach it.

    ``full_write`` is false for a write to a part-select, element, or field.
    ``index_operands`` are run-time selectors on the left-hand side.
    ``written`` is ``(offset, width)`` of the bits written, counted from the
    target's left bit, when they are known at elaboration; None means the
    written bits depend on a run-time value or are not modeled.
    """

    target: str
    full_write: bool
    rhs: ir.Expr
    guards: tuple[ir.Guard, ...]
    index_operands: tuple[ir.Expr, ...]
    blocking: bool
    span: SourceSpan | None
    text: str
    written: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class Driver:
    """Everything one process contributes to one signal.

    ``assigns`` are in execution order, so the last one whose guards hold wins.
    ``unsupported`` explains why the driver cannot be traversed, if it cannot.
    ``caveats`` describe ways the model is imprecise for this driver.
    """

    target: str
    edge_type: EdgeType
    process: ProcessKind
    clock: ClockSpec | None
    assigns: tuple[GuardedAssign, ...]
    span: SourceSpan | None
    unsupported: str | None = None
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuralEdge:
    """A static dependence of ``target`` on ``source``, active under ``guards``."""

    source: str
    target: str
    edge_type: EdgeType
    role: EdgeRole
    guards: tuple[ir.Guard, ...]
    span: SourceSpan | None


def _no_aliases() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class DesignGraph:
    """The lowered design.

    ``aliases`` maps a child's input port signal to the parent signal it is
    connected to, when the connection is a plain whole-signal reference. It is
    used to resolve clocks through the hierarchy; dependencies still follow the
    port ``Driver``.
    """

    top_module: str
    sources: tuple[SourceFile, ...]
    signals: Mapping[str, SignalInfo]
    drivers: Mapping[str, tuple[Driver, ...]]
    edges: tuple[StructuralEdge, ...]
    diagnostics: tuple[Diagnostic, ...]
    aliases: Mapping[str, str] = field(default_factory=_no_aliases)

    def canonical(self, signal: str) -> str:
        """The signal ``signal`` is an alias of, following aliases to the top."""
        seen = {signal}
        while signal in self.aliases:
            signal = self.aliases[signal]
            if signal in seen:
                break
            seen.add(signal)
        return signal

    def fanin(self, signal: str) -> tuple[StructuralEdge, ...]:
        """Edges whose dependent signal is ``signal``."""
        return tuple(edge for edge in self.edges if edge.target == signal)

    def fanout(self, signal: str) -> tuple[StructuralEdge, ...]:
        """Edges that read ``signal``."""
        return tuple(edge for edge in self.edges if edge.source == signal)


def build_design_graph(
    files: Sequence[Path | str], *, root: Path | str, top: str | None = None
) -> DesignGraph:
    """Elaborate ``files`` and lower the top instance named ``top``.

    ``root`` is the directory that source paths in the result are relative to;
    every file must lie inside it. ``top`` may be omitted when the sources
    elaborate to exactly one top-level module. Raises ``DesignError`` for a
    missing file, a compile error, or an unresolved top module.
    """
    root_path = Path(root).resolve()
    by_relative: dict[str, Path] = {}
    for entry in files:
        path = Path(entry).resolve()
        if not path.is_file():
            raise DesignError(f"source file not found: {entry}")
        try:
            relative = path.relative_to(root_path).as_posix()
        except ValueError as error:
            raise DesignError(f"{path} is not inside the source root {root_path}") from error
        by_relative[relative] = path
    if not by_relative:
        raise DesignError("no source files were given")

    sources: list[SourceFile] = []
    source_ids: dict[str, str] = {}
    compilation = ast.Compilation()
    for position, relative in enumerate(sorted(by_relative)):
        path = by_relative[relative]
        source_id = f"src{position}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sources.append(SourceFile(id=source_id, path=relative, sha256=digest))
        source_ids[os.path.normcase(str(path))] = source_id
        compilation.addSyntaxTree(syntax.SyntaxTree.fromFile(str(path)))

    manager = compilation.sourceManager
    engine = pyslang.DiagnosticEngine(manager)
    errors = [
        f"{manager.getFileName(d.location)}:{manager.getLineNumber(d.location)}: "
        f"{engine.formatMessage(d)}"
        for d in compilation.getAllDiagnostics()
        if d.isError()
    ]
    if errors:
        raise DesignError("SystemVerilog elaboration failed:\n" + "\n".join(errors))

    instances = list(compilation.getRoot().topInstances)
    if top is None:
        if len(instances) != 1:
            names = ", ".join(sorted(instance.name for instance in instances)) or "none"
            raise DesignError(f"expected exactly one top module, found: {names}")
        instance = instances[0]
    else:
        matches = [candidate for candidate in instances if candidate.name == top]
        if not matches:
            names = ", ".join(sorted(candidate.name for candidate in instances)) or "none"
            raise DesignError(f"top module {top!r} not found among top instances: {names}")
        instance = matches[0]

    paths = {source.id: source.path for source in sources}
    lowerer = _Lowerer(manager, source_ids, paths, instance)
    return lowerer.build(tuple(sources))


# --------------------------------------------------------------------------
# Lowering
# --------------------------------------------------------------------------

_UNARY_OPS: dict[ast.UnaryOperator, UnaryOp] = {
    ast.UnaryOperator.Plus: UnaryOp.PLUS,
    ast.UnaryOperator.Minus: UnaryOp.MINUS,
    ast.UnaryOperator.BitwiseNot: UnaryOp.BIT_NOT,
    ast.UnaryOperator.LogicalNot: UnaryOp.LOGIC_NOT,
    ast.UnaryOperator.BitwiseAnd: UnaryOp.RED_AND,
    ast.UnaryOperator.BitwiseNand: UnaryOp.RED_NAND,
    ast.UnaryOperator.BitwiseOr: UnaryOp.RED_OR,
    ast.UnaryOperator.BitwiseNor: UnaryOp.RED_NOR,
    ast.UnaryOperator.BitwiseXor: UnaryOp.RED_XOR,
    ast.UnaryOperator.BitwiseXnor: UnaryOp.RED_XNOR,
}

_BINARY_OPS: dict[ast.BinaryOperator, BinaryOp] = {
    ast.BinaryOperator.Add: BinaryOp.ADD,
    ast.BinaryOperator.Subtract: BinaryOp.SUB,
    ast.BinaryOperator.Multiply: BinaryOp.MUL,
    ast.BinaryOperator.Divide: BinaryOp.DIV,
    ast.BinaryOperator.Mod: BinaryOp.MOD,
    ast.BinaryOperator.Power: BinaryOp.POW,
    ast.BinaryOperator.BinaryAnd: BinaryOp.AND,
    ast.BinaryOperator.BinaryOr: BinaryOp.OR,
    ast.BinaryOperator.BinaryXor: BinaryOp.XOR,
    ast.BinaryOperator.BinaryXnor: BinaryOp.XNOR,
    ast.BinaryOperator.Equality: BinaryOp.EQ,
    ast.BinaryOperator.Inequality: BinaryOp.NE,
    ast.BinaryOperator.CaseEquality: BinaryOp.CASE_EQ,
    ast.BinaryOperator.CaseInequality: BinaryOp.CASE_NE,
    ast.BinaryOperator.WildcardEquality: BinaryOp.WILD_EQ,
    ast.BinaryOperator.WildcardInequality: BinaryOp.WILD_NE,
    ast.BinaryOperator.LessThan: BinaryOp.LT,
    ast.BinaryOperator.LessThanEqual: BinaryOp.LE,
    ast.BinaryOperator.GreaterThan: BinaryOp.GT,
    ast.BinaryOperator.GreaterThanEqual: BinaryOp.GE,
    ast.BinaryOperator.LogicalAnd: BinaryOp.LOGIC_AND,
    ast.BinaryOperator.LogicalOr: BinaryOp.LOGIC_OR,
    ast.BinaryOperator.LogicalImplication: BinaryOp.IMPLIES,
    ast.BinaryOperator.LogicalEquivalence: BinaryOp.EQUIV,
    ast.BinaryOperator.LogicalShiftLeft: BinaryOp.SHL,
    ast.BinaryOperator.LogicalShiftRight: BinaryOp.SHR,
    ast.BinaryOperator.ArithmeticShiftLeft: BinaryOp.ASHL,
    ast.BinaryOperator.ArithmeticShiftRight: BinaryOp.ASHR,
}

_CASE_FLAVORS: dict[ast.CaseStatementCondition, CaseFlavor] = {
    ast.CaseStatementCondition.Normal: CaseFlavor.NORMAL,
    ast.CaseStatementCondition.WildcardJustZ: CaseFlavor.CASEZ,
    ast.CaseStatementCondition.WildcardXOrZ: CaseFlavor.CASEX,
}

_PROCESS_KINDS: dict[ast.ProceduralBlockKind, ProcessKind] = {
    ast.ProceduralBlockKind.AlwaysComb: ProcessKind.ALWAYS_COMB,
    ast.ProceduralBlockKind.AlwaysFF: ProcessKind.ALWAYS_FF,
    ast.ProceduralBlockKind.Always: ProcessKind.ALWAYS,
    ast.ProceduralBlockKind.AlwaysLatch: ProcessKind.ALWAYS_LATCH,
}

_DIRECTIONS: dict[ast.ArgumentDirection, SignalKind] = {
    ast.ArgumentDirection.In: SignalKind.INPUT,
    ast.ArgumentDirection.Out: SignalKind.OUTPUT,
    ast.ArgumentDirection.InOut: SignalKind.INOUT,
    ast.ArgumentDirection.Ref: SignalKind.INOUT,
}


# The stubs omit EdgeKind.None_ (a level-sensitive event), so edges are matched positively.
_EDGE_KINDS = (ast.EdgeKind.PosEdge, ast.EdgeKind.NegEdge, ast.EdgeKind.BothEdges)


MAX_ARRAY_ELEMENTS: Final = 4096
"""Unpacked arrays with more elements than this stay one aggregate signal."""


@dataclass(frozen=True, slots=True)
class _ArrayLayout:
    """A fixed-size unpacked array of integral elements, declared element by element."""

    name: str
    ranges: tuple[tuple[int, int], ...]
    width: int
    signed: bool
    elements: tuple[str, ...]

    def element(self, indices: Sequence[int]) -> str | None:
        """The element signal at declared ``indices``, or None if out of range."""
        if len(indices) != len(self.ranges):
            return None
        for index, (left, right) in zip(indices, self.ranges):
            if not min(left, right) <= index <= max(left, right):
                return None
        return self.name + "".join(f"[{index}]" for index in indices)


@dataclass(frozen=True, slots=True)
class _Root:
    """A signal an lvalue writes, and the element selection that must hold."""

    signal: str
    full_write: bool
    index_operands: tuple[ir.Expr, ...]
    guards: tuple[ir.Guard, ...]
    bits: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    """One signal written by an assignment and the slice of the RHS it takes.

    ``guards`` are ``index`` guards: an element of an unpacked array written
    through a run-time index is a separate target, written only when the
    index selects it.
    """

    signal: str
    full_write: bool
    index_operands: tuple[ir.Expr, ...]
    rhs_offset: int
    width: int
    guards: tuple[ir.Guard, ...] = ()
    bits: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _Timing:
    edge_type: EdgeType
    clock: ClockSpec | None
    unsupported: str | None
    caveat: str | None


def _svint_bits(value: pyslang.SVInt) -> str:
    """Bits of ``value``, most significant first, preserving x and z."""
    return "".join(str(value[index]) for index in reversed(range(value.bitWidth)))


def _integral(type_: ast.Type) -> tuple[int, bool] | None:
    canonical = type_.canonicalType
    if not canonical.isIntegral or canonical.bitWidth < 1:
        return None
    return canonical.bitWidth, canonical.isSigned


def _packed(canonical: ast.Type) -> tuple[int, BitRange | None, bool] | None:
    """Width, declared range, and signedness of an integral type, or None."""
    if not canonical.isIntegral or canonical.bitWidth < 1:
        return None
    width = canonical.bitWidth
    bit_range: BitRange | None
    if canonical.isScalar:
        bit_range = None
    else:
        declared = canonical.fixedRange
        simple = canonical.isSimpleBitVector and min(declared.left, declared.right) >= 0
        if simple and abs(declared.left - declared.right) + 1 == width:
            bit_range = BitRange(left=declared.left, right=declared.right)
        else:
            bit_range = BitRange(left=width - 1, right=0)
    return width, bit_range, canonical.isSigned


def _signal_refs(expr: ir.Expr) -> tuple[ir.SignalRef, ...]:
    """Signals a lowered name stands for: itself, or every element of an array."""
    if isinstance(expr, ir.SignalRef):
        return (expr,)
    if isinstance(expr, ir.Unsupported):
        return tuple(operand for operand in expr.operands if isinstance(operand, ir.SignalRef))
    return ()


def _const_int(expr: ir.Expr) -> int | None:
    if not isinstance(expr, ir.Const):
        return None
    value = Vec(expr.bits, expr.signed)
    return value.to_int() if value.is_known else None


class _Lowerer:
    def __init__(
        self,
        manager: pyslang.SourceManager,
        source_ids: Mapping[str, str],
        source_paths: Mapping[str, str],
        instance: ast.InstanceSymbol,
    ) -> None:
        self._manager = manager
        self._source_ids = source_ids
        self._source_paths = source_paths
        self._instance = instance
        self._prefix = f"{instance.hierarchicalPath}."
        self._signals: dict[str, SignalInfo] = {}
        self._drivers: dict[str, list[Driver]] = {}
        self._aliases: dict[str, str] = {}
        self._arrays: dict[str, _ArrayLayout] = {}
        # Unpacked structs: leaf signals holding the value, and immediate fields in order.
        self._records: dict[str, tuple[str, ...]] = {}
        self._fields: dict[str, tuple[str, ...]] = {}
        self._diagnostics: dict[Diagnostic, None] = {}
        self._texts: dict[str, str] = {}
        self._decisions = 0
        self._lvalue: ir.Expr | None = None
        # Value of the child port while an output port connection is lowered.
        self._port_value: ir.Expr | None = None
        # Name prefix of the instance being lowered: "" for the top, "u_core." below it.
        self._scope = ""

    # ---- top level ------------------------------------------------------

    def build(self, sources: tuple[SourceFile, ...]) -> DesignGraph:
        # Declare the whole hierarchy first, so a reference resolves wherever it appears.
        self._declare_scope(self._instance, "")
        self._lower_scope(self._instance, "")
        drivers = {name: tuple(found) for name, found in self._drivers.items()}
        return DesignGraph(
            top_module=self._instance.definition.name,
            sources=sources,
            signals=MappingProxyType(dict(self._signals)),
            drivers=MappingProxyType(drivers),
            edges=structural_edges(drivers),
            diagnostics=tuple(self._diagnostics),
            aliases=MappingProxyType(dict(self._aliases)),
        )

    def _declare_scope(self, instance: ast.InstanceSymbol, scope: str) -> None:
        members = list(instance.body)
        directions: dict[str, SignalKind] = {}
        if not scope:
            for member in members:
                if isinstance(member, ast.PortSymbol) and member.internalSymbol is not None:
                    directions[member.internalSymbol.name] = _DIRECTIONS.get(
                        member.direction, SignalKind.INOUT
                    )
        for member in members:
            if isinstance(member, (ast.VariableSymbol, ast.NetSymbol)):
                kind = directions.get(member.name, SignalKind.INTERNAL)
                self._declare(member, f"{scope}{member.name}", kind)
            elif isinstance(member, ast.InstanceSymbol):
                self._declare_scope(member, f"{scope}{member.name}.")

    def _lower_scope(self, instance: ast.InstanceSymbol, scope: str) -> None:
        outer, self._scope = self._scope, scope
        for member in instance.body:
            if isinstance(member, ast.ContinuousAssignSymbol):
                self._lower_continuous(member)
            elif isinstance(member, ast.ProceduralBlockSymbol):
                self._lower_process(member)
            elif isinstance(member, ast.NetSymbol):
                self._lower_net_initializer(member)
            elif isinstance(member, ast.InstanceSymbol):
                child = f"{scope}{member.name}."
                self._lower_scope(member, child)
                self._lower_instance(member, child)
            elif member.kind in (
                ast.SymbolKind.GenerateBlock,
                ast.SymbolKind.GenerateBlockArray,
                ast.SymbolKind.InstanceArray,
                ast.SymbolKind.PrimitiveInstance,
                ast.SymbolKind.CheckerInstance,
            ):
                self._diag(
                    "UNSUPPORTED_SCOPE",
                    f"{member.kind.name} {member.name or '(unnamed)'} at "
                    f"{self._where(member.location)} is not traversed; signals it drives "
                    "appear undriven.",
                )
        self._scope = outer

    def _declare(
        self, symbol: ast.VariableSymbol | ast.NetSymbol, name: str, kind: SignalKind
    ) -> None:
        canonical = symbol.type.canonicalType
        span = self._line_span(symbol.location)
        if self._declare_value(name, canonical, kind, span, symbol.location) is None:
            self._diag(
                "UNSUPPORTED_SIGNAL_TYPE",
                f"{name} has type {symbol.type} at {self._where(symbol.location)}; "
                "its value is not reconstructed and reads of it stay unknown.",
            )
            self._declare_aggregate(name, canonical, kind, span)

    def _declare_value(
        self,
        name: str,
        canonical: ast.Type,
        kind: SignalKind,
        span: SourceSpan | None,
        location: pyslang.SourceLocation,
    ) -> tuple[str, ...] | None:
        """Declare ``name`` and return the leaf signals that hold its value.

        An integral value is one signal. An unpacked array of integral elements
        is one signal per element, ``mem[2]``. An unpacked struct is flattened
        to one signal per leaf field, ``s.field`` or ``s.inner.field``. Returns
        None, declaring nothing, for any other type.
        """
        packed = _packed(canonical)
        if packed is not None:
            width, bit_range, signed = packed
            self._signals[name] = SignalInfo(name, width, bit_range, signed, kind, span)
            return (name,)
        if canonical.isUnpackedArray:
            return self._declare_array(name, canonical, kind, span, location)
        if not isinstance(canonical, ast.UnpackedStructType):
            return None
        leaves: list[str] = []
        fields: list[str] = []
        for member in canonical:
            if not isinstance(member, ast.FieldSymbol):
                continue
            field_name = f"{name}.{member.name}"
            field_type = member.type.canonicalType
            found = self._declare_value(field_name, field_type, kind, span, location)
            if found is None:
                self._diag(
                    "UNSUPPORTED_SIGNAL_TYPE",
                    f"{field_name} has type {member.type} at {self._where(location)}; its value "
                    "is not reconstructed and reads of it stay unknown.",
                )
                self._declare_aggregate(field_name, field_type, kind, span)
                found = (field_name,)
            fields.append(field_name)
            leaves.extend(found)
        self._records[name] = tuple(leaves)
        self._fields[name] = tuple(fields)
        return tuple(leaves)

    def _declare_aggregate(
        self, name: str, canonical: ast.Type, kind: SignalKind, span: SourceSpan | None
    ) -> None:
        width = max(1, canonical.bitstreamWidth)
        self._signals[name] = SignalInfo(
            name, width, BitRange(left=width - 1, right=0), False, kind, span, True
        )

    def _declare_array(
        self,
        name: str,
        canonical: ast.Type,
        kind: SignalKind,
        span: SourceSpan | None,
        location: pyslang.SourceLocation,
    ) -> tuple[str, ...] | None:
        """Declare one signal per element, named ``mem[0]``; None if not possible.

        None leaves the declaration to the caller, which reports the type. An
        array that has integral elements but cannot be expanded is declared
        here as one aggregate signal, with a diagnostic saying why.
        """
        ranges: list[tuple[int, int]] = []
        current = canonical
        while current.isUnpackedArray and current.isFixedSize:
            declared = current.fixedRange
            ranges.append((declared.left, declared.right))
            current = current.arrayElementType.canonicalType
        packed = None if current.isUnpackedArray else _packed(current)
        if packed is None:
            return None
        count = math.prod(ir.dimension_size(bounds) for bounds in ranges)
        reason = None
        if any(min(bounds) < 0 for bounds in ranges):
            reason = "a negative index cannot be named in a signal path"
        elif count > MAX_ARRAY_ELEMENTS:
            reason = f"it has {count} elements, more than the limit of {MAX_ARRAY_ELEMENTS}"
        if reason is not None:
            self._diag(
                "ARRAY_NOT_EXPANDED",
                f"{name} at {self._where(location)} is kept as one aggregate signal "
                f"because {reason}; reads of its elements stay unknown and every write to it "
                "is kept.",
            )
            self._declare_aggregate(name, canonical, kind, span)
            return (name,)
        width, bit_range, signed = packed
        elements: list[str] = []
        for indices in ir.element_indices(tuple(ranges)):
            element = name + "".join(f"[{index}]" for index in indices)
            self._signals[element] = SignalInfo(element, width, bit_range, signed, kind, span)
            elements.append(element)
        self._arrays[name] = _ArrayLayout(name, tuple(ranges), width, signed, tuple(elements))
        return tuple(elements)

    def _add_drivers(
        self,
        assigns: Sequence[GuardedAssign],
        edge_type: EdgeType,
        process: ProcessKind,
        clock: ClockSpec | None,
        span: SourceSpan | None,
        unsupported: str | None,
        caveat: str | None,
    ) -> None:
        by_target: dict[str, list[GuardedAssign]] = {}
        for assign in assigns:
            by_target.setdefault(assign.target, []).append(assign)
        written = set(by_target)
        for target, group in by_target.items():
            caveats = [caveat] if caveat else []
            procedural = process not in (ProcessKind.CONTINUOUS, ProcessKind.PORT)
            if edge_type is EdgeType.COMBINATIONAL and procedural:
                caveats.extend(_intermediate_reads(assigns, group, written))
            self._drivers.setdefault(target, []).append(
                Driver(
                    target=target,
                    edge_type=edge_type,
                    process=process,
                    clock=clock,
                    assigns=tuple(group),
                    span=span,
                    unsupported=unsupported,
                    caveats=tuple(caveats),
                )
            )

    def _lower_continuous(self, symbol: ast.ContinuousAssignSymbol) -> None:
        assigns: list[GuardedAssign] = []
        assignment = symbol.assignment
        span = self._span(assignment.sourceRange)
        if isinstance(assignment, ast.AssignmentExpression):
            self._lower_assignment(assignment, (), assigns, span)
        else:
            self._diag(
                "UNSUPPORTED_STATEMENT",
                f"continuous assignment at {self._where(symbol.location)} is not an assignment.",
            )
        unsupported = None
        if symbol.delay is not None:
            unsupported = "delayed continuous assignment (delays are outside the MVP)"
        self._add_drivers(
            assigns,
            EdgeType.COMBINATIONAL,
            ProcessKind.CONTINUOUS,
            None,
            span,
            unsupported,
            None,
        )

    def _lower_net_initializer(self, symbol: ast.NetSymbol) -> None:
        initializer = symbol.initializer
        name = f"{self._scope}{symbol.name}"
        info = self._signals.get(name)
        if initializer is None or info is None:
            return
        span = self._span(initializer.sourceRange) or info.span
        rhs = self._lower(initializer)
        assign = GuardedAssign(
            target=name,
            full_write=True,
            rhs=rhs,
            guards=(),
            index_operands=(),
            blocking=True,
            span=span,
            text=f"{name} = {self._text(initializer.sourceRange) or ir.to_sv(rhs)}",
        )
        self._add_drivers(
            [assign], EdgeType.COMBINATIONAL, ProcessKind.CONTINUOUS, None, span, None, None
        )

    def _lower_instance(self, instance: ast.InstanceSymbol, child_scope: str) -> None:
        """Lower the port connections of ``instance``; called in the parent's scope."""
        label = f"instance {child_scope[:-1]} of {instance.definition.name}"
        where = self._where(instance.location)
        fallback = self._line_span(instance.location)
        for connection in instance.portConnections:
            port = connection.port
            internal = port.internalSymbol if isinstance(port, ast.PortSymbol) else None
            child = f"{child_scope}{internal.name}" if internal is not None else None
            if not isinstance(port, ast.PortSymbol) or child is None or child not in self._signals:
                self._diag(
                    "UNSUPPORTED_PORT",
                    f"Port {port.name} of {label} at {where} is not a simple signal port; "
                    "nothing is traced through it.",
                )
                continue
            expression = connection.expression
            if port.direction is ast.ArgumentDirection.In:
                if expression is None:
                    self._diag(
                        "UNCONNECTED_PORT",
                        f"Input port {child} of {label} at {where} is not connected; it has no "
                        "driver and a slice that reaches it ends there.",
                    )
                    continue
                self._lower_input_port(child, expression, fallback)
            elif port.direction is ast.ArgumentDirection.Out:
                if expression is None:
                    continue
                if not isinstance(expression, ast.AssignmentExpression):
                    self._diag(
                        "UNSUPPORTED_PORT",
                        f"Output port {child} of {label} at {where} has a connection that is not "
                        "an assignable expression; nothing is traced through it.",
                    )
                    continue
                self._lower_output_port(child, expression, fallback)
            else:
                self._lower_bidirectional_port(child, port, expression, label, where, fallback)

    def _lower_input_port(
        self, child: str, expression: ast.Expression, fallback: SourceSpan | None
    ) -> None:
        info = self._signals[child]
        span = self._span(expression.sourceRange) or fallback
        rhs = self._lower(expression)
        if rhs.width != info.width:
            rhs = ir.Convert(info.width, info.signed, rhs)
        if isinstance(rhs, ir.SignalRef) and rhs.width == self._signals[rhs.signal].width:
            # A plain whole-signal connection: the child signal is the same net.
            self._aliases[child] = rhs.signal
        text = f"{child} = {self._text(expression.sourceRange) or ir.to_sv(rhs)}"
        assign = GuardedAssign(child, True, rhs, (), (), True, span, text)
        self._add_drivers(
            [assign], EdgeType.COMBINATIONAL, ProcessKind.PORT, None, span, None, None
        )

    def _lower_output_port(
        self, child: str, expression: ast.AssignmentExpression, fallback: SourceSpan | None
    ) -> None:
        info = self._signals[child]
        span = self._span(expression.sourceRange) or fallback
        self._port_value = ir.SignalRef(info.width, info.signed and not info.is_aggregate, child)
        assigns: list[GuardedAssign] = []
        target_text = self._text(expression.left.sourceRange) or "<port connection>"
        self._lower_assignment(expression, (), assigns, span, text=f"{target_text} = {child}")
        self._port_value = None
        self._add_drivers(
            assigns, EdgeType.COMBINATIONAL, ProcessKind.PORT, None, span, None, None
        )

    def _lower_bidirectional_port(
        self,
        child: str,
        port: ast.PortSymbol,
        expression: ast.Expression | None,
        label: str,
        where: str,
        fallback: SourceSpan | None,
    ) -> None:
        reason = (
            f"connected through {port.direction.name} port {child} of {label}; "
            "bidirectional ports are not traced"
        )
        self._diag(
            "UNSUPPORTED_PORT",
            f"{port.direction.name} port {child} of {label} at {where} is not traced; signals "
            "on either side of it end the slice.",
        )
        targets = [child]
        if expression is not None:
            lvalue = (
                expression.left if isinstance(expression, ast.AssignmentExpression) else expression
            )
            targets.extend(target.signal for target in self._targets(lvalue) or ())
        for target in dict.fromkeys(targets):
            self._drivers.setdefault(target, []).append(
                Driver(
                    target=target,
                    edge_type=EdgeType.COMBINATIONAL,
                    process=ProcessKind.INSTANCE,
                    clock=None,
                    assigns=(),
                    span=fallback,
                    unsupported=reason,
                )
            )

    def _lower_process(self, block: ast.ProceduralBlockSymbol) -> None:
        kind = block.procedureKind
        if kind is ast.ProceduralBlockKind.Final:
            return
        if kind is ast.ProceduralBlockKind.Initial:
            self._diag(
                "IGNORED_INITIAL_BLOCK",
                f"initial block at {self._where(block.location)} is not synthesizable RTL and "
                "is not part of the dependency graph.",
            )
            return
        process = _PROCESS_KINDS[kind]
        body = block.body
        span = self._span(body.sourceRange)
        statement: ast.Statement = body
        if isinstance(body, ast.TimedStatement):
            timing = self._classify_timing(body.timing)
            statement = body.stmt
        elif kind in (ast.ProceduralBlockKind.AlwaysComb, ast.ProceduralBlockKind.AlwaysLatch):
            timing = _Timing(EdgeType.COMBINATIONAL, None, None, None)
        else:
            timing = _Timing(
                EdgeType.COMBINATIONAL, None, "always block without an event control", None
            )
        unsupported = timing.unsupported
        if kind is ast.ProceduralBlockKind.AlwaysLatch:
            unsupported = "always_latch (latches are outside the MVP)"
        assigns: list[GuardedAssign] = []
        self._lower_statement(statement, (), assigns)
        if (
            unsupported is None
            and timing.edge_type is EdgeType.SEQUENTIAL
            and any(assign.blocking for assign in assigns)
        ):
            unsupported = (
                "blocking assignment in a clocked process; intra-process ordering is not "
                "reconstructed yet"
            )
        self._add_drivers(
            assigns, timing.edge_type, process, timing.clock, span, unsupported, timing.caveat
        )

    def _classify_timing(self, timing: ast.TimingControl) -> _Timing:
        if isinstance(timing, ast.ImplicitEventControl):
            return _Timing(EdgeType.COMBINATIONAL, None, None, None)
        events: list[ast.SignalEventControl] = []
        if isinstance(timing, ast.SignalEventControl):
            events = [timing]
        elif isinstance(timing, ast.EventListControl):
            for event in timing.events:
                if not isinstance(event, ast.SignalEventControl):
                    return _Timing(
                        EdgeType.COMBINATIONAL, None, "unsupported event in sensitivity list", None
                    )
                events.append(event)
        else:
            return _Timing(
                EdgeType.COMBINATIONAL, None, f"timing control {timing.kind.name}", None
            )
        edges = [event for event in events if event.edge in _EDGE_KINDS]
        if not edges:
            return _Timing(
                EdgeType.COMBINATIONAL,
                None,
                None,
                "explicit level-sensitive event list treated as combinational logic",
            )
        if len(events) > 1:
            return _Timing(
                EdgeType.SEQUENTIAL,
                None,
                "several events in the sensitivity list (asynchronous reset or more than "
                "one clock is outside the MVP)",
                None,
            )
        event = edges[0]
        if event.iffCondition is not None:
            return _Timing(EdgeType.SEQUENTIAL, None, "iff-qualified clock event", None)
        if event.edge is ast.EdgeKind.BothEdges:
            return _Timing(EdgeType.SEQUENTIAL, None, "dual-edge clock event", None)
        clock = self._lower(event.expr)
        if not isinstance(clock, ir.SignalRef) or clock.width != 1:
            return _Timing(
                EdgeType.SEQUENTIAL, None, "clock event is not a one-bit signal", None
            )
        return _Timing(
            EdgeType.SEQUENTIAL,
            ClockSpec(clock.signal, event.edge is ast.EdgeKind.PosEdge),
            None,
            None,
        )

    # ---- statements -----------------------------------------------------

    def _lower_statement(
        self, statement: ast.Statement, guards: tuple[ir.Guard, ...], out: list[GuardedAssign]
    ) -> None:
        if isinstance(statement, ast.StatementList):
            for child in statement.list:
                self._lower_statement(child, guards, out)
        elif isinstance(statement, ast.BlockStatement):
            self._lower_statement(statement.body, guards, out)
        elif isinstance(statement, (ast.EmptyStatement, ast.VariableDeclStatement)):
            return
        elif isinstance(statement, ast.ExpressionStatement) and isinstance(
            statement.expr, ast.AssignmentExpression
        ):
            self._lower_assignment(
                statement.expr, guards, out, self._span(statement.sourceRange)
            )
        elif isinstance(statement, ast.ConditionalStatement):
            self._lower_conditional(statement, guards, out)
        elif isinstance(statement, ast.CaseStatement):
            self._lower_case(statement, guards, out)
        else:
            self._lower_conservatively(statement, guards, out)

    def _next_decision(self) -> int:
        self._decisions += 1
        return self._decisions

    def _lower_conditional(
        self,
        statement: ast.ConditionalStatement,
        guards: tuple[ir.Guard, ...],
        out: list[GuardedAssign],
    ) -> None:
        conditions = list(statement.conditions)
        decision = self._next_decision()
        if len(conditions) == 1 and conditions[0].pattern is None:
            condition = conditions[0].expr
            predicate = self._lower(condition)
            kind = ir.GuardKind.IF
            text = self._text(condition.sourceRange) or ir.to_sv(predicate)
            span = self._span(condition.sourceRange)
        else:
            self._diag(
                "UNSUPPORTED_STATEMENT",
                f"pattern or multi-clause condition at {self._where(statement.sourceRange.start)} "
                "is not evaluated; both branches are kept.",
            )
            predicate = ir.Const(1, False, "x")
            kind = ir.GuardKind.UNSUPPORTED
            text = "pattern condition"
            span = self._span(statement.sourceRange)
        on_true = ir.Guard(decision, kind, predicate, text, True, span=span)
        self._lower_statement(statement.ifTrue, (*guards, on_true), out)
        if statement.ifFalse is not None:
            on_false = ir.Guard(decision, kind, predicate, text, False, span=span)
            self._lower_statement(statement.ifFalse, (*guards, on_false), out)

    def _lower_case(
        self,
        statement: ast.CaseStatement,
        guards: tuple[ir.Guard, ...],
        out: list[GuardedAssign],
    ) -> None:
        decision = self._next_decision()
        selector = self._lower(statement.expr)
        text = self._text(statement.expr.sourceRange) or ir.to_sv(selector)
        span = self._span(statement.expr.sourceRange)
        flavor = _CASE_FLAVORS.get(statement.condition)
        if flavor is None:
            self._diag(
                "UNSUPPORTED_STATEMENT",
                f"case-inside at {self._where(statement.sourceRange.start)} is not evaluated; "
                "every arm is kept.",
            )
        earlier: list[ir.Expr] = []
        for item in statement.items:
            items = tuple(self._lower(expression) for expression in item.expressions)
            guard = ir.Guard(
                decision,
                ir.GuardKind.CASE_ITEM if flavor else ir.GuardKind.UNSUPPORTED,
                selector,
                text,
                flavor=flavor or CaseFlavor.NORMAL,
                items=items,
                earlier=tuple(earlier),
                span=span,
            )
            self._lower_statement(item.stmt, (*guards, guard), out)
            earlier.extend(items)
        if statement.defaultCase is not None:
            guard = ir.Guard(
                decision,
                ir.GuardKind.CASE_DEFAULT if flavor else ir.GuardKind.UNSUPPORTED,
                selector,
                text,
                flavor=flavor or CaseFlavor.NORMAL,
                earlier=tuple(earlier),
                span=span,
            )
            self._lower_statement(statement.defaultCase, (*guards, guard), out)

    def _lower_assignment(
        self,
        assignment: ast.AssignmentExpression,
        guards: tuple[ir.Guard, ...],
        out: list[GuardedAssign],
        span: SourceSpan | None,
        text: str | None = None,
    ) -> None:
        left_type = assignment.left.type.canonicalType
        composite = left_type.isUnpackedStruct or left_type.isUnpackedArray
        if composite and not assignment.isCompound:
            if self._lower_composite_assignment(assignment, guards, out, span, text):
                return
        targets = self._targets(assignment.left)
        if assignment.isCompound:
            self._lvalue = self._lower(assignment.left)
        rhs = self._lower(assignment.right)
        self._lvalue = None
        where = self._where(assignment.sourceRange.start)
        if assignment.timingControl is not None:
            self._diag(
                "UNSUPPORTED_STATEMENT",
                f"intra-assignment timing control at {where} is ignored.",
            )
        if targets is None:
            self._diag(
                "UNSUPPORTED_LVALUE",
                f"assignment target at {where} is not a signal of the analyzed module; "
                "the assignment is not in the graph.",
            )
            return
        if text is None:
            text = self._text(assignment.sourceRange) or f"<assignment> {ir.to_sv(rhs)}"
        for target in targets:
            # Element targets of one indexed write are alternatives: each takes the
            # whole RHS. Targets of a concatenation take consecutive slices of it.
            value = rhs
            if target.rhs_offset != 0 or target.width != rhs.width:
                if isinstance(rhs, ir.Unsupported):
                    value = ir.Unsupported(target.width, False, rhs.what, rhs.operands)
                else:
                    label = f"[{rhs.width - target.rhs_offset - 1}-:{target.width}]"
                    value = ir.PartSelect(target.width, False, rhs, target.rhs_offset, label)
            out.append(
                GuardedAssign(
                    target=target.signal,
                    full_write=target.full_write,
                    rhs=value,
                    guards=(*guards, *target.guards),
                    index_operands=target.index_operands,
                    blocking=not assignment.isNonBlocking,
                    span=span,
                    text=text,
                    written=target.bits,
                )
            )

    def _lower_composite_assignment(
        self,
        assignment: ast.AssignmentExpression,
        guards: tuple[ir.Guard, ...],
        out: list[GuardedAssign],
        span: SourceSpan | None,
        text: str | None,
    ) -> bool:
        """Lower a whole unpacked struct or array assignment leaf by leaf.

        Handles a copy from another value of the same shape and an assignment
        pattern. Returns False, emitting nothing, for any other right-hand
        side; the caller then keeps it as a partial write of unknown value.
        """
        target = self._aggregate_name(assignment.left)
        if target is None or self._leaves(target) is None:
            return False
        pairs = self._composite_sources(assignment.right, target)
        if pairs is None:
            return False
        if text is None:
            text = self._text(assignment.sourceRange) or "<assignment>"
        for leaf, rhs in pairs:
            info = self._signals[leaf]
            out.append(
                GuardedAssign(
                    target=leaf,
                    full_write=True,
                    rhs=rhs,
                    guards=guards,
                    index_operands=(),
                    blocking=not assignment.isNonBlocking,
                    span=span,
                    text=text,
                    written=(0, info.width),
                )
            )
        return True

    def _composite_sources(
        self, expression: ast.Expression, target: str
    ) -> list[tuple[str, ir.Expr]] | None:
        """The value each leaf of ``target`` takes from ``expression``, or None."""
        while isinstance(expression, ast.ConversionExpression) and not _integral(expression.type):
            expression = expression.operand
        leaves = self._leaves(target)
        if leaves is None:
            info = self._signals.get(target)
            if info is None:
                return None
            value = self._lower(expression)
            if value.width != info.width:
                value = ir.Convert(info.width, info.signed, value)
            return [(target, value)]
        source = self._aggregate_name(expression)
        if source is not None:
            pairs: list[tuple[str, ir.Expr]] = []
            for leaf in leaves:
                other = self._signals.get(source + leaf[len(target) :])
                if other is None:
                    return None
                pairs.append((leaf, ir.SignalRef(other.width, other.signed, other.name)))
            return pairs
        patterns = (ast.StructuredAssignmentPatternExpression, ast.SimpleAssignmentPatternExpression)
        if not isinstance(expression, patterns):
            return None
        children = self._fields.get(target)
        if children is None:
            layout = self._arrays.get(target)
            if layout is None or len(layout.ranges) != 1:
                return None
            children = layout.elements
        elements = list(expression.elements)
        if len(elements) != len(children):
            return None
        pairs = []
        for child, element in zip(children, elements):
            found = self._composite_sources(element, child)
            if found is None:
                return None
            pairs.extend(found)
        return pairs

    def _lower_conservatively(
        self,
        statement: ast.Statement,
        guards: tuple[ir.Guard, ...],
        out: list[GuardedAssign],
    ) -> None:
        what = statement.kind.name
        self._diag(
            "UNSUPPORTED_STATEMENT",
            f"{what} statement at {self._where(statement.sourceRange.start)} is not modeled; "
            "assignments inside it are kept as unknown dependencies on every signal it reads.",
        )
        assignments: list[ast.AssignmentExpression] = []
        reads: dict[str, ir.Expr] = {}

        def visit(node: object) -> None:
            if isinstance(node, ast.AssignmentExpression):
                assignments.append(node)
            elif isinstance(node, ast.NamedValueExpression):
                for ref in _signal_refs(self._lower_named(node, quiet=True)):
                    reads.setdefault(ref.signal, ref)

        statement.visit(visit)
        span = self._span(statement.sourceRange)
        guard = ir.Guard(
            self._next_decision(),
            ir.GuardKind.UNSUPPORTED,
            ir.Const(1, False, "x"),
            what,
            span=span,
        )
        for assignment in assignments:
            for target in self._targets(assignment.left) or ():
                operands = tuple(
                    operand for signal, operand in reads.items() if signal != target.signal
                )
                out.append(
                    GuardedAssign(
                        target=target.signal,
                        full_write=False,
                        rhs=ir.Unsupported(target.width, False, f"{what} statement", operands),
                        guards=(*guards, *target.guards, guard),
                        index_operands=(),
                        blocking=not assignment.isNonBlocking,
                        span=span,
                        text=f"{what} statement",
                    )
                )

    # ---- lvalues --------------------------------------------------------

    def _targets(self, lvalue: ast.Expression) -> list[_Target] | None:
        """Signals written by ``lvalue``; None if any part cannot be resolved."""
        if isinstance(lvalue, ast.ConcatenationExpression):
            found: list[_Target] = []
            offset = 0
            for operand in lvalue.operands:
                width = operand.type.bitWidth
                nested = self._targets(operand)
                if nested is None:
                    return None
                for target in nested:
                    found.append(
                        _Target(
                            target.signal,
                            target.full_write,
                            target.index_operands,
                            offset + target.rhs_offset,
                            target.width,
                            target.guards,
                            target.bits,
                        )
                    )
                offset += width
            return found
        roots = self._lvalue_roots(lvalue)
        if roots is None:
            return None
        lvalue_type = lvalue.type.canonicalType
        partial = max(1, lvalue_type.bitstreamWidth) if lvalue_type.isIntegral else None
        found = []
        for root in roots:
            info = self._signals[root.signal]
            width = info.width if root.full_write or partial is None else partial
            found.append(
                _Target(
                    root.signal, root.full_write, root.index_operands, 0, width, root.guards, root.bits
                )
            )
        return found

    def _lvalue_roots(self, lvalue: ast.Expression) -> list[_Root] | None:
        """Signals ``lvalue`` writes; None if it cannot be resolved."""
        access = self._array_access(lvalue)
        if access is not None:
            return self._element_roots(lvalue, *access)
        where = self._where(lvalue.sourceRange.start)
        name = self._aggregate_name(lvalue)
        if name is not None:
            info = self._signals.get(name)
            if info is not None:
                return [_Root(name, True, (), (), (0, info.width))]
            leaves = self._leaves(name)
            if leaves is None:
                return None
            self._diag(
                "UNSUPPORTED_AGGREGATE_ACCESS",
                f"Assignment to all of {name} at {where} is not modeled leaf by leaf; every "
                "element or field keeps it as a partial write of unknown value.",
            )
            return [_Root(leaf, False, (), ()) for leaf in leaves]
        if isinstance(lvalue, ast.NamedValueExpression):
            return None
        if isinstance(lvalue, ast.ElementSelectExpression):
            selectors: tuple[ast.Expression, ...] = (lvalue.selector,)
        elif isinstance(lvalue, ast.RangeSelectExpression):
            selectors = (lvalue.left, lvalue.right)
        elif isinstance(lvalue, ast.MemberAccessExpression):
            selectors = ()
        else:
            return None
        inner = self._lvalue_roots(lvalue.value)
        if inner is None:
            return None
        if lvalue.value.type.canonicalType.isUnpackedArray:
            self._diag(
                "UNSUPPORTED_AGGREGATE_ACCESS",
                f"Assignment to a slice of an unpacked array at {where} is not modeled element by "
                "element; every element it may touch keeps it as a partial write.",
            )
        lowered = [self._lower(selector) for selector in selectors]
        dynamic = tuple(value for value in lowered if not isinstance(value, ir.Const))
        return [
            _Root(
                root.signal,
                False,
                (*root.index_operands, *dynamic),
                root.guards,
                self._select_bits(lvalue, root.bits, lowered),
            )
            for root in inner
        ]

    @staticmethod
    def _select_bits(
        lvalue: ast.Expression, bits: tuple[int, int] | None, selectors: Sequence[ir.Expr]
    ) -> tuple[int, int] | None:
        """Bits of the root signal a constant packed select or field of ``bits`` covers.

        Offsets count from the root's left bit. None when the position depends
        on a run-time value or cannot be computed.
        """
        if bits is None:
            return None
        base_offset, base_width = bits
        if not isinstance(
            lvalue,
            (ast.MemberAccessExpression, ast.ElementSelectExpression, ast.RangeSelectExpression),
        ):
            return None
        result = _integral(lvalue.type)
        container = lvalue.value.type.canonicalType
        if result is None or not container.isIntegral:
            return None
        width = result[0]
        if isinstance(lvalue, ast.MemberAccessExpression):
            member = lvalue.member
            if not isinstance(member, ast.FieldSymbol):
                return None
            offset = base_width - member.bitOffset - width
        else:
            values = [_const_int(selector) for selector in selectors]
            known = [value for value in values if value is not None]
            if len(known) != len(values):
                return None
            declared = container.fixedRange
            left, right = declared.left, declared.right
            element = base_width // (abs(left - right) + 1)
            if element < 1:
                return None
            if not isinstance(lvalue, ast.RangeSelectExpression):
                indices = [known[0]]
            elif lvalue.selectionKind is ast.RangeSelectionKind.Simple:
                indices = known[:2]
            elif lvalue.selectionKind is ast.RangeSelectionKind.IndexedUp:
                indices = [known[0], known[0] + width // element - 1]
            else:
                indices = [known[0] - width // element + 1, known[0]]
            positions = [left - index if left >= right else index - left for index in indices]
            offset = min(positions) * element
        if offset < 0 or offset + width > base_width:
            return None
        return base_offset + offset, width

    def _element_roots(
        self, lvalue: ast.Expression, layout: _ArrayLayout, selectors: Sequence[ast.Expression]
    ) -> list[_Root]:
        """Elements an indexed array write may store to, each under its ``index`` guards.

        A constant index selects its element directly. A run-time index makes
        every element a target, written only when the index equals its declared
        index. Selecting fewer dimensions than declared writes a sub-array; its
        elements keep the write as a partial one.
        """
        lowered = [self._lower(selector) for selector in selectors]
        full = len(lowered) == len(layout.ranges)
        if not full:
            self._diag(
                "UNSUPPORTED_AGGREGATE_ACCESS",
                f"Assignment to a sub-array of {layout.name} at "
                f"{self._where(lvalue.sourceRange.start)} is not modeled element by element; "
                "every element it may touch keeps it as a partial write.",
            )
        decisions = [self._next_decision() for _ in lowered]
        roots: list[_Root] = []
        for indices, element in zip(ir.element_indices(layout.ranges), layout.elements):
            guards: list[ir.Guard] = []
            for dimension, selector in enumerate(lowered):
                if isinstance(selector, ir.Const):
                    if _const_int(selector) != indices[dimension]:
                        break
                    continue
                source = selectors[dimension].sourceRange
                guards.append(
                    ir.index_guard(
                        decisions[dimension],
                        selector,
                        indices[dimension],
                        self._text(source) or None,
                        self._span(source),
                    )
                )
            else:
                bits = (0, layout.width) if full else None
                roots.append(_Root(element, full, (), tuple(guards), bits))
        if not roots:
            self._diag(
                "ARRAY_INDEX_OUT_OF_RANGE",
                f"Assignment to {layout.name} at {self._where(lvalue.sourceRange.start)} has a "
                "constant index that is unknown or outside the declared range; it writes no "
                "element (IEEE 1800 section 7.4.6).",
            )
        return roots

    def _array_access(
        self, expression: ast.Expression
    ) -> tuple[_ArrayLayout, list[ast.Expression]] | None:
        """The expanded array and the index expressions of an element select chain."""
        selectors: list[ast.Expression] = []
        current = expression
        while (
            isinstance(current, ast.ElementSelectExpression)
            and current.value.type.canonicalType.isUnpackedArray
        ):
            selectors.append(current.selector)
            current = current.value
        base = self._aggregate_name(current) if selectors else None
        layout = self._arrays.get(base) if base is not None else None
        if layout is None:
            return None
        selectors.reverse()
        return layout, selectors

    def _aggregate_name(self, expression: ast.Expression) -> str | None:
        """Flattened name of a variable or an unpacked struct field chain, ``s.inner.f``."""
        if isinstance(expression, ast.NamedValueExpression):
            return self._relative(expression.symbol)
        if (
            isinstance(expression, ast.MemberAccessExpression)
            and expression.value.type.canonicalType.isUnpackedStruct
        ):
            base = self._aggregate_name(expression.value)
            return f"{base}.{expression.member.name}" if base is not None else None
        return None

    def _leaves(self, name: str) -> tuple[str, ...] | None:
        """Leaf signals of an expanded unpacked array or struct, or None."""
        layout = self._arrays.get(name)
        if layout is not None:
            return layout.elements
        return self._records.get(name)

    def _relative(self, symbol: ast.Symbol) -> str | None:
        """Path of ``symbol`` relative to the top instance, or None outside it."""
        path = symbol.hierarchicalPath
        return path[len(self._prefix) :] if path.startswith(self._prefix) else None

    def _local_name(self, symbol: ast.Symbol) -> str | None:
        """Path of a declared signal relative to the top instance, or None.

        Every instance has its own elaborated body, so ``hierarchicalPath``
        tells two instances of one module apart.
        """
        name = self._relative(symbol)
        return name if name is not None and name in self._signals else None

    # ---- expressions ----------------------------------------------------

    def _lower(self, expression: ast.Expression) -> ir.Expr:
        return self._fold(self._lower_raw(expression))

    @staticmethod
    def _fold(lowered: ir.Expr) -> ir.Expr:
        operands = ir.children(lowered)
        if not operands or isinstance(lowered, (ir.Unsupported, ir.ArrayElement)):
            return lowered
        if not all(isinstance(operand, ir.Const) for operand in operands):
            return lowered
        value = ir.evaluate(lowered, lambda _signal: None)
        return ir.Const(lowered.width, lowered.signed, value.bits)

    def _lower_raw(self, expression: ast.Expression) -> ir.Expr:
        if isinstance(expression, ast.NamedValueExpression):
            return self._lower_named(expression)
        if isinstance(expression, ast.EmptyArgumentExpression) and self._port_value is not None:
            # The right-hand side of an output port connection: the child port's value.
            return self._port_value
        info = _integral(expression.type)
        if info is None:
            return self._unsupported(expression, f"{expression.kind.name} of non-integral type", 1)
        width, signed = info
        if isinstance(expression, (ast.IntegerLiteral, ast.UnbasedUnsizedIntegerLiteral)):
            bits = fs.resize(Vec(_svint_bits(expression.value), signed), width).bits
            return ir.Const(width, signed, bits)
        if isinstance(expression, ast.ConversionExpression):
            operand = self._lower(expression.operand)
            if operand.width == width and operand.signed == signed:
                return operand
            propagated = expression.conversionKind is ast.ConversionKind.Propagated
            if propagated and operand.signed != signed:
                # IEEE 1800 11.8.2: a context-determined operand takes the propagated
                # type first, so it is sign-extended only if that type is signed.
                operand = self._fold(ir.Convert(operand.width, signed, operand))
            return ir.Convert(width, signed, operand)
        if isinstance(expression, ast.UnaryExpression):
            unary_op = _UNARY_OPS.get(expression.op)
            if unary_op is None:
                return self._unsupported(expression, f"operator {expression.op.name}", width, signed)
            return ir.Unary(width, signed, unary_op, self._lower(expression.operand))
        if isinstance(expression, ast.BinaryExpression):
            binary_op = _BINARY_OPS.get(expression.op)
            if binary_op is None:
                return self._unsupported(expression, f"operator {expression.op.name}", width, signed)
            left, right = self._lower(expression.left), self._lower(expression.right)
            return ir.Binary(width, signed, binary_op, left, right)
        if isinstance(expression, ast.ConditionalExpression):
            return self._lower_ternary(expression, width, signed)
        if isinstance(expression, ast.ConcatenationExpression):
            parts = tuple(
                self._lower(operand)
                for operand in expression.operands
                if _integral(operand.type) is not None
            )
            if sum(part.width for part in parts) != width:
                return self._unsupported(expression, "concatenation", width, signed)
            return ir.Concat(width, signed, parts)
        if isinstance(expression, ast.ReplicationExpression):
            count = _const_int(self._lower(expression.count))
            operand = self._lower(expression.concat)
            if count is None or count < 1 or operand.width * count != width:
                return self._unsupported(expression, "replication", width, signed)
            return ir.Replicate(width, signed, count, operand)
        if isinstance(expression, ast.ElementSelectExpression):
            return self._lower_element_select(expression, width, signed)
        if isinstance(expression, ast.RangeSelectExpression):
            return self._lower_range_select(expression, width, signed)
        if isinstance(expression, ast.MemberAccessExpression):
            return self._lower_member(expression, width, signed)
        if isinstance(expression, ast.CallExpression):
            arguments = list(expression.arguments)
            if expression.subroutineName in ("$signed", "$unsigned") and len(arguments) == 1:
                return ir.Convert(width, signed, self._lower(arguments[0]))
            return self._unsupported(expression, f"call to {expression.subroutineName}", width, signed)
        if isinstance(expression, ast.LValueReferenceExpression) and self._lvalue is not None:
            return self._lvalue
        return self._unsupported(expression, expression.kind.name, width, signed)

    def _lower_named(self, expression: ast.NamedValueExpression, quiet: bool = False) -> ir.Expr:
        symbol = expression.symbol
        info = _integral(expression.type)
        if isinstance(symbol, (ast.ParameterSymbol, ast.EnumValueSymbol)):
            constant = symbol.value.value
            if info is not None and isinstance(constant, pyslang.SVInt):
                width, signed = info
                bits = fs.resize(Vec(_svint_bits(constant), constant.isSigned), width).bits
                return ir.Const(width, signed, bits)
        name = self._local_name(symbol)
        if name is not None:
            declared = self._signals[name]
            if info is not None and not declared.is_aggregate:
                return ir.SignalRef(info[0], info[1], name)
            return ir.SignalRef(declared.width, False, name)
        width, signed = info if info is not None else (1, False)
        relative = self._relative(symbol)
        leaves = self._leaves(relative) if relative is not None else None
        if relative is not None and leaves is not None:
            what = f"whole-value reference to {relative}"
            if not quiet:
                self._diag(
                    "UNSUPPORTED_EXPRESSION",
                    f"{what} at {self._where(expression.sourceRange.start)} is not evaluated "
                    "leaf by leaf; its value is unknown and every element or field is kept.",
                )
            refs = tuple(
                ir.SignalRef(self._signals[leaf].width, self._signals[leaf].signed, leaf)
                for leaf in leaves
            )
            return ir.Unsupported(width, signed, what, refs)
        what = f"reference to {symbol.kind.name} {symbol.name}"
        if not quiet:
            self._diag(
                "UNSUPPORTED_EXPRESSION",
                f"{what} at {self._where(expression.sourceRange.start)} is not a signal of the "
                "analyzed module; its value is treated as unknown.",
            )
        return ir.Unsupported(width, signed, what, ())

    def _lower_ternary(
        self, expression: ast.ConditionalExpression, width: int, signed: bool
    ) -> ir.Expr:
        conditions = list(expression.conditions)
        if len(conditions) != 1 or conditions[0].pattern is not None:
            return self._unsupported(expression, "pattern conditional", width, signed)
        return ir.Conditional(
            width,
            signed,
            self._next_decision(),
            self._lower(conditions[0].expr),
            self._lower(expression.left),
            self._lower(expression.right),
            self._span(expression.sourceRange),
        )

    def _lower_element_select(
        self, expression: ast.ElementSelectExpression, width: int, signed: bool
    ) -> ir.Expr:
        container = expression.value.type.canonicalType
        if container.isUnpackedArray:
            access = self._array_access(expression)
            if access is not None:
                return self._lower_array_read(expression, *access, width, signed)
            # An array kept as one aggregate signal.
            value = self._lower(expression.value)
            return ir.ArrayElement(width, signed, value, self._lower(expression.selector))
        if not container.isIntegral:
            return self._unsupported(expression, "element select", width, signed)
        value = self._lower(expression.value)
        index = self._lower(expression.selector)
        declared = container.fixedRange
        left, right = declared.left, declared.right
        constant = _const_int(index)
        if constant is None:
            return ir.IndexedSelect(
                width, signed, value, index, left, right, width, ir.SelectMode.ELEMENT
            )
        position = left - constant if left >= right else constant - left
        if not 0 <= position <= abs(left - right):
            return ir.Const(width, signed, "x" * width)
        return ir.PartSelect(width, signed, value, position * width, f"[{constant}]")

    def _lower_array_read(
        self,
        expression: ast.ElementSelectExpression,
        layout: _ArrayLayout,
        selectors: Sequence[ast.Expression],
        width: int,
        signed: bool,
    ) -> ir.Expr:
        if len(selectors) != len(layout.ranges):
            return self._unsupported(expression, "sub-array select", width, signed)
        indices = tuple(self._lower(selector) for selector in selectors)
        if all(isinstance(index, ir.Const) for index in indices):
            values = [_const_int(index) for index in indices]
            known = [value for value in values if value is not None]
            element = layout.element(known) if len(known) == len(values) else None
            if element is None:
                # IEEE 1800 7.4.6: an unknown or out-of-range index reads all-x.
                return ir.Const(width, signed, "x" * width)
            return ir.SignalRef(width, signed, element)
        elements = tuple(
            ir.SignalRef(layout.width, layout.signed, element) for element in layout.elements
        )
        decisions = tuple(self._next_decision() for _ in indices)
        return ir.ArrayRead(
            width, signed, layout.name, elements, indices, layout.ranges, decisions
        )

    def _lower_range_select(
        self, expression: ast.RangeSelectExpression, width: int, signed: bool
    ) -> ir.Expr:
        container = expression.value.type.canonicalType
        if not container.isIntegral:
            return self._unsupported(expression, "slice of an unpacked array", width, signed)
        declared = container.fixedRange
        left, right = declared.left, declared.right
        total = abs(left - right) + 1
        value = self._lower(expression.value)
        elem_width = value.width // total
        first = self._lower(expression.left)
        second = self._lower(expression.right)
        if elem_width < 1 or width % elem_width:
            return self._unsupported(expression, "range select", width, signed)
        if expression.selectionKind is ast.RangeSelectionKind.Simple:
            high, low = _const_int(first), _const_int(second)
            if high is None or low is None:
                return self._unsupported(expression, "range select", width, signed)
            position = left - high if left >= right else high - left
            if position < 0 or position * elem_width + width > value.width:
                return ir.Const(width, signed, "x" * width)
            return ir.PartSelect(width, signed, value, position * elem_width, f"[{high}:{low}]")
        mode = (
            ir.SelectMode.UP
            if expression.selectionKind is ast.RangeSelectionKind.IndexedUp
            else ir.SelectMode.DOWN
        )
        return ir.IndexedSelect(width, signed, value, first, left, right, elem_width, mode)

    def _lower_member(
        self, expression: ast.MemberAccessExpression, width: int, signed: bool
    ) -> ir.Expr:
        member = expression.member
        if expression.value.type.canonicalType.isUnpackedStruct:
            name = self._aggregate_name(expression)
            info = self._signals.get(name) if name is not None else None
            if info is None:
                return self._unsupported(expression, "member access", width, signed)
            if info.is_aggregate:
                return ir.SignalRef(info.width, False, info.name)
            return ir.SignalRef(width, signed, info.name)
        container = _integral(expression.value.type)
        if container is None or not isinstance(member, ast.FieldSymbol):
            return self._unsupported(expression, "member access", width, signed)
        value = self._lower(expression.value)
        offset = value.width - member.bitOffset - width
        if offset < 0:
            return self._unsupported(expression, "member access", width, signed)
        return ir.PartSelect(width, signed, value, offset, f".{member.name}")

    def _unsupported(
        self, expression: ast.Expression, what: str, width: int, signed: bool = False
    ) -> ir.Expr:
        operands: dict[str, ir.Expr] = {}

        def visit(node: object) -> None:
            if isinstance(node, ast.NamedValueExpression):
                for ref in _signal_refs(self._lower_named(node, quiet=True)):
                    operands.setdefault(ref.signal, ref)

        expression.visit(visit)
        self._diag(
            "UNSUPPORTED_EXPRESSION",
            f"{what} at {self._where(expression.sourceRange.start)} is not evaluated; its "
            "value is treated as unknown and every signal it reads is kept.",
        )
        return ir.Unsupported(width, signed, what, tuple(operands.values()))

    # ---- source locations ----------------------------------------------

    def _source_id(self, location: pyslang.SourceLocation) -> str | None:
        try:
            path = self._manager.getFullPath(location.buffer)
        except (RuntimeError, ValueError):
            return None
        return self._source_ids.get(os.path.normcase(str(Path(path).resolve())))

    def _span(self, source_range: pyslang.SourceRange) -> SourceSpan | None:
        start, end = source_range.start, source_range.end
        source_id = self._source_id(start)
        if source_id is None:
            return None
        line = self._manager.getLineNumber(start)
        end_line = line
        if end.buffer == start.buffer and end.offset > start.offset:
            last = pyslang.SourceLocation(end.buffer, end.offset - 1)
            end_line = max(line, self._manager.getLineNumber(last))
        if line < 1:
            return None
        return SourceSpan(source_id=source_id, line=line, end_line=end_line)

    def _line_span(self, location: pyslang.SourceLocation) -> SourceSpan | None:
        source_id = self._source_id(location)
        line = self._manager.getLineNumber(location)
        if source_id is None or line < 1:
            return None
        return SourceSpan(source_id=source_id, line=line, end_line=line)

    def _text(self, source_range: pyslang.SourceRange) -> str:
        """Source text of ``source_range`` on one line, or '' if unavailable."""
        start, end = source_range.start, source_range.end
        if self._source_id(start) is None or end.buffer != start.buffer:
            return ""
        key = os.path.normcase(str(self._manager.getFullPath(start.buffer)))
        if key not in self._texts:
            self._texts[key] = self._manager.getSourceText(start.buffer)
        return " ".join(self._texts[key][start.offset : end.offset].split())

    def _where(self, location: pyslang.SourceLocation) -> str:
        source_id = self._source_id(location)
        name = self._source_paths[source_id] if source_id is not None else "<unknown source>"
        return f"{name}:{self._manager.getLineNumber(location)}"

    def _diag(self, code: str, message: str) -> None:
        self._diagnostics.setdefault(Diagnostic(code=code, message=message))


def _intermediate_reads(
    ordered: Sequence[GuardedAssign], group: Sequence[GuardedAssign], written: set[str]
) -> list[str]:
    """Caveats for reads that may observe a value the trace never records.

    Inside one combinational process a blocking assignment takes effect at
    once, so a read placed before the last write to the same signal sees an
    intermediate value. The trace only holds the settled one.
    """
    last_write = {assign.target: position for position, assign in enumerate(ordered)}
    positions = {id(assign): position for position, assign in enumerate(ordered)}
    caveats: dict[str, None] = {}
    for assign in group:
        position = positions[id(assign)]
        for signal in _assign_signals(assign):
            if signal in written and last_write[signal] >= position:
                caveats.setdefault(
                    f"{signal} is read by `{assign.text}` before its last blocking "
                    "assignment in the same process; the value read is an intermediate "
                    "that the trace does not record"
                )
    return list(caveats)


def _assign_signals(assign: GuardedAssign) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for operand in (assign.rhs, *assign.index_operands):
        found.update(dict.fromkeys(ir.signals_of(operand)))
    for guard in assign.guards:
        for operand in ir.guard_operands(guard):
            found.update(dict.fromkeys(ir.signals_of(operand)))
    return tuple(found)


def structural_edges(drivers: Mapping[str, tuple[Driver, ...]]) -> tuple[StructuralEdge, ...]:
    """The flat driver-to-load edge list implied by ``drivers``, in a stable order."""
    edges: dict[StructuralEdge, None] = {}
    for target, found in drivers.items():
        for driver in found:
            for assign in driver.assigns:
                for read in ir.collect_reads(assign.rhs):
                    edges.setdefault(
                        StructuralEdge(
                            read.signal,
                            target,
                            driver.edge_type,
                            read.role,
                            (*assign.guards, *read.guards),
                            assign.span,
                        )
                    )
                for operand in assign.index_operands:
                    for read in ir.collect_reads(operand, EdgeRole.CONTROL):
                        edges.setdefault(
                            StructuralEdge(
                                read.signal,
                                target,
                                driver.edge_type,
                                EdgeRole.CONTROL,
                                (*assign.guards, *read.guards),
                                assign.span,
                            )
                        )
                for depth, guard in enumerate(assign.guards):
                    for operand in ir.guard_operands(guard):
                        for read in ir.collect_reads(operand, EdgeRole.CONTROL):
                            edges.setdefault(
                                StructuralEdge(
                                    read.signal,
                                    target,
                                    driver.edge_type,
                                    EdgeRole.CONTROL,
                                    (*assign.guards[:depth], *read.guards),
                                    guard.span,
                                )
                            )
    return tuple(edges)
