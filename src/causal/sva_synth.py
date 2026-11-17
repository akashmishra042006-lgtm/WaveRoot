"""Candidate SystemVerilog assertion from the top suspect's causal chain.

The generated property is a point property taken from one trace observation:

    (values the suspect consumed, plus the rest of the pre-edge state the
     failing signal depends on)  |-> / |=> / ##K  (sink == expected value)

It restates the failure as something a simulator or formal tool can check. It
fails on the analyzed RTL at the failing cycle and should hold once the suspect
is fixed, if the expected value from the golden model is right. It is a
candidate, never an independent oracle, and this module never marks it
validated.

Sampling. A concurrent assertion samples in the preponed region, which is the
contract's ``before`` phase at a clock edge. A value that is ``settled`` at edge
k is therefore sampled at edge k+1. The latency between antecedent and
consequent is computed from that rule, not from graph depth.

The text is a self-contained checker module plus a ``bind`` statement, so it
can be compiled next to the design without editing it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.causal.ast_graph import DesignGraph, SignalInfo, SignalKind
from src.causal.expr import literal
from src.contracts import (
    AssertionValidation,
    BranchStatus,
    CandidateAssertion,
    CausalEdge,
    CausalNode,
    Diagnostic,
    EdgeType,
    FailureAnchor,
    Observation,
    SamplingPhase,
    SuspectLocation,
)


@dataclass(frozen=True, slots=True)
class SvaResult:
    """A candidate assertion, or None with diagnostics saying why there is none."""

    assertion: CandidateAssertion | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class _Term:
    signal: str
    bits: str
    is_cause: bool


def _sample_index(observation: Observation) -> int:
    """Clock edge, counted in cycles, at which an assertion samples this value."""
    return observation.cycle + (1 if observation.phase is SamplingPhase.SETTLED else 0)


def _port_name(signal: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", signal.replace(".", "__"))


def _port_decl(name: str, info: SignalInfo) -> str:
    signed = " signed" if info.signed else ""
    if info.bit_range is None:
        return f"input logic{signed} {name}"
    return f"input logic{signed} [{info.bit_range.left}:{info.bit_range.right}] {name}"


def synthesize_assertion(
    *,
    failure: FailureAnchor,
    nodes: Sequence[CausalNode],
    edges: Sequence[CausalEdge],
    suspect: SuspectLocation,
    design: DesignGraph,
    trace_scope: str,
    reset_path: str | None = None,
    reset_active: str | None = None,
    assertion_id: str = "a1",
) -> SvaResult:
    """Build a candidate ``assert property`` for ``suspect``.

    ``nodes`` and ``edges`` are the causal graph the suspect belongs to.
    ``trace_scope`` is the trace path of the analyzed top instance. ``reset_path``
    and ``reset_active`` (``"0"`` or ``"1"``) add a ``disable iff``; without them
    the property is not reset-qualified.
    """
    diagnostics: list[Diagnostic] = []

    def give_up(code: str, message: str) -> SvaResult:
        diagnostics.append(Diagnostic(code=code, message=message))
        return SvaResult(None, tuple(diagnostics))

    prefix = f"{trace_scope}."

    def local(path: str) -> str | None:
        name = path[len(prefix) :] if path.startswith(prefix) else None
        return name if name is not None and name in design.signals else None

    if failure.expected is None:
        return give_up(
            "SVA_NO_EXPECTED_VALUE",
            "No assertion was generated: the failure carries no expected value to assert.",
        )
    by_id = {node.id: node for node in nodes}
    suspect_node = by_id.get(suspect.node_id)
    anchor = next(
        (
            node
            for node in nodes
            if node.signal_path == failure.signal_path
            and node.observation == failure.observation
        ),
        None,
    )
    sink = local(failure.signal_path)
    clock = local(failure.clock_path)
    if suspect_node is None or anchor is None or sink is None:
        return give_up(
            "SVA_NO_CHAIN",
            "No assertion was generated: the suspect or the failing signal is not in the graph.",
        )
    if clock is None:
        return give_up(
            "SVA_CLOCK_NOT_IN_DESIGN",
            f"No assertion was generated: clock {failure.clock_path} is not a signal of "
            f"{design.top_module}.",
        )

    reset = local(reset_path) if reset_path is not None else None
    if reset_path is not None and (reset is None or reset_active not in ("0", "1")):
        diagnostics.append(
            Diagnostic(
                code="SVA_RESET_NOT_APPLIED",
                message=f"Reset {reset_path} is not a signal of {design.top_module} or its "
                "active level is not 0 or 1; the assertion has no disable iff.",
            )
        )
        reset = None

    live = [edge for edge in edges if edge.branch is not BranchStatus.PRUNED]

    def is_carry(edge: CausalEdge) -> bool:
        different = by_id[edge.source].observation != by_id[edge.target].observation
        return edge.edge_type is EdgeType.COMBINATIONAL and different

    causes = [
        by_id[edge.source] for edge in live if edge.target == suspect_node.id and not is_carry(edge)
    ] or [suspect_node]
    launch = causes[0].observation
    context: list[CausalNode] = []
    if launch.phase is SamplingPhase.BEFORE:
        context = [
            by_id[edge.source]
            for edge in live
            if edge.edge_type is EdgeType.SEQUENTIAL and by_id[edge.source].observation == launch
        ]

    terms: dict[str, _Term] = {}
    unobserved: dict[str, None] = {}
    for node, is_cause in [(node, True) for node in causes] + [(node, False) for node in context]:
        name = local(node.signal_path)
        if name is None or name in (clock, reset) or node.observation != launch:
            continue
        info = design.signals[name]
        if node.value is None or info.is_aggregate or node.value.width != info.width:
            unobserved.setdefault(node.signal_path)
            continue
        terms.setdefault(name, _Term(name, node.value.bits, is_cause))
    if not terms:
        return give_up(
            "SVA_NO_ANTECEDENT",
            "No assertion was generated: the top suspect has no observed operand to use as "
            "an antecedent.",
        )

    latency = _sample_index(anchor.observation) - _sample_index(launch)
    if latency < 0:
        return give_up(
            "SVA_NO_CHAIN",
            "No assertion was generated: the suspect is observed after the failing signal.",
        )
    notes: list[str] = []
    if unobserved:
        names = ", ".join(unobserved)
        notes.append(f"Not constrained (no observed value): {names}.")
        diagnostics.append(
            Diagnostic(
                code="SVA_OPERAND_NOT_OBSERVED",
                message=f"The candidate assertion does not constrain {names}: no value was "
                "observed for it.",
            )
        )
    if latency > 1:
        notes.append(
            f"The consequent is checked {latency} clock edges after the antecedent; enables "
            "and holds in the intermediate cycles are not encoded."
        )
        diagnostics.append(
            Diagnostic(
                code="SVA_LATENCY_ASSUMED",
                message=f"The candidate assertion assumes a fixed latency of {latency} cycles; "
                "conditions in the intermediate cycles are not encoded.",
            )
        )
    unstable = _settled_inputs(anchor, by_id, live, design, local)
    if unstable and latency >= 1:
        names = ", ".join(unstable)
        notes.append(
            f"Assumes {names} keeps its value until the next clock edge, where the "
            "consequent is sampled."
        )
        diagnostics.append(
            Diagnostic(
                code="SVA_INPUT_STABILITY_ASSUMED",
                message=f"The candidate assertion assumes input {names} is stable from the "
                "failing clock edge to the next one.",
            )
        )

    text = _render(
        assertion_id=assertion_id,
        failure=failure,
        suspect=suspect,
        suspect_path=suspect_node.signal_path,
        design=design,
        clock=clock,
        reset=reset,
        reset_active=reset_active,
        terms=list(terms.values()),
        sink=sink,
        latency=latency,
        notes=notes,
    )
    assertion = CandidateAssertion(
        id=assertion_id, text=text, validation=AssertionValidation.CANDIDATE, tool=None
    )
    return SvaResult(assertion, tuple(diagnostics))


def _settled_inputs(
    anchor: CausalNode,
    by_id: dict[str, CausalNode],
    live: Sequence[CausalEdge],
    design: DesignGraph,
    local: Callable[[str], str | None],
) -> list[str]:
    """Module inputs the sink reads combinationally in a ``settled`` observation."""
    if anchor.observation.phase is not SamplingPhase.SETTLED:
        return []
    incoming: dict[str, list[str]] = {}
    for edge in live:
        incoming.setdefault(edge.target, []).append(edge.source)
    seen = {anchor.id}
    stack = [anchor.id]
    found: dict[str, None] = {}
    while stack:
        current = stack.pop()
        for source_id in incoming.get(current, ()):
            source = by_id[source_id]
            if source_id in seen or source.observation != anchor.observation:
                continue
            seen.add(source_id)
            stack.append(source_id)
            name = local(source.signal_path)
            if name is not None and design.signals[name].kind is SignalKind.INPUT:
                found.setdefault(source.signal_path)
    return list(found)


def _render(
    *,
    assertion_id: str,
    failure: FailureAnchor,
    suspect: SuspectLocation,
    suspect_path: str,
    design: DesignGraph,
    clock: str,
    reset: str | None,
    reset_active: str | None,
    terms: Sequence[_Term],
    sink: str,
    latency: int,
    notes: Sequence[str],
) -> str:
    tag = re.sub(r"[^A-Za-z0-9_]", "_", assertion_id)
    assert failure.expected is not None
    observation = failure.observation
    expected = literal(failure.expected.bits)
    actual = literal(failure.actual.bits) if failure.actual is not None else "no value"

    ports: dict[str, str] = {}
    connections: dict[str, str] = {}
    for signal in (clock, *([reset] if reset else []), *(term.signal for term in terms), sink):
        name = _port_name(signal)
        ports.setdefault(name, _port_decl(name, design.signals[signal]))
        connections.setdefault(name, signal)

    disable = ""
    if reset is not None:
        inactive = "1'b1" if reset_active == "0" else "1'b0"
        disable = f" disable iff ({_port_name(reset)} !== {inactive})"
    clocking = f"@(posedge {_port_name(clock)}){disable}"
    antecedent = " && ".join(
        f"({_port_name(term.signal)} === {literal(term.bits)})" for term in terms
    )
    sink_name = _port_name(sink)
    consequent = f"(!$isunknown({sink_name}) && ({sink_name} == {expected}))"
    implication = {0: "|->", 1: "|=>"}.get(latency, f"|-> ##{latency}")

    location = ""
    if suspect.location is not None:
        path = next(
            (source.path for source in design.sources if source.id == suspect.location.source_id),
            suspect.location.source_id,
        )
        location = f" ({path}:{suspect.location.line})"
    cause = ", ".join(term.signal for term in terms if term.is_cause) or "none observed"
    context = ", ".join(term.signal for term in terms if not term.is_cause)

    lines = [
        f"// WaveRoot candidate assertion {assertion_id}. Not validated: it restates one",
        "// trace observation and the golden expected value. Review before relying on it.",
        f"// Failure: {failure.signal_path} expected {expected}, observed {actual} at "
        f"{observation.time_ps} ps",
        f"//   (epoch {observation.epoch}, cycle {observation.cycle}, "
        f"{observation.phase.value}).",
        f"// Top suspect: {suspect_path}{location}.",
        f"// Cause terms (operands the suspect consumed): {cause}.",
    ]
    if context:
        lines.append(f"// Context terms (remaining pre-edge state of the sink): {context}.")
    if reset is None:
        lines.append("// No reset was supplied, so the property is not reset-qualified.")
    lines.extend(f"// {note}" for note in notes)
    lines.append(f"module waveroot_chk_{tag} (")
    lines.append(",\n".join(f"  {decl}" for decl in ports.values()))
    lines.append(");")
    lines.append(f"  property p_waveroot_{tag};")
    lines.append(f"    {clocking}")
    lines.append(f"    ({antecedent})")
    lines.append(f"    {implication} {consequent};")
    lines.append("  endproperty")
    lines.append(f"  a_waveroot_{tag}: assert property (p_waveroot_{tag});")
    lines.append(f"  c_waveroot_{tag}: cover property ({clocking} ({antecedent}));")
    lines.append("endmodule")
    bound = ", ".join(f".{name}({signal})" for name, signal in connections.items())
    lines.append(
        f"bind {design.top_module} waveroot_chk_{tag} u_waveroot_chk_{tag} ({bound});"
    )
    return "\n".join(lines) + "\n"
