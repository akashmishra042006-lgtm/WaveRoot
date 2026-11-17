"""Track B engine tests: dependency graph, backward slicing, assertion synthesis.

These tests run the real ``src.causal`` engine. Waveform values come from
``MockWaveformProvider``, either over the shared mock FIFO trace or over small
hand-written traces defined here; RTL is elaborated by ``pyslang``. They are
not end-to-end coverage: no simulator runs, and Track A's provider and Track
C's wiring are not involved.

The hand-authored causal graph inside ``mock_fifo_failure.json`` is fixture
data, so nothing here compares against it. Only its trace and failure are used.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pyslang import ast, syntax

from src.causal import (
    DesignError,
    DesignGraph,
    SliceOptions,
    analyze_comparison,
    build_design_graph,
    slice_failure,
    synthesize_assertion,
)
from src.causal import expr as ir
from src.causal import fourstate as fs
from src.causal.ast_graph import ProcessKind, SignalKind
from src.causal.fourstate import CaseFlavor, Tri, Vec
from src.contracts import (
    AnalysisStatus,
    AssertionValidation,
    BitRange,
    BranchStatus,
    CausalEdge,
    CausalNode,
    CausalReport,
    ComparisonMode,
    ComparisonResult,
    Diagnostic,
    EdgeRole,
    EdgeType,
    FailureAnchor,
    FailureKind,
    LogicValue,
    MockFixture,
    MockSignal,
    MockTrace,
    MockWaveformProvider,
    Observation,
    ProvenanceKind,
    SamplingPhase,
    SignalDecl,
    SignalSample,
    TimeInterval,
    TraceCoverage,
    load_mock_fixture,
)

REPO = Path(__file__).resolve().parents[1]
FIFO_SV = REPO / "tests" / "fixtures" / "mock_fifo_wrap.sv"
FIFO_JSON = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"
SCOPE = "tb.dut"
BEFORE = SamplingPhase.BEFORE
SETTLED = SamplingPhase.SETTLED

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def lv(bits: str) -> LogicValue:
    return LogicValue(width=len(bits), bits=bits)


def clock_samples(edges: Sequence[int], high_ps: int = 2000) -> list[tuple[int, str]]:
    """A clock that rises at each time in ``edges`` and falls ``high_ps`` later."""
    samples = [(0, "0")]
    for edge in edges:
        samples.extend([(edge, "1"), (edge + high_ps, "0")])
    return samples


def make_trace(
    end_ps: int,
    signals: Mapping[str, Sequence[tuple[int, str]]],
    unavailable: Sequence[tuple[int, int]] = (),
) -> MockTrace:
    """A mock trace from ``{local name: [(time_ps, bits), ...]}`` under ``tb.dut``."""
    built = []
    for name, samples in signals.items():
        width = len(samples[0][1])
        decl = SignalDecl(
            path=f"{SCOPE}.{name}",
            width=width,
            bit_range=None if width == 1 else BitRange(left=width - 1, right=0),
        )
        built.append(
            MockSignal(
                decl=decl,
                samples=tuple(SignalSample(time_ps=time, value=lv(bits)) for time, bits in samples),
            )
        )
    return MockTrace(
        coverage=TraceCoverage(start_ps=0, end_ps=end_ps),
        unavailable=tuple(TimeInterval(start_ps=a, end_ps=b) for a, b in unavailable),
        signals=tuple(built),
    )


EDGES = (5000, 15000, 25000)
"""Rising clock edges of the small traces: cycles 0, 1, and 2."""


def static_provider(
    values: Mapping[str, str],
    changing: Mapping[str, Sequence[tuple[int, str]]] | None = None,
    edges: Sequence[int] = EDGES,
) -> MockWaveformProvider:
    """Signals in ``values`` hold one value for the whole trace; ``changing`` vary."""
    signals: dict[str, Sequence[tuple[int, str]]] = {"clk": clock_samples(edges)}
    signals.update({name: [(0, bits)] for name, bits in values.items()})
    signals.update(changing or {})
    return MockWaveformProvider(make_trace(edges[-1] + 5000, signals))


def failure_at(
    signal: str,
    cycle: int,
    time_ps: int,
    actual: str,
    expected: str,
    phase: SamplingPhase = SETTLED,
) -> FailureAnchor:
    known = "x" not in actual and "z" not in actual
    return FailureAnchor(
        kind=FailureKind.VALUE_MISMATCH if known else FailureKind.UNKNOWN_VALUE,
        mode=ComparisonMode.CYCLE,
        signal_path=f"{SCOPE}.{signal}",
        clock_path=f"{SCOPE}.clk",
        observation=Observation(epoch=0, cycle=cycle, phase=phase, time_ps=time_ps),
        expected=lv(expected),
        actual=lv(actual),
        detail="Test failure anchor.",
    )


def build(tmp_path: Path, source: str, name: str = "dut.sv") -> DesignGraph:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return build_design_graph([path], root=tmp_path)


def node_of(report: CausalReport, signal: str, cycle: int, phase: SamplingPhase) -> CausalNode:
    found = [
        node
        for node in report.nodes
        if node.signal_path == f"{SCOPE}.{signal}"
        and node.observation.cycle == cycle
        and node.observation.phase is phase
    ]
    assert len(found) == 1, f"expected one node for {signal} c{cycle} {phase.value}: {found}"
    return found[0]


def has_node(report: CausalReport, signal: str) -> bool:
    return any(node.signal_path == f"{SCOPE}.{signal}" for node in report.nodes)


def edges_of(
    report: CausalReport,
    source: tuple[str, int, SamplingPhase],
    target: tuple[str, int, SamplingPhase],
    role: EdgeRole | None = None,
) -> list[CausalEdge]:
    source_id = node_of(report, *source).id
    target_id = node_of(report, *target).id
    return [
        edge
        for edge in report.edges
        if edge.source == source_id
        and edge.target == target_id
        and (role is None or edge.role is role)
    ]


def branches(edges: Sequence[CausalEdge]) -> set[BranchStatus]:
    return {edge.branch for edge in edges}


def codes(report: CausalReport) -> set[str]:
    return {diagnostic.code for diagnostic in report.diagnostics}


def compile_errors(text: str) -> list[str]:
    """Elaborate SystemVerilog text with pyslang and return its error codes."""
    compilation = ast.Compilation()
    compilation.addSyntaxTree(syntax.SyntaxTree.fromText(text))
    return [str(d.code) for d in compilation.getAllDiagnostics() if d.isError()]


OPTIONS = SliceOptions(trace_scope=SCOPE)


@pytest.fixture(scope="module")
def fifo() -> MockFixture:
    return load_mock_fixture(FIFO_JSON)


@pytest.fixture(scope="module")
def fifo_provider(fifo: MockFixture) -> MockWaveformProvider:
    return MockWaveformProvider(fifo.trace)


@pytest.fixture(scope="module")
def fifo_design() -> DesignGraph:
    return build_design_graph([FIFO_SV], root=REPO)


@pytest.fixture(scope="module")
def fifo_failure(fifo: MockFixture) -> FailureAnchor:
    assert fifo.comparison.failure is not None
    return fifo.comparison.failure


@pytest.fixture(scope="module")
def fifo_report(
    fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> CausalReport:
    options = SliceOptions(
        trace_scope=SCOPE,
        reset_path="tb.dut.rst_n",
        reset_active="0",
        case_id="mock_fifo_wrap",
    )
    return slice_failure(fifo_failure, fifo_provider, fifo_design, options)


# A design that exercises ?:, case, casez, last-write-wins, and a register enable.
DUT_SV = """\
module dut (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       sel,
  input  logic       en,
  input  logic [1:0] op,
  input  logic [3:0] a,
  input  logic [3:0] b,
  input  logic       c,
  input  logic       d,
  output logic [3:0] y,
  output logic [3:0] alu,
  output logic [3:0] dec,
  output logic [3:0] last,
  output logic [3:0] q
);
  logic [3:0] gated;
  assign gated = {4{c & d}} & b;
  assign y = sel ? a : gated;

  always_comb begin
    case (op)
      2'b00:   alu = a + b;
      2'b01:   alu = a - b;
      default: alu = a ^ b;
    endcase
  end

  always_comb begin
    dec = 4'd0;
    casez (a)
      4'b1???: dec = b;
      4'b01??: dec = gated;
      default: ;
    endcase
  end

  always_comb begin
    last = a;
    if (en) last = b;
  end

  always_ff @(posedge clk) begin
    if (!rst_n) q <= 4'd0;
    else if (en) q <= y;
  end
endmodule
"""

# Values consistent with DUT_SV for a = 1010, b = 0110, sel = en = c = d = 1, op = 00.
DUT_VALUES = {
    "rst_n": "1",
    "sel": "1",
    "en": "1",
    "op": "00",
    "a": "1010",
    "b": "0110",
    "c": "1",
    "d": "1",
    "gated": "0110",
    "y": "1010",
    "alu": "0000",
    "dec": "0110",
    "last": "0110",
    "q": "1010",
}


@pytest.fixture()
def dut(tmp_path: Path) -> DesignGraph:
    return build(tmp_path, DUT_SV)


def dut_provider(**overrides: str) -> MockWaveformProvider:
    return static_provider({**DUT_VALUES, **overrides})


# --------------------------------------------------------------------------
# Four-state evaluation
# --------------------------------------------------------------------------

_ATOMS = [
    "a", "b", "c", "s", "a[2:1]", "b[3]", "a[k]", "b[k[0] +: 2]", "b[k -: 2]", "c[2 -: 2]",
    "4'd3", "4'b1x0z", "2'sb11", "3'sd2", "$signed(a)", "$unsigned(c)", "{a, s}", "{2{s}}",
    "1'b1", "8'hf0", "4'sb1001",
]  # fmt: skip
_BINARY = [
    "+", "-", "*", "/", "%", "&", "|", "^", "~^", "==", "!=", "===", "!==", "==?", "!=?",
    "<", "<=", ">", ">=", "&&", "||", "<<", ">>", "<<<", ">>>", "**", "->", "<->",
]  # fmt: skip
_UNARY = ["~", "!", "-", "+", "&", "|", "^", "~&", "~|", "~^"]
_OPERANDS = [
    ("a", "logic [3:0]", 4, ""),
    ("b", "logic [3:0]", 4, ""),
    ("c", "logic signed [3:0]", 4, "s"),
    ("s", "logic [1:0]", 2, ""),
    ("k", "logic [1:0]", 2, ""),
]


def _random_expression(rng: random.Random, depth: int) -> str:
    roll = rng.random()
    if depth == 0 or roll < 0.25:
        return rng.choice(_ATOMS)
    if roll < 0.65:
        left, right = _random_expression(rng, depth - 1), _random_expression(rng, depth - 1)
        return f"({left} {rng.choice(_BINARY)} {right})"
    if roll < 0.8:
        return f"({rng.choice(_UNARY)}{_random_expression(rng, depth - 1)})"
    if roll < 0.93:
        parts = [_random_expression(rng, depth - 1) for _ in range(3)]
        return f"({parts[0]} ? {parts[1]} : {parts[2]})"
    return "{" + f"{_random_expression(rng, depth - 1)}, {_random_expression(rng, depth - 1)}" + "}"


def _slang_constants(text: str) -> dict[str, str]:
    """Values slang's constant evaluator gives the ``y*`` parameters of ``text``."""
    compilation = ast.Compilation()
    compilation.addSyntaxTree(syntax.SyntaxTree.fromText(text))
    errors = [str(d.code) for d in compilation.getAllDiagnostics() if d.isError()]
    assert not errors, f"reference module does not elaborate: {errors[:3]}"
    values: dict[str, str] = {}
    for member in compilation.getRoot().topInstances[0].body:
        if isinstance(member, ast.ParameterSymbol) and member.name.startswith("y"):
            value = member.value.value
            values[member.name] = "".join(
                str(value[index]) for index in reversed(range(value.bitWidth))
            )
    return values


