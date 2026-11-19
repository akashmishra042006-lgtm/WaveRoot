"""Value-pruned backward temporal slice from a failure to its candidate causes.

``slice_failure`` starts at the failing observation
``(FailureAnchor.signal_path, FailureAnchor.observation)`` and walks the
``DesignGraph`` backward, breadth first, building the time-unrolled graph that
``spec.md`` section 6.2 defines. A graph node is one signal at one observation
(epoch, cycle, phase, clock-edge time).

Time. The slicer never computes a time from a clock period. It reads the real
rising edges of the failure's clock from the provider and maps cycle numbers to
those edge times.

* A combinational driver is followed inside the same observation.
* A register that is ``settled`` at edge k is followed across that edge: its
  operands are sampled ``before`` edge k (one ``sequential`` edge).
* A register that is read ``before`` edge k carries back to the earlier edge
  that last wrote it, where it is ``settled`` (one ``combinational`` carry
  edge). Cycles in which every write guard was false are skipped.

Pruning. Each driver is a list of guarded assignments. Every ``if``, ``case``,
and ``?:`` decision is evaluated with four-state values from
``WaveformProvider.get_value_at``. A decision that is false prunes its branch;
the dependence is still reported, marked ``pruned``, but not followed. A
decision that is ``x``, or that reads a signal the trace does not hold, prunes
nothing: its branches are followed and marked ``unknown``.

Ranking. Suspects are an ordinal list of source locations, never a
probability. See ``_rank`` for the ordering.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from src.causal import expr as ir
from src.causal import fourstate as fs
from src.causal.ast_graph import (
    DesignGraph,
    Driver,
    GuardedAssign,
    ProcessKind,
    SignalKind,
    build_design_graph,
)
from src.causal.fourstate import Tri, Vec
from src.causal.sva_synth import synthesize_assertion
from src.contracts import (
    AnalysisStatus,
    BranchStatus,
    CandidateAssertion,
    CausalEdge,
    CausalNode,
    CausalReport,
    ComparisonResult,
    ContractError,
    DataUnavailableError,
    Diagnostic,
    EdgeRole,
    EdgeType,
    FailureAnchor,
    LogicValue,
    Observation,
    OutOfRangeError,
    Provenance,
    ProvenanceKind,
    SamplingPhase,
    SignalNotFoundError,
    SourceSpan,
    SuspectLocation,
    WaveformProvider,
    WaveformWindow,
)

_PROVENANCE_NOTE: Final = (
    "Backward dependency slice computed by src.causal from the RTL and the trace. "
    "Suspects are ranked candidates for investigation, not proof of a root cause."
)
# Design diagnostics that mean a driver may be missing from the graph, or that the
# graph may not describe the current sources. They are repeated in every report
# because they weaken any slice of that design.
_GLOBAL_CAVEATS: Final = frozenset(
    {"UNSUPPORTED_SCOPE", "UNSUPPORTED_LVALUE", "SOURCE_CHANGED", "SOURCE_MISSING"}
)


@dataclass(frozen=True, slots=True)
class SliceOptions:
    """Configuration of one slice.

    ``trace_scope`` is the trace path of the analyzed top instance, for example
    ``tb.dut``; a design signal ``wptr`` is then read as ``tb.dut.wptr``. It is
    supplied by the caller and never guessed.

    ``max_cycles_back`` bounds how many clock cycles before the failure the
    slice may reach (None means back to cycle 0 of the failure's epoch).
    ``expand_holds`` makes a held register step back one cycle at a time, which
    exposes the hold condition of every cycle instead of one carry edge.
    ``reset_path`` and ``reset_active`` name the reset as a trace path and its
    active level (``"0"`` or ``"1"``). They qualify the generated assertion and
    let a register that the reset branch never assigns be located at that
    branch. Nothing is inferred from names, so without them such a register
    stays an epoch boundary at its declaration.
    """

    trace_scope: str
    max_cycles_back: int | None = None
    max_nodes: int = 5000
    max_suspects: int | None = 10
    expand_holds: bool = False
    include_pruned: bool = True
    synthesize_assertion: bool = True
    include_waveform: bool = True
    reset_path: str | None = None
    reset_active: str | None = None
    case_id: str | None = None


def analyze_failure(
    failure: FailureAnchor,
    provider: WaveformProvider,
    sources: Sequence[Path | str],
    *,
    root: Path | str,
    options: SliceOptions,
    top: str | None = None,
) -> CausalReport:
    """Track C's entry point: elaborate ``sources`` and slice back from ``failure``.

    ``sources`` are the RTL files the analyzer may see (a benchmark's
    ``analysis_inputs.rtl``), not the testbench. Each ``SourceFile.path`` in the
    report is relative to ``root``, and every source must lie inside it.
    ``top`` names the module to analyze when the sources hold more than one
    top-level module. ``provider`` serves the same trace the failure came from,
    and ``options.trace_scope`` is the trace path of that module's instance.

    The report validates against ``src.contracts.CausalReport`` and holds only
    analysis (``provenance.kind`` is ``analysis``). Raises ``DesignError`` when a
    source is missing or outside ``root``, elaboration fails, or ``top`` is not
    resolved.
    """
    design = build_design_graph(sources, root=root, top=top)
    return slice_failure(failure, provider, design, options)


def slice_failure(
    failure: FailureAnchor,
    provider: WaveformProvider,
    design: DesignGraph,
    options: SliceOptions,
) -> CausalReport:
    """Slice backward from ``failure`` and return a ``fail`` ``CausalReport``.

    The report always validates against the contract. When the failure cannot
    be localized (the signal is not in the design, the trace does not reproduce
    it, the failure kind has no observed value) the graph is empty and the
    diagnostics say why.
    """
    return _Slicer(failure, provider, design, options).run()


def analyze_comparison(
    comparison: ComparisonResult,
    provider: WaveformProvider,
    design: DesignGraph,
    options: SliceOptions,
) -> CausalReport:
    """Produce the ``CausalReport`` that corresponds to a ``ComparisonResult``."""
    if comparison.status is AnalysisStatus.FAIL and comparison.failure is not None:
        return slice_failure(comparison.failure, provider, design, options)
    provenance = Provenance(
        kind=ProvenanceKind.ANALYSIS, case_id=options.case_id, note=_PROVENANCE_NOTE
    )
    if comparison.status is AnalysisStatus.PASS:
        return CausalReport(
            schema_version=1, provenance=provenance, status=AnalysisStatus.PASS, failure=None
        )
    note = Diagnostic(
        code="NO_FAILURE_TO_SLICE",
        message=f"The comparison ended as {comparison.status.value}; there is no failing "
        "observation to slice from.",
    )
    return CausalReport(
        schema_version=1,
        provenance=provenance,
        status=comparison.status,
        failure=None,
        diagnostics=(*comparison.diagnostics, note),
    )


# --------------------------------------------------------------------------
# Internal state
# --------------------------------------------------------------------------

_Key = tuple[str, int, SamplingPhase]
"""A graph node: signal (relative to the top instance), cycle, phase."""


class _Kind(StrEnum):
    PENDING = "pending"
    STATE_UPDATE = "state_update"
    STATE_HOLD = "state_hold"
    COMBINATIONAL = "combinational"
    CARRY = "carry"
    INPUT = "input"
    EPOCH_BOUNDARY = "epoch_boundary"
    UNRESET = "unreset"
    UNDRIVEN = "undriven"
    TRUNCATED = "truncated"


# Ordering of suspects. Lower sorts first. Statements that compute a value come
# first, then statements that only forward one, then the places where the slice
# merely ends.
_STATEMENT_TIER: Final = 0
_FORWARD_TIER: Final = 1
_BOUNDARY_TIER: Final = 2
_BOUNDARY_RANK: Final = {
    _Kind.TRUNCATED: 0,
    _Kind.UNDRIVEN: 1,
    _Kind.EPOCH_BOUNDARY: 2,
    _Kind.INPUT: 3,
}


@dataclass(slots=True)
class _Node:
    key: _Key
    node_id: str
    value: LogicValue | None
    location: SourceSpan | None
    kind: _Kind = _Kind.PENDING
    note: str = ""
    changed: bool | None = None
    forward: bool = False


def _is_forward(expr: ir.Expr) -> bool:
    """True when ``expr`` only passes one source on unchanged.

    That is a signal, a select of it (constant or run-time), or one element of
    an unpacked array. A conversion that changes width, or turns x/z into 0,
    computes a new value and is not a forward; one that only changes
    signedness is. A ``?:`` chooses between sources and is not a forward.
    """
    while isinstance(expr, (ir.Convert, ir.PartSelect, ir.IndexedSelect)):
        if isinstance(expr, ir.Convert):
            if expr.two_state or expr.width != expr.operand.width:
                return False
            expr = expr.operand
        else:
            expr = expr.value
    return isinstance(expr, (ir.SignalRef, ir.ArrayRead))


@dataclass(slots=True)
class _Edge:
    source: _Key
    target: _Key
    edge_type: EdgeType
    role: EdgeRole
    branch: BranchStatus
    reasons: dict[str, None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Verdict:
    """Whether one guarded assignment took effect at an observation."""

    status: BranchStatus
    why: str
    blocked: bool


@dataclass(slots=True)
class _Decision:
    """One ``if`` or ``case`` construct as seen from one driver."""

    guard: ir.Guard
    prefix: list[Tri]
    prefix_guards: tuple[ir.Guard, ...]
    operands: dict[ir.Expr, None] = field(default_factory=dict)
    all_blocked: bool = True


def _covers(paths: list[tuple[ir.Guard, ...]], outcome: Callable[[ir.Guard], Tri]) -> bool:
    """True when every execution of a process reaches one of ``paths``.

    Each path is the guard chain of a full write. A guard chain is reached
    unconditionally when it is empty. Otherwise one ``if`` or ``case`` decision
    covers when the arm it is known to take is covered, or, when that arm is
    not known, the decision always takes some arm (an ``if`` with an ``else``,
    a ``case`` with a ``default``) and every arm it may take is covered.
    Anything else, including a ``case`` whose items happen to be exhaustive,
    is not proven, so the answer errs towards "may keep a stored value".
    """
    if any(not path for path in paths):
        return True
    decisions: dict[int, dict[ir.Guard, list[tuple[ir.Guard, ...]]]] = {}
    for path in paths:
        decisions.setdefault(path[0].decision, {}).setdefault(path[0], []).append(path[1:])
    for arms in decisions.values():
        taken = [guard for guard in arms if outcome(guard) is Tri.TRUE]
        if taken:
            if _covers(arms[taken[0]], outcome):
                return True
            continue
        kinds = {(guard.kind, guard.polarity) for guard in arms}
        complete = {(ir.GuardKind.IF, True), (ir.GuardKind.IF, False)} <= kinds or any(
            kind is ir.GuardKind.CASE_DEFAULT for kind, _ in kinds
        )
        if complete and all(
            _covers(tails, outcome)
            for guard, tails in arms.items()
            if outcome(guard) is not Tri.FALSE
        ):
            return True
    return False


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _read_mask(read: ir.Read) -> int | None:
    """Bits of the read signal a read uses, as a position mask; None for every bit."""
    if read.bits is None:
        return None
    offset, width = read.bits
    return ((1 << width) - 1) << offset


class _Slicer:
    def __init__(
        self,
        failure: FailureAnchor,
        provider: WaveformProvider,
        design: DesignGraph,
        options: SliceOptions,
    ) -> None:
        self._failure = failure
        self._provider = provider
        self._design = design
        self._options = options
        self._prefix = f"{options.trace_scope}."
        self._epoch = failure.observation.epoch
        self._anchor_cycle = failure.observation.cycle
        self._times: dict[int, int] = {self._anchor_cycle: failure.observation.time_ps}
        self._window_start = failure.observation.time_ps
        self._values: dict[_Key, LogicValue | None] = {}
        self._nodes: dict[_Key, _Node] = {}
        self._node_ids: set[str] = set()
        self._edges: dict[tuple[_Key, _Key, EdgeType, EdgeRole, BranchStatus], _Edge] = {}
        self._queue: deque[_Key] = deque()
        self._reached: dict[_Key, None] = {}
        # Bits of each reached node the slice must explain, as a mask over positions
        # counted from the signal's left bit. A node is expanded again when it grows.
        self._demand: dict[_Key, int] = {}
        self._queued: set[_Key] = set()
        self._expanded: set[_Key] = set()
        self._diagnostics: dict[Diagnostic, None] = {}
        self._truncated = False
        # (signal, demanded bits, cycle) -> the cycle a hold scan starting there ends at.
        self._hold_origins: dict[tuple[str, int, int], int] = {}

    # ---- entry ----------------------------------------------------------

    def run(self) -> CausalReport:
        failure = self._failure
        observation = failure.observation
        signal = self._local(failure.signal_path)
        if signal is None:
            return self._unlocalized(
                "ANCHOR_NOT_IN_DESIGN",
                f"{failure.signal_path} is not a signal of {self._design.top_module} under "
                f"trace scope {self._options.trace_scope}; no slice was computed.",
            )
        if failure.actual is None:
            return self._unlocalized(
                "UNSUPPORTED_FAILURE_KIND",
                f"A {failure.kind.value} failure has no observed value to slice from; "
                "no slice was computed.",
            )
        observed = self._sample(signal, observation.cycle, observation.phase)
        if observed != failure.actual:
            shown = "no value" if observed is None else ir.literal(observed.bits)
            return self._unlocalized(
                "ANCHOR_NOT_REPRODUCIBLE",
                f"The trace gives {shown} for {failure.signal_path} at {observation.time_ps} ps "
                f"({observation.phase.value}), not the failure's actual value "
                f"{ir.literal(failure.actual.bits)}; no slice was computed.",
            )
        for diagnostic in self._design.diagnostics:
            if diagnostic.code in _GLOBAL_CAVEATS:
                self._diagnostics.setdefault(diagnostic)
        self._load_cycle_times()

        anchor: _Key = (signal, observation.cycle, observation.phase)
        self._node(anchor)
        self._reach(anchor)
        while self._queue:
            key = self._queue.popleft()
            self._queued.discard(key)
            if len(self._nodes) >= self._options.max_nodes:
                self._node_limit(self._nodes[key])
                continue
            if key in self._expanded:
                self._reset(key)
            self._expanded.add(key)
            self._expand(key)
        return self._report(anchor)

    def _node_limit(self, node: _Node) -> None:
        self._stop(
            node,
            "SLICE_NODE_LIMIT",
            f"The slice reached the limit of {self._options.max_nodes} nodes; nodes beyond it "
            "were not added or expanded.",
        )

    def _reset(self, key: _Key) -> None:
        """Forget an expansion, so the node can be expanded again for more bits.

        Demand only grows, so a later expansion keeps or strengthens every edge
        of the earlier one; nothing it reached becomes unreachable.
        """
        for identity in [identity for identity, edge in self._edges.items() if edge.target == key]:
            del self._edges[identity]
        node = self._nodes[key]
        info = self._design.signals.get(key[0])
        node.kind = _Kind.PENDING
        node.note = ""
        node.changed = None
        node.location = info.span if info is not None else None

    def _unlocalized(self, code: str, message: str) -> CausalReport:
        self._diag(code, message)
        return CausalReport(
            schema_version=1,
            provenance=self._provenance(),
            status=AnalysisStatus.FAIL,
            failure=self._failure,
            diagnostics=tuple(self._diagnostics),
        )

    def _provenance(self) -> Provenance:
        return Provenance(
            kind=ProvenanceKind.ANALYSIS, case_id=self._options.case_id, note=_PROVENANCE_NOTE
        )

    # ---- time and values -------------------------------------------------

    def _load_cycle_times(self) -> None:
        """Map every cycle of the failure's epoch up to the failure to its edge time."""
        failure = self._failure
        anchor_time = failure.observation.time_ps
        try:
            start = self._provider.get_coverage().start_ps
            edges = self._provider.get_clock_edges(failure.clock_path, start, anchor_time + 1)
        except ContractError as error:
            self._limit(
                "CLOCK_EDGES_UNAVAILABLE",
                f"Clock edges of {failure.clock_path} before {anchor_time} ps could not be "
                f"read ({error}); the slice cannot leave the failing cycle.",
            )
            return
        needed = self._anchor_cycle + 1
        if len(edges) < needed or edges[-1] != anchor_time:
            self._limit(
                "CLOCK_EDGES_INCONSISTENT",
                f"{failure.clock_path} has {len(edges)} rising edges up to {anchor_time} ps, "
                f"which does not match cycle {self._anchor_cycle} at that time; the slice "
                "cannot leave the failing cycle.",
            )
            return
        for offset in range(needed):
            self._times[self._anchor_cycle - offset] = edges[-1 - offset]
        self._window_start = edges[-needed - 1] if len(edges) > needed else start

    def _trace_path(self, signal: str) -> str:
        return f"{self._prefix}{signal}"

    def _local(self, path: str) -> str | None:
        if not path.startswith(self._prefix):
            return None
        name = path[len(self._prefix) :]
        return name if name in self._design.signals else None

    def _sample(self, signal: str, cycle: int, phase: SamplingPhase) -> LogicValue | None:
        """The trace value of ``signal`` at an observation, or None if unobservable."""
        key: _Key = (signal, cycle, phase)
        if key in self._values:
            return self._values[key]
        value: LogicValue | None = None
        time_ps = self._times.get(cycle)
        info = self._design.signals.get(signal)
        if time_ps is not None and info is not None:
            path = self._trace_path(signal)
            try:
                value = self._provider.get_value_at(path, time_ps, phase=phase)
            except SignalNotFoundError:
                self._diag(
                    "SIGNAL_NOT_OBSERVED",
                    f"{path} is not present in the trace; its value is unknown and every "
                    "branch that depends on it is kept.",
                )
            except (OutOfRangeError, DataUnavailableError) as error:
                self._diag(
                    "DATA_UNAVAILABLE",
                    f"{path} could not be read at {time_ps} ps ({phase.value}): {error}",
                )
            except ContractError as error:
                self._diag("QUERY_FAILED", f"Reading {path} at {time_ps} ps failed: {error}")
            else:
                if value.width != info.width and not info.is_aggregate:
                    self._diag(
                        "WIDTH_MISMATCH",
                        f"{path} is {value.width} bits in the trace and {info.width} bits in "
                        "the RTL; its value is treated as unknown.",
                    )
                    value = None
        self._values[key] = value
        return value

    def _lookup(self, cycle: int, phase: SamplingPhase) -> ir.Lookup:
        def lookup(signal: str) -> Vec | None:
            value = self._sample(signal, cycle, phase)
            return None if value is None else Vec(value.bits)

        return lookup

    def _shown(self, signal: str, cycle: int, phase: SamplingPhase) -> str:
        value = self._sample(signal, cycle, phase)
        return "unobserved" if value is None else ir.literal(value.bits)

    @staticmethod
    def _when(cycle: int, phase: SamplingPhase) -> str:
        if phase is SamplingPhase.BEFORE:
            return f"just before the cycle {cycle} clock edge"
        return f"after the cycle {cycle} clock edge settles"

    # ---- graph construction ---------------------------------------------

    def _node(self, key: _Key) -> _Node:
        node = self._nodes.get(key)
        if node is None:
            signal, cycle, phase = key
            stem = re.sub(r"[^A-Za-z0-9_.:\-]", "_", signal)
            node_id = f"{stem}.e{self._epoch}c{cycle}.{phase.value}"
            suffix = 2
            while node_id in self._node_ids:
                node_id = f"{stem}.e{self._epoch}c{cycle}.{phase.value}-{suffix}"
                suffix += 1
            self._node_ids.add(node_id)
            info = self._design.signals.get(signal)
            node = _Node(
                key,
                node_id,
                self._sample(signal, cycle, phase),
                info.span if info is not None else None,
            )
            self._nodes[key] = node
        return node

    def _full(self, signal: str) -> int:
        return (1 << self._design.signals[signal].width) - 1

    def _reach(self, key: _Key, demand: int | None = None) -> None:
        """Queue ``key`` for expansion of the ``demand`` bits (None: every bit)."""
        full = self._full(key[0])
        wanted = full if demand is None else (demand & full) or full
        known = self._demand.get(key)
        if known is None:
            self._demand[key] = wanted
            self._reached[key] = None
        elif wanted & ~known:
            self._demand[key] = known | wanted
        else:
            return
        if key not in self._queued:
            self._queued.add(key)
            self._queue.append(key)

    def _bits_text(self, signal: str, mask: int) -> str:
        """Declared bit indices in ``mask``, for example ``bits [7:4], [0]``."""
        info = self._design.signals[signal]
        if mask == self._full(signal):
            return "all bits"
        left = info.bit_range.left if info.bit_range is not None else 0
        right = info.bit_range.right if info.bit_range is not None else 0

        def index(position: int) -> int:
            return left - position if left >= right else left + position

        runs: list[str] = []
        position = 0
        while position < info.width:
            if mask >> position & 1:
                start = position
                while position + 1 < info.width and mask >> (position + 1) & 1:
                    position += 1
                high, low = index(start), index(position)
                runs.append(f"[{high}]" if start == position else f"[{high}:{low}]")
            position += 1
        return ("bit " if len(runs) == 1 and ":" not in runs[0] else "bits ") + ", ".join(runs)

    def _add_edge(
        self,
        source: _Key,
        target: _Key,
        edge_type: EdgeType,
        role: EdgeRole,
        branch: BranchStatus,
        reason: str,
        demand: int | None = None,
    ) -> None:
        """Add an edge; a non-pruned one reaches ``demand`` bits of ``source``."""
        if source == target:
            self._diag(
                "COMBINATIONAL_SELF_REFERENCE",
                f"{self._trace_path(target[0])} reads itself inside one observation; that "
                "dependence is not represented as an edge.",
            )
            return
        if branch is BranchStatus.PRUNED and not self._options.include_pruned:
            return
        if source not in self._nodes and len(self._nodes) >= self._options.max_nodes:
            # The budget holds during expansion, not only between nodes.
            self._node_limit(self._nodes[target])
            return
        self._node(source)
        identity = (source, target, edge_type, role, branch)
        edge = self._edges.get(identity)
        if edge is None:
            edge = self._edges[identity] = _Edge(source, target, edge_type, role, branch)
        edge.reasons.setdefault(reason)
        if branch is not BranchStatus.PRUNED:
            self._reach(source, demand)

    def _diag(self, code: str, message: str) -> None:
        self._diagnostics.setdefault(Diagnostic(code=code, message=message))

    def _limit(self, code: str, message: str) -> None:
        self._truncated = True
        self._diag(code, message)

    def _stop(self, node: _Node, code: str, message: str) -> None:
        """End the slice at ``node`` for a depth, time, or capability reason."""
        self._limit(code, message)
        node.kind = _Kind.TRUNCATED
        node.note = (
            f"The slice stops at {self._trace_path(node.key[0])} and does not explain its "
            f"value ({code})"
        )

    # ---- expansion ------------------------------------------------------

    def _expand(self, key: _Key) -> None:
        signal, cycle, phase = key
        node = self._nodes[key]
        info = self._design.signals[signal]
        path = self._trace_path(signal)
        when = self._when(cycle, phase)
        drivers = self._design.drivers.get(signal, ())
        if not drivers:
            shown = self._shown(signal, cycle, phase)
            if info.kind in (SignalKind.INPUT, SignalKind.INOUT):
                node.kind = _Kind.INPUT
                node.note = (
                    f"{path} is a module input, driven from outside the analyzed RTL; it is "
                    f"{shown} {when}"
                )
            else:
                self._limit(
                    "NO_DRIVER",
                    f"{path} has no driver in the analyzed RTL; the slice ends there.",
                )
                node.kind = _Kind.UNDRIVEN
                node.note = f"{path} has no driver in the analyzed RTL; it is {shown} {when}"
            return
        if len(drivers) > 1:
            self._diag(
                "MULTIPLE_DRIVERS",
                f"{path} is driven by {len(drivers)} processes; each one is followed.",
            )
        sequential = [d for d in drivers if d.edge_type is EdgeType.SEQUENTIAL]
        if sequential and phase is SamplingPhase.BEFORE:
            self._carry(key, node, drivers)
            return
        if self._held_from_epoch(key, drivers):
            shown = self._shown(signal, cycle, phase)
            self._epoch_origin(
                node,
                drivers,
                f"is {shown} after the cycle 0 clock edge, which held the value it had before "
                f"cycle 0 of epoch {self._epoch}",
            )
            return
        for driver in drivers:
            if driver.unsupported is not None:
                self._stop(
                    node,
                    "UNSUPPORTED_DRIVER",
                    f"{path} is not traced further: {driver.unsupported}.",
                )
                continue
            for caveat in driver.caveats:
                self._diag("IMPRECISE_DRIVER", f"{path}: {caveat}.")
            if driver.intermediate:
                names = ", ".join(self._trace_path(signal) for signal in driver.intermediate)
                self._limit(
                    "INTERMEDIATE_VALUE",
                    f"{path} reads {names} at an intermediate point of its process, where the "
                    "trace does not record the value; those reads are treated as unknown, no "
                    "branch is pruned on them, and the slice does not follow them.",
                )
            if driver.edge_type is EdgeType.COMBINATIONAL:
                self._expand_driver(key, node, driver, cycle, phase)
            elif self._clock_matches(driver):
                self._expand_driver(key, node, driver, cycle, SamplingPhase.BEFORE)
            else:
                self._stop(
                    node,
                    "UNSUPPORTED_CLOCK",
                    f"{path} is not clocked by the rising edge of {self._failure.clock_path}; "
                    "other clocks and edges are not traced.",
                )

    def _clock_matches(self, driver: Driver) -> bool:
        """True when ``driver`` is clocked by the rising edge of the failure's clock.

        A clock port of a child instance matches when it is connected to the
        failure's clock by plain whole-signal port connections. A clock that
        passes through logic is a different signal and does not match.
        """
        clock = driver.clock
        if clock is None or not clock.rising:
            return False
        if self._trace_path(clock.signal) == self._failure.clock_path:
            return True
        failure_clock = self._local(self._failure.clock_path)
        if failure_clock is None:
            return False
        return self._design.canonical(clock.signal) == self._design.canonical(failure_clock)

    def _written(self, assign: GuardedAssign, signal: str) -> tuple[int, bool]:
        """Bits ``assign`` may write, and whether exactly those bits are written."""
        if assign.full_write:
            return self._full(signal), True
        if assign.written is None:
            return self._full(signal), False
        offset, width = assign.written
        return ((1 << width) - 1) << offset, True

    def _verdicts(
        self, driver: Driver, outcomes: list[list[Tri]], when: str, signal: str, demand: int
    ) -> tuple[list[_Verdict], int]:
        """Decide which assignments took effect for the ``demand`` bits of ``signal``.

        The last executed write of a bit wins. An assignment is overwritten
        only when later definite writes cover every demanded bit it writes,
        and one that writes no demanded bit is not followed. Returns the
        verdicts in execution order and the demanded bits that no definite
        write covers, which hold their previous value.
        """
        path = self._trace_path(signal)
        verdicts: list[_Verdict] = []
        covered = 0
        writers: list[tuple[GuardedAssign, int]] = []
        for assign, guards in zip(reversed(driver.assigns), reversed(outcomes)):
            written, exact = self._written(assign, signal)
            relevant = written & demand
            total = fs.tri_and(*guards)
            overwritten = relevant & ~covered == 0
            if not relevant:
                why = (
                    f"`{assign.text}` writes only {self._bits_text(signal, written)} of {path}; "
                    f"the slice follows {self._bits_text(signal, demand)}."
                )
                verdicts.append(_Verdict(BranchStatus.PRUNED, why, True))
            elif total is Tri.FALSE:
                guard = assign.guards[guards.index(Tri.FALSE)]
                why = (
                    f"`{assign.text}` does not execute: {ir.describe_guard(guard)} is false "
                    f"{when}."
                )
                verdicts.append(_Verdict(BranchStatus.PRUNED, why, overwritten))
            elif overwritten:
                later = [f"`{a.text}`" for a, bits in reversed(writers) if bits & relevant]
                noun = "assignment" if len(later) == 1 else "assignments"
                why = f"`{assign.text}` is overwritten by the later {noun} {', '.join(later)}."
                verdicts.append(_Verdict(BranchStatus.PRUNED, why, True))
            elif total is Tri.TRUE:
                verdicts.append(
                    _Verdict(BranchStatus.RETAINED, f"`{assign.text}` executes {when}.", False)
                )
                if exact:
                    writers.append((assign, relevant))
                    covered |= relevant
            else:
                guard = assign.guards[guards.index(Tri.UNKNOWN)]
                why = (
                    f"`{assign.text}` may execute: {ir.describe_guard(guard)} is unknown "
                    f"{when}."
                )
                verdicts.append(_Verdict(BranchStatus.UNKNOWN, why, False))
        verdicts.reverse()
        return verdicts, demand & ~covered

    def _expand_driver(
        self, key: _Key, node: _Node, driver: Driver, cycle: int, phase: SamplingPhase
    ) -> None:
        """Add the edges of one driver, with operands sampled at ``(cycle, phase)``."""
        signal = key[0]
        path = self._trace_path(signal)
        edge_type = driver.edge_type
        observed = self._lookup(cycle, phase)
        hidden = frozenset(driver.intermediate)

        def lookup(name: str) -> Vec | None:
            # An intermediate value read inside the process is not the traced one.
            return None if name in hidden else observed(name)

        when = self._when(cycle, phase)
        outcomes = [
            [ir.evaluate_guard(guard, lookup) for guard in assign.guards]
            for assign in driver.assigns
        ]
        demand = self._demand.get(key, self._full(signal))
        verdicts, unwritten = self._verdicts(driver, outcomes, when, signal, demand)

        for assign, verdict in zip(driver.assigns, verdicts):
            if verdict.status is not BranchStatus.PRUNED and not ir.is_supported(assign.rhs):
                self._diag(
                    "UNSUPPORTED_OPERAND",
                    f"`{assign.text}` contains a construct whose value is not computed; its "
                    "operands are kept as dependencies without evaluation.",
                )
            for read in ir.collect_reads(assign.rhs):
                if read.signal in hidden:
                    continue
                status, reason = verdict.status, verdict.why
                if status is not BranchStatus.PRUNED:
                    for guard in read.guards:
                        outcome = ir.evaluate_guard(guard, lookup)
                        if outcome is Tri.FALSE:
                            status = BranchStatus.PRUNED
                            reason = self._unselected(guard, assign, read.signal, when)
                            break
                        if outcome is Tri.UNKNOWN:
                            status = BranchStatus.UNKNOWN
                            reason = self._unresolved(guard, assign, when)
                self._add_edge(
                    (read.signal, cycle, phase),
                    key,
                    edge_type,
                    read.role,
                    status,
                    reason,
                    _read_mask(read),
                )
            for operand in assign.index_operands:
                for read in ir.collect_reads(operand, EdgeRole.CONTROL):
                    if read.signal in hidden:
                        continue
                    self._add_edge(
                        (read.signal, cycle, phase),
                        key,
                        edge_type,
                        EdgeRole.CONTROL,
                        verdict.status,
                        f"Selects the written element: {verdict.why}",
                        _read_mask(read),
                    )

        decisions: dict[int, _Decision] = {}
        for assign, guards, verdict in zip(driver.assigns, outcomes, verdicts):
            for depth, guard in enumerate(assign.guards):
                decision = decisions.get(guard.decision)
                if decision is None:
                    decision = decisions[guard.decision] = _Decision(
                        guard, guards[:depth], assign.guards[:depth]
                    )
                for operand in ir.guard_operands(guard):
                    decision.operands.setdefault(operand)
                if not verdict.blocked:
                    decision.all_blocked = False
        for decision in decisions.values():
            status, reason = self._judge_decision(decision, signal, lookup, when)
            for operand in decision.operands:
                for read in ir.collect_reads(operand, EdgeRole.CONTROL):
                    if read.signal in hidden:
                        continue
                    self._add_edge(
                        (read.signal, cycle, phase),
                        key,
                        edge_type,
                        EdgeRole.CONTROL,
                        status,
                        reason,
                        _read_mask(read),
                    )

        live = [
            (assign, verdict)
            for assign, verdict in zip(driver.assigns, verdicts)
            if verdict.status is not BranchStatus.PRUNED
        ]
        if edge_type is EdgeType.SEQUENTIAL:
            if unwritten:
                uncertain = any(v.status is BranchStatus.UNKNOWN for _, v in live)
                if not live:
                    reason = (
                        f"{path} holds its value: no assignment to it executes at the cycle "
                        f"{cycle} clock edge."
                    )
                elif uncertain:
                    reason = (
                        f"{path} may hold its value: an assignment to it has an unknown "
                        f"condition at the cycle {cycle} clock edge."
                    )
                elif unwritten == self._full(signal):
                    reason = (
                        f"Bits of {path} that are not written at the cycle {cycle} clock edge "
                        "hold their value."
                    )
                else:
                    shown = self._bits_text(signal, unwritten)
                    verb = "is" if shown.startswith("bit ") else "are"
                    reason = (
                        f"{shown.capitalize()} of {path} {verb} not written at the cycle {cycle} "
                        "clock edge and hold their value."
                    )
                self._add_edge(
                    (signal, cycle, SamplingPhase.BEFORE),
                    key,
                    EdgeType.SEQUENTIAL,
                    EdgeRole.DATA,
                    BranchStatus.UNKNOWN if uncertain else BranchStatus.RETAINED,
                    reason,
                    unwritten,
                )
        elif not live and any(self._written(assign, signal)[0] & demand for assign in driver.assigns):
            # The value was kept from an earlier evaluation (an inferred latch) or left
            # undriven; neither is traced, so the slice ends here and says so (spec 12).
            # A process whose writes all miss the demanded bits explains nothing here,
            # and is not a latch of those bits.
            self._stop(
                node,
                "NO_ACTIVE_ASSIGNMENT",
                f"No assignment to {path} executes {when}; the process leaves it undriven or "
                "infers storage, which is outside the MVP, so its value is not traced.",
            )
        elif unwritten and driver.process is not ProcessKind.CONTINUOUS:
            exposed = self._exposed(driver, live, signal, unwritten, lookup)
            if exposed:
                # Those bits may keep a value from an earlier evaluation: a partial latch.
                self._stop(
                    node,
                    "PARTIAL_ASSIGNMENT",
                    f"No assignment is certain to write {self._bits_text(signal, exposed)} of "
                    f"{path} {when}, so they may keep a value from an earlier evaluation "
                    "(inferred storage, which is outside the MVP); its value is not traced.",
                )
        self._classify(node, driver, live, outcomes)

    def _exposed(
        self,
        driver: Driver,
        live: list[tuple[GuardedAssign, _Verdict]],
        signal: str,
        bits: int,
        lookup: ir.Lookup,
    ) -> int:
        """Bits of ``bits`` this process writes on some path but not on every path.

        Bits the process never writes belong to other drivers. A bit is covered
        when the exact writes of it form a complete cover (see ``_covers``);
        a write whose bits depend on a run-time index covers nothing.
        """
        possible = 0
        for assign in driver.assigns:
            possible |= self._written(assign, signal)[0]
        exact = [
            (assign.guards, written)
            for assign, _ in live
            for written, known in [self._written(assign, signal)]
            if known
        ]
        exposed = 0
        verdict: dict[tuple[int, ...], bool] = {}
        remaining, bit = bits & possible, 0
        while remaining:
            if remaining & 1:
                writers = tuple(index for index, (_, mask) in enumerate(exact) if mask >> bit & 1)
                if writers not in verdict:
                    verdict[writers] = _covers(
                        [exact[index][0] for index in writers],
                        lambda guard: ir.evaluate_guard(guard, lookup),
                    )
                if not verdict[writers]:
                    exposed |= 1 << bit
            remaining >>= 1
            bit += 1
        return exposed

    def _unselected(
        self, guard: ir.Guard, assign: GuardedAssign, signal: str, when: str
    ) -> str:
        """Why a read inside ``assign`` is not selected."""
        if guard.kind is ir.GuardKind.INDEX:
            return (
                f"`{assign.text}` does not read {self._trace_path(signal)}: the index "
                f"`{guard.text}` is not {guard.element} {when}."
            )
        return (
            f"`?:` arm of `{assign.text}` is not selected: {ir.describe_guard(guard)} is false "
            f"{when}."
        )

    @staticmethod
    def _unresolved(guard: ir.Guard, assign: GuardedAssign, when: str) -> str:
        """Why the selection of a read inside ``assign`` is unknown."""
        if guard.kind is ir.GuardKind.INDEX:
            return (
                f"The element `{assign.text}` reads is unknown: the index `{guard.text}` is "
                f"unknown {when}, so every element is kept."
            )
        return f"`?:` selection in `{assign.text}` is unknown: `{guard.text}` is unknown {when}."

    def _judge_decision(
        self, decision: _Decision, signal: str, lookup: ir.Lookup, when: str
    ) -> tuple[BranchStatus, str]:
        """Status of the control dependence on one ``if``, ``case``, or index selection."""
        guard = decision.guard
        is_case = guard.kind in (ir.GuardKind.CASE_ITEM, ir.GuardKind.CASE_DEFAULT)
        label = "Case selector" if is_case else "Condition"
        if guard.kind is ir.GuardKind.INDEX:
            label = "Index"
        enclosing = fs.tri_and(*decision.prefix)
        if enclosing is Tri.FALSE:
            outer = decision.prefix_guards[decision.prefix.index(Tri.FALSE)]
            return (
                BranchStatus.PRUNED,
                f"{label} `{guard.text}` is not evaluated: {ir.describe_guard(outer)} is false "
                f"{when}.",
            )
        path = self._trace_path(signal)
        if decision.all_blocked:
            return (
                BranchStatus.PRUNED,
                f"{label} `{guard.text}` does not affect {path}: every assignment it guards "
                "is overwritten by a later assignment.",
            )
        if guard.kind is ir.GuardKind.UNSUPPORTED:
            return (
                BranchStatus.UNKNOWN,
                f"{guard.text} is not evaluated; every branch it guards is kept.",
            )
        if ir.guard_is_opaque(guard, lookup):
            return (
                BranchStatus.UNKNOWN,
                f"{label} `{guard.text}` reads a value that is not available or not computed "
                f"{when}; every branch it guards is kept.",
            )
        value = ir.evaluate(guard.predicate, lookup)
        shown = ir.literal(value.bits)
        if enclosing is Tri.UNKNOWN or not value.is_known:
            return (
                BranchStatus.UNKNOWN,
                f"{label} `{guard.text}` is {shown} {when}; an unknown condition keeps every "
                "branch it guards.",
            )
        return (
            BranchStatus.RETAINED,
            f"{label} `{guard.text}` is {shown} {when} and decides which assignment to "
            f"{path} executes.",
        )

    def _classify(
        self,
        node: _Node,
        driver: Driver,
        live: list[tuple[GuardedAssign, _Verdict]],
        outcomes: list[list[Tri]],
    ) -> None:
        """Record what kind of event produced the node's value and where."""
        signal, cycle, phase = node.key
        path = self._trace_path(signal)
        shown = self._shown(signal, cycle, phase)
        if driver.edge_type is EdgeType.COMBINATIONAL:
            kind = _Kind.COMBINATIONAL
        else:
            kind = _Kind.STATE_UPDATE if live else _Kind.STATE_HOLD
        upgrade = node.kind is _Kind.STATE_HOLD and kind is _Kind.STATE_UPDATE
        if node.kind is not _Kind.PENDING and not upgrade:
            return
        node.kind = kind
        when = self._when(cycle, phase)
        if not live:
            if kind is _Kind.COMBINATIONAL:
                node.location = driver.span or node.location
                node.note = f"No assignment to {path} executes {when}; it is {shown}"
            else:
                edge = f"the cycle {cycle} clock edge ({self._times[cycle]} ps)"
                node.location = self._hold_location(driver, outcomes) or node.location
                node.note = f"{path} held {shown} at {edge}: no assignment to it executed"
            return
        assign, verdict = live[-1]
        node.location = assign.span or driver.span or node.location
        node.forward = all(_is_forward(candidate.rhs) for candidate, _ in live)
        if kind is _Kind.COMBINATIONAL:
            node.note = f"`{assign.text}` drives {path} to {shown} {when}"
            return
        edge = f"the cycle {cycle} clock edge ({self._times[cycle]} ps)"
        verb = "executed" if verdict.status is BranchStatus.RETAINED else "may have executed"
        before = self._sample(signal, cycle, SamplingPhase.BEFORE)
        if before is None or node.value is None:
            node.note = f"`{assign.text}` {verb} at {edge}; {path} is {shown} afterwards"
            return
        node.changed = before != node.value
        if node.changed:
            node.note = (
                f"`{assign.text}` {verb} at {edge} and changed {path} from "
                f"{ir.literal(before.bits)} to {shown}"
            )
        else:
            node.note = f"`{assign.text}` {verb} at {edge} and left {path} at {shown}"

    @staticmethod
    def _hold_location(driver: Driver, outcomes: list[list[Tri]]) -> SourceSpan | None:
        """Where a register's update was suppressed: the deepest condition that failed."""
        best: tuple[int, GuardedAssign] | None = None
        for assign, guards in zip(driver.assigns, outcomes):
            depth = 0
            while depth < len(guards) and guards[depth] is Tri.TRUE:
                depth += 1
            if best is None or depth >= best[0]:
                best = (depth, assign)
        if best is None:
            return driver.span
        depth, assign = best
        span = assign.span
        guard_span = assign.guards[depth].span if depth < len(assign.guards) else None
        if span is None:
            return guard_span or driver.span
        if guard_span is None or guard_span.source_id != span.source_id:
            return span
        return SourceSpan(
            source_id=span.source_id,
            line=min(span.line, guard_span.line),
            end_line=max(span.end_line, guard_span.end_line),
        )

    def _held(self, signal: str, drivers: tuple[Driver, ...], cycle: int, demand: int) -> bool:
        """True when ``signal`` provably held at that clock edge, so a carry may skip it.

        Assignments that write no ``demand`` bit are ignored, but the whole
        value must be unchanged: a carry edge joins equal whole-signal values
        (spec section 6.2).
        """
        lookup = self._lookup(cycle, SamplingPhase.BEFORE)
        for driver in drivers:
            for assign in driver.assigns:
                if not self._written(assign, signal)[0] & demand:
                    continue
                guards = [ir.evaluate_guard(guard, lookup) for guard in assign.guards]
                if fs.tri_and(*guards) is not Tri.FALSE:
                    return False
        before = self._sample(signal, cycle, SamplingPhase.BEFORE)
        after = self._sample(signal, cycle, SamplingPhase.SETTLED)
        return before is None or after is None or before == after

    def _unreset_branch(self, signal: str, drivers: tuple[Driver, ...]) -> SourceSpan | None:
        """The reset branch that leaves ``signal`` unassigned, if there is one.

        Uses only the reset named in ``SliceOptions``; nothing is detected by
        name. The register's clocked process must open with an ``if`` on that
        reset alone, every assignment to the register must sit in the other
        branch, and the span covers the condition and the reset branch's
        assignments to other registers.
        """
        reset_path, active = self._options.reset_path, self._options.reset_active
        reset = self._local(reset_path) if reset_path is not None else None
        if reset is None or active not in ("0", "1"):
            return None
        for driver in drivers:
            opening = [assign.guards[0] for assign in driver.assigns if assign.guards]
            if not opening or len(opening) != len(driver.assigns):
                continue
            guard = opening[0]
            if guard.kind is not ir.GuardKind.IF or guard.span is None:
                continue
            if any(other.decision != guard.decision for other in opening):
                continue
            # Inside a child instance the reset arrives through ports: compare through aliases.
            canonical = self._design.canonical
            reads = ir.signals_of(guard.predicate)
            if len(reads) != 1 or canonical(reads[0]) != canonical(reset):
                continue
            taken = fs.truth(
                ir.evaluate(guard.predicate, lambda name: Vec(active) if name == reads[0] else None)
            )
            if taken is Tri.UNKNOWN:
                continue
            in_reset = taken is Tri.TRUE
            if any(other.polarity is in_reset for other in opening):
                continue
            lines = [guard.span.line, guard.span.end_line]
            for found in self._design.drivers.values():
                for sibling in found:
                    for assign in sibling.assigns:
                        first = assign.guards[0] if assign.guards else None
                        if (
                            first is not None
                            and first.decision == guard.decision
                            and first.polarity is in_reset
                            and assign.span is not None
                            and assign.span.source_id == guard.span.source_id
                        ):
                            lines.extend((assign.span.line, assign.span.end_line))
            return SourceSpan(
                source_id=guard.span.source_id, line=min(lines), end_line=max(lines)
            )
        return None

    def _epoch_origin(self, node: _Node, drivers: tuple[Driver, ...], state: str) -> None:
        """Classify a register value that is the one it had before cycle 0.

        It is located at the reset branch when that branch never assigns the
        register (and the reset is known); otherwise it is an epoch boundary.
        """
        path = self._trace_path(node.key[0])
        reset_branch = self._unreset_branch(node.key[0], drivers)
        if reset_branch is not None:
            node.kind = _Kind.UNRESET
            node.location = reset_branch
            node.note = (
                f"{path} {state}, but the reset branch of its clocked process does not assign "
                "it, so its value is left over from before reset"
            )
            return
        node.kind = _Kind.EPOCH_BOUNDARY
        node.note = (
            f"{path} {state}; that value was established by reset or before the epoch began"
        )

    def _held_from_epoch(self, key: _Key, drivers: tuple[Driver, ...]) -> bool:
        """True when a register's value settled at the cycle 0 edge provably held through it.

        Holds at later edges are skipped by one carry edge (``_carry``). A carry must
        start from a settled value, so the hold at the cycle 0 edge cannot be skipped
        the same way; instead its settled node takes the origin of the value before
        cycle 0 (B-12). Without this, that one hold would add a storage boundary and
        rank its unmet condition ahead of the register's real origin.
        """
        signal, cycle, phase = key
        if cycle != 0 or phase is not SamplingPhase.SETTLED or self._options.expand_holds:
            return False
        if not all(
            d.unsupported is None
            and d.edge_type is EdgeType.SEQUENTIAL
            and self._clock_matches(d)
            for d in drivers
        ):
            return False
        return self._held(signal, drivers, 0, self._demand.get(key, self._full(signal)))

    def _carry(self, key: _Key, node: _Node, drivers: tuple[Driver, ...]) -> None:
        """Link a register read ``before`` an edge to the earlier edge that wrote it."""
        signal, cycle, _ = key
        path = self._trace_path(signal)
        shown = self._shown(signal, cycle, SamplingPhase.BEFORE)
        for driver in drivers:
            if driver.unsupported is not None:
                self._stop(
                    node,
                    "UNSUPPORTED_DRIVER",
                    f"{path} is not traced further: {driver.unsupported}.",
                )
                return
            if driver.edge_type is not EdgeType.SEQUENTIAL:
                self._stop(
                    node,
                    "UNSUPPORTED_DRIVER",
                    f"{path} is driven by both clocked and combinational processes; it is not "
                    "traced further.",
                )
                return
            if not self._clock_matches(driver):
                self._stop(
                    node,
                    "UNSUPPORTED_CLOCK",
                    f"{path} is not clocked by the rising edge of {self._failure.clock_path}; "
                    "other clocks and edges are not traced.",
                )
                return
        if cycle == 0:
            self._epoch_origin(node, drivers, f"is {shown} before cycle 0 of epoch {self._epoch}")
            return
        source_cycle = cycle - 1
        if source_cycle not in self._times:
            self._stop(
                node,
                "SLICE_TIME_LIMIT",
                f"The clock edge before cycle {cycle} is not known, so {path} is not traced "
                "to the edge that wrote it.",
            )
            return
        demand = self._demand.get(key, self._full(signal))
        limit = self._options.max_cycles_back
        if not self._options.expand_holds:
            # Never scan past the depth limit: the work stays bounded by it.
            floor = 0 if limit is None else self._anchor_cycle - limit
            # Holds already scanned for these bits are reused, so tracing a held value
            # from every cycle of a long window stays linear in the window. A hold at
            # the cycle 0 edge is not skipped here, because a carry must start from a
            # settled value; ``_expand`` gives that settled node its origin (B-12).
            scanned: list[int] = []
            while source_cycle > 0 and source_cycle >= floor:
                known = self._hold_origins.get((signal, demand, source_cycle))
                if known is not None:
                    source_cycle = known
                    break
                if not self._held(signal, drivers, source_cycle, demand):
                    break
                scanned.append(source_cycle)
                source_cycle -= 1
            for start in scanned:
                self._hold_origins[(signal, demand, start)] = source_cycle
        if limit is not None and self._anchor_cycle - source_cycle > limit:
            self._stop(
                node,
                "SLICE_DEPTH_LIMIT",
                f"The slice stops {_plural(limit, 'cycle', 'cycles')} before the failure; "
                f"{path} is not traced to cycle {source_cycle}.",
            )
            return
        source: _Key = (signal, source_cycle, SamplingPhase.SETTLED)
        settled = self._sample(signal, source_cycle, SamplingPhase.SETTLED)
        if settled is not None and node.value is not None and settled != node.value:
            self._stop(
                node,
                "CARRY_VALUE_MISMATCH",
                f"{path} is {ir.literal(settled.bits)} after the cycle {source_cycle} clock "
                f"edge but {shown} before the cycle {cycle} clock edge; it changed between "
                "clock edges, which the synchronous model does not explain.",
            )
            return
        node.kind = _Kind.CARRY
        self._add_edge(
            source,
            key,
            EdgeType.COMBINATIONAL,
            EdgeRole.DATA,
            BranchStatus.RETAINED,
            f"Carry: the value of {path} settled at the cycle {source_cycle} clock edge is "
            f"unchanged before the cycle {cycle} clock edge.",
            demand,
        )

    # ---- report ---------------------------------------------------------

    def _distances(
        self, anchor: _Key
    ) -> tuple[dict[_Key, int], dict[_Key, int], dict[_Key, _Key], dict[_Key, EdgeRole]]:
        """Hops and storage boundaries from the anchor, each node's next hop to it, and
        the role of the edge that hop uses."""
        incoming: dict[_Key, list[_Edge]] = {}
        for edge in self._edges.values():
            if edge.branch is not BranchStatus.PRUNED:
                incoming.setdefault(edge.target, []).append(edge)
        hops = {anchor: 0}
        toward: dict[_Key, _Key] = {}
        via: dict[_Key, EdgeRole] = {}
        queue = deque([anchor])
        while queue:
            current = queue.popleft()
            for edge in incoming.get(current, ()):
                if edge.source not in hops:
                    hops[edge.source] = hops[current] + 1
                    toward[edge.source] = current
                    via[edge.source] = edge.role
                    queue.append(edge.source)
        boundaries = {anchor: 0}
        pending = deque([anchor])
        while pending:
            current = pending.popleft()
            for edge in incoming.get(current, ()):
                cost = 1 if edge.edge_type is EdgeType.SEQUENTIAL else 0
                candidate = boundaries[current] + cost
                if edge.source not in boundaries or candidate < boundaries[edge.source]:
                    boundaries[edge.source] = candidate
                    if cost:
                        pending.append(edge.source)
                    else:
                        pending.appendleft(edge.source)
        return hops, boundaries, toward, via

    def _rank(self, anchor: _Key) -> tuple[SuspectLocation, ...]:
        """Order candidate causes.

        Every node the slice reached is a candidate, except carry nodes, which
        only restate a register value. Candidates that share a source location
        collapse to the one nearest the failure. The order is:

        1. RTL statements that compute a value, then statements that only
           forward one (a register or wire assigned a signal or a constant
           select of it), then places where the slice merely ends (module
           inputs, the reset boundary, undriven or untraced signals). A copy
           cannot have created a wrong value, so the statement that computed
           it is the better first candidate. A register that the reset branch
           leaves unassigned counts as a computing statement located at that
           reset branch, because that is where the fix goes;
        2. fewer storage boundaries from the failing observation first, because
           the failure is the first divergence and the most recent logic is the
           most likely to have introduced it;
        3. a register write that changed state or a register left unreset, then
           combinational logic, then a register write that changed nothing,
           then a hold;
        4. fewer dependency hops; then a node reached through a control edge
           before one reached through data, since at equal distance the
           condition that chose a path is a likelier defect than a value it
           forwarded; then signal path, later cycle, and phase.

        The rank is ordinal and deterministic. It is a heuristic, not a
        probability.
        """
        hops, boundaries, toward, via = self._distances(anchor)
        scored: list[tuple[tuple[int, int, int, int, int, str, int, str], _Key]] = []
        for key in self._reached:
            node = self._nodes[key]
            if node.kind in (_Kind.PENDING, _Kind.CARRY) or key not in hops:
                continue
            if node.kind in _BOUNDARY_RANK:
                tier, kind_rank = _BOUNDARY_TIER, _BOUNDARY_RANK[node.kind]
            elif node.kind is _Kind.UNRESET:
                tier, kind_rank = _STATEMENT_TIER, 0
            elif node.kind is _Kind.STATE_UPDATE:
                tier, kind_rank = _STATEMENT_TIER, 0 if node.changed else 2
            elif node.kind is _Kind.COMBINATIONAL:
                tier, kind_rank = _STATEMENT_TIER, 1
            else:
                tier, kind_rank = _STATEMENT_TIER, 3
            if tier == _STATEMENT_TIER and node.forward:
                tier = _FORWARD_TIER
            role = 0 if via.get(key) is EdgeRole.CONTROL else 1
            signal, cycle, phase = key
            score = (tier, boundaries[key], kind_rank, hops[key], role, signal, -cycle, phase.value)
            scored.append((score, key))
        scored.sort()

        suspects: list[SuspectLocation] = []
        seen: set[tuple[str, int, int]] = set()
        limit = self._options.max_suspects
        for _, key in scored:
            if limit is not None and len(suspects) >= limit:
                break
            node = self._nodes[key]
            if node.location is not None:
                place = (node.location.source_id, node.location.line, node.location.end_line)
                if place in seen:
                    continue
                seen.add(place)
            if hops[key] == 0:
                distance = "This is the failing observation itself."
            else:
                distance = (
                    f"It is {_plural(hops[key], 'dependency hop', 'dependency hops')} and "
                    f"{_plural(boundaries[key], 'storage boundary', 'storage boundaries')} from "
                    "the failing observation."
                )
            chain = [key]
            while chain[-1] in toward:
                chain.append(toward[chain[-1]])
            rank = len(suspects) + 1
            suspects.append(
                SuspectLocation(
                    id=f"s{rank}",
                    rank=rank,
                    node_id=node.node_id,
                    location=node.location,
                    reason=f"{node.note}. {distance}",
                    evidence_node_ids=tuple(self._nodes[step].node_id for step in chain),
                )
            )
        return tuple(suspects)

    def _report_loops(self) -> None:
        """Report combinational cycles inside one observation; they stay in the graph."""
        within: dict[_Key, list[_Key]] = {}
        for edge in self._edges.values():
            same_observation = edge.source[1:] == edge.target[1:]
            if (
                edge.branch is not BranchStatus.PRUNED
                and edge.edge_type is EdgeType.COMBINATIONAL
                and same_observation
            ):
                within.setdefault(edge.target, []).append(edge.source)
        finished: set[_Key] = set()
        for root in within:
            if root in finished:
                continue
            path = [root]
            pending = [iter(within.get(root, ()))]
            while pending:
                following = next(pending[-1], None)
                if following is None:
                    finished.add(path.pop())
                    pending.pop()
                elif following in path:
                    loop = path[path.index(following) :]
                    names = ", ".join(sorted({self._trace_path(key[0]) for key in loop}))
                    self._diag(
                        "COMBINATIONAL_LOOP",
                        f"{names} depend on each other {self._when(following[1], following[2])} "
                        "with no storage in between; the loop is kept in the graph.",
                    )
                elif following not in finished:
                    path.append(following)
                    pending.append(iter(within.get(following, ())))

    def _waveform(self) -> WaveformWindow | None:
        paths: dict[str, None] = {self._failure.clock_path: None}
        for key in self._reached:
            if self._nodes[key].value is not None:
                paths.setdefault(self._trace_path(key[0]))
        end = self._failure.observation.time_ps + 1
        try:
            return self._provider.get_window_slice(list(paths), self._window_start, end)
        except ContractError as error:
            self._diag(
                "WAVEFORM_UNAVAILABLE", f"No waveform window is attached to the report: {error}"
            )
            return None

    def _report(self, anchor: _Key) -> CausalReport:
        self._report_loops()
        nodes = tuple(
            CausalNode(
                id=node.node_id,
                signal_path=self._trace_path(node.key[0]),
                bit_range=None,
                observation=Observation(
                    epoch=self._epoch,
                    cycle=node.key[1],
                    phase=node.key[2],
                    time_ps=self._times[node.key[1]],
                ),
                value=node.value,
                location=node.location,
            )
            for node in self._nodes.values()
        )
        edges = tuple(
            CausalEdge(
                id=f"e{position:04d}",
                source=self._nodes[edge.source].node_id,
                target=self._nodes[edge.target].node_id,
                edge_type=edge.edge_type,
                role=edge.role,
                branch=edge.branch,
                reason=" ".join(list(edge.reasons)[:3]),
            )
            for position, edge in enumerate(self._edges.values(), start=1)
        )
        suspects = self._rank(anchor)
        assertions: tuple[CandidateAssertion, ...] = ()
        if self._options.synthesize_assertion and suspects:
            result = synthesize_assertion(
                failure=self._failure,
                nodes=nodes,
                edges=edges,
                suspect=suspects[0],
                design=self._design,
                trace_scope=self._options.trace_scope,
                reset_path=self._options.reset_path,
                reset_active=self._options.reset_active,
            )
            for diagnostic in result.diagnostics:
                self._diagnostics.setdefault(diagnostic)
            if result.assertion is not None:
                assertions = (result.assertion,)
        waveform = self._waveform() if self._options.include_waveform else None
        return CausalReport(
            schema_version=1,
            provenance=self._provenance(),
            status=AnalysisStatus.FAIL,
            failure=self._failure,
            anchor_node_id=self._nodes[anchor].node_id,
            nodes=nodes,
            edges=edges,
            suspects=suspects,
            sources=self._design.sources,
            assertions=assertions,
            waveform=waveform,
            truncated=self._truncated,
            diagnostics=tuple(self._diagnostics),
        )
