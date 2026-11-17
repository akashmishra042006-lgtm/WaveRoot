"""Structural driver-to-load graph of one elaborated SystemVerilog module.

``build_design_graph`` elaborates SystemVerilog with ``pyslang`` and lowers one
top instance into a ``DesignGraph``:

* every declared signal, with its width, declared range, and source line;
* for every driven signal, its ``Driver`` records: the guarded assignments of
  one continuous assignment or procedural block, in execution order, each with
  the ``if`` / ``case`` decisions that enclose it;
* a flat list of ``StructuralEdge`` records (source signal to dependent signal)
  labeled ``combinational`` or ``sequential``, ``data`` or ``control``, with the
  guards under which the dependence is active and its source span.

The graph is static. It names signals relative to the analyzed instance (for
example ``wptr``) and knows nothing about time; ``backward_slicer`` unrolls it
over real clock edges and prunes it with waveform values.

Scope of this version: one flat module, ``assign``, ``always_comb``,
``always_ff`` / ``always @(posedge clk)``, ``if``, ``case`` / ``casez`` /
``casex``, ``?:``, packed vectors, part-selects, and packed struct fields.
Constructs outside that scope are never dropped: they are recorded as
diagnostics and as drivers or operands marked unsupported, so the slicer can
report where its result is incomplete.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

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
    INSTANCE = "instance"


@dataclass(frozen=True, slots=True)
class SignalInfo:
    """A declared signal. ``name`` is relative to the analyzed top instance."""

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
    """

    target: str
    full_write: bool
    rhs: ir.Expr
    guards: tuple[ir.Guard, ...]
    index_operands: tuple[ir.Expr, ...]
    blocking: bool
    span: SourceSpan | None
    text: str


@dataclass(frozen=True, slots=True)
class Driver:
    """Everything one process contributes to one signal.

    ``assigns`` are in execution order, so the last one whose guards hold wins.
    ``unsupported`` explains why the driver cannot be traversed, if it cannot.
    ``caveats`` describe ways the model is imprecise for this driver.
    ``intermediate`` names signals the assignments read before the last
    blocking write to them in the same process: the value read there is not
    the one the trace records, so it must not be taken from the trace.
    """

    target: str
    edge_type: EdgeType
    process: ProcessKind
    clock: ClockSpec | None
    assigns: tuple[GuardedAssign, ...]
    span: SourceSpan | None
    unsupported: str | None = None
    caveats: tuple[str, ...] = ()
    intermediate: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuralEdge:
    """A static dependence of ``target`` on ``source``, active under ``guards``."""

    source: str
    target: str
    edge_type: EdgeType
    role: EdgeRole
    guards: tuple[ir.Guard, ...]
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class DesignGraph:
    top_module: str
    sources: tuple[SourceFile, ...]
    signals: Mapping[str, SignalInfo]
    drivers: Mapping[str, tuple[Driver, ...]]
    edges: tuple[StructuralEdge, ...]
    diagnostics: tuple[Diagnostic, ...]

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