@pytest.mark.parametrize("seed", [1, 2])
def test_evaluator_agrees_with_slang_constant_evaluation(tmp_path: Path, seed: int) -> None:
    """Differential test of lowering plus four-state evaluation.

    Random expressions over four-state operands are lowered from ``assign``
    statements and evaluated by this package. The same expressions, with the
    operands replaced by literals, are evaluated by slang as ``localparam``
    values. Select indices stay known because slang rejects an ``x`` index in a
    constant expression; that case has its own test below.

    slang is the oracle here, not the standard. In one known case they differ:
    slang 12.1 gives ``z`` for ``x ? z : z``, where IEEE 1800 Table 11-20 and
    this evaluator give ``x``. ``test_finding4_unknown_conditional_follows_ieee
    _table_11_20`` pins the standard, so a mismatch on that case must not be
    fixed by following slang.
    """
    rng = random.Random(seed)
    expressions = [_random_expression(rng, 3) for _ in range(80)]
    ports = ", ".join(f"input {type_} {name}" for name, type_, _, _ in _OPERANDS)
    outputs = "".join(f", output logic [7:0] y{i}" for i in range(len(expressions)))
    assigns = "".join(f"  assign y{i} = {text};\n" for i, text in enumerate(expressions))
    design = build(tmp_path, f"module ev({ports}{outputs});\n{assigns}endmodule\n", "ev.sv")
    assert not design.diagnostics, "every generated construct must be supported"

    checked = 0
    for trial in range(20):
        values = {
            name: "".join(
                rng.choice("01" if trial < 5 or name == "k" or rng.random() < 0.6 else "01xz")
                for _ in range(width)
            )
            for name, _, width, _ in _OPERANDS
        }
        params = "".join(
            f"  localparam {type_} {name} = {width}'{sign}b{values[name]};\n"
            for name, type_, width, sign in _OPERANDS
        )
        results = "".join(
            f"  localparam logic [7:0] y{i} = {text};\n" for i, text in enumerate(expressions)
        )
        expected = _slang_constants(f"module oracle;\n{params}{results}endmodule\n")
        for i, text in enumerate(expressions):
            rhs = design.drivers[f"y{i}"][0].assigns[0].rhs
            got = ir.evaluate(rhs, lambda signal: Vec(values[signal])).bits
            assert got == expected[f"y{i}"], f"{text} with {values}"
            checked += 1
    assert checked == 1600


def test_unknown_select_index_yields_unknown_bits(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m(input logic [3:0] a, input logic [1:0] k, output logic y, output logic [1:0] z);\n"
        "  assign y = a[k];\n  assign z = a[k +: 2];\nendmodule\n",
    )
    y = design.drivers["y"][0].assigns[0].rhs
    z = design.drivers["z"][0].assigns[0].rhs
    known = {"a": Vec("1010"), "k": Vec("01")}
    unknown = {"a": Vec("1010"), "k": Vec("x1")}
    assert ir.evaluate(y, known.__getitem__).bits == "1"
    assert ir.evaluate(z, known.__getitem__).bits == "01"
    assert ir.evaluate(y, unknown.__getitem__).bits == "x"
    assert ir.evaluate(z, unknown.__getitem__).bits == "xx"
    out_of_range = {"a": Vec("1010"), "k": Vec("11")}
    assert ir.evaluate(z, out_of_range.__getitem__).bits == "x1"


def test_unobserved_signal_evaluates_to_unknown_not_zero(tmp_path: Path) -> None:
    design = build(
        tmp_path, "module m(input logic a, b, output logic y);\n  assign y = a | b;\nendmodule\n"
    )
    rhs = design.drivers["y"][0].assigns[0].rhs
    assert ir.evaluate(rhs, {"a": Vec("0"), "b": None}.__getitem__).bits == "x"
    assert ir.evaluate(rhs, {"a": Vec("1"), "b": None}.__getitem__).bits == "1"


def test_unknown_ternary_merges_arms_instead_of_choosing_one() -> None:
    assert fs.merge(Vec("1100"), Vec("1010"), 4, False).bits == "1xx0"
    assert fs.merge(Vec("1z"), Vec("1z"), 2, False).bits == "1x"


@pytest.mark.parametrize(
    ("selector", "item", "flavor", "expected"),
    [
        ("01", "01", CaseFlavor.NORMAL, Tri.TRUE),
        ("01", "00", CaseFlavor.NORMAL, Tri.FALSE),
        ("0x", "00", CaseFlavor.NORMAL, Tri.UNKNOWN),
        ("1x", "00", CaseFlavor.NORMAL, Tri.FALSE),
        ("0z", "0z", CaseFlavor.NORMAL, Tri.UNKNOWN),
        ("1010", "1zzz", CaseFlavor.CASEZ, Tri.TRUE),
        ("z010", "1zzz", CaseFlavor.CASEZ, Tri.TRUE),
        ("x010", "1zzz", CaseFlavor.CASEZ, Tri.UNKNOWN),
        ("0010", "1zzz", CaseFlavor.CASEZ, Tri.FALSE),
        ("x010", "1xxx", CaseFlavor.CASEX, Tri.TRUE),
        ("x011", "1xx0", CaseFlavor.CASEX, Tri.FALSE),
    ],
)
def test_case_item_matching(selector: str, item: str, flavor: CaseFlavor, expected: Tri) -> None:
    assert fs.case_match(Vec(selector), Vec(item), flavor) is expected


# --------------------------------------------------------------------------
# Structural graph
# --------------------------------------------------------------------------


def test_fifo_signals_carry_widths_ranges_and_declaration_lines(fifo_design: DesignGraph) -> None:
    signals = fifo_design.signals
    assert fifo_design.top_module == "mock_fifo_wrap"
    assert signals["clk"].kind is SignalKind.INPUT and signals["clk"].bit_range is None
    assert signals["full"].kind is SignalKind.OUTPUT and signals["full"].width == 1
    wptr = signals["wptr"]
    assert (wptr.kind, wptr.width, wptr.bit_range) == (
        SignalKind.INTERNAL,
        3,
        BitRange(left=2, right=0),
    )
    assert wptr.span is not None and wptr.span.line == 16
    assert signals["in_data"].bit_range == BitRange(left=7, right=0)
    assert signals["mem"].is_aggregate


def test_fifo_register_driver_records_clock_guards_and_exact_lines(
    fifo_design: DesignGraph,
) -> None:
    (driver,) = fifo_design.drivers["wptr"]
    assert driver.edge_type is EdgeType.SEQUENTIAL
    assert driver.process is ProcessKind.ALWAYS_FF
    assert driver.clock is not None and (driver.clock.signal, driver.clock.rising) == ("clk", True)
    assert driver.unsupported is None
    lines = [assign.span.line for assign in driver.assigns if assign.span is not None]
    assert lines == [32, 38, 40]
    wrap = driver.assigns[1]
    assert wrap.text == "wptr <= {wptr[2], 2'b00}"
    assert [ir.describe_guard(guard) for guard in wrap.guards] == [
        "!(!rst_n)",
        "push",
        "wptr[1:0] == 2'b11",
    ]
    assert all(guard.kind is ir.GuardKind.IF for guard in wrap.guards)
    increment = driver.assigns[2]
    assert increment.guards[2].decision == wrap.guards[2].decision
    assert (wrap.guards[2].polarity, increment.guards[2].polarity) == (True, False)


def test_fifo_structural_edges_label_type_and_role(fifo_design: DesignGraph) -> None:
    def labels(source: str, target: str) -> set[tuple[EdgeType, EdgeRole]]:
        return {
            (edge.edge_type, edge.role)
            for edge in fifo_design.edges
            if edge.source == source and edge.target == target
        }

    assert labels("wptr", "full") == {(EdgeType.COMBINATIONAL, EdgeRole.DATA)}
    assert labels("in_valid", "push") == {(EdgeType.COMBINATIONAL, EdgeRole.DATA)}
    assert labels("push", "wptr") == {(EdgeType.SEQUENTIAL, EdgeRole.CONTROL)}
    assert labels("rst_n", "rptr") == {(EdgeType.SEQUENTIAL, EdgeRole.CONTROL)}
    assert labels("wptr", "wptr") == {
        (EdgeType.SEQUENTIAL, EdgeRole.DATA),
        (EdgeType.SEQUENTIAL, EdgeRole.CONTROL),
    }
    # The array read index and the array write index are selectors, not data.
    assert labels("rptr", "out_data") == {(EdgeType.COMBINATIONAL, EdgeRole.CONTROL)}
    assert labels("wptr", "mem") == {(EdgeType.SEQUENTIAL, EdgeRole.CONTROL)}
    assert labels("in_data", "mem") == {(EdgeType.SEQUENTIAL, EdgeRole.DATA)}
    assert labels("clk", "wptr") == set()

    sequential_targets = {
        edge.target for edge in fifo_design.edges if edge.edge_type is EdgeType.SEQUENTIAL
    }
    assert sequential_targets == {"wptr", "rptr", "mem"}
    wrap_data = [
        edge
        for edge in fifo_design.fanin("wptr")
        if edge.role is EdgeRole.DATA and edge.span is not None and edge.span.line == 38
    ]
    assert [ir.describe_guard(guard) for guard in wrap_data[0].guards][-1] == "wptr[1:0] == 2'b11"
    assert {edge.target for edge in fifo_design.fanout("pop")} == {"in_ready", "rptr"}


