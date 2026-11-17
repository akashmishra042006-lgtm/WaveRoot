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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from src.causal import expr as ir
from src.causal import fourstate as fs
from src.causal.ast_graph import DesignGraph, Driver, GuardedAssign, SignalKind
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
# Design diagnostics that mean a driver may be missing from the graph. They are
# repeated in every report because they weaken any slice of that design.
_GLOBAL_CAVEATS: Final = frozenset({"UNSUPPORTED_SCOPE", "UNSUPPORTED_LVALUE"})


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
    ``reset_path`` and ``reset_active`` only qualify the generated assertion.
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
    UNDRIVEN = "undriven"
    TRUNCATED = "truncated"


# Ordering of suspects. Lower sorts first. RTL statements come before the
# places where the slice merely ends.
_STATEMENT_TIER: Final = 0
_BOUNDARY_TIER: Final = 1
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


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


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
        self._diagnostics: dict[Diagnostic, None] = {}
        self._truncated = False

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
            if len(self._nodes) >= self._options.max_nodes:
                self._stop(
                    self._nodes[key],
                    "SLICE_NODE_LIMIT",
                    f"The slice reached the limit of {self._options.max_nodes} nodes; nodes "
                    "beyond it were not expanded.",
                )
                continue
            self._expand(key)
        return self._report(anchor)

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

    def _reach(self, key: _Key) -> None:
        if key not in self._reached:
            self._reached[key] = None
            self._queue.append(key)

    def _add_edge(
        self,
        source: _Key,
        target: _Key,
        edge_type: EdgeType,
        role: EdgeRole,
        branch: BranchStatus,
        reason: str,
    ) -> None:
        if source == target:
            self._diag(
                "COMBINATIONAL_SELF_REFERENCE",
                f"{self._trace_path(target[0])} reads itself inside one observation; that "
                "dependence is not represented as an edge.",
            )
            return
        if branch is BranchStatus.PRUNED and not self._options.include_pruned:
            return
        self._node(source)
        identity = (source, target, edge_type, role, branch)
        edge = self._edges.get(identity)
        if edge is None:
            edge = self._edges[identity] = _Edge(source, target, edge_type, role, branch)
        edge.reasons.setdefault(reason)
        if branch is not BranchStatus.PRUNED:
            self._reach(source)

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
        clock = driver.clock
        return (
            clock is not None
            and clock.rising
            and self._trace_path(clock.signal) == self._failure.clock_path
        )

    def _verdicts(
        self, driver: Driver, outcomes: list[list[Tri]], when: str
    ) -> tuple[list[_Verdict], bool]:
        """Decide which assignments took effect; the last executed full write wins."""
        verdicts: list[_Verdict] = []
        blocker: GuardedAssign | None = None
        for assign, guards in zip(reversed(driver.assigns), reversed(outcomes)):
            total = fs.tri_and(*guards)
            if total is Tri.FALSE:
                guard = assign.guards[guards.index(Tri.FALSE)]
                why = (
                    f"`{assign.text}` does not execute: {ir.describe_guard(guard)} is false "
                    f"{when}."
                )
                verdicts.append(_Verdict(BranchStatus.PRUNED, why, blocker is not None))
            elif blocker is not None:
                why = f"`{assign.text}` is overwritten by the later assignment `{blocker.text}`."
                verdicts.append(_Verdict(BranchStatus.PRUNED, why, True))
            elif total is Tri.TRUE:
                verdicts.append(
                    _Verdict(BranchStatus.RETAINED, f"`{assign.text}` executes {when}.", False)
                )
                if assign.full_write:
                    blocker = assign
            else:
                guard = assign.guards[guards.index(Tri.UNKNOWN)]
                why = (
                    f"`{assign.text}` may execute: {ir.describe_guard(guard)} is unknown "
                    f"{when}."
                )
                verdicts.append(_Verdict(BranchStatus.UNKNOWN, why, False))
        verdicts.reverse()
        return verdicts, blocker is not None

    def _expand_driver(
        self, key: _Key, node: _Node, driver: Driver, cycle: int, phase: SamplingPhase
    ) -> None:
        """Add the edges of one driver, with operands sampled at ``(cycle, phase)``."""
        signal = key[0]
        path = self._trace_path(signal)
        edge_type = driver.edge_type
        lookup = self._lookup(cycle, phase)
        when = self._when(cycle, phase)
        outcomes = [
            [ir.evaluate_guard(guard, lookup) for guard in assign.guards]
            for assign in driver.assigns
        ]
        verdicts, definite = self._verdicts(driver, outcomes, when)

        for assign, verdict in zip(driver.assigns, verdicts):
            if verdict.status is not BranchStatus.PRUNED and not ir.is_supported(assign.rhs):
                self._diag(
                    "UNSUPPORTED_OPERAND",
                    f"`{assign.text}` contains a construct whose value is not computed; its "
                    "operands are kept as dependencies without evaluation.",
                )
            for read in ir.collect_reads(assign.rhs):
                status, reason = verdict.status, verdict.why
                if status is not BranchStatus.PRUNED:
                    for guard in read.guards:
                        outcome = ir.evaluate_guard(guard, lookup)
                        if outcome is Tri.FALSE:
                            status = BranchStatus.PRUNED
                            reason = (
                                f"`?:` arm of `{assign.text}` is not selected: "
                                f"{ir.describe_guard(guard)} is false {when}."
                            )
                            break
                        if outcome is Tri.UNKNOWN:
                            status = BranchStatus.UNKNOWN
                            reason = (
                                f"`?:` selection in `{assign.text}` is unknown: `{guard.text}` "
                                f"is unknown {when}."
                            )
                self._add_edge(
                    (read.signal, cycle, phase), key, edge_type, read.role, status, reason
                )
            for operand in assign.index_operands:
                for read in ir.collect_reads(operand, EdgeRole.CONTROL):
                    self._add_edge(
                        (read.signal, cycle, phase),
                        key,
                        edge_type,
                        EdgeRole.CONTROL,
                        verdict.status,
                        f"Selects the written element: {verdict.why}",
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
                    self._add_edge(
                        (read.signal, cycle, phase),
                        key,
                        edge_type,
                        EdgeRole.CONTROL,
                        status,
                        reason,
                    )

        live = [
            (assign, verdict)
            for assign, verdict in zip(driver.assigns, verdicts)
            if verdict.status is not BranchStatus.PRUNED
        ]
        if edge_type is EdgeType.SEQUENTIAL:
            if not definite:
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
                else:
                    reason = (
                        f"Bits of {path} that are not written at the cycle {cycle} clock edge "
                        "hold their value."
                    )
                self._add_edge(
                    (signal, cycle, SamplingPhase.BEFORE),
                    key,
                    EdgeType.SEQUENTIAL,
                    EdgeRole.DATA,
                    BranchStatus.UNKNOWN if uncertain else BranchStatus.RETAINED,
                    reason,
                )
        elif not live:
            self._diag(
                "NO_ACTIVE_ASSIGNMENT",
                f"No assignment to {path} executes {when}; the process leaves it undriven or "
                "infers storage, which is outside the MVP.",
            )
        self._classify(node, driver, live, outcomes)

    def _judge_decision(
        self, decision: _Decision, signal: str, lookup: ir.Lookup, when: str
    ) -> tuple[BranchStatus, str]:
        """Status of the control dependence on one ``if`` or ``case`` construct."""
        guard = decision.guard
        is_case = guard.kind in (ir.GuardKind.CASE_ITEM, ir.GuardKind.CASE_DEFAULT)
        label = "Case selector" if is_case else "Condition"
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

    def _held(self, signal: str, drivers: tuple[Driver, ...], cycle: int) -> bool:
        """True when no assignment to ``signal`` can have executed at that clock edge."""
        lookup = self._lookup(cycle, SamplingPhase.BEFORE)
        for driver in drivers:
            for assign in driver.assigns:
                guards = [ir.evaluate_guard(guard, lookup) for guard in assign.guards]
                if fs.tri_and(*guards) is not Tri.FALSE:
                    return False
        before = self._sample(signal, cycle, SamplingPhase.BEFORE)
        after = self._sample(signal, cycle, SamplingPhase.SETTLED)
        return before is None or after is None or before == after

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
            node.kind = _Kind.EPOCH_BOUNDARY
            node.note = (
                f"{path} is {shown} before cycle 0 of epoch {self._epoch}; that value was "
                "established by reset or before the epoch began"
            )
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
        if not self._options.expand_holds:
            while source_cycle > 0 and self._held(signal, drivers, source_cycle):
                source_cycle -= 1
        limit = self._options.max_cycles_back
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
        )

    # ---- report ---------------------------------------------------------

    def _distances(
        self, anchor: _Key
    ) -> tuple[dict[_Key, int], dict[_Key, int], dict[_Key, _Key]]:
        """Hops and storage boundaries from the anchor, and each node's next hop to it."""
        incoming: dict[_Key, list[_Edge]] = {}
        for edge in self._edges.values():
            if edge.branch is not BranchStatus.PRUNED:
                incoming.setdefault(edge.target, []).append(edge)
        hops = {anchor: 0}
        toward: dict[_Key, _Key] = {}
        queue = deque([anchor])
        while queue:
            current = queue.popleft()
            for edge in incoming.get(current, ()):
                if edge.source not in hops:
                    hops[edge.source] = hops[current] + 1
                    toward[edge.source] = current
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
        return hops, boundaries, toward

    def _rank(self, anchor: _Key) -> tuple[SuspectLocation, ...]:
        """Order candidate causes.

        Every node the slice reached is a candidate, except carry nodes, which
        only restate a register value. Candidates that share a source location
        collapse to the one nearest the failure. The order is:

        1. RTL statements before places where the slice merely ends (module
           inputs, the reset boundary, undriven or untraced signals);
        2. fewer storage boundaries from the failing observation first, because
           the failure is the first divergence and the most recent logic is the
           most likely to have introduced it;
        3. a register write that changed state, then combinational logic, then
           a register write that changed nothing, then a hold;
        4. fewer dependency hops, then signal path, later cycle, and phase.

        The rank is ordinal and deterministic. It is a heuristic, not a
        probability.
        """
        hops, boundaries, toward = self._distances(anchor)
        scored: list[tuple[tuple[int, int, int, int, str, int, str], _Key]] = []
        for key in self._reached:
            node = self._nodes[key]
            if node.kind in (_Kind.PENDING, _Kind.CARRY) or key not in hops:
                continue
            if node.kind in _BOUNDARY_RANK:
                tier, kind_rank = _BOUNDARY_TIER, _BOUNDARY_RANK[node.kind]
            elif node.kind is _Kind.STATE_UPDATE:
                tier, kind_rank = _STATEMENT_TIER, 0 if node.changed else 2
            elif node.kind is _Kind.COMBINATIONAL:
                tier, kind_rank = _STATEMENT_TIER, 1
            else:
                tier, kind_rank = _STATEMENT_TIER, 3
            signal, cycle, phase = key
            score = (tier, boundaries[key], kind_rank, hops[key], signal, -cycle, phase.value)
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