@dataclass(frozen=True, slots=True)
class _Target:
    """One signal written by an assignment and the slice of the RHS it takes."""

    signal: str
    full_write: bool
    index_operands: tuple[ir.Expr, ...]
    rhs_offset: int
    width: int


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
        self._diagnostics: dict[Diagnostic, None] = {}
        self._texts: dict[str, str] = {}
        self._decisions = 0
        self._lvalue: ir.Expr | None = None
        # Set when the process being lowered contains an intra-assignment delay.
        self._delayed = False

    # ---- top level ------------------------------------------------------

    def build(self, sources: tuple[SourceFile, ...]) -> DesignGraph:
        members = list(self._instance.body)
        directions: dict[str, SignalKind] = {}
        for member in members:
            if isinstance(member, ast.PortSymbol) and member.internalSymbol is not None:
                directions[member.internalSymbol.name] = _DIRECTIONS.get(
                    member.direction, SignalKind.INOUT
                )
        for member in members:
            if isinstance(member, (ast.VariableSymbol, ast.NetSymbol)):
                self._declare(member, directions.get(member.name, SignalKind.INTERNAL))
        for member in members:
            if isinstance(member, ast.ContinuousAssignSymbol):
                self._lower_continuous(member)
            elif isinstance(member, ast.ProceduralBlockSymbol):
                self._lower_process(member)
            elif isinstance(member, ast.NetSymbol):
                self._lower_net_initializer(member)
            elif isinstance(member, ast.InstanceSymbol):
                self._lower_instance(member)
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
        drivers = {name: tuple(found) for name, found in self._drivers.items()}
        return DesignGraph(
            top_module=self._instance.definition.name,
            sources=sources,
            signals=MappingProxyType(dict(self._signals)),
            drivers=MappingProxyType(drivers),
            edges=_structural_edges(drivers),
            diagnostics=tuple(self._diagnostics),
        )

    def _declare(self, symbol: ast.VariableSymbol | ast.NetSymbol, kind: SignalKind) -> None:
        canonical = symbol.type.canonicalType
        span = self._line_span(symbol.location)
        if canonical.isIntegral and canonical.bitWidth >= 1:
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
            info = SignalInfo(symbol.name, width, bit_range, canonical.isSigned, kind, span)
        else:
            self._diag(
                "UNSUPPORTED_SIGNAL_TYPE",
                f"{symbol.name} has type {symbol.type} at {self._where(symbol.location)}; "
                "its value is not reconstructed and reads of it stay unknown.",
            )
            width = max(1, canonical.bitstreamWidth)
            info = SignalInfo(
                symbol.name, width, BitRange(left=width - 1, right=0), False, kind, span, True
            )
        self._signals[symbol.name] = info

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
            intermediate: tuple[str, ...] = ()
            if edge_type is EdgeType.COMBINATIONAL and process is not ProcessKind.CONTINUOUS:
                found = _intermediate_reads(assigns, group, written)
                caveats.extend(found.values())
                intermediate = tuple(found)
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
                    intermediate=intermediate,
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
        info = self._signals.get(symbol.name)
        if initializer is None or info is None:
            return
        span = self._span(initializer.sourceRange) or info.span
        rhs = self._lower(initializer)
        assign = GuardedAssign(
            target=symbol.name,
            full_write=True,
            rhs=rhs,
            guards=(),
            index_operands=(),
            blocking=True,
            span=span,
            text=f"{symbol.name} = {self._text(initializer.sourceRange) or ir.to_sv(rhs)}",
        )
        self._add_drivers(
            [assign], EdgeType.COMBINATIONAL, ProcessKind.CONTINUOUS, None, span, None, None
        )

    def _lower_instance(self, instance: ast.InstanceSymbol) -> None:
        reason = (
            f"driven by instance {instance.name} of module {instance.definition.name}; "
            "module instances are not traversed yet"
        )
        self._diag(
            "UNSUPPORTED_INSTANCE",
            f"Instance {instance.name} of {instance.definition.name} at "
            f"{self._where(instance.location)} is not traversed; signals connected to its "
            "outputs end the slice.",
        )
        span = self._line_span(instance.location)
        for connection in instance.portConnections:
            port = connection.port
            if not isinstance(port, ast.PortSymbol) or port.direction is ast.ArgumentDirection.In:
                continue
            expression = connection.expression
            if expression is None:
                continue
            lvalue = (
                expression.left if isinstance(expression, ast.AssignmentExpression) else expression
            )
            for target in self._targets(lvalue) or ():
                self._drivers.setdefault(target.signal, []).append(
                    Driver(
                        target=target.signal,
                        edge_type=EdgeType.COMBINATIONAL,
                        process=ProcessKind.INSTANCE,
                        clock=None,
                        assigns=(),
                        span=span,
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
        self._delayed = False
        self._lower_statement(statement, (), assigns)
        if unsupported is None and self._delayed:
            unsupported = (
                "intra-assignment timing control such as `q <= #1 d` (delays are outside the "
                "MVP, so the value is not taken at the clock edge)"
            )
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
    ) -> None:
        targets = self._targets(assignment.left)
        if assignment.isCompound:
            self._lvalue = self._lower(assignment.left)
        rhs = self._lower(assignment.right)
        self._lvalue = None
        where = self._where(assignment.sourceRange.start)
        if assignment.timingControl is not None:
            self._delayed = True
            self._diag(
                "UNSUPPORTED_STATEMENT",
                f"intra-assignment timing control at {where} is not modeled; the process that "
                "contains it is not traced.",
            )
        if targets is None:
            self._diag(
                "UNSUPPORTED_LVALUE",
                f"assignment target at {where} is not a signal of the analyzed module; "
                "the assignment is not in the graph.",
            )
            return
        text = self._text(assignment.sourceRange) or f"<assignment> {ir.to_sv(rhs)}"
        whole = len(targets) == 1 and targets[0].width == rhs.width
        for target in targets:
            value = rhs
            if not whole:
                label = f"[{rhs.width - target.rhs_offset - 1}-:{target.width}]"
                value = ir.PartSelect(target.width, False, rhs, target.rhs_offset, label)
            out.append(
                GuardedAssign(
                    target=target.signal,
                    full_write=target.full_write,
                    rhs=value,
                    guards=guards,
                    index_operands=target.index_operands,
                    blocking=not assignment.isNonBlocking,
                    span=span,
                    text=text,
                )
            )

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
                lowered = self._lower_named(node, quiet=True)
                if isinstance(lowered, ir.SignalRef):
                    reads.setdefault(lowered.signal, lowered)

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
                        guards=(*guards, guard),
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
                        )
                    )
                offset += width
            return found
        root = self._lvalue_root(lvalue)
        if root is None:
            return None
        signal, full_write, index_operands = root
        info = self._signals[signal]
        width = info.width if full_write else max(1, lvalue.type.bitstreamWidth)
        return [_Target(signal, full_write, index_operands, 0, width)]

    def _lvalue_root(
        self, lvalue: ast.Expression
    ) -> tuple[str, bool, tuple[ir.Expr, ...]] | None:
        if isinstance(lvalue, ast.NamedValueExpression):
            name = self._local_name(lvalue.symbol)
            return (name, True, ()) if name is not None else None
        if isinstance(lvalue, ast.ElementSelectExpression):
            selectors: tuple[ast.Expression, ...] = (lvalue.selector,)
        elif isinstance(lvalue, ast.RangeSelectExpression):
            selectors = (lvalue.left, lvalue.right)
        elif isinstance(lvalue, ast.MemberAccessExpression):
            selectors = ()
        else:
            return None
        inner = self._lvalue_root(lvalue.value)
        if inner is None:
            return None
        signal, _, index_operands = inner
        dynamic = tuple(
            lowered
            for lowered in (self._lower(selector) for selector in selectors)
            if not isinstance(lowered, ir.Const)
        )
        return signal, False, (*index_operands, *dynamic)

    def _local_name(self, symbol: ast.Symbol) -> str | None:
        """Name of a module-level signal, or None for anything else."""
        path = symbol.hierarchicalPath
        if not path.startswith(self._prefix):
            return None
        name = path[len(self._prefix) :]
        return name if name in self._signals else None

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
        info = _integral(expression.type)
        if info is None:
            return self._unsupported(expression, f"{expression.kind.name} of non-integral type", 1)
        width, signed = info
        if isinstance(expression, (ast.IntegerLiteral, ast.UnbasedUnsizedIntegerLiteral)):
            bits = fs.resize(Vec(_svint_bits(expression.value), signed), width).bits
            return ir.Const(width, signed, bits)
        if isinstance(expression, ast.ConversionExpression):
            operand = self._lower(expression.operand)
            # A cast to a two-state type such as bit turns x and z into 0 (6.24.1).
            two_state = expression.operand.type.isFourState and not expression.type.isFourState
            if operand.width == width and operand.signed == signed and not two_state:
                return operand
            propagated = expression.conversionKind is ast.ConversionKind.Propagated
            if propagated and operand.signed != signed:
                # IEEE 1800 11.8.2: a context-determined operand takes the propagated
                # type first, so it is sign-extended only if that type is signed.
                operand = self._fold(ir.Convert(operand.width, signed, operand))
            return ir.Convert(width, signed, operand, two_state)
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
        value = self._lower(expression.value)
        index = self._lower(expression.selector)
        if not container.isIntegral:
            if container.isUnpackedArray:
                return ir.ArrayElement(width, signed, value, index)
            return self._unsupported(expression, "element select", width, signed)
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
            return self._constant_slice(value, high, low, left, right, elem_width, width, signed)
        mode = (
            ir.SelectMode.UP
            if expression.selectionKind is ast.RangeSelectionKind.IndexedUp
            else ir.SelectMode.DOWN
        )
        return ir.IndexedSelect(width, signed, value, first, left, right, elem_width, mode)

    @staticmethod
    def _constant_slice(
        value: ir.Expr,
        high: int,
        low: int,
        left: int,
        right: int,
        elem_width: int,
        width: int,
        signed: bool,
    ) -> ir.Expr:
        """``value[high:low]``; elements outside ``[left:right]`` read ``x`` (7.4.6).

        A select that is only partly out of range keeps its in-range elements,
        and so its dependence on ``value``.
        """
        step = -1 if high >= low else 1
        total = abs(left - right) + 1
        runs: list[tuple[int | None, int]] = []  # (first position or None for x, elements)
        for index in range(high, low + step, step):
            position = left - index if left >= right else index - left
            inside = 0 <= position < total
            start = position if inside else None
            if runs:
                previous, count = runs[-1]
                if start is None and previous is None:
                    runs[-1] = (None, count + 1)
                    continue
                if start is not None and previous is not None and previous + count == start:
                    runs[-1] = (previous, count + 1)
                    continue
            runs.append((start, 1))
        parts: list[ir.Expr] = []
        for start, count in runs:
            bits = count * elem_width
            if start is None:
                parts.append(ir.Const(bits, False, "x" * bits))
            else:
                parts.append(ir.PartSelect(bits, False, value, start * elem_width, f"[{high}:{low}]"))
        if sum(part.width for part in parts) != width:
            return ir.Const(width, signed, "x" * width)
        if len(parts) == 1:
            only = parts[0]
            if isinstance(only, ir.PartSelect):
                return ir.PartSelect(width, signed, value, only.offset, only.label)
            return ir.Const(width, signed, "x" * width)
        return ir.Concat(width, signed, tuple(parts))

    def _lower_member(
        self, expression: ast.MemberAccessExpression, width: int, signed: bool
    ) -> ir.Expr:
        member = expression.member
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
                lowered = self._lower_named(node, quiet=True)
                if isinstance(lowered, ir.SignalRef):
                    operands.setdefault(lowered.signal, lowered)

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
) -> dict[str, str]:
    """Signals read where they may hold a value the trace never records, with a caveat each.

    Inside one combinational process a blocking assignment takes effect at
    once, so a read placed before the last write to the same signal sees an
    intermediate value. The trace only holds the settled one.

    An assignment reads its operands at its own position. An ``if`` or
    ``case`` reads its condition before any statement it controls, so a guard
    is read at the position of the first assignment that carries it; a write
    inside its own branch therefore comes after the read.
    """
    last_write = {assign.target: position for position, assign in enumerate(ordered)}
    positions = {id(assign): position for position, assign in enumerate(ordered)}
    evaluated: dict[int, int] = {}
    for position, assign in enumerate(ordered):
        for guard in assign.guards:
            evaluated.setdefault(guard.decision, position)
    found: dict[str, str] = {}
    for assign in group:
        reads: list[tuple[str, int]] = [
            (signal, positions[id(assign)])
            for operand in (assign.rhs, *assign.index_operands)
            for signal in ir.signals_of(operand)
        ]
        for guard in assign.guards:
            for operand in ir.guard_operands(guard):
                reads.extend(
                    (signal, evaluated[guard.decision]) for signal in ir.signals_of(operand)
                )
        for signal, position in reads:
            if signal in written and last_write[signal] >= position:
                found.setdefault(
                    signal,
                    f"{signal} is read by `{assign.text}` before its last blocking "
                    "assignment in the same process; the value read is an intermediate "
                    "that the trace does not record",
                )
    return found


def _structural_edges(drivers: Mapping[str, tuple[Driver, ...]]) -> tuple[StructuralEdge, ...]:
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