def test_source_file_is_identified_by_relative_path_and_real_hash(
    fifo_design: DesignGraph,
) -> None:
    (source,) = fifo_design.sources
    assert source.path == "tests/fixtures/mock_fifo_wrap.sv"
    assert source.sha256 == hashlib.sha256(FIFO_SV.read_bytes()).hexdigest()


def test_case_and_ternary_decisions_become_guards(dut: DesignGraph) -> None:
    (alu,) = dut.drivers["alu"]
    assert alu.edge_type is EdgeType.COMBINATIONAL and alu.process is ProcessKind.ALWAYS_COMB
    kinds = [assign.guards[0].kind for assign in alu.assigns]
    assert kinds == [ir.GuardKind.CASE_ITEM, ir.GuardKind.CASE_ITEM, ir.GuardKind.CASE_DEFAULT]
    assert len({assign.guards[0].decision for assign in alu.assigns}) == 1
    default = alu.assigns[2].guards[0]
    assert [ir.to_sv(item) for item in default.earlier] == ["2'b00", "2'b01"]

    (dec,) = dut.drivers["dec"]
    assert dec.assigns[0].guards == ()
    assert dec.assigns[1].guards[0].flavor is CaseFlavor.CASEZ
    assert ir.to_sv(dec.assigns[1].guards[0].items[0]) == "4'b1zzz"

    y_edges = {(edge.source, edge.role): edge for edge in dut.fanin("y")}
    assert y_edges[("sel", EdgeRole.CONTROL)].guards == ()
    true_arm = y_edges[("a", EdgeRole.DATA)].guards
    false_arm = y_edges[("gated", EdgeRole.DATA)].guards
    assert [(g.kind, g.polarity) for g in true_arm] == [(ir.GuardKind.TERNARY, True)]
    assert [(g.kind, g.polarity) for g in false_arm] == [(ir.GuardKind.TERNARY, False)]


def test_always_forms_are_classified_by_their_event_control(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m(input logic clk, a, b, output logic c1, c2, s1, s2);\n"
        "  always @* c1 = a & b;\n"
        "  always @(a or b) c2 = a | b;\n"
        "  always @(posedge clk) s1 <= a;\n"
        "  always @(negedge clk) s2 <= b;\n"
        "endmodule\n",
    )
    assert design.drivers["c1"][0].edge_type is EdgeType.COMBINATIONAL
    assert design.drivers["c2"][0].edge_type is EdgeType.COMBINATIONAL
    assert design.drivers["c2"][0].caveats
    rising, falling = design.drivers["s1"][0].clock, design.drivers["s2"][0].clock
    assert rising is not None and rising.rising
    assert falling is not None and not falling.rising


def test_unsupported_constructs_are_reported_and_never_dropped(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module sub(input logic i, output logic o);\n  assign o = ~i;\nendmodule\n"
        "module top(input logic clk, rst_n, en, input logic [3:0] a,\n"
        "           output logic [3:0] cnt, loop, output logic q, lat, inst_out);\n"
        "  sub u_sub(.i(a[0]), .o(inst_out));\n"
        "  always_ff @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) q <= 1'b0; else q <= a[0];\n"
        "  always_ff @(posedge clk) cnt += 4'd1;\n"
        "  always_comb for (int i = 0; i < 4; i++) loop[i] = a[3 - i] & en;\n"
        "  always_latch if (en) lat = a[1];\n"
        "endmodule\n",
        "top.sv",
    )
    reported = {diagnostic.code for diagnostic in design.diagnostics}
    assert {"UNSUPPORTED_INSTANCE", "UNSUPPORTED_STATEMENT"} <= reported
    for signal, fragment in [
        ("inst_out", "instance u_sub"),
        ("q", "asynchronous reset"),
        ("cnt", "blocking assignment"),
        ("lat", "always_latch"),
    ]:
        (driver,) = design.drivers[signal]
        assert driver.unsupported is not None and fragment in driver.unsupported
    # The loop body is not modeled, but its target keeps a dependence on what it reads.
    (loop,) = design.drivers["loop"]
    assert loop.unsupported is None
    assert not loop.assigns[0].full_write
    assert set(ir.signals_of(loop.assigns[0].rhs)) == {"a", "en"}
    assert loop.assigns[0].guards[-1].kind is ir.GuardKind.UNSUPPORTED
    assert {edge.source for edge in design.fanin("loop")} == {"a", "en"}


def test_read_of_an_intermediate_blocking_value_is_flagged(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m(input logic a, b, output logic y, t);\n"
        "  always_comb begin\n    t = a;\n    y = t;\n    t = b;\n  end\nendmodule\n",
    )
    (y,) = design.drivers["y"]
    assert len(y.caveats) == 1 and "intermediate" in y.caveats[0]
    (t,) = design.drivers["t"]
    assert t.caveats == ()


def test_net_initializer_concatenated_target_and_constants(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m #(parameter logic [3:0] K = 4'hA) (input logic [3:0] a, b,\n"
        "    output logic hi, lo, output logic [3:0] k);\n"
        "  typedef enum logic [1:0] {IDLE, BUSY} state_t;\n"
        "  typedef struct packed { logic [1:0] upper; logic [1:0] lower; } pair_t;\n"
        "  pair_t p;\n"
        "  wire [3:0] n = a & b;\n"
        "  assign p = a;\n"
        "  assign {hi, lo} = n[1:0];\n"
        "  assign k = (p.upper == BUSY) ? K : 4'd0;\n"
        "endmodule\n",
    )
    assert not design.diagnostics
    assert {edge.source for edge in design.fanin("n")} == {"a", "b"}
    hi = design.drivers["hi"][0].assigns[0].rhs
    lo = design.drivers["lo"][0].assigns[0].rhs
    values = {"n": Vec("0110"), "p": Vec("0111")}
    assert ir.evaluate(hi, values.__getitem__).bits == "1"
    assert ir.evaluate(lo, values.__getitem__).bits == "0"
    k = design.drivers["k"][0].assigns[0].rhs
    assert ir.signals_of(k) == ("p",)
    assert ir.evaluate(k, values.__getitem__).bits == "1010"
    assert ir.evaluate(k, {"p": Vec("1011")}.__getitem__).bits == "0000"


def test_design_errors_are_raised_not_swallowed(tmp_path: Path) -> None:
    good = tmp_path / "good.sv"
    good.write_text("module good(input logic a, output logic y);\n  assign y = a;\nendmodule\n")
    bad = tmp_path / "bad.sv"
    bad.write_text("module bad(input logic a, output logic y);\n  assign y = nope;\nendmodule\n")
    with pytest.raises(DesignError, match="elaboration failed"):
        build_design_graph([bad], root=tmp_path)
    with pytest.raises(DesignError, match="not found"):
        build_design_graph([tmp_path / "missing.sv"], root=tmp_path)
    with pytest.raises(DesignError, match="top module 'other' not found"):
        build_design_graph([good], root=tmp_path, top="other")
    with pytest.raises(DesignError, match="not inside the source root"):
        build_design_graph([good], root=tmp_path / "elsewhere")
    with pytest.raises(DesignError, match="no source files"):
        build_design_graph([], root=tmp_path)


# --------------------------------------------------------------------------
# Backward slice of the mock FIFO failure
# --------------------------------------------------------------------------


def test_fifo_report_validates_and_round_trips(fifo_report: CausalReport) -> None:
    assert fifo_report.status is AnalysisStatus.FAIL
    assert fifo_report.provenance.kind is ProvenanceKind.ANALYSIS
    assert fifo_report.provenance.case_id == "mock_fifo_wrap"
    assert CausalReport.model_validate_json(fifo_report.model_dump_json()) == fifo_report
    assert not fifo_report.truncated
    assert codes(fifo_report) == set()


def test_fifo_anchor_and_every_node_reproduce_from_the_provider(
    fifo_report: CausalReport, fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider
) -> None:
    anchor = node_of(fifo_report, "full", 3, SETTLED)
    assert fifo_report.anchor_node_id == anchor.id
    assert anchor.observation == fifo_failure.observation
    assert anchor.value == fifo_failure.actual == lv("0")
    for node in fifo_report.nodes:
        observed = fifo_provider.get_value_at(
            node.signal_path, node.observation.time_ps, phase=node.observation.phase
        )
        assert node.value == observed, node.id


def test_fifo_observations_use_the_real_clock_edges(
    fifo_report: CausalReport, fifo_provider: MockWaveformProvider
) -> None:
    edges = fifo_provider.get_clock_edges("tb.dut.clk", 0, 55001)
    epoch_edges = edges[-4:]
    assert epoch_edges == (25000, 35000, 45000, 55000)
    for node in fifo_report.nodes:
        assert node.observation.epoch == 0
        assert node.observation.time_ps == epoch_edges[node.observation.cycle]
    assert {node.observation.cycle for node in fifo_report.nodes} == {0, 1, 2, 3}


def test_fifo_wrap_arm_is_retained_and_increment_arm_is_pruned(
    fifo_report: CausalReport,
) -> None:
    at_wrap = edges_of(fifo_report, ("wptr", 3, BEFORE), ("wptr", 3, SETTLED), EdgeRole.DATA)
    assert all(edge.edge_type is EdgeType.SEQUENTIAL for edge in at_wrap)
    by_branch = {edge.branch: edge.reason for edge in at_wrap}
    assert set(by_branch) == {BranchStatus.RETAINED, BranchStatus.PRUNED}
    assert "wptr <= {wptr[2], 2'b00}" in by_branch[BranchStatus.RETAINED]
    assert "wptr <= wptr + 3'd1" in by_branch[BranchStatus.PRUNED]
    assert "is false" in by_branch[BranchStatus.PRUNED]

    # One cycle earlier the address bits are 10, so the roles swap.
    earlier = edges_of(fifo_report, ("wptr", 2, BEFORE), ("wptr", 2, SETTLED), EdgeRole.DATA)
    swapped = {edge.branch: edge.reason for edge in earlier}
    assert "wptr <= wptr + 3'd1" in swapped[BranchStatus.RETAINED]
    assert "wptr <= {wptr[2], 2'b00}" in swapped[BranchStatus.PRUNED]

    controls = edges_of(fifo_report, ("push", 3, BEFORE), ("wptr", 3, SETTLED), EdgeRole.CONTROL)
    assert branches(controls) == {BranchStatus.RETAINED}
    assert "`push` is 1'b1" in controls[0].reason


def test_fifo_held_register_holds_and_carries_across_cycles(fifo_report: CausalReport) -> None:
    hold = edges_of(fifo_report, ("rptr", 3, BEFORE), ("rptr", 3, SETTLED), EdgeRole.DATA)
    reasons = {edge.branch: edge.reason for edge in hold}
    assert "holds its value" in reasons[BranchStatus.RETAINED]
    assert "rptr <= rptr + 3'd1" in reasons[BranchStatus.PRUNED]
    blocked_by = edges_of(fifo_report, ("pop", 3, BEFORE), ("rptr", 3, SETTLED))
    assert [(edge.role, edge.branch) for edge in blocked_by] == [
        (EdgeRole.CONTROL, BranchStatus.RETAINED)
    ]

    # rptr never changed in the epoch: one carry edge spans cycles 0..3.
    (carry,) = edges_of(fifo_report, ("rptr", 0, SETTLED), ("rptr", 3, BEFORE))
    assert carry.edge_type is EdgeType.COMBINATIONAL and carry.reason.startswith("Carry")
    assert not any(
        node.signal_path == "tb.dut.rptr"
        and node.observation.phase is SETTLED
        and node.observation.cycle in (1, 2)
        for node in fifo_report.nodes
    )
    # wptr was written every cycle, so it carries exactly one cycle.
    (step,) = edges_of(fifo_report, ("wptr", 2, SETTLED), ("wptr", 3, BEFORE))
    assert step.edge_type is EdgeType.COMBINATIONAL
    assert node_of(fifo_report, "wptr", 2, SETTLED).value == lv("011")


def test_fifo_slice_ends_at_inputs_and_the_reset_boundary(fifo_report: CausalReport) -> None:
    targets = {edge.target for edge in fifo_report.edges}
    leaves = {
        node.signal_path.removeprefix("tb.dut.")
        for node in fifo_report.nodes
        if node.id not in targets
    }
    assert leaves == {"rst_n", "in_valid", "out_ready", "wptr", "rptr"}
    for register in ("wptr", "rptr"):
        boundary = node_of(fifo_report, register, 0, BEFORE)
        assert boundary.id not in targets and boundary.value == lv("000")
    assert not has_node(fifo_report, "mem") and not has_node(fifo_report, "in_data")


def test_fifo_top_suspect_is_the_wrap_assignment(fifo_report: CausalReport) -> None:
    suspects = fifo_report.suspects
    assert [suspect.rank for suspect in suspects] == list(range(1, len(suspects) + 1))
    assert len(suspects) == 10
    top = suspects[0]
    assert top.node_id == node_of(fifo_report, "wptr", 3, SETTLED).id
    assert top.location is not None and (top.location.line, top.location.end_line) == (38, 38)
    assert "changed tb.dut.wptr from 3'b011 to 3'b000" in top.reason
    assert top.evidence_node_ids == (top.node_id, fifo_report.anchor_node_id)

    lines = [suspect.location.line for suspect in suspects if suspect.location is not None]
    assert lines[:4] == [38, 23, 42, 40]
    assert len(set(lines)) == len(lines), "one suspect per source location"
    # Module inputs are where the slice ends, so they rank after every RTL statement.
    assert lines[-1] == 6 and "module input" in suspects[-1].reason
    for suspect in suspects:
        assert suspect.evidence_node_ids[0] == suspect.node_id
        assert suspect.evidence_node_ids[-1] == fifo_report.anchor_node_id


def test_fifo_suspects_point_at_real_hashed_source(fifo_report: CausalReport) -> None:
    (source,) = fifo_report.sources
    text = (REPO / source.path).read_bytes()
    assert hashlib.sha256(text).hexdigest() == source.sha256
    lines = text.decode("utf-8").splitlines()
    top = fifo_report.suspects[0]
    assert top.location is not None and top.location.source_id == source.id
    assert lines[top.location.line - 1].strip() == "wptr <= {wptr[2], 2'b00};"
    anchor = node_of(fifo_report, "full", 3, SETTLED)
    assert anchor.location is not None
    assert lines[anchor.location.line - 1].strip().startswith("assign full")


def test_slice_is_deterministic(
    fifo_report: CausalReport,
    fifo_failure: FailureAnchor,
    fifo_provider: MockWaveformProvider,
    fifo_design: DesignGraph,
) -> None:
    options = SliceOptions(
        trace_scope=SCOPE, reset_path="tb.dut.rst_n", reset_active="0", case_id="mock_fifo_wrap"
    )
    rebuilt = build_design_graph([FIFO_SV], root=REPO)
    again = slice_failure(fifo_failure, fifo_provider, rebuilt, options)
    assert again == fifo_report
    assert again.model_dump_json() == fifo_report.model_dump_json()
    assert slice_failure(fifo_failure, fifo_provider, fifo_design, options) == fifo_report


def test_fifo_waveform_window_covers_the_slice(fifo_report: CausalReport) -> None:
    window = fifo_report.waveform
    assert window is not None
    assert (window.start_ps, window.end_ps) == (15000, 55001)
    paths = [signal.signal_path for signal in window.signals]
    assert paths[0] == "tb.dut.clk" and "tb.dut.wptr" in paths and "tb.dut.mem" not in paths


def test_unobserved_array_is_reported_and_its_writes_are_kept(
    fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> None:
    failure = failure_at("out_data", 4, 65000, actual="01010101", expected="00100010")
    report = slice_failure(failure, fifo_provider, fifo_design, OPTIONS)
    assert {"SIGNAL_NOT_OBSERVED", "UNSUPPORTED_OPERAND"} <= codes(report)
    memory = node_of(report, "mem", 4, SETTLED)
    assert memory.value is None
    (read,) = edges_of(report, ("mem", 4, SETTLED), ("out_data", 4, SETTLED))
    assert (read.role, read.branch) == (EdgeRole.DATA, BranchStatus.RETAINED)
    (index,) = edges_of(report, ("rptr", 4, SETTLED), ("out_data", 4, SETTLED))
    assert index.role is EdgeRole.CONTROL
    # The element written is not resolved, so the write and the hold are both kept.
    write = edges_of(report, ("in_data", 4, BEFORE), ("mem", 4, SETTLED), EdgeRole.DATA)
    hold = edges_of(report, ("mem", 4, BEFORE), ("mem", 4, SETTLED), EdgeRole.DATA)
    assert branches(write) == branches(hold) == {BranchStatus.RETAINED}


def test_expand_holds_steps_back_one_cycle_at_a_time(
    fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> None:
    options = SliceOptions(trace_scope=SCOPE, expand_holds=True)
    report = slice_failure(fifo_failure, fifo_provider, fifo_design, options)
    for cycle in (3, 2, 1):
        (carry,) = edges_of(report, ("rptr", cycle - 1, SETTLED), ("rptr", cycle, BEFORE))
        assert carry.reason.startswith("Carry")
        hold = edges_of(report, ("pop", cycle - 1, BEFORE), ("rptr", cycle - 1, SETTLED))
        assert branches(hold) == {BranchStatus.RETAINED}


def test_depth_limit_stops_the_slice_and_sets_truncated(
    fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> None:
    options = SliceOptions(trace_scope=SCOPE, max_cycles_back=1)
    report = slice_failure(fifo_failure, fifo_provider, fifo_design, options)
    assert report.truncated and "SLICE_DEPTH_LIMIT" in codes(report)
    assert min(node.observation.cycle for node in report.nodes) == 2
    assert CausalReport.model_validate_json(report.model_dump_json()) == report
    everything = SliceOptions(trace_scope=SCOPE, max_cycles_back=1, max_suspects=None)
    full = slice_failure(fifo_failure, fifo_provider, fifo_design, everything)
    assert any("slice stops" in suspect.reason for suspect in full.suspects)


def test_node_limit_stops_the_slice_and_sets_truncated(
    fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> None:
    report = slice_failure(
        fifo_failure, fifo_provider, fifo_design, SliceOptions(trace_scope=SCOPE, max_nodes=6)
    )
    assert report.truncated and "SLICE_NODE_LIMIT" in codes(report)
    assert len(report.nodes) < 20


def test_pruned_edges_can_be_left_out_of_the_report(
    fifo_report: CausalReport,
    fifo_failure: FailureAnchor,
    fifo_provider: MockWaveformProvider,
    fifo_design: DesignGraph,
) -> None:
    lean = slice_failure(
        fifo_failure,
        fifo_provider,
        fifo_design,
        SliceOptions(trace_scope=SCOPE, include_pruned=False),
    )
    assert any(edge.branch is BranchStatus.PRUNED for edge in fifo_report.edges)
    assert not any(edge.branch is BranchStatus.PRUNED for edge in lean.edges)
    kept = [edge for edge in fifo_report.edges if edge.branch is not BranchStatus.PRUNED]
    assert len(lean.edges) == len(kept)
    assert [s.location for s in lean.suspects] == [s.location for s in fifo_report.suspects]


# --------------------------------------------------------------------------
# Value pruning on a design with ?:, case, casez, and a register enable
# --------------------------------------------------------------------------


def test_false_ternary_arm_is_pruned_and_not_followed(dut: DesignGraph) -> None:
    report = slice_failure(
        failure_at("y", 2, 25000, actual="1010", expected="0101"), dut_provider(), dut, OPTIONS
    )
    y = ("y", 2, SETTLED)
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.RETAINED}
    (control,) = edges_of(report, ("sel", 2, SETTLED), y)
    assert (control.role, control.branch) == (EdgeRole.CONTROL, BranchStatus.RETAINED)
    (pruned,) = edges_of(report, ("gated", 2, SETTLED), y)
    assert pruned.branch is BranchStatus.PRUNED and "not selected" in pruned.reason
    # The pruned operand is shown with its value, but its own cone is not explored.
    gated = node_of(report, "gated", 2, SETTLED)
    assert gated.value == lv("0110")
    assert not has_node(report, "b") and not has_node(report, "c") and not has_node(report, "d")
    assert all(gated.id not in suspect.evidence_node_ids for suspect in report.suspects)
    assert all(gated.id != suspect.node_id for suspect in report.suspects)


def test_unknown_ternary_control_keeps_and_follows_both_arms(dut: DesignGraph) -> None:
    provider = dut_provider(sel="x", y="xx10")
    report = slice_failure(
        failure_at("y", 2, 25000, actual="xx10", expected="1010"), provider, dut, OPTIONS
    )
    y = ("y", 2, SETTLED)
    assert report.failure is not None and report.failure.kind is FailureKind.UNKNOWN_VALUE
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert branches(edges_of(report, ("gated", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert node_of(report, "sel", 2, SETTLED).value == lv("x")
    # The false arm is followed: its cone is in the graph.
    gated = ("gated", 2, SETTLED)
    for operand in ("b", "c", "d"):
        assert branches(edges_of(report, (operand, 2, SETTLED), gated)) == {BranchStatus.RETAINED}
    assert not any(edge.branch is BranchStatus.PRUNED for edge in report.edges)


def test_control_signal_missing_from_the_trace_keeps_both_arms(dut: DesignGraph) -> None:
    values = {name: bits for name, bits in DUT_VALUES.items() if name != "sel"}
    report = slice_failure(
        failure_at("y", 2, 25000, actual="1010", expected="0101"),
        static_provider(values),
        dut,
        OPTIONS,
    )
    y = ("y", 2, SETTLED)
    assert "SIGNAL_NOT_OBSERVED" in codes(report)
    assert node_of(report, "sel", 2, SETTLED).value is None
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert branches(edges_of(report, ("gated", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert has_node(report, "c") and has_node(report, "d")
    assert CausalReport.model_validate_json(report.model_dump_json()) == report


def test_case_statement_follows_only_the_matching_arm(dut: DesignGraph) -> None:
    provider = dut_provider(op="01", alu="0100")
    report = slice_failure(
        failure_at("alu", 2, 25000, actual="0100", expected="0000"), provider, dut, OPTIONS
    )
    alu = ("alu", 2, SETTLED)
    for operand in ("a", "b"):
        by_branch = {
            edge.branch: edge.reason for edge in edges_of(report, (operand, 2, SETTLED), alu)
        }
        assert set(by_branch) == {BranchStatus.RETAINED, BranchStatus.PRUNED}
        assert "alu = a - b" in by_branch[BranchStatus.RETAINED]
        assert "alu = a + b" in by_branch[BranchStatus.PRUNED]
        assert "alu = a ^ b" in by_branch[BranchStatus.PRUNED]
    (selector,) = edges_of(report, ("op", 2, SETTLED), alu)
    assert (selector.role, selector.branch) == (EdgeRole.CONTROL, BranchStatus.RETAINED)
    assert report.suspects[0].location is not None and report.suspects[0].location.line == 24


@pytest.mark.parametrize(
    ("op", "retained", "unknown", "pruned"),
    [
        # Bit 1 is 1, so neither 00 nor 01 can match: the default arm is certain.
        ("1x", {"alu = a ^ b"}, set(), {"alu = a + b", "alu = a - b"}),
        # Bit 0 is 1, so 00 cannot match; 01 and the default both remain feasible.
        ("x1", set(), {"alu = a - b", "alu = a ^ b"}, {"alu = a + b"}),
        ("xx", set(), {"alu = a + b", "alu = a - b", "alu = a ^ b"}, set()),
    ],
)
def test_partly_unknown_case_selector_keeps_exactly_the_feasible_arms(
    dut: DesignGraph, op: str, retained: set[str], unknown: set[str], pruned: set[str]
) -> None:
    provider = dut_provider(op=op, alu="xxxx")
    report = slice_failure(
        failure_at("alu", 2, 25000, actual="xxxx", expected="0000"), provider, dut, OPTIONS
    )
    edges = edges_of(report, ("a", 2, SETTLED), ("alu", 2, SETTLED), EdgeRole.DATA)

    def statements(branch: BranchStatus) -> set[str]:
        reasons = " ".join(edge.reason for edge in edges if edge.branch is branch)
        return {text for text in ("alu = a + b", "alu = a - b", "alu = a ^ b") if text in reasons}

    assert statements(BranchStatus.RETAINED) == retained
    assert statements(BranchStatus.UNKNOWN) == unknown
    assert statements(BranchStatus.PRUNED) == pruned
    (selector,) = edges_of(report, ("op", 2, SETTLED), ("alu", 2, SETTLED))
    assert selector.branch is BranchStatus.UNKNOWN


def test_casez_wildcard_arm_wins_by_priority(dut: DesignGraph) -> None:
    report = slice_failure(
        failure_at("dec", 2, 25000, actual="0110", expected="0000"), dut_provider(), dut, OPTIONS
    )
    dec = ("dec", 2, SETTLED)
    (taken,) = edges_of(report, ("b", 2, SETTLED), dec)
    assert taken.branch is BranchStatus.RETAINED and "dec = b" in taken.reason
    (skipped,) = edges_of(report, ("gated", 2, SETTLED), dec)
    assert skipped.branch is BranchStatus.PRUNED
    (selector,) = edges_of(report, ("a", 2, SETTLED), dec)
    assert (selector.role, selector.branch) == (EdgeRole.CONTROL, BranchStatus.RETAINED)
    assert not has_node(report, "c")


@pytest.mark.parametrize(
    ("en", "last", "a_branch", "b_branch", "en_branch"),
    [
        ("1", "0110", BranchStatus.PRUNED, BranchStatus.RETAINED, BranchStatus.RETAINED),
        ("0", "1010", BranchStatus.RETAINED, BranchStatus.PRUNED, BranchStatus.RETAINED),
        ("x", "xx10", BranchStatus.RETAINED, BranchStatus.UNKNOWN, BranchStatus.UNKNOWN),
    ],
)
def test_last_executed_assignment_wins(
    dut: DesignGraph,
    en: str,
    last: str,
    a_branch: BranchStatus,
    b_branch: BranchStatus,
    en_branch: BranchStatus,
) -> None:
    provider = dut_provider(en=en, last=last)
    report = slice_failure(
        failure_at("last", 2, 25000, actual=last, expected="0000"), provider, dut, OPTIONS
    )
    target = ("last", 2, SETTLED)
    (from_a,) = edges_of(report, ("a", 2, SETTLED), target)
    (from_b,) = edges_of(report, ("b", 2, SETTLED), target)
    (from_en,) = edges_of(report, ("en", 2, SETTLED), target)
    assert (from_a.branch, from_b.branch, from_en.branch) == (a_branch, b_branch, en_branch)
    if en == "1":
        assert "overwritten by the later assignment" in from_a.reason


def test_enabled_register_follows_its_data_across_the_clock_edge(dut: DesignGraph) -> None:
    report = slice_failure(
        failure_at("q", 2, 25000, actual="1010", expected="0000"), dut_provider(), dut, OPTIONS
    )
    q = ("q", 2, SETTLED)
    (data,) = edges_of(report, ("y", 2, BEFORE), q)
    assert (data.edge_type, data.role, data.branch) == (
        EdgeType.SEQUENTIAL,
        EdgeRole.DATA,
        BranchStatus.RETAINED,
    )
    assert node_of(report, "y", 2, BEFORE).observation.time_ps == 25000
    q_id = node_of(report, "q", 2, SETTLED).id
    assert not any(edge.target == q_id and "holds" in edge.reason for edge in report.edges)
    # The operands of y are read in the same pre-edge observation.
    assert branches(edges_of(report, ("a", 2, BEFORE), ("y", 2, BEFORE))) == {BranchStatus.RETAINED}


def test_disabled_register_holds_and_carries_to_the_epoch_boundary(dut: DesignGraph) -> None:
    provider = dut_provider(en="0", q="0011", last="1010")
    report = slice_failure(
        failure_at("q", 2, 25000, actual="0011", expected="1010"), provider, dut, OPTIONS
    )
    q = ("q", 2, SETTLED)
    (hold,) = edges_of(report, ("q", 2, BEFORE), q)
    assert hold.branch is BranchStatus.RETAINED and "holds its value" in hold.reason
    (data,) = edges_of(report, ("y", 2, BEFORE), q)
    assert data.branch is BranchStatus.PRUNED
    (carry,) = edges_of(report, ("q", 0, SETTLED), ("q", 2, BEFORE))
    assert carry.edge_type is EdgeType.COMBINATIONAL
    boundary = node_of(report, "q", 0, BEFORE)
    assert boundary.id not in {edge.target for edge in report.edges}
    assert not report.truncated
    held = report.suspects[0]
    assert "held 4'b0011" in held.reason
    assert held.location is not None and (held.location.line, held.location.end_line) == (45, 45)


def test_unknown_enable_keeps_both_the_write_and_the_hold(dut: DesignGraph) -> None:
    provider = dut_provider(en="x", q="xxxx", last="xx10")
    report = slice_failure(
        failure_at("q", 2, 25000, actual="xxxx", expected="1010"), provider, dut, OPTIONS
    )
    q = ("q", 2, SETTLED)
    (write,) = edges_of(report, ("y", 2, BEFORE), q)
    (hold,) = edges_of(report, ("q", 2, BEFORE), q)
    (enable,) = edges_of(report, ("en", 2, BEFORE), q)
    assert write.branch is hold.branch is enable.branch is BranchStatus.UNKNOWN
    assert "may execute" in write.reason and "may hold" in hold.reason
    assert has_node(report, "a")


def test_before_phase_anchor_stays_in_the_pre_edge_observation(dut: DesignGraph) -> None:
    failure = failure_at("y", 2, 25000, actual="1010", expected="0101", phase=BEFORE)
    report = slice_failure(failure, dut_provider(), dut, OPTIONS)
    assert {node.observation.phase for node in report.nodes} == {BEFORE}
    assert {node.observation.cycle for node in report.nodes} == {2}
    assert branches(edges_of(report, ("a", 2, BEFORE), ("y", 2, BEFORE))) == {BranchStatus.RETAINED}


PIPE_SV = """\
module pipe (input logic clk, input logic d, output logic q3);
  logic q1, q2;
  always_ff @(posedge clk) begin
    q1 <= d;
    q2 <= q1;
    q3 <= q2;
  end
endmodule
"""


def test_paused_clock_is_followed_through_its_real_edges(tmp_path: Path) -> None:
    """Cycles 0, 1, 2 are 7 ns and then 18 ns apart; no period is assumed."""
    design = build(tmp_path, PIPE_SV, "pipe.sv")
    edges = (5000, 12000, 30000)
    provider = static_provider(
        {},
        changing={
            "d": [(0, "1"), (6000, "0")],
            "q1": [(0, "x"), (5000, "1"), (12000, "0")],
            "q2": [(0, "x"), (12000, "1"), (30000, "0")],
            "q3": [(0, "x"), (30000, "1")],
        },
        edges=edges,
    )
    report = slice_failure(
        failure_at("q3", 2, 30000, actual="1", expected="0"), provider, design, OPTIONS
    )
    assert not report.truncated and codes(report) == set()
    chain: list[tuple[str, int, SamplingPhase, int, str]] = [
        ("q3", 2, SETTLED, 30000, "1"),
        ("q2", 2, BEFORE, 30000, "1"),
        ("q2", 1, SETTLED, 12000, "1"),
        ("q1", 1, BEFORE, 12000, "1"),
        ("q1", 0, SETTLED, 5000, "1"),
        ("d", 0, BEFORE, 5000, "1"),
    ]
    assert len(report.nodes) == len(chain)
    for signal, cycle, phase, time_ps, bits in chain:
        node = node_of(report, signal, cycle, phase)
        assert (node.observation.time_ps, node.value) == (time_ps, lv(bits))
    kinds = [
        edges_of(report, chain[i + 1][:3], chain[i][:3])[0].edge_type for i in range(len(chain) - 1)
    ]
    sequential, carry = EdgeType.SEQUENTIAL, EdgeType.COMBINATIONAL
    assert kinds == [sequential, carry, sequential, carry, sequential]
    # d had already fallen by the cycle 1 and cycle 2 edges; only the real cycle 0 edge sees 1.
    assert provider.get_value_at("tb.dut.d", 12000, phase=BEFORE) == lv("0")


def test_unsupported_register_driver_ends_the_slice_with_a_diagnostic(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module dut(input logic clk, rst_n, d, output logic q, y);\n"
        "  always_ff @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) q <= 1'b0; else q <= d;\n"
        "  assign y = q;\n"
        "endmodule\n",
    )
    provider = static_provider({"rst_n": "1", "d": "1", "q": "1", "y": "1"})
    report = slice_failure(
        failure_at("y", 2, 25000, actual="1", expected="0"), provider, design, OPTIONS
    )
    assert report.truncated and "UNSUPPORTED_DRIVER" in codes(report)
    assert has_node(report, "q") and not has_node(report, "d")
    message = next(d.message for d in report.diagnostics if d.code == "UNSUPPORTED_DRIVER")
    assert "asynchronous reset" in message
    stopped = [s for s in report.suspects if "slice stops at tb.dut.q" in s.reason]
    assert len(stopped) == 1


def test_combinational_loop_is_reported_and_does_not_hang(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module dut(input logic clk, s, r, output logic q, qn);\n"
        "  assign q = ~(r | qn);\n  assign qn = ~(s | q);\nendmodule\n",
    )
    provider = static_provider({"s": "0", "r": "0", "q": "1", "qn": "0"})
    report = slice_failure(
        failure_at("q", 2, 25000, actual="1", expected="0"), provider, design, OPTIONS
    )
    assert "COMBINATIONAL_LOOP" in codes(report) and not report.truncated
    message = next(d.message for d in report.diagnostics if d.code == "COMBINATIONAL_LOOP")
    assert "tb.dut.q, tb.dut.qn" in message
    assert edges_of(report, ("qn", 2, SETTLED), ("q", 2, SETTLED))
    assert edges_of(report, ("q", 2, SETTLED), ("qn", 2, SETTLED))
    assert {node.signal_path for node in report.nodes} == {
        "tb.dut.q",
        "tb.dut.qn",
        "tb.dut.r",
        "tb.dut.s",
    }


def test_dump_gap_confines_the_slice_to_the_failing_cycle(dut: DesignGraph) -> None:
    """Clock edges across a $dumpoff gap cannot be established, so no cycle is assumed."""
    values = {**DUT_VALUES, "en": "0", "q": "0011", "last": "1010"}
    signals: dict[str, Sequence[tuple[int, str]]] = {
        "clk": [(0, "0"), (5000, "1"), (5500, "0"), (9000, "0"), (15000, "1"), (17000, "0"),
                (25000, "1"), (27000, "0")],
    }  # fmt: skip
    signals.update({name: [(0, bits), (9000, bits)] for name, bits in values.items()})
    provider = MockWaveformProvider(make_trace(30000, signals, unavailable=[(6000, 9000)]))
    report = slice_failure(
        failure_at("q", 2, 25000, actual="0011", expected="1010"), provider, dut, OPTIONS
    )
    assert report.truncated
    assert {"CLOCK_EDGES_UNAVAILABLE", "SLICE_TIME_LIMIT"} <= codes(report)
    assert {node.observation.cycle for node in report.nodes} == {2}
    held = edges_of(report, ("q", 2, BEFORE), ("q", 2, SETTLED))
    assert branches(held) == {BranchStatus.RETAINED}
    assert CausalReport.model_validate_json(report.model_dump_json()) == report


# --------------------------------------------------------------------------
# Failures that cannot be localized, and non-fail comparisons
# --------------------------------------------------------------------------


def test_anchor_outside_the_design_gives_an_explained_empty_graph(dut: DesignGraph) -> None:
    failure = failure_at("y", 2, 25000, actual="1010", expected="0101")
    report = slice_failure(failure, dut_provider(), dut, SliceOptions(trace_scope="tb.other"))
    assert report.status is AnalysisStatus.FAIL and report.failure == failure
    assert not report.nodes and not report.suspects and report.anchor_node_id is None
    assert codes(report) == {"ANCHOR_NOT_IN_DESIGN"}


def test_anchor_the_trace_does_not_reproduce_is_not_sliced(dut: DesignGraph) -> None:
    failure = failure_at("y", 2, 25000, actual="1111", expected="0101")
    report = slice_failure(failure, dut_provider(), dut, OPTIONS)
    assert not report.nodes and codes(report) == {"ANCHOR_NOT_REPRODUCIBLE"}
    assert "4'b1010" in report.diagnostics[0].message


def test_missing_transaction_has_no_value_to_slice_from(dut: DesignGraph) -> None:
    failure = FailureAnchor(
        kind=FailureKind.MISSING_TRANSACTION,
        mode=ComparisonMode.HANDSHAKE,
        signal_path="tb.dut.y",
        clock_path="tb.dut.clk",
        observation=Observation(epoch=0, cycle=2, phase=BEFORE, time_ps=25000),
        transaction_index=0,
        expected=lv("1010"),
        actual=None,
        detail="No transfer was observed.",
    )
    report = slice_failure(failure, dut_provider(), dut, OPTIONS)
    assert not report.nodes and codes(report) == {"UNSUPPORTED_FAILURE_KIND"}


def test_analyze_comparison_mirrors_the_comparison_status(
    fifo: MockFixture,
    fifo_report: CausalReport,
    fifo_provider: MockWaveformProvider,
    fifo_design: DesignGraph,
) -> None:
    options = SliceOptions(
        trace_scope=SCOPE, reset_path="tb.dut.rst_n", reset_active="0", case_id="mock_fifo_wrap"
    )
    assert analyze_comparison(fifo.comparison, fifo_provider, fifo_design, options) == fifo_report

    passed = ComparisonResult(
        schema_version=1,
        status=AnalysisStatus.PASS,
        mode=ComparisonMode.CYCLE,
        failure=None,
        compared_values=4,
    )
    report = analyze_comparison(passed, fifo_provider, fifo_design, options)
    assert report.status is AnalysisStatus.PASS and not report.nodes and not report.diagnostics

    reason = Diagnostic(code="RESET_UNKNOWN", message="Reset was x at a sampling edge.")
    inconclusive = ComparisonResult(
        schema_version=1,
        status=AnalysisStatus.INCONCLUSIVE,
        mode=ComparisonMode.CYCLE,
        failure=None,
        compared_values=0,
        diagnostics=(reason,),
    )
    report = analyze_comparison(inconclusive, fifo_provider, fifo_design, options)
    assert report.status is AnalysisStatus.INCONCLUSIVE and report.failure is None
    assert [d.code for d in report.diagnostics] == ["RESET_UNKNOWN", "NO_FAILURE_TO_SLICE"]


# --------------------------------------------------------------------------
# Assertion synthesis
# --------------------------------------------------------------------------


def test_fifo_assertion_states_the_launch_cut_and_the_expected_sink(
    fifo_report: CausalReport,
) -> None:
    (assertion,) = fifo_report.assertions
    assert assertion.validation is AssertionValidation.CANDIDATE and assertion.tool is None
    text = assertion.text
    assert "a_waveroot_a1: assert property (p_waveroot_a1);" in text
    assert "c_waveroot_a1: cover property" in text
    assert "@(posedge clk) disable iff (rst_n !== 1'b1)" in text
    antecedent = "((wptr === 3'b011) && (push === 1'b1) && (pop === 1'b0) && (rptr === 3'b000))"
    assert antecedent in text
    # The registers are written at the failing edge, so the sink is sampled one edge later.
    assert "|=> (!$isunknown(full) && (full == 1'b1));" in text
    assert "Not validated" in text
    assert "tests/fixtures/mock_fifo_wrap.sv:38" in text
    assert text.rstrip().splitlines()[-1].startswith("bind mock_fifo_wrap waveroot_chk_a1 ")


def test_fifo_assertion_elaborates_next_to_the_design(fifo_report: CausalReport) -> None:
    design_text = FIFO_SV.read_text(encoding="utf-8")
    assert compile_errors(design_text) == []
    assert compile_errors(design_text + "\n" + fifo_report.assertions[0].text) == []
    # Negative control: the same check rejects a bind that does not fit the checker.
    broken = fifo_report.assertions[0].text.replace(".push(push)", ".no_such_port(push)")
    assert compile_errors(design_text + "\n" + broken) != []


def test_assertion_without_reset_is_not_reset_qualified(
    fifo_failure: FailureAnchor, fifo_provider: MockWaveformProvider, fifo_design: DesignGraph
) -> None:
    report = slice_failure(fifo_failure, fifo_provider, fifo_design, OPTIONS)
    text = report.assertions[0].text
    assert "disable iff" not in text and "No reset was supplied" in text
    # Without a disable iff, reset is an ordinary antecedent term.
    assert "(rst_n === 1'b1)" in text
    assert compile_errors(FIFO_SV.read_text(encoding="utf-8") + "\n" + text) == []

    wrong = SliceOptions(trace_scope=SCOPE, reset_path="tb.dut.nope", reset_active="0")
    report = slice_failure(fifo_failure, fifo_provider, fifo_design, wrong)
    assert "SVA_RESET_NOT_APPLIED" in codes(report)
    assert "disable iff" not in report.assertions[0].text


def test_same_observation_chain_uses_overlapped_implication(dut: DesignGraph) -> None:
    failure = failure_at("y", 2, 25000, actual="1010", expected="0101", phase=BEFORE)
    report = slice_failure(failure, dut_provider(), dut, OPTIONS)
    text = report.assertions[0].text
    assert "((sel === 1'b1) && (a === 4'b1010))" in text
    assert "|-> (!$isunknown(y) && (y == 4'b0101));" in text
    assert "input logic [3:0] a" in text
    assert compile_errors(DUT_SV + "\n" + text) == []


def test_registered_sink_is_checked_one_edge_after_its_operands(dut: DesignGraph) -> None:
    report = slice_failure(
        failure_at("q", 2, 25000, actual="1010", expected="0000"), dut_provider(), dut, OPTIONS
    )
    text = report.assertions[0].text
    assert "|=> (!$isunknown(q) && (q == 4'b0000));" in text
    assert "(en === 1'b1)" in text and "(y === 4'b1010)" in text
    assert compile_errors(DUT_SV + "\n" + text) == []


def test_settled_sink_that_also_reads_an_input_states_the_stability_assumption(
    tmp_path: Path,
) -> None:
    source = (
        "module dut(input logic clk, d, m, output logic q, y);\n"
        "  always_ff @(posedge clk) q <= ~d;\n"
        "  assign y = q ^ m;\n"
        "endmodule\n"
    )
    design = build(tmp_path, source)
    provider = static_provider(
        {"d": "0", "m": "0"},
        changing={"q": [(0, "0"), (25000, "1")], "y": [(0, "0"), (25000, "1")]},
    )
    report = slice_failure(
        failure_at("y", 2, 25000, actual="1", expected="0"), provider, design, OPTIONS
    )
    assert report.suspects[0].node_id == node_of(report, "q", 2, SETTLED).id
    text = report.assertions[0].text
    # q is written at edge 2 from d; y is then sampled at edge 3, by which time m may differ.
    assert "((d === 1'b0))" in text and "|=> (!$isunknown(y) && (y == 1'b0));" in text
    assert "SVA_INPUT_STABILITY_ASSUMED" in codes(report)
    assert "Assumes tb.dut.m keeps its value until the next clock edge" in text
    assert compile_errors(source + "\n" + text) == []


def test_failure_without_an_expected_value_gets_no_assertion(dut: DesignGraph) -> None:
    failure = FailureAnchor(
        kind=FailureKind.EXTRA_TRANSACTION,
        mode=ComparisonMode.HANDSHAKE,
        signal_path="tb.dut.y",
        clock_path="tb.dut.clk",
        observation=Observation(epoch=0, cycle=2, phase=BEFORE, time_ps=25000),
        transaction_index=0,
        expected=None,
        actual=lv("1010"),
        detail="A transfer arrived with no expectation left.",
    )
    report = slice_failure(failure, dut_provider(), dut, OPTIONS)
    assert report.nodes and report.suspects
    assert report.assertions == () and "SVA_NO_EXPECTED_VALUE" in codes(report)


def test_unobserved_operand_is_left_out_of_the_assertion_and_reported(dut: DesignGraph) -> None:
    values = {name: bits for name, bits in DUT_VALUES.items() if name != "sel"}
    report = slice_failure(
        failure_at("y", 2, 25000, actual="1010", expected="0101", phase=BEFORE),
        static_provider(values),
        dut,
        OPTIONS,
    )
    text = report.assertions[0].text
    assert "sel ===" not in text and "Not constrained (no observed value): tb.dut.sel." in text
    assert "SVA_OPERAND_NOT_OBSERVED" in codes(report)


def test_synthesize_assertion_works_from_a_report_alone(
    fifo_report: CausalReport, fifo_failure: FailureAnchor, fifo_design: DesignGraph
) -> None:
    second = fifo_report.suspects[1]
    result = synthesize_assertion(
        failure=fifo_failure,
        nodes=fifo_report.nodes,
        edges=fifo_report.edges,
        suspect=second,
        design=fifo_design,
        trace_scope=SCOPE,
        reset_path="tb.dut.rst_n",
        reset_active="0",
        assertion_id="a2",
    )
    assert result.assertion is not None and result.assertion.id == "a2"
    # The second suspect is the full comparison itself: its operands are sampled with the sink.
    assert "((wptr === 3'b000) && (rptr === 3'b000))" in result.assertion.text
    assert "|-> (!$isunknown(full) && (full == 1'b1));" in result.assertion.text
    design_text = FIFO_SV.read_text(encoding="utf-8")
    assert compile_errors(design_text + "\n" + result.assertion.text) == []

    # The fourth suspect is the write one cycle earlier: two sampling edges before the sink.
    earlier = fifo_report.suspects[3]
    assert earlier.node_id == node_of(fifo_report, "wptr", 2, SETTLED).id
    deep = synthesize_assertion(
        failure=fifo_failure,
        nodes=fifo_report.nodes,
        edges=fifo_report.edges,
        suspect=earlier,
        design=fifo_design,
        trace_scope=SCOPE,
        assertion_id="a3",
    )
    assert deep.assertion is not None
    assert "(wptr === 3'b010)" in deep.assertion.text
    assert "|-> ##2 (!$isunknown(full) && (full == 1'b1));" in deep.assertion.text
    assert [d.code for d in deep.diagnostics] == ["SVA_LATENCY_ASSUMED"]
    assert compile_errors(design_text + "\n" + deep.assertion.text) == []


# --------------------------------------------------------------------------
# Review of PR #6: audit findings 1-7 and the benchmark ranking gaps
# --------------------------------------------------------------------------

PICK_FAILURE = failure_at("y", 2, 25000, actual="1010", expected="0101")
PICK_VALUES = {"a": "1010", "b": "0101", "y": "1010"}


def test_finding1_intermediate_blocking_value_never_prunes(tmp_path: Path) -> None:
    """``t`` is 1 where ``if (t)`` reads it but 0 in the trace; simulation takes ``a``."""
    design = build(
        tmp_path,
        "module m (input logic s, input logic [3:0] a, b, output logic [3:0] y);\n"
        "  logic t;\n"
        "  always_comb begin\n"
        "    t = s;\n"
        "    if (t) y = a;\n"
        "    else y = b;\n"
        "    t = 1'b0;\n"
        "  end\n"
        "endmodule\n",
    )
    assert design.drivers["y"][0].intermediate == ("t",)
    provider = static_provider({**PICK_VALUES, "s": "1", "t": "0"})
    report = slice_failure(PICK_FAILURE, provider, design, OPTIONS)
    y = ("y", 2, SETTLED)
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert branches(edges_of(report, ("b", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    # The traced t is not the value the if read, so it is not followed.
    assert not [e for e in report.edges if e.target == node_of(report, *y).id
                and e.source.startswith("t.")]  # fmt: skip
    assert report.truncated and "INTERMEDIATE_VALUE" in codes(report)
    assert CausalReport.model_validate_json(report.model_dump_json()) == report


def test_finding2_missing_signal_is_not_a_recorded_x(tmp_path: Path) -> None:
    """``===`` would turn a fabricated x into a definite 0 and prune the true arm."""
    design = build(
        tmp_path,
        "module m (input logic sel, input logic [3:0] a, b, output logic [3:0] y);\n"
        "  assign y = (sel === 1'b1) ? a : b;\n"
        "endmodule\n",
    )
    report = slice_failure(PICK_FAILURE, static_provider(PICK_VALUES), design, OPTIONS)
    assert "SIGNAL_NOT_OBSERVED" in codes(report)
    y = ("y", 2, SETTLED)
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    assert branches(edges_of(report, ("b", 2, SETTLED), y)) == {BranchStatus.UNKNOWN}
    # The predicate is read whichever arm is taken, so that dependence is certain.
    (control,) = edges_of(report, ("sel", 2, SETTLED), y)
    assert (control.role, control.branch) == (EdgeRole.CONTROL, BranchStatus.RETAINED)
    # A recorded x is still evaluated: === x is a definite 0, so the true arm is pruned.
    recorded = slice_failure(
        failure_at("y", 2, 25000, actual="0101", expected="1010"),
        static_provider({**PICK_VALUES, "sel": "x", "y": "0101"}),
        design,
        OPTIONS,
    )
    assert branches(edges_of(recorded, ("a", 2, SETTLED), y)) == {BranchStatus.PRUNED}


def test_finding3_intra_assignment_delay_is_not_traced(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m (input logic clk, s, output logic q);\n"
        "  always_ff @(posedge clk) q <= #1 s;\n"
        "endmodule\n",
    )
    (driver,) = design.drivers["q"]
    assert driver.unsupported is not None and "intra-assignment timing" in driver.unsupported
    report = slice_failure(
        failure_at("q", 2, 25000, actual="1", expected="0"),
        static_provider({"s": "1", "q": "1"}),
        design,
        OPTIONS,
    )
    assert report.truncated and "UNSUPPORTED_DRIVER" in codes(report)
    assert not has_node(report, "s")


@pytest.mark.parametrize("condition", ["x", "z"])
def test_finding4_unknown_conditional_follows_ieee_table_11_20(condition: str) -> None:
    """IEEE 1800 Table 11-20: only agreeing 0/0 and 1/1 bits survive an unknown condition.

    slang 12.1 gives ``z`` for ``x ? z : z``; the standard and this evaluator give ``x``.
    The coordinator ruled for the standard, so it is encoded here directly.
    """
    for left in "01xz":
        for right in "01xz":
            expected = left if left == right and left in "01" else "x"
            expr = ir.Conditional(
                1,
                False,
                1,
                ir.Const(1, False, condition),
                ir.Const(1, False, left),
                ir.Const(1, False, right),
                None,
            )
            assert ir.evaluate(expr, lambda _signal: None).bits == expected, (left, right)


def test_finding5_two_state_cast_turns_x_into_zero(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m (input logic sel, input logic [3:0] a, b, output logic [3:0] y);\n"
        "  assign y = (bit'(sel) === 1'b0) ? a : b;\n"
        "endmodule\n",
    )
    rhs = design.drivers["y"][0].assigns[0].rhs
    values = {"sel": Vec("x"), "a": Vec("1010"), "b": Vec("0101")}
    assert ir.evaluate(rhs, values.__getitem__).bits == "1010", "slang: bit'(1'bx) is 0"
    report = slice_failure(
        PICK_FAILURE, static_provider({**PICK_VALUES, "sel": "x"}), design, OPTIONS
    )
    y = ("y", 2, SETTLED)
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.RETAINED}
    assert branches(edges_of(report, ("b", 2, SETTLED), y)) == {BranchStatus.PRUNED}


def test_finding6_partly_out_of_range_select_keeps_its_in_range_bits(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m (input logic [3:0] a, b, output logic [3:0] y, z);\n"
        "  logic [3:0] v;\n"
        "  logic [0:3] w;\n"
        "  assign v = 4'b1010;\n"
        "  assign w = 4'b1010;\n"
        "  assign y = (v[4:1] === 4'bx101) ? a : b;\n"
        "  assign z = {w[2:5], 2'b00} >> 2;\n"
        "endmodule\n",
    )
    values = {"v": Vec("1010"), "w": Vec("1010"), "a": Vec("1010"), "b": Vec("0101")}
    assert ir.evaluate(design.drivers["y"][0].assigns[0].rhs, values.__getitem__).bits == "1010"
    # w is [0:3]: w[2:5] is w[2], w[3], then two out-of-range x bits.
    z = design.drivers["z"][0].assigns[0].rhs
    assert "w" in ir.signals_of(z)
    report = slice_failure(
        PICK_FAILURE, static_provider({**PICK_VALUES, "v": "1010", "w": "1010"}), design, OPTIONS
    )
    y = ("y", 2, SETTLED)
    assert branches(edges_of(report, ("a", 2, SETTLED), y)) == {BranchStatus.RETAINED}
    assert branches(edges_of(report, ("b", 2, SETTLED), y)) == {BranchStatus.PRUNED}
    (control,) = edges_of(report, ("v", 2, SETTLED), y)
    assert control.role is EdgeRole.CONTROL, "v stays in the graph"


class CountingProvider(MockWaveformProvider):
    """Counts value queries, to check that a limit bounds the work."""

    def __init__(self, trace: MockTrace) -> None:
        super().__init__(trace)
        self.queries = 0

    def get_value_at(self, *args: object, **kwargs: object) -> LogicValue:
        self.queries += 1
        return super().get_value_at(*args, **kwargs)  # type: ignore[arg-type]


def test_finding7_depth_limit_bounds_the_hold_scan(tmp_path: Path) -> None:
    design = build(
        tmp_path,
        "module m (input logic clk, en, d, output logic y);\n"
        "  always_ff @(posedge clk) if (en) y <= d;\n"
        "endmodule\n",
    )
    cycles = 2000
    edges = tuple(5000 + 10000 * cycle for cycle in range(cycles))
    signals: dict[str, Sequence[tuple[int, str]]] = {"clk": clock_samples(edges)}
    signals.update({"en": [(0, "0")], "d": [(0, "1")], "y": [(0, "0")]})
    trace = make_trace(edges[-1] + 5000, signals)
    failure = failure_at("y", cycles - 1, edges[-1], actual="0", expected="1")

    limited = CountingProvider(trace)
    options = SliceOptions(trace_scope=SCOPE, max_cycles_back=1, include_waveform=False)
    report = slice_failure(failure, limited, design, options)
    assert report.truncated and "SLICE_DEPTH_LIMIT" in codes(report)
    assert limited.queries < 40, limited.queries

    unlimited = CountingProvider(trace)
    slice_failure(failure, unlimited, design, SliceOptions(trace_scope=SCOPE, include_waveform=False))
    assert unlimited.queries > cycles


MAC_SV = """\
module mac (
  input  logic               clk,
  input  logic               rst_n,
  input  logic               in_valid,
  input  logic signed  [7:0] a,
  input  logic signed  [7:0] b,
  output logic               out_valid,
  output logic signed [15:0] out_data
);
  logic signed [15:0] acc_q;
  logic               valid_q;
  logic signed [15:0] prod;
  logic signed [16:0] sum_wide;
  logic signed [16:0] sum_cmp;
  logic signed [15:0] next_acc;

  assign prod     = 16'(a * b);
  assign sum_wide = 17'(acc_q) + 17'(prod);
  assign sum_cmp  = 17'($signed(sum_wide[15:0]));
  assign next_acc = (sum_cmp > 17'sd32767)  ? 16'sd32767  :
                    (sum_cmp < -17'sd32768) ? -16'sd32768 :
                    sum_wide[15:0];

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      acc_q     <= 16'sd0;
      valid_q   <= 1'b0;
      out_valid <= 1'b0;
      out_data  <= 16'sd0;
    end else begin
      if (in_valid)
        acc_q <= next_acc;
      valid_q   <= in_valid;
      out_valid <= valid_q;
      out_data  <= acc_q;
    end
  end
endmodule
"""


def _line_of(source: str, text: str) -> int:
    return next(n for n, line in enumerate(source.splitlines(), 1) if text in line)


def _bits(value: int, width: int) -> str:
    return format(value & ((1 << width) - 1), f"0{width}b")


def _sext(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value >> (width - 1) else value


def mac_provider() -> MockWaveformProvider:
    """The benchmark's directed run: three -128 * 125 accumulations, then idle.

    Values follow the mutant RTL cycle by cycle, so the trace is what a
    four-state simulator would record.
    """
    edges = (5000, 15000, 25000, 35000)
    a, b = -128, 125
    valid_before = [1, 1, 1, 0]

    def comb(acc: int) -> dict[str, str]:
        prod = _sext(a * b, 16)
        wide = _sext(acc + prod, 17)
        cmp = _sext(_sext(wide, 16), 17)
        nxt = 32767 if cmp > 32767 else -32768 if cmp < -32768 else _sext(wide, 16)
        return {
            "prod": _bits(prod, 16), "sum_wide": _bits(wide, 17), "sum_cmp": _bits(cmp, 17),
            "next_acc": _bits(nxt, 16), "acc_q": _bits(acc, 16),
        }  # fmt: skip

    acc, out, valid_q, out_valid = 0, 0, 0, 0
    states = [(0, {**comb(acc), "out_data": _bits(out, 16), "valid_q": "0", "out_valid": "0"})]
    for edge, valid in zip(edges, valid_before):
        new_acc = int(comb(acc)["next_acc"], 2) if valid else acc
        acc, out = _sext(new_acc, 16), acc
        valid_q, out_valid = valid, valid_q
        states.append(
            (edge, {**comb(acc), "out_data": _bits(out, 16), "valid_q": str(valid_q),
                    "out_valid": str(out_valid)})  # fmt: skip
        )
    changing: dict[str, list[tuple[int, str]]] = {}
    for time, values in states:
        for name, bits in values.items():
            samples = changing.setdefault(name, [])
            if not samples or samples[-1][1] != bits:
                samples.append((time, bits))
    changing["in_valid"] = [(0, "1"), (30000, "0")]
    return static_provider(
        {"rst_n": "1", "a": _bits(a, 8), "b": _bits(b, 8)}, changing=changing, edges=edges
    )


def test_ranking_puts_the_computing_line_above_plain_copies(tmp_path: Path) -> None:
    """Benchmark case 03: the narrowing on the sum_cmp line creates the wrong value."""
    design = build(tmp_path, MAC_SV, "mac.sv")
    failure = failure_at("out_data", 3, 35000, actual=_bits(17536, 16), expected=_bits(-32768, 16))
    options = SliceOptions(trace_scope=SCOPE, reset_path="tb.dut.rst_n", reset_active="0")
    report = slice_failure(failure, mac_provider(), design, options)
    assert node_of(report, "out_data", 3, SETTLED).value == lv(_bits(17536, 16))
    lines = [s.location.line for s in report.suspects if s.location is not None]
    assert _line_of(MAC_SV, "assign sum_cmp") in lines[:2], lines
    copies = [_line_of(MAC_SV, "out_data  <= acc_q"), _line_of(MAC_SV, "acc_q <= next_acc")]
    computing = [_line_of(MAC_SV, "assign sum_cmp"), _line_of(MAC_SV, "assign sum_wide")]
    assert max(lines.index(line) for line in computing) < min(lines.index(line) for line in copies)
    assert CausalReport.model_validate_json(report.model_dump_json()) == report


ALU_SV = """\
module alu (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       in_valid,
  input  logic [7:0] a,
  input  logic [7:0] b,
  input  logic       cfg_we,
  input  logic       cfg_sel,
  output logic       out_valid,
  output logic [7:0] out_data
);
  logic sel_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      out_valid <= 1'b0;
      out_data  <= 8'h00;
    end else begin
      if (cfg_we)
        sel_q <= cfg_sel;
      out_valid <= in_valid;
      if (in_valid)
        out_data <= sel_q ? b : a;
    end
  end
endmodule
"""


def alu_provider() -> MockWaveformProvider:
    """Benchmark case 04, directed: reset for two edges, then a valid input at cycle 0."""
    return static_provider(
        {"in_valid": "1", "a": "00000000", "b": "11111111", "cfg_we": "0", "cfg_sel": "0",
         "sel_q": "x"},  # fmt: skip
        changing={
            "rst_n": [(0, "0"), (20000, "1")],
            "out_valid": [(0, "x"), (5000, "0"), (25000, "1")],
            "out_data": [(0, "x" * 8), (5000, "0" * 8), (25000, "x" * 8)],
        },
    )


def test_unreset_register_is_located_at_the_reset_branch(tmp_path: Path) -> None:
    """Benchmark case 04: the reset branch never assigns sel_q, so it is x at cycle 0."""
    design = build(tmp_path, ALU_SV, "alu.sv")
    failure = failure_at("out_data", 0, 25000, actual="x" * 8, expected="0" * 8)
    options = SliceOptions(trace_scope=SCOPE, reset_path="tb.dut.rst_n", reset_active="0")
    report = slice_failure(failure, alu_provider(), design, options)
    reset_first = _line_of(ALU_SV, "if (!rst_n)")
    reset_last = _line_of(ALU_SV, "out_data  <= 8'h00")
    second = report.suspects[1]
    assert second.node_id == node_of(report, "sel_q", 0, BEFORE).id
    assert second.location is not None
    assert (second.location.line, second.location.end_line) == (reset_first, reset_last)
    assert "reset branch of its clocked process does not assign it" in second.reason
    assert report.suspects[0].location is not None
    assert report.suspects[0].location.line == _line_of(ALU_SV, "out_data <= sel_q ? b : a")
    assert CausalReport.model_validate_json(report.model_dump_json()) == report

    # Without a reset in the options nothing is guessed: the value stays an epoch boundary.
    plain = slice_failure(failure, alu_provider(), design, OPTIONS)
    boundary = next(s for s in plain.suspects if s.node_id == node_of(plain, "sel_q", 0, BEFORE).id)
    assert boundary.location is not None
    assert boundary.location.line == _line_of(ALU_SV, "logic sel_q;")
