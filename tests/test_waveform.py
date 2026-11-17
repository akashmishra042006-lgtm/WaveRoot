"""Track A tests: VCD parsing, time queries, and golden comparison.

VCD inputs here are handcrafted text or text generated from the shared mock
fixture. No HDL simulator runs in this suite, so it is not end-to-end coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.contracts import (
    AnalysisStatus,
    BitRange,
    ClockEdge,
    ComparisonMode,
    ComparisonResult,
    DataUnavailableError,
    FailureKind,
    GoldenTrace,
    IndeterminateClockError,
    InvalidQueryError,
    MAX_TIME_PS,
    LogicValue,
    MockFixture,
    MockTrace,
    MockWaveformProvider,
    OutOfRangeError,
    SamplingPhase,
    SignalNotFoundError,
    TimeInterval,
    TraceCoverage,
    UnsupportedPrecisionError,
    WaveformProvider,
    load_mock_fixture,
    parse_golden_jsonl,
)
from src.waveform import (
    InvalidTraceError,
    UnsupportedTraceError,
    VcdWaveformProvider,
    compare,
    compare_files,
    match_paths,
    parse_vcd,
)

FIXTURES = Path(__file__).parent / "fixtures"
BEFORE = SamplingPhase.BEFORE
SETTLED = SamplingPhase.SETTLED


def lv(bits: str) -> LogicValue:
    return LogicValue(width=len(bits), bits=bits)


# --------------------------------------------------------------------------
# VCD builders
# --------------------------------------------------------------------------

HEADER = """$date today $end
$version handcrafted $end
$timescale 1 ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 8 " data [7:0] $end
$upscope $end
$enddefinitions $end
"""


def change(code: str, bits: str) -> str:
    return f"{bits}{code}" if len(bits) == 1 else f"b{bits} {code}"


def vcd_from_mock(trace: MockTrace, *, timescale: str = "1 ps", divisor: int = 1) -> str:
    """Render a MockTrace as VCD text, one identifier code per signal."""
    lines = [f"$timescale {timescale} $end"]
    codes: dict[str, str] = {}
    events: dict[int, list[str]] = {}
    for index, signal in enumerate(trace.signals):
        code = f"s{index}"
        codes[signal.decl.path] = code
        *scopes, name = signal.decl.path.split(".")
        lines += [f"$scope module {scope} $end" for scope in scopes]
        rng = signal.decl.bit_range
        suffix = f" [{rng.left}:{rng.right}]" if rng is not None else ""
        lines.append(f"$var wire {signal.decl.width} {code} {name}{suffix} $end")
        lines += ["$upscope $end"] * len(scopes)
        for sample in signal.samples:
            events.setdefault(sample.time_ps, []).append(change(code, sample.value.bits))
    lines.append("$enddefinitions $end")
    events.setdefault(trace.coverage.end_ps, [])
    for time_ps in sorted(events):
        assert time_ps % divisor == 0
        lines.append(f"#{time_ps // divisor}")
        lines += events[time_ps]
    return "\n".join(lines) + "\n"


def build_vcd(
    widths: dict[str, int],
    rows: list[dict[str, str]],
    *,
    reset_edges: int = 2,
    idle: dict[str, str] | None = None,
    tail_cycles: int = 0,
) -> str:
    """A single-clock VCD under scope ``top`` with a 10 ns clock.

    The clock rises at 5, 15, ... ns. ``rst_n`` is low for ``reset_edges``
    rising edges and is released on a falling edge. ``rows[k]`` gives the values
    driven on the falling edge before cycle k and may set ``rst_n``. Signals
    hold their previous value when a row omits them.
    """
    names = ["clk", "rst_n", *widths]
    all_widths = {"clk": 1, "rst_n": 1, **widths}
    codes = {name: f"c{index}" for index, name in enumerate(names)}
    lines = ["$timescale 1 ns $end", "$scope module top $end"]
    for name in names:
        width = all_widths[name]
        suffix = f" [{width - 1}:0]" if width > 1 else ""
        lines.append(f"$var wire {width} {codes[name]} {name}{suffix} $end")
    lines += ["$upscope $end", "$enddefinitions $end"]
    state = {"clk": "0", "rst_n": "0", **{n: "0" * w for n, w in widths.items()}}
    state.update(idle or {})
    lines.append("#0")
    lines += [change(codes[name], state[name]) for name in names]
    release = 10 * reset_edges
    last_fall = release + 10 * (len(rows) + tail_cycles)
    for time_ns in range(5, last_fall + 1, 5):
        updates = {"clk": "1" if time_ns % 10 == 5 else "0"}
        row_index = (time_ns - release) // 10
        if time_ns % 10 == 0 and time_ns >= release:
            updates["rst_n"] = "1"
            if row_index < len(rows):
                updates.update(rows[row_index])
        lines.append(f"#{time_ns}")
        for name, value in updates.items():
            if state[name] != value:
                state[name] = value
                lines.append(change(codes[name], value))
    return "\n".join(lines) + "\n"


def edge_ps(cycle: int, *, reset_edges: int = 2) -> int:
    return (10 * reset_edges + 5 + 10 * cycle) * 1000


def golden(header: dict[str, Any], records: list[dict[str, Any]]) -> GoldenTrace:
    base = {"type": "metadata", "schema_version": 1, "clock": "top.clk", "reset": "top.rst_n", "reset_active": "0"}
    lines = [json.dumps({**base, **header})] + [json.dumps({"type": "expected", **r}) for r in records]
    return parse_golden_jsonl("\n".join(lines) + "\n")


@pytest.fixture(scope="module")
def fixture() -> MockFixture:
    return load_mock_fixture(FIXTURES / "mock_fifo_failure.json")


@pytest.fixture(scope="module")
def fifo(fixture: MockFixture) -> VcdWaveformProvider:
    return VcdWaveformProvider.from_text(vcd_from_mock(fixture.trace))


# --------------------------------------------------------------------------
# Provider agrees with the shared contract's mock provider
# --------------------------------------------------------------------------


def test_real_provider_satisfies_the_protocol(fifo: VcdWaveformProvider) -> None:
    assert isinstance(fifo, WaveformProvider)


def test_real_provider_matches_mock_provider_on_the_fifo_fixture(
    fixture: MockFixture, fifo: VcdWaveformProvider
) -> None:
    mock = MockWaveformProvider(fixture.trace)
    paths = [signal.decl.path for signal in fixture.trace.signals]
    assert list(fifo.trace.signal_paths) == paths
    assert fifo.get_coverage() == mock.get_coverage()
    for path in paths:
        assert fifo.get_signal_decl(path) == mock.get_signal_decl(path)
        for time_ps in range(0, 100001, 2500):
            for phase in (BEFORE, SETTLED):
                assert fifo.get_value_at(path, time_ps, phase=phase) == mock.get_value_at(
                    path, time_ps, phase=phase
                ), (path, time_ps, phase)
    for start, end in [(0, 100001), (45000, 65000), (54999, 55001), (20000, 20001)]:
        assert fifo.get_window_slice(paths, start, end) == mock.get_window_slice(paths, start, end)
    clk = "tb.dut.clk"
    for edge in ClockEdge:
        assert fifo.get_clock_edges(clk, 0, 100001, edge=edge) == mock.get_clock_edges(
            clk, 0, 100001, edge=edge
        )
    assert fifo.get_clock_edges(clk, 25000, 35000) == (25000,)
    assert fifo.get_clock_edges(clk, 25001, 35000) == ()


def test_equivalent_timescales_give_identical_results(fixture: MockFixture) -> None:
    paths = [signal.decl.path for signal in fixture.trace.signals]
    reference = VcdWaveformProvider.from_text(vcd_from_mock(fixture.trace))
    for timescale, divisor in [("1 ns", 1000), ("1ns", 1000), ("10 ps", 10), ("100ps", 100)]:
        other = VcdWaveformProvider.from_text(
            vcd_from_mock(fixture.trace, timescale=timescale, divisor=divisor)
        )
        assert other.get_coverage() == reference.get_coverage()
        assert other.get_window_slice(paths, 0, 100001) == reference.get_window_slice(paths, 0, 100001)


def test_provider_rejects_malformed_and_out_of_range_queries(fifo: VcdWaveformProvider) -> None:
    clk, wptr = "tb.dut.clk", "tb.dut.wptr"
    with pytest.raises(SignalNotFoundError):
        fifo.get_value_at("tb.dut.mem", 5)
    with pytest.raises(OutOfRangeError):
        fifo.get_value_at(clk, 100001)
    with pytest.raises(OutOfRangeError):
        fifo.get_window_slice([clk], 0, 100002)
    with pytest.raises(InvalidQueryError):
        fifo.get_value_at("tb.dut.mem", -1)
    with pytest.raises(InvalidQueryError):
        fifo.get_clock_edges(wptr, 0, 10)
    with pytest.raises(InvalidQueryError):
        fifo.get_window_slice(clk, 0, 10)
    with pytest.raises(InvalidQueryError):
        fifo.get_window_slice([clk], 10, 10)
    assert fifo.get_value_at(clk, 0, phase=BEFORE) == lv("x")
    assert fifo.get_value_at(wptr, 4999) == lv("xxx")


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def test_femtosecond_traces_are_exact_or_rejected() -> None:
    body = "$scope module top $end\n$var wire 1 ! a $end\n$upscope $end\n$enddefinitions $end\n"
    whole = VcdWaveformProvider.from_text(f"$timescale 1 fs $end\n{body}#0\n0!\n#5000\n1!\n#9000\n")
    assert whole.get_coverage() == TraceCoverage(start_ps=0, end_ps=9)
    assert whole.get_value_at("top.a", 5, phase=BEFORE) == lv("0")
    assert whole.get_value_at("top.a", 5) == lv("1")
    with pytest.raises(UnsupportedPrecisionError):
        parse_vcd(f"$timescale 1 fs $end\n{body}#0\n0!\n#5001\n1!\n")
    with pytest.raises(UnsupportedPrecisionError):
        parse_vcd(f"$timescale 100 fs $end\n{body}#0\n0!\n#15\n1!\n")


def test_large_timestamps_stay_exact() -> None:
    big = 10**20 + 7
    text = (
        "$timescale 1 s $end\n$scope module top $end\n$var wire 1 ! a $end\n$upscope $end\n"
        f"$enddefinitions $end\n#0\n0!\n#{big}\n1!\n#{big + 1}\n"
    )
    provider = VcdWaveformProvider.from_text(text)
    edge = big * 10**12
    assert provider.get_coverage().end_ps == (big + 1) * 10**12
    assert provider.get_clock_edges("top.a", 0, edge + 1) == (edge,)
    assert provider.get_value_at("top.a", edge - 1) == lv("0")
    window = provider.get_window_slice(["top.a"], edge - 1, edge + 2)
    assert json.loads(window.model_dump_json())["signals"][0]["samples"][0]["time_ps"] == str(edge)


# --------------------------------------------------------------------------
# Values, widths, names
# --------------------------------------------------------------------------


def test_vector_values_are_extended_without_losing_bits() -> None:
    text = HEADER + "#0\nb101 \"\n#1\nbx1 \"\n#2\nbZ \"\n#3\nB1X0 \"\n#4\nb11111111 \"\n#5\nb0 \"\n#6\n"
    provider = VcdWaveformProvider.from_text(text)
    observed = [provider.get_value_at("top.data", t * 1000).bits for t in range(6)]
    assert observed == ["00000101", "xxxxxxx1", "zzzzzzzz", "000001x0", "11111111", "00000000"]


def test_scalar_values_normalize_case_and_keep_x_and_z_distinct() -> None:
    provider = VcdWaveformProvider.from_text(HEADER + "#0\nX!\n#1\nZ!\n#2\n1!\n#3\nx!\n#4\n")
    assert [provider.get_value_at("top.clk", t * 1000).bits for t in range(4)] == ["x", "z", "1", "x"]


def test_declared_ranges_and_indices_are_preserved() -> None:
    text = """$timescale 1ps $end
$scope module top $end
$var wire 8 a desc [7:0] $end
$var wire 8 b asc [0:7] $end
$var integer 32 c count $end
$var wire 1 d flag $end
$var wire 4 e mem[3] [3:0] $end
$var wire 1 f bits [2] $end
$var reg 8 g joined[15:8] $end
$scope module gen[2] $end
$var wire 1 h q $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b10000000 a
b10000000 b
#1
"""
    provider = VcdWaveformProvider.from_text(text)
    decl = provider.get_signal_decl
    assert decl("top.desc").bit_range == BitRange(left=7, right=0)
    assert decl("top.asc").bit_range == BitRange(left=0, right=7)
    assert decl("top.asc") != decl("top.desc").model_copy(update={"path": "top.asc"})
    assert decl("top.count").bit_range == BitRange(left=31, right=0)
    assert decl("top.flag").bit_range is None
    assert decl("top.mem[3]").bit_range == BitRange(left=3, right=0)
    assert decl("top.bits[2]").width == 1
    assert decl("top.joined").bit_range == BitRange(left=15, right=8)
    assert decl("top.gen[2].q").width == 1
    # The same characters come back for both directions; nothing is reversed.
    assert provider.get_value_at("top.desc", 0) == provider.get_value_at("top.asc", 0) == lv("10000000")


def test_identifier_code_aliases_resolve_to_every_path() -> None:
    text = """$timescale 1ps $end
$scope module top $end
$var wire 4 ! bus [3:0] $end
$scope module u $end
$var wire 4 ! port [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b1010 !
#5
b0x01 !
#9
"""
    provider = VcdWaveformProvider.from_text(text)
    for time_ps, bits in [(0, "1010"), (5, "0x01")]:
        assert provider.get_value_at("top.bus", time_ps) == lv(bits)
        assert provider.get_value_at("top.u.port", time_ps) == lv(bits)


def test_unrepresentable_variables_are_listed_not_rewritten() -> None:
    text = """$timescale 1ps $end
$scope module top $end
$var real 64 r voltage $end
$var wire 1 e \\weird.name $end
$var event 1 v tick $end
$var wire 1 n neg [-1:-1] $end
$var wire 1 ! ok $end
$scope module bad-scope $end
$var wire 1 s inner $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
r1.5 r
1e
1v
1n
1s
0!
#4
r2.5 r
1!
#8
"""
    provider = VcdWaveformProvider.from_text(text)
    assert provider.trace.signal_paths == ("top.ok",)
    assert provider.get_value_at("top.ok", 4) == lv("1")
    reasons = {skipped.reference: skipped.reason for skipped in provider.trace.skipped}
    assert set(reasons) == {"voltage", "\\weird.name", "tick", "neg[-1:-1]", "inner"}
    for absent in ("top.voltage", "top.weird.name", "top.weird", "top.inner", "top.bad-scope.inner"):
        with pytest.raises((SignalNotFoundError, InvalidQueryError)):
            provider.get_signal_decl(absent)


# --------------------------------------------------------------------------
# Same-timestamp records and clocks
# --------------------------------------------------------------------------


def test_final_value_per_timestamp_wins_and_repeated_timestamps_merge() -> None:
    text = HEADER + '#0\n0!\nb0 "\n#10\nb1 "\nb10 "\n#10\nb11 "\n#20\nb11 "\n#30\n'
    provider = VcdWaveformProvider.from_text(text)
    assert provider.get_value_at("top.data", 10000, phase=BEFORE) == lv("00000000")
    assert provider.get_value_at("top.data", 10000) == lv("00000011")
    window = provider.get_window_slice(["top.data"], 0, 30001)
    # The repeated value at 20 ns is not a change and is not reported as one.
    assert [s.time_ps for s in window.signals[0].samples] == [10000]


def test_clock_glitch_at_one_timestamp_is_indeterminate() -> None:
    text = HEADER + "#0\n0!\n#5\n1!\n#10\n0!\n#15\n1!\n0!\n#25\n1!\n#30\n0!\n#35\n"
    provider = VcdWaveformProvider.from_text(text)
    assert provider.get_clock_edges("top.clk", 0, 15000) == (5000,)
    assert provider.get_clock_edges("top.clk", 15001, 35001) == (25000,)
    for start, end in [(0, 35001), (15000, 15001)]:
        with pytest.raises(IndeterminateClockError):
            provider.get_clock_edges("top.clk", start, end)


def test_unknown_clock_transitions_pauses_and_falling_edges() -> None:
    text = HEADER + "#0\nx!\n#5\n1!\n#10\n0!\n#15\n1!\n#20\nx!\n#25\n0!\n#30\n1!\n#400\n0!\n#405\n1!\n#410\n"
    provider = VcdWaveformProvider.from_text(text)
    assert provider.get_clock_edges("top.clk", 0, 410001) == (15000, 30000, 405000)
    assert provider.get_clock_edges("top.clk", 0, 410001, edge=ClockEdge.FALLING) == (10000, 400000)


# --------------------------------------------------------------------------
# Dump gaps
# --------------------------------------------------------------------------

GAP_TEXT = HEADER + """#0
$dumpvars
0!
b00000001 "
$end
#10
1!
#20
0!
b00000010 "
#30
$dumpoff
x!
bx "
$end
#40
#50
$dumpon
1!
b00000010 "
$end
#60
0!
#70
"""


def test_dump_gap_is_unavailable_and_resume_is_restated() -> None:
    provider = VcdWaveformProvider.from_text(GAP_TEXT)
    data = "top.data"
    assert provider.trace.unavailable == (TimeInterval(start_ps=30000, end_ps=50000),)
    assert provider.get_value_at(data, 29999) == lv("00000010")
    for time_ps in (30000, 40000, 49999):
        with pytest.raises(DataUnavailableError):
            provider.get_value_at(data, time_ps)
    assert provider.get_value_at(data, 30000, phase=BEFORE) == lv("00000010")
    assert provider.get_value_at(data, 50000) == lv("00000010")
    with pytest.raises(DataUnavailableError):
        provider.get_value_at(data, 50000, phase=BEFORE)
    window = provider.get_window_slice([data], 0, 70001)
    assert window.unavailable == (TimeInterval(start_ps=30000, end_ps=50000),)
    # The x values written by $dumpoff are a dump artifact and never appear.
    assert [(s.time_ps, s.value.bits) for s in window.signals[0].samples] == [
        (20000, "00000010"),
        (50000, "00000010"),
    ]
    assert provider.get_clock_edges("top.clk", 0, 30000) == (10000,)
    for start, end in [(0, 30001), (50000, 70001), (35000, 45000)]:
        with pytest.raises(DataUnavailableError):
            provider.get_clock_edges("top.clk", start, end)
    assert provider.get_clock_edges("top.clk", 50001, 70001) == ()


def test_trailing_dumpoff_makes_the_rest_of_the_trace_unavailable() -> None:
    provider = VcdWaveformProvider.from_text(HEADER + '#0\n0!\nb1 "\n#10\n$dumpoff\n$end\n#40\n')
    assert provider.get_coverage() == TraceCoverage(start_ps=0, end_ps=40000)
    assert provider.trace.unavailable == (TimeInterval(start_ps=10000, end_ps=40001),)
    with pytest.raises(DataUnavailableError):
        provider.get_value_at("top.clk", 40000)


def test_dumpon_must_restate_every_signal() -> None:
    with pytest.raises(InvalidTraceError, match="did not restate"):
        parse_vcd(HEADER + '#0\n0!\nb1 "\n#10\n$dumpoff\n$end\n#20\n$dumpon\n0!\n$end\n#30\n')


# --------------------------------------------------------------------------
# Malformed traces
# --------------------------------------------------------------------------

NO_TIMESCALE = HEADER.replace("$timescale 1 ns $end\n", "")


@pytest.mark.parametrize(
    "text",
    [
        NO_TIMESCALE + "#0\n0!\n",
        HEADER.replace("1 ns", "3 ns") + "#0\n0!\n",
        HEADER.replace("1 ns", "1 ns $end $timescale 1 ps") + "#0\n0!\n",
        HEADER.replace("$enddefinitions $end\n", ""),
        HEADER.replace("$enddefinitions $end\n", "$enddefinitions"),
        HEADER.replace("$upscope $end\n", "") + "#0\n",
        HEADER.replace("$scope module top $end\n", "") + "#0\n",
        HEADER.replace("8 \" data [7:0]", "8 \" data [3:0]") + "#0\n",
        HEADER.replace('8 " data', "0 \" data") + "#0\n",
        HEADER.replace('$var wire 8 " data [7:0]', "$var wire 1 # clk") + "#0\n",
        HEADER.replace('8 " data [7:0]', "4 ! other [3:0]") + "#0\n",
        HEADER,  # no simulation time at all
        HEADER + "#10\n0!\n#5\n1!\n",  # time goes backwards
        HEADER + "#0\n0?\n",  # undeclared identifier code
        HEADER + '#0\nb111111111 "\n',  # wider than declared
        HEADER + '#0\n1"\n',  # scalar change for a vector
        HEADER + "#0\nb12 \"\n",  # invalid digit
        HEADER + "#0\nb1\n",  # truncated vector change
        HEADER + "#0\n0\n",  # scalar change without a code
        HEADER + "#1.5\n0!\n",  # fractional timestamp
        HEADER + "#0\nhello\n",  # unknown token
        HEADER + "#0\nr1.5 !\n",  # real value for a digital signal
        HEADER + "#0\n$comment never closed\n",
        HEADER[: HEADER.index("$var")] + "$var wire 1 !",  # truncated declaration
    ],
)
def test_malformed_traces_are_rejected(text: str) -> None:
    with pytest.raises(InvalidTraceError):
        parse_vcd(text)


# --------------------------------------------------------------------------
# Property tests against a linear-scan oracle
# --------------------------------------------------------------------------

record_lists = st.lists(
    st.tuples(st.integers(min_value=0, max_value=60), st.text(alphabet="01xz", min_size=1, max_size=4)),
    min_size=1,
    max_size=30,
)


def oracle_value(records: list[tuple[int, str]], instant: int) -> str:
    """Final record at or before ``instant``, by plain scan."""
    current = "xxxx"
    for time, bits in records:
        if time <= instant:
            fill = bits[0] if bits[0] in "xz" else "0"
            current = fill * (4 - len(bits)) + bits
    return current


@settings(max_examples=150, deadline=None)
@given(record_lists, st.integers(min_value=0, max_value=5))
def test_lookups_and_windows_agree_with_a_linear_scan(records: list[tuple[int, str]], tail: int) -> None:
    records = sorted(records, key=lambda item: item[0])
    end = records[-1][0] + tail
    lines = ["$timescale 1ps $end", "$scope module top $end", "$var wire 4 ! v [3:0] $end", "$upscope $end", "$enddefinitions $end", "#0"]
    for time, bits in records:
        lines += [f"#{time}", f"b{bits} !"]
    lines.append(f"#{end}")
    provider = VcdWaveformProvider.from_text("\n".join(lines) + "\n")
    assert provider.get_coverage() == TraceCoverage(start_ps=0, end_ps=end)
    for instant in range(end + 1):
        assert provider.get_value_at("top.v", instant).bits == oracle_value(records, instant)
        expected_before = "xxxx" if instant == 0 else oracle_value(records, instant - 1)
        assert provider.get_value_at("top.v", instant, phase=BEFORE).bits == expected_before
    for start in range(0, end + 1, 7):
        window = provider.get_window_slice(["top.v"], start, end + 1).signals[0]
        assert window.initial is not None
        value = window.initial.bits
        changes = {sample.time_ps: sample.value.bits for sample in window.samples}
        for instant in range(start, end + 1):
            value = changes.get(instant, value)
            assert value == oracle_value(records, instant)
        # Every reported sample is a real change from the value just before it.
        for time in changes:
            assert oracle_value(records, time) != oracle_value(records, time - 1)


# --------------------------------------------------------------------------
# Comparison: the shared FIFO fixture
# --------------------------------------------------------------------------


def without_detail(result: ComparisonResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.model_dump_json())
    for anchor in [data["failure"], *data["additional_mismatches"]]:
        if anchor is not None:
            del anchor["detail"]
    return data


def assert_reproducible(provider: WaveformProvider, result: ComparisonResult) -> None:
    assert result.failure is not None
    for anchor in (result.failure, *result.additional_mismatches):
        if anchor.actual is not None:
            observed = provider.get_value_at(
                anchor.signal_path, anchor.observation.time_ps, phase=anchor.observation.phase
            )
            assert observed == anchor.actual
        assert anchor.observation.time_ps in provider.get_clock_edges(
            anchor.clock_path, 0, provider.get_coverage().end_ps + 1
        )


def test_fifo_fixture_comparison_matches_the_contract_fixture(
    fixture: MockFixture, fifo: VcdWaveformProvider
) -> None:
    for provider in (fifo, MockWaveformProvider(fixture.trace)):
        result = compare(provider, fixture.golden)
        assert without_detail(result) == without_detail(fixture.comparison)
        assert result.failure is not None
        assert result.failure.signal_path == "tb.dut.full"
        assert result.failure.observation.time_ps == 55000
        assert [a.signal_path for a in result.additional_mismatches] == ["tb.dut.empty", "tb.dut.out_valid"]
        assert_reproducible(provider, result)


def test_fifo_fixture_passes_against_expectations_taken_from_the_trace(
    fixture: MockFixture, fifo: VcdWaveformProvider
) -> None:
    header = fixture.golden.header
    lines = [header.model_dump_json(exclude_none=True)]
    for cycle in range(8):
        values = {s.name: fifo.get_value_at(s.path, 25000 + 10000 * cycle).bits for s in header.signals}
        lines.append(json.dumps({"type": "expected", "epoch": 0, "cycle_index": cycle, "values": values}))
    result = compare(fifo, parse_golden_jsonl("\n".join(lines) + "\n"))
    assert result.status is AnalysisStatus.PASS
    assert result.failure is None
    assert result.compared_values == 32


@pytest.mark.parametrize("simulator", ["icarus", "verilator"])
def test_real_simulator_dumps_reproduce_the_fixture_failure(simulator: str, fixture: MockFixture) -> None:
    """VCDs written by Icarus Verilog 13.0 and Verilator 5.054 from
    ``mock_fifo_wrap.sv`` (see ``tests/fixtures/waveform/mock_fifo_tb.sv``).

    This checks the reader on real simulator output. It is not end-to-end
    coverage: the simulators ran once to produce these files, not in this test.
    """
    path = FIXTURES / "waveform" / f"mock_fifo_{simulator}.vcd"
    provider = VcdWaveformProvider.from_path(path)
    assert provider.trace.skipped == ()
    assert provider.get_coverage() == TraceCoverage(start_ps=0, end_ps=100000)
    assert provider.get_clock_edges("tb.dut.clk", 0, 100001) == tuple(range(5000, 100000, 10000))
    for signal in fixture.trace.signals:
        path_ = signal.decl.path
        assert provider.get_signal_decl(path_).width == signal.decl.width
        # The simulated values match the hand-stepped fixture from cycle 0 on.
        for time_ps in range(25000, 100001, 10000):
            assert provider.get_value_at(path_, time_ps) == MockWaveformProvider(fixture.trace).get_value_at(
                path_, time_ps
            ), (simulator, path_, time_ps)
    result = compare_files(path, FIXTURES / "mock_fifo_golden.jsonl")
    assert without_detail(result) == without_detail(fixture.comparison)
    assert_reproducible(provider, result)


def test_compare_files_round_trip(fixture: MockFixture, tmp_path: Path) -> None:
    vcd_path = tmp_path / "sim.vcd"
    vcd_path.write_text(vcd_from_mock(fixture.trace), encoding="utf-8")
    result = compare_files(vcd_path, FIXTURES / "mock_fifo_golden.jsonl")
    assert without_detail(result) == without_detail(fixture.comparison)

    missing = compare_files(tmp_path / "absent.vcd", FIXTURES / "mock_fifo_golden.jsonl")
    assert missing.status is AnalysisStatus.ERROR

    bad_golden = tmp_path / "bad.jsonl"
    bad_golden.write_text('{"type":"metadata","mode":"handshake"}\n', encoding="utf-8")
    invalid = compare_files(vcd_path, bad_golden)
    assert (invalid.status, invalid.mode) == (AnalysisStatus.INVALID_INPUT, ComparisonMode.HANDSHAKE)

    bad_vcd = tmp_path / "bad.vcd"
    bad_vcd.write_text(NO_TIMESCALE + "#0\n0!\n", encoding="utf-8")
    assert compare_files(bad_vcd, FIXTURES / "mock_fifo_golden.jsonl").status is AnalysisStatus.INVALID_INPUT

    fs_vcd = tmp_path / "fs.vcd"
    fs_vcd.write_text(HEADER.replace("1 ns", "1 fs") + "#0\n0!\n#1\n1!\n", encoding="utf-8")
    assert compare_files(fs_vcd, FIXTURES / "mock_fifo_golden.jsonl").status is AnalysisStatus.UNSUPPORTED


# --------------------------------------------------------------------------
# Comparison: cycle mode
# --------------------------------------------------------------------------

CYCLE_HEADER = {
    "mode": "cycle",
    "sampling": "settled",
    "signals": [{"name": "q", "path": "top.q", "width": 4}, {"name": "ok", "path": "top.ok", "width": 1}],
}


def cycle_records(*rows: dict[str, str], epoch: int = 0) -> list[dict[str, Any]]:
    return [{"epoch": epoch, "cycle_index": index, "values": row} for index, row in enumerate(rows)]


def cycle_vcd(rows: list[dict[str, str]], **kwargs: Any) -> VcdWaveformProvider:
    """``q``/``ok`` take row k's values at the rising edge of cycle k (registered outputs)."""
    text = build_vcd({"q": 4, "ok": 1}, [], tail_cycles=len(rows), **kwargs)
    lines = text.rstrip("\n").split("\n")
    out: list[str] = []
    state = {"q": "0000", "ok": "0"}
    codes = {"q": "c2", "ok": "c3"}
    for line in lines:
        out.append(line)
        if line.startswith("#"):
            time_ns = int(line[1:])
            cycle = (time_ns - 25) // 10
            if time_ns >= 25 and time_ns % 10 == 5 and cycle < len(rows):
                for name, value in rows[cycle].items():
                    if state[name] != value:
                        state[name] = value
                        out.append(change(codes[name], value))
    return VcdWaveformProvider.from_text("\n".join(out) + "\n")


def test_cycle_mode_pass_fail_and_declaration_order_tie_break() -> None:
    rows = [{"q": "0001", "ok": "1"}, {"q": "0010"}, {"q": "0011", "ok": "0"}]
    provider = cycle_vcd(rows)
    expected = [{"q": "0001", "ok": "1"}, {"q": "0010", "ok": "1"}, {"q": "0011", "ok": "0"}]
    assert compare(provider, golden(CYCLE_HEADER, cycle_records(*expected))).status is AnalysisStatus.PASS

    wrong = [{"q": "0001", "ok": "1"}, {"q": "0110", "ok": "0"}, {"q": "1111", "ok": "1"}]
    result = compare(provider, golden(CYCLE_HEADER, cycle_records(*wrong)))
    assert result.status is AnalysisStatus.FAIL
    failure = result.failure
    assert failure is not None
    assert (failure.signal_path, failure.observation.cycle, failure.observation.time_ps) == ("top.q", 1, edge_ps(1))
    assert (failure.expected, failure.actual) == (lv("0110"), lv("0010"))
    assert failure.observation.phase is SETTLED and failure.transaction_index is None
    assert [a.signal_path for a in result.additional_mismatches] == ["top.ok"]
    assert result.compared_values == 4  # stops at the first failing cycle
    assert_reproducible(provider, result)

    # Reversing the declaration order moves the anchor to the other signal.
    reversed_header = {**CYCLE_HEADER, "signals": list(reversed(CYCLE_HEADER["signals"]))}
    flipped = compare(provider, golden(reversed_header, cycle_records(*wrong)))
    assert flipped.failure is not None and flipped.failure.signal_path == "top.ok"


def test_cycle_mode_sampling_phase_selects_pre_or_post_edge_value() -> None:
    provider = cycle_vcd([{"q": "0001"}, {"q": "0010"}])
    settled = cycle_records({"q": "0001"}, {"q": "0010"})
    before = cycle_records({"q": "0000"}, {"q": "0001"})
    only_q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}
    assert compare(provider, golden(only_q, settled)).status is AnalysisStatus.PASS
    assert compare(provider, golden({**only_q, "sampling": "before"}, before)).status is AnalysisStatus.PASS
    crossed = compare(provider, golden({**only_q, "sampling": "before"}, settled))
    assert crossed.failure is not None
    assert (crossed.failure.observation.phase, crossed.failure.actual) == (BEFORE, lv("0000"))
    assert_reproducible(provider, crossed)


def test_unknown_actual_value_is_a_failure_not_a_wildcard() -> None:
    provider = cycle_vcd([{"q": "0001"}, {"q": "0x01"}, {"q": "zzzz"}])
    only_q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}
    result = compare(provider, golden(only_q, cycle_records({"q": "0001"}, {"q": "0001"}, {"q": "0000"})))
    assert result.status is AnalysisStatus.FAIL
    assert result.failure is not None
    assert result.failure.kind is FailureKind.UNKNOWN_VALUE
    assert (result.failure.observation.cycle, result.failure.actual) == (1, lv("0x01"))


def test_cycle_mode_omitted_signals_are_not_compared() -> None:
    provider = cycle_vcd([{"q": "1111", "ok": "0"}, {"q": "0010", "ok": "1"}])
    result = compare(provider, golden(CYCLE_HEADER, cycle_records({"ok": "0"}, {"q": "0010", "ok": "1"})))
    assert (result.status, result.compared_values) == (AnalysisStatus.PASS, 3)


# --------------------------------------------------------------------------
# Comparison: non-pass, non-fail outcomes
# --------------------------------------------------------------------------


def codes_of(result: ComparisonResult) -> list[str]:
    return [diagnostic.code for diagnostic in result.diagnostics]


def test_incomplete_evidence_is_inconclusive_never_a_pass() -> None:
    only_q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}
    provider = cycle_vcd([{"q": "0001"}, {"q": "0010"}])
    three = golden(only_q, cycle_records({"q": "0001"}, {"q": "0010"}, {"q": "0010"}, {"q": "0010"}))
    early = compare(provider, three)
    assert (early.status, codes_of(early)) == (AnalysisStatus.INCONCLUSIVE, ["TRACE_ENDED_EARLY"])
    assert early.failure is None and early.compared_values == 2


def test_missing_or_wrong_width_signals_are_invalid_input() -> None:
    provider = cycle_vcd([{"q": "0001"}])
    absent = {**CYCLE_HEADER, "signals": [{"name": "q", "path": "top.nope", "width": 4}]}
    result = compare(provider, golden(absent, cycle_records({"q": "0001"})))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INVALID_INPUT, ["SIGNAL_NOT_FOUND"])
    narrow = {**CYCLE_HEADER, "signals": [{"name": "q", "path": "top.q", "width": 3}]}
    result = compare(provider, golden(narrow, cycle_records({"q": "001"})))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INVALID_INPUT, ["WIDTH_MISMATCH"])
    wide_clock = {**CYCLE_HEADER, "clock": "top.q", "signals": CYCLE_HEADER["signals"][:1]}
    result = compare(provider, golden(wide_clock, cycle_records({"q": "0001"})))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INVALID_INPUT, ["WIDTH_MISMATCH"])


def reset_trace(reset_values: str) -> VcdWaveformProvider:
    """``reset_values[k]`` is rst_n as seen before rising edge k (edges at 5, 15, ... ns)."""
    lines = ["$timescale 1 ns $end", "$scope module top $end", "$var wire 1 c clk $end", "$var wire 1 r rst_n $end",
             "$var wire 4 q q [3:0] $end", "$upscope $end", "$enddefinitions $end", "#0", "0c", f"{reset_values[0]}r", "b0001 q"]
    for index in range(len(reset_values)):
        lines += [f"#{10 * index + 5}", "1c", f"#{10 * index + 10}", "0c"]
        if index + 1 < len(reset_values) and reset_values[index + 1] != reset_values[index]:
            lines.append(f"{reset_values[index + 1]}r")
    return VcdWaveformProvider.from_text("\n".join(lines) + "\n")


def test_reset_rules() -> None:
    only_q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}
    one = golden(only_q, cycle_records({"q": "0001"}))
    two = golden(only_q, cycle_records({"q": "0001"}, {"q": "0001"}, {"q": "0001"}))

    assert compare(reset_trace("0011"), one).status is AnalysisStatus.PASS
    # x before reset is first asserted is ignored; the epoch still starts after it.
    assert compare(reset_trace("x011"), one).status is AnalysisStatus.PASS

    never = compare(reset_trace("1111"), one)
    assert (never.status, codes_of(never)) == (AnalysisStatus.UNSUPPORTED, ["NO_RESET_OBSERVED"])
    held = compare(reset_trace("0000"), one)
    assert (held.status, codes_of(held)) == (AnalysisStatus.INCONCLUSIVE, ["TRACE_ENDED_EARLY"])
    unknown = compare(reset_trace("01x1"), two)
    assert (unknown.status, codes_of(unknown)) == (AnalysisStatus.INCONCLUSIVE, ["RESET_UNKNOWN"])
    cut_short = compare(reset_trace("0110111"), two)
    assert (cut_short.status, codes_of(cut_short)) == (AnalysisStatus.INCONCLUSIVE, ["EPOCH_ENDED_EARLY"])

    # Active-high reset uses the same rules with the levels swapped.
    high = golden({**only_q, "reset_active": "1"}, cycle_records({"q": "0001"}))
    assert compare(reset_trace("1100"), high).status is AnalysisStatus.PASS


def test_reset_changing_at_the_sampling_edge_is_inconclusive() -> None:
    text = HEADER.replace('$var wire 8 " data [7:0]', "$var wire 1 r rst_n") + "#0\n0!\n0r\n#5\n1!\n#10\n0!\n1r\n#15\n1!\n0r\n#20\n0!\n#25\n"
    header = {"mode": "cycle", "sampling": "settled", "signals": [{"name": "c", "path": "top.clk", "width": 1}]}
    result = compare(VcdWaveformProvider.from_text(text), golden(header, cycle_records({"c": "1"})))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["RESET_AT_EDGE"])


def test_two_epochs_restart_cycle_indexing() -> None:
    only_q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}
    records = cycle_records({"q": "0001"}, {"q": "0001"}) + cycle_records({"q": "0001"}, epoch=1)
    result = compare(reset_trace("0110111"), golden(only_q, records))
    assert (result.status, result.compared_values) == (AnalysisStatus.PASS, 3)
    wrong = cycle_records({"q": "0001"}, {"q": "0001"}) + cycle_records({"q": "1001"}, epoch=1)
    failed = compare(reset_trace("0110111"), golden(only_q, wrong))
    assert failed.failure is not None
    assert (failed.failure.observation.epoch, failed.failure.observation.cycle) == (1, 0)
    assert failed.failure.observation.time_ps == 45000


def test_dump_gap_and_ambiguous_clock_stop_the_comparison() -> None:
    header = {"mode": "cycle", "sampling": "settled", "signals": [{"name": "d", "path": "top.data", "width": 8}]}
    wiring = HEADER.replace("$upscope", "$var wire 1 r rst_n $end\n$upscope")
    gap = wiring + '#0\n0!\n0r\nb1 "\n#5\n1!\n#10\n0!\n1r\n#15\n1!\n#20\n0!\n#22\n$dumpoff\n$end\n#40\n$dumpon\n0!\n1r\nb1 "\n$end\n#45\n1!\n#50\n'
    provider = VcdWaveformProvider.from_text(gap)
    one = [{"epoch": 0, "cycle_index": 0, "values": {"d": "00000001"}}]
    assert compare(provider, golden(header, one)).status is AnalysisStatus.PASS
    two = one + [{"epoch": 0, "cycle_index": 1, "values": {"d": "00000001"}}]
    blocked = compare(provider, golden(header, two))
    assert (blocked.status, codes_of(blocked)) == (AnalysisStatus.INCONCLUSIVE, ["TRACE_GAP"])

    glitch = wiring + '#0\n0!\n0r\nb1 "\n#5\n1!\n#10\n0!\n1r\n#15\n1!\n0!\n1!\n#20\n0!\n#25\n'
    ambiguous = compare(VcdWaveformProvider.from_text(glitch), golden(header, one))
    assert (ambiguous.status, codes_of(ambiguous)) == (AnalysisStatus.INCONCLUSIVE, ["CLOCK_INDETERMINATE"])


# --------------------------------------------------------------------------
# Comparison: handshake mode
# --------------------------------------------------------------------------

STREAM = {"valid": 1, "ready": 1, "data": 8}
HANDSHAKE_HEADER: dict[str, Any] = {
    "mode": "handshake",
    "sampling": "before",
    "valid": "top.valid",
    "ready": "top.ready",
    "signals": [{"name": "data", "path": "top.data", "width": 8}],
}


def byte(value: int) -> str:
    return format(value, "08b")


def stream(rows: list[tuple[str, str, int]], **kwargs: Any) -> VcdWaveformProvider:
    """``rows[k]`` is (valid, ready, payload) as seen before the edge of cycle k."""
    drive = [{"valid": v, "ready": r, "data": byte(d)} for v, r, d in rows]
    return VcdWaveformProvider.from_text(build_vcd(STREAM, drive, **kwargs))


def transactions(*payloads: int, epoch: int = 0) -> list[dict[str, Any]]:
    return [{"epoch": epoch, "transaction_index": i, "values": {"data": byte(p)}} for i, p in enumerate(payloads)]


def test_handshake_pass_with_stalls_and_idle_cycles() -> None:
    rows = [("1", "1", 1), ("1", "0", 2), ("1", "0", 2), ("1", "1", 2), ("0", "1", 99), ("0", "0", 98), ("1", "1", 3), ("0", "1", 0)]
    result = compare(stream(rows), golden(HANDSHAKE_HEADER, transactions(1, 2, 3)))
    assert (result.status, result.compared_values, result.diagnostics) == (AnalysisStatus.PASS, 3, ())


@settings(max_examples=100, deadline=None)
@given(
    st.lists(st.integers(min_value=0, max_value=255), min_size=1, max_size=8),
    st.lists(st.tuples(st.sampled_from("01"), st.sampled_from("01")), max_size=40),
)
def test_any_stall_schedule_yields_the_same_matched_sequence(payloads: list[int], stalls: list[tuple[str, str]]) -> None:
    rows: list[tuple[str, str, int]] = []
    pending = list(payloads)
    for valid, ready in stalls:
        if not pending:
            break
        rows.append((valid, ready, pending[0] if valid == "1" else 0))
        if valid == "1" and ready == "1":
            pending.pop(0)
    rows += [("1", "1", payload) for payload in pending]
    rows.append(("0", "1", 0))
    result = compare(stream(rows), golden(HANDSHAKE_HEADER, transactions(*payloads)))
    assert (result.status, result.compared_values) == (AnalysisStatus.PASS, len(payloads))


def test_handshake_first_wrong_payload_is_the_anchor() -> None:
    rows = [("1", "1", 1), ("1", "0", 7), ("1", "1", 7), ("1", "1", 3), ("0", "1", 0)]
    provider = stream(rows)
    result = compare(provider, golden(HANDSHAKE_HEADER, transactions(1, 2, 3)))
    failure = result.failure
    assert failure is not None
    assert (failure.kind, failure.transaction_index) == (FailureKind.VALUE_MISMATCH, 1)
    assert (failure.observation.cycle, failure.observation.phase) == (2, BEFORE)
    assert failure.observation.time_ps == edge_ps(2)
    assert (failure.expected, failure.actual) == (lv(byte(2)), lv(byte(7)))
    assert result.compared_values == 2
    assert_reproducible(provider, result)


def test_handshake_dropped_word_is_not_hidden_by_resynchronizing() -> None:
    # The design drops the second of three identical-looking words: 5, 5, 6 becomes 5, 6.
    rows = [("1", "1", 5), ("1", "1", 6), ("0", "1", 0), ("0", "1", 0)]
    result = compare(stream(rows), golden(HANDSHAKE_HEADER, transactions(5, 5, 6)))
    assert result.failure is not None
    assert (result.failure.transaction_index, result.failure.actual) == (1, lv(byte(6)))


def test_handshake_extra_transaction() -> None:
    rows = [("1", "1", 1), ("1", "1", 2), ("0", "1", 0), ("1", "1", 9), ("0", "1", 0)]
    provider = stream(rows)
    result = compare(provider, golden(HANDSHAKE_HEADER, transactions(1, 2)))
    failure = result.failure
    assert failure is not None
    assert (failure.kind, failure.transaction_index) == (FailureKind.EXTRA_TRANSACTION, 2)
    assert (failure.expected, failure.actual) == (None, lv(byte(9)))
    assert failure.observation.cycle == 3
    assert_reproducible(provider, result)


def test_handshake_missing_transaction_needs_an_observed_deadline() -> None:
    rows = [("1", "1", 1), ("0", "1", 0), ("0", "1", 0), ("0", "1", 0), ("0", "1", 0)]
    provider = stream(rows)
    expected = transactions(1, 2)

    no_deadline = compare(provider, golden(HANDSHAKE_HEADER, expected))
    assert (no_deadline.status, codes_of(no_deadline)) == (AnalysisStatus.INCONCLUSIVE, ["TRACE_ENDED_EARLY"])

    late_deadline = compare(provider, golden({**HANDSHAKE_HEADER, "deadline_cycle": 50}, expected))
    assert late_deadline.status is AnalysisStatus.INCONCLUSIVE

    result = compare(provider, golden({**HANDSHAKE_HEADER, "deadline_cycle": 3}, expected))
    failure = result.failure
    assert failure is not None
    assert (failure.kind, failure.transaction_index) == (FailureKind.MISSING_TRANSACTION, 1)
    assert (failure.expected, failure.actual) == (lv(byte(2)), None)
    assert (failure.observation.cycle, failure.observation.time_ps) == (3, edge_ps(3))

    # A transfer that lands exactly on the deadline edge satisfies it.
    on_time = stream([("1", "1", 1), ("0", "1", 0), ("0", "1", 0), ("1", "1", 2), ("0", "1", 0)])
    assert compare(on_time, golden({**HANDSHAKE_HEADER, "deadline_cycle": 3}, expected)).status is AnalysisStatus.PASS


def test_handshake_unknown_valid_or_ready_is_never_guessed() -> None:
    expected = golden(HANDSHAKE_HEADER, transactions(1))
    for valid, ready in [("x", "1"), ("1", "x"), ("z", "1"), ("x", "x")]:
        result = compare(stream([(valid, ready, 1), ("1", "1", 1), ("0", "1", 0)]), expected)
        assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["HANDSHAKE_UNKNOWN"])
    # A known 0 on either side means no transfer, whatever the other side is.
    for valid, ready in [("0", "x"), ("x", "0")]:
        result = compare(stream([(valid, ready, 1), ("1", "1", 1), ("0", "1", 0)]), expected)
        assert result.status is AnalysisStatus.PASS


def test_handshake_unknown_payload_is_unknown_value() -> None:
    text = build_vcd(STREAM, [{"valid": "1", "ready": "1", "data": "0000x001"}, {"valid": "0"}])
    result = compare(VcdWaveformProvider.from_text(text), golden(HANDSHAKE_HEADER, transactions(1)))
    assert result.failure is not None
    assert (result.failure.kind, result.failure.actual) == (FailureKind.UNKNOWN_VALUE, lv("0000x001"))


def test_handshake_reset_starts_a_new_epoch_and_cancels_pending_transactions() -> None:
    drive = [
        {"valid": "1", "ready": "1", "data": byte(1)},
        {"valid": "0"},
        {"rst_n": "0"},
        {"rst_n": "1", "valid": "1", "data": byte(7)},
        {"valid": "0"},
    ]
    # build_vcd re-drives rst_n high on every falling edge unless the row sets it.
    provider = VcdWaveformProvider.from_text(build_vcd(STREAM, drive))
    two_epochs = transactions(1) + transactions(7, epoch=1)
    result = compare(provider, golden(HANDSHAKE_HEADER, two_epochs))
    assert (result.status, result.compared_values, result.diagnostics) == (AnalysisStatus.PASS, 2, ())

    # Spec 7 (decision 10): expectations cancelled by reset are not waived, so a
    # word the design may have dropped before the reset can never pass.
    pending = transactions(1, 2) + transactions(7, epoch=1)
    cancelled = compare(provider, golden(HANDSHAKE_HEADER, pending))
    assert (cancelled.status, codes_of(cancelled)) == (
        AnalysisStatus.INCONCLUSIVE,
        ["EXPECTATIONS_CANCELLED_BY_RESET"],
    )
    assert cancelled.failure is None and cancelled.compared_values == 1

    # Reset does not rewind one global stream: epoch 1 is checked against epoch 1.
    rewound = compare(provider, golden(HANDSHAKE_HEADER, transactions(1) + transactions(1, epoch=1)))
    assert rewound.failure is not None
    assert (rewound.failure.observation.epoch, rewound.failure.transaction_index) == (1, 0)

    one_epoch = compare(provider, golden(HANDSHAKE_HEADER, transactions(1)))
    assert one_epoch.failure is not None
    assert one_epoch.failure.kind is FailureKind.EXTRA_TRANSACTION
    assert one_epoch.failure.observation.epoch == 1


def test_handshake_multi_signal_payload_uses_declaration_order() -> None:
    widths = {"valid": 1, "ready": 1, "data": 8, "last": 1}
    text = build_vcd(widths, [{"valid": "1", "ready": "1", "data": byte(4), "last": "1"}, {"valid": "0"}])
    provider = VcdWaveformProvider.from_text(text)
    header = {**HANDSHAKE_HEADER, "signals": [{"name": "last", "path": "top.last", "width": 1}, *HANDSHAKE_HEADER["signals"]]}
    records = [{"epoch": 0, "transaction_index": 0, "values": {"data": byte(5), "last": "0"}}]
    result = compare(provider, golden(header, records))
    assert result.failure is not None and result.failure.signal_path == "top.last"
    assert [a.signal_path for a in result.additional_mismatches] == ["top.data"]
    assert all(a.transaction_index == 0 for a in (result.failure, *result.additional_mismatches))
    assert result.compared_values == 2


def test_handshake_gap_after_completion_cannot_rule_out_extras() -> None:
    text = build_vcd(STREAM, [{"valid": "1", "ready": "1", "data": byte(1)}, {"valid": "0"}], tail_cycles=1)
    gapped = text + "#62\n$dumpoff\n$end\n#80\n"
    result = compare(VcdWaveformProvider.from_text(gapped), golden(HANDSHAKE_HEADER, transactions(1)))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["TRACE_GAP"])
    assert result.compared_values == 1


# --------------------------------------------------------------------------
# Hierarchy glob selection (keep)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tb.dut.*", "tb.dut.clk", True),
        ("tb.dut.*", "tb.dut.u.clk", False),  # * stays inside one component
        ("tb.dut.*", "tb.dut", False),
        ("tb.*.clk", "tb.dut.clk", True),
        ("tb.**.clk", "tb.clk", True),  # ** matches zero components
        ("tb.**.clk", "tb.dut.clk", True),
        ("tb.**.clk", "tb.dut.u.v.clk", True),
        ("tb.**.clk", "tb.dut.clk_en", False),
        ("tb.**", "tb.dut.clk", True),
        ("tb.**", "tb", True),
        ("**", "tb.dut.clk", True),
        ("**.clk", "clk", True),
        ("tb.**.**.clk", "tb.clk", True),
        ("tb.**.u.**.q", "tb.a.u.b.c.q", True),
        ("tb.**.u.**.q", "tb.a.b.q", False),
        ("tb.dut.w?tr", "tb.dut.wptr", True),
        ("tb.dut.w?tr", "tb.dut.wtr", False),
        ("tb.dut.w?tr", "tb.dut.wpptr", False),
        ("tb.dut.?ptr", "tb.dut.wptr", True),
        ("tb.d?t.wptr", "tb.d.t.wptr", False),  # ? never matches the separator
        ("tb.dut.*ptr", "tb.dut.ptr", True),  # * may match nothing
        ("tb.dut.in_*", "tb.dut.in_valid", True),
        ("tb.dut.CLK", "tb.dut.clk", False),  # case-sensitive
        ("tb.dut.clk", "tb.dut.clk", True),
        ("tb.dut.clk", "tb.dut.clk2", False),  # whole path, not a prefix
        ("dut.clk", "tb.dut.clk", False),  # anchored at the start
        ("top.mem[3]", "top.mem[3]", True),  # brackets are literal
        ("top.mem[3]", "top.mem3", False),
        ("top.mem[?]", "top.mem[3]", True),
        ("top.mem[*]", "top.mem[12]", True),
        ("top.gen[2].*", "top.gen[2].q", True),
        ("top.gen[2].*", "top.gen[1].q", False),
        ("top.my$sig", "top.my$sig", True),  # regex metacharacters are literal
        ("top.a+b", "top.aab", False),
    ],
)
def test_glob_pattern_semantics(pattern: str, path: str, expected: bool) -> None:
    if expected:
        assert match_paths([path], [pattern]) == (path,)
    else:
        with pytest.raises(InvalidQueryError, match=re.escape(repr(pattern))):
            match_paths([path], [pattern])


def test_select_preserves_input_order_without_duplicates() -> None:
    paths = ["tb.dut.wptr", "tb.dut.clk", "tb.dut.rptr", "tb.top.clk"]
    assert match_paths(paths, ["**.clk", "tb.dut.*", "tb.dut.?ptr"]) == tuple(paths)
    assert match_paths(paths, ["tb.dut.rptr", "tb.dut.wptr"]) == ("tb.dut.wptr", "tb.dut.rptr")
    assert match_paths([*paths, "tb.dut.clk"], ["tb.dut.clk"]) == ("tb.dut.clk",)


SELECT_HEADER = """$timescale 1 ns $end
$scope module tb $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 3 # wptr [2:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
"""


@pytest.mark.parametrize(
    ("patterns", "fragment"),
    [
        (["tb.dut.nothing"], "tb.dut.nothing"),
        (["tb.dut.clk", "tb.**.nope"], "tb.**.nope"),
        (["TB.dut.clk"], "TB.dut.clk"),
        ([""], "non-empty"),
        (["tb..clk"], "empty component"),
        (["tb.dut."], "empty component"),
        (["tb.a**.clk"], "whole component"),
        ([], "at least one"),
        ("tb.dut.clk", "not one string"),
    ],
)
def test_select_rejects_unmatched_and_malformed_patterns(patterns: Any, fragment: str) -> None:
    with pytest.raises(InvalidQueryError, match=re.escape(fragment)):
        match_paths(["tb.dut.clk", "tb.dut.wptr"], patterns)
    with pytest.raises(InvalidQueryError, match=re.escape(fragment)):
        parse_vcd(SELECT_HEADER, keep=patterns)


def assert_kept_signals_unchanged(full: VcdWaveformProvider, kept: VcdWaveformProvider, paths: list[str]) -> None:
    assert kept.trace.signal_paths == tuple(paths)
    assert kept.get_coverage() == full.get_coverage()
    assert kept.trace.unavailable == full.trace.unavailable
    assert kept.trace.skipped == full.trace.skipped
    start, end = full.get_coverage().start_ps, full.get_coverage().end_ps + 1
    for path in paths:
        assert kept.get_signal_decl(path) == full.get_signal_decl(path)
        assert kept.trace._series_by_path[path] == full.trace._series_by_path[path]
    assert kept.get_window_slice(paths, start, end) == full.get_window_slice(paths, start, end)


def test_keep_matches_unfiltered_results_on_the_fifo_fixture(fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    full = VcdWaveformProvider.from_text(text)
    keep = ["tb.dut.clk", "tb.dut.rst_n", "tb.dut.?ptr", "tb.**.full", "tb.dut.empty", "tb.dut.out_*"]
    kept = VcdWaveformProvider(parse_vcd(text, keep=keep))
    paths = [
        "tb.dut.clk", "tb.dut.rst_n", "tb.dut.out_valid", "tb.dut.out_ready", "tb.dut.out_data",
        "tb.dut.full", "tb.dut.empty", "tb.dut.wptr", "tb.dut.rptr",
    ]
    assert_kept_signals_unchanged(full, kept, paths)
    for path in paths:
        for time_ps in range(0, 100001, 2500):
            for phase in (BEFORE, SETTLED):
                assert kept.get_value_at(path, time_ps, phase=phase) == full.get_value_at(path, time_ps, phase=phase)
    for edge in ClockEdge:
        assert kept.get_clock_edges("tb.dut.clk", 0, 100001, edge=edge) == full.get_clock_edges(
            "tb.dut.clk", 0, 100001, edge=edge
        )
    for absent in ("tb.dut.in_data", "tb.dut.in_valid", "tb.dut.push", "tb.dut.pop"):
        with pytest.raises(SignalNotFoundError):
            kept.get_value_at(absent, 0)
        with pytest.raises(SignalNotFoundError):
            kept.get_signal_decl(absent)

    # The golden comparison only needs the kept signals and is unchanged by filtering.
    assert compare(kept, fixture.golden) == compare(full, fixture.golden)
    # A golden signal left out of keep is reported as missing, never guessed.
    narrow = VcdWaveformProvider(parse_vcd(text, keep=["tb.dut.clk", "tb.dut.rst_n"]))
    missing = compare(narrow, fixture.golden)
    assert (missing.status, codes_of(missing)) == (AnalysisStatus.INVALID_INPUT, ["SIGNAL_NOT_FOUND"])


def test_keep_none_is_the_unfiltered_parse_and_keep_everything_is_identical(fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    assert parse_vcd(text, keep=None) == parse_vcd(text)
    assert parse_vcd(text, keep=["**"]) == parse_vcd(text)


def test_keep_retains_an_identifier_code_shared_with_an_unkept_alias() -> None:
    text = """$timescale 1ps $end
$scope module top $end
$var wire 4 ! bus [3:0] $end
$var wire 1 # other $end
$scope module u $end
$var wire 4 ! port [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b1010 !
0#
#5
b0x01 !
1#
#9
"""
    full = VcdWaveformProvider.from_text(text)
    kept = VcdWaveformProvider(parse_vcd(text, keep=["top.u.port"]))
    assert kept.trace.signal_paths == ("top.u.port",)
    for time_ps in range(10):
        assert kept.get_value_at("top.u.port", time_ps) == full.get_value_at("top.bus", time_ps)
    for absent in ("top.bus", "top.other"):
        with pytest.raises(SignalNotFoundError):
            kept.get_value_at(absent, 5)
    both = VcdWaveformProvider(parse_vcd(text, keep=["top.bus", "top.**.port"]))
    assert both.trace.signal_paths == ("top.bus", "top.u.port")
    assert both.trace._series_by_path["top.bus"] is both.trace._series_by_path["top.u.port"]


def test_keep_still_validates_unselected_value_changes() -> None:
    keep = ["top.clk"]
    for bad in [
        HEADER + '#0\n0!\nb111111111 "\n',  # wider than declared
        HEADER + '#0\n0!\n1"\n',  # scalar change for a vector
        HEADER + '#0\n0!\nb12 "\n',  # invalid digit
        HEADER + '#0\n0!\nr1.5 "\n',  # real value for a digital signal
        HEADER + "#0\n0!\n0?\n",  # undeclared identifier code
    ]:
        with pytest.raises(InvalidTraceError):
            parse_vcd(bad)
        with pytest.raises(InvalidTraceError):
            parse_vcd(bad, keep=keep)
    # A record for an unselected code before the first timestamp still sets coverage.
    early = HEADER + 'b1 "\n#3\n1!\n#7\n'
    assert parse_vcd(early, keep=keep).coverage == parse_vcd(early).coverage == TraceCoverage(start_ps=0, end_ps=7000)


def test_keep_leaves_dump_gaps_unchanged_and_restate_checks_kept_signals_only() -> None:
    full = VcdWaveformProvider.from_text(GAP_TEXT)
    for path in ("top.data", "top.clk"):
        kept = VcdWaveformProvider(parse_vcd(GAP_TEXT, keep=[path]))
        assert_kept_signals_unchanged(full, kept, [path])
        with pytest.raises(DataUnavailableError):
            kept.get_value_at(path, 40000)

    partial = HEADER + '#0\n0!\nb1 "\n#10\n$dumpoff\n$end\n#20\n$dumpon\n0!\n$end\n#30\n'
    with pytest.raises(InvalidTraceError, match="did not restate"):
        parse_vcd(partial)
    with pytest.raises(InvalidTraceError, match="did not restate"):
        parse_vcd(partial, keep=["top.data"])
    kept_clk = VcdWaveformProvider(parse_vcd(partial, keep=["top.clk"]))
    assert kept_clk.trace.unavailable == (TimeInterval(start_ps=10000, end_ps=20000),)
    assert kept_clk.get_value_at("top.clk", 20000) == lv("0")


def test_keep_preserves_same_timestamp_clock_ambiguity() -> None:
    text = HEADER + '#0\n0!\nb0 "\n#5\n1!\n0!\n1!\nb1 "\nb10 "\n#10\n0!\n#15\n'
    kept = VcdWaveformProvider(parse_vcd(text, keep=["top.clk"]))
    with pytest.raises(IndeterminateClockError):
        kept.get_clock_edges("top.clk", 0, 15001)
    assert kept.trace._series_by_path["top.clk"] == parse_vcd(text)._series_by_path["top.clk"]


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=40),
            st.sampled_from("abc"),
            st.text(alphabet="01xz", min_size=1, max_size=2),
        ),
        min_size=1,
        max_size=30,
    ),
    st.sets(st.sampled_from(["top.a", "top.u.b", "top.u.c"]), min_size=1),
)
def test_keep_never_changes_a_kept_signal_under_property_tests(
    records: list[tuple[int, str, str]], kept_paths: set[str]
) -> None:
    lines = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$var wire 2 a a [1:0] $end",
        "$scope module u $end",
        "$var wire 2 b b [1:0] $end",
        "$var wire 2 c c [1:0] $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    for time, code, bits in sorted(records, key=lambda item: item[0]):
        lines += [f"#{time}", f"b{bits} {code}"]
    text = "\n".join(lines) + "\n"
    full = VcdWaveformProvider.from_text(text)
    paths = [path for path in full.trace.signal_paths if path in kept_paths]
    kept = VcdWaveformProvider(parse_vcd(text, keep=sorted(kept_paths)))
    assert_kept_signals_unchanged(full, kept, paths)


# --------------------------------------------------------------------------
# Review regressions (PR #4 audit)
# --------------------------------------------------------------------------

CRQ_HEADER = """$timescale 1 ns $end
$scope module top $end
$var wire 1 c clk $end
$var wire 1 r rst_n $end
$var wire 4 q q [3:0] $end
$upscope $end
$enddefinitions $end
"""
ONLY_Q = {**CYCLE_HEADER, "signals": CYCLE_HEADER["signals"][:1]}


def crq(events: dict[int, str]) -> VcdWaveformProvider:
    """clk/rst_n/q trace from ``{time_ns: "space-separated records"}``."""
    lines = [CRQ_HEADER.rstrip("\n")]
    for time_ns in sorted(events):
        lines.append(f"#{time_ns}")
        lines += events[time_ns].split()
    return VcdWaveformProvider.from_text("\n".join(lines) + "\n")


def clocked(extra: dict[int, str], *, last: int = 60) -> dict[int, str]:
    """clk toggling every 5 ns (rising at 5, 15, ...), rst_n low until its release at 10 ns."""
    events = {0: "0c 0r b0001 q"}
    for time_ns in range(5, last + 1, 5):
        events[time_ns] = "1c" if time_ns % 10 == 5 else "0c"
    events[10] += " 1r"
    for time_ns, records in extra.items():
        events[time_ns] = f"{events.get(time_ns, '')} {records}".strip()
    return events


def test_reset_released_exactly_at_a_rising_edge_is_inconclusive() -> None:
    events = clocked({})
    events[10] = "0c"  # no release on the falling edge ...
    events[15] = "1c 1r"  # ... it is released at the rising edge instead
    result = compare(crq(events), golden(ONLY_Q, cycle_records({"q": "0001"})))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["RESET_AT_EDGE"])


def test_reset_asserted_exactly_at_a_rising_edge_is_inconclusive() -> None:
    events = clocked({35: "0r"})
    three = golden(ONLY_Q, cycle_records({"q": "0001"}, {"q": "0001"}, {"q": "0001"}))
    result = compare(crq(events), three)
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["RESET_AT_EDGE"])


def test_same_timestamp_reset_pulse_at_an_edge_is_inconclusive() -> None:
    events = clocked({35: "0r 1r"})  # 1 -> 0 -> 1 at the edge; the final value is unchanged
    three = golden(ONLY_Q, cycle_records({"q": "0001"}, {"q": "0001"}, {"q": "0001"}))
    result = compare(crq(events), three)
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["RESET_AT_EDGE"])
    # The same pulse between edges is legal and does not affect sampling.
    between = compare(crq(clocked({30: "0r 1r"})), three)
    assert between.status is AnalysisStatus.PASS


def test_later_clock_ambiguity_does_not_hide_an_earlier_failure() -> None:
    glitch = clocked({35: "0c 1c"})  # three clock records at 35 ns
    provider = crq(glitch)
    with pytest.raises(IndeterminateClockError):
        provider.get_clock_edges("top.clk", 0, provider.get_coverage().end_ps + 1)

    wrong = golden(ONLY_Q, cycle_records({"q": "0011"}, {"q": "0011"}, {"q": "0011"}))
    failed = compare(provider, wrong)
    assert failed.status is AnalysisStatus.FAIL
    assert failed.failure is not None and failed.failure.observation.time_ps == 15000
    assert_reproducible_before(provider, failed, 35000)

    # Cycle mode that completes before the ambiguity passes, like a later dump gap.
    assert compare(provider, golden(ONLY_Q, cycle_records({"q": "0001"}, {"q": "0001"}))).status is AnalysisStatus.PASS
    # Expectations that need the ambiguous region stay inconclusive.
    blocked = compare(provider, golden(ONLY_Q, cycle_records({"q": "0001"}, {"q": "0001"}, {"q": "0001"})))
    assert (blocked.status, codes_of(blocked)) == (AnalysisStatus.INCONCLUSIVE, ["CLOCK_INDETERMINATE"])
    assert blocked.compared_values == 2


def assert_reproducible_before(provider: VcdWaveformProvider, result: ComparisonResult, limit_ps: int) -> None:
    assert result.failure is not None
    for anchor in (result.failure, *result.additional_mismatches):
        if anchor.actual is not None:
            observed = provider.get_value_at(anchor.signal_path, anchor.observation.time_ps, phase=anchor.observation.phase)
            assert observed == anchor.actual
        assert anchor.observation.time_ps in provider.get_clock_edges(anchor.clock_path, 0, limit_ps)


def test_handshake_epoch_cancelled_without_any_transfer_is_not_a_pass() -> None:
    drive = [{"valid": "0"}, {"rst_n": "0"}, {"valid": "0"}, {"valid": "0"}]
    provider = VcdWaveformProvider.from_text(build_vcd(STREAM, drive))
    result = compare(provider, golden(HANDSHAKE_HEADER, transactions(1)))
    assert result.status is AnalysisStatus.INCONCLUSIVE
    assert result.compared_values == 0
    assert codes_of(result) == ["EXPECTATIONS_CANCELLED_BY_RESET"]


@pytest.mark.parametrize(
    "body",
    [
        '#0\n$dumpvars\n0!\nb1 "\n',  # $dumpvars never closed
        '#0\n$dumpvars\n0!\nb1 "\n#10\n1!\n#20\n',  # time advances inside $dumpvars
        '#0\n$dumpvars\n0!\n$dumpall\nb1 "\n$end\n#10\n',  # nested block
        '#0\n0!\nb1 "\n$end\n#10\n',  # $end with no open block
        '#0\n0!\nb1 "\n#10\n$dumpoff\nx!\nbx "\n',  # $dumpoff never closed
    ],
)
def test_unterminated_or_unbalanced_dump_blocks_are_rejected(body: str) -> None:
    with pytest.raises(InvalidTraceError):
        parse_vcd(HEADER + body)


def test_value_changes_inside_a_dump_gap_are_still_validated() -> None:
    wide = HEADER + '#0\n0!\nb1 "\n#10\n$dumpoff\nx!\nb111111111 "\n$end\n#20\n$dumpon\n0!\nb1 "\n$end\n#30\n'
    for keep in (None, ["top.clk"]):
        with pytest.raises(InvalidTraceError, match="wider"):
            parse_vcd(wide, keep=keep)
    scalar = HEADER + '#0\n0!\nb1 "\n#10\n$dumpoff\nx!\nx"\n$end\n#20\n$dumpon\n0!\nb1 "\n$end\n#30\n'
    with pytest.raises(InvalidTraceError, match="scalar"):
        parse_vcd(scalar)


@pytest.mark.parametrize(
    "text",
    [
        HEADER + "#0\n0!\n#²\n",  # superscript two passes str.isdigit()
        HEADER + "#0\n0!\n#٣\n",  # Arabic-Indic three
        HEADER.replace('$var wire 8 "', '$var wire ² "') + "#0\n",
    ],
)
def test_non_ascii_digits_are_invalid_not_a_crash(text: str) -> None:
    with pytest.raises(InvalidTraceError):
        parse_vcd(text)


def test_timestamps_beyond_max_time_ps_are_unsupported_not_a_crash(tmp_path: Path) -> None:
    ps_header = HEADER.replace("1 ns", "1 ps")
    fs_header = HEADER.replace("1 ns", "1 fs")
    for text in [
        HEADER + f"#0\n0!\n#{'9' * 5000}\n",  # never reaches int()
        ps_header + f"#0\n0!\n#{MAX_TIME_PS}\n",  # coverage end must stay below the bound
        ps_header + f"#0\n0!\n#{MAX_TIME_PS + 1}\n",
        HEADER + f"#0\n0!\n#{MAX_TIME_PS // 1000 + 1}\n",  # in bound as ticks, not as ps
    ]:
        with pytest.raises(UnsupportedTraceError, match="MAX_TIME_PS"):
            parse_vcd(text)
    vcd = tmp_path / "long.vcd"
    vcd.write_text(HEADER + f"#0\n0!\n#{'9' * 5000}\n", encoding="utf-8")
    result = compare_files(vcd, FIXTURES / "mock_fifo_golden.jsonl")
    assert (result.status, codes_of(result)) == (AnalysisStatus.UNSUPPORTED, ["TRACE_UNSUPPORTED"])
    # The largest legal values stay exact, including femtosecond ticks above 39 digits.
    top = VcdWaveformProvider.from_text(ps_header + f"#0\n0!\n#{MAX_TIME_PS - 1}\n1!\n")
    assert top.get_coverage().end_ps == MAX_TIME_PS - 1
    fs_top = VcdWaveformProvider.from_text(fs_header + f"#0\n0!\n#{(MAX_TIME_PS - 1) * 1000}\n1!\n")
    assert fs_top.get_coverage().end_ps == MAX_TIME_PS - 1


def test_unknown_clock_transition_after_reset_is_inconclusive() -> None:
    """Spec 6.1 (decision 9): an x/z clock span may hide an edge and shift every
    later cycle index, so the comparison stops at the next edge."""
    events = clocked({})
    events[25] = "xc"  # instead of rising at 25 ns, the clock goes unknown
    provider = crq(events)
    assert provider.get_clock_edges("top.clk", 0, 60001) == (5000, 15000, 35000, 45000, 55000)
    two = golden(ONLY_Q, cycle_records({"q": "0001"}, {"q": "0001"}))
    result = compare(provider, two)
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["CLOCK_UNKNOWN_TRANSITION"])
    assert "25000" in result.diagnostics[0].message
    assert result.compared_values == 1  # cycle 0 at 15 ns was compared before the x span

    # A failure found before the x span is still a failure.
    wrong = compare(provider, golden(ONLY_Q, cycle_records({"q": "0011"}, {"q": "0001"})))
    assert wrong.status is AnalysisStatus.FAIL
    assert wrong.failure is not None and wrong.failure.observation.time_ps == 15000
    # A comparison that completes before the x span is unaffected.
    assert compare(provider, golden(ONLY_Q, cycle_records({"q": "0001"}))).status is AnalysisStatus.PASS
    # An x span during reset, after reset was first observed, still counts.
    during_reset = clocked({})
    during_reset[10] = "xc"  # reset releases on this falling edge; clock goes x instead
    during_reset[12] = "0c 1r"
    blocked = compare(crq(during_reset), golden(ONLY_Q, cycle_records({"q": "0001"})))
    assert codes_of(blocked) == ["CLOCK_UNKNOWN_TRANSITION"]
    # An unknown clock before reset is first observed active is ignored.
    early = clocked({})
    early[0] = "xc 0r b0001 q"
    early[2] = "0c"
    quiet = compare(crq(early), golden(ONLY_Q, cycle_records({"q": "0001"})))
    assert (quiet.status, quiet.diagnostics) == (AnalysisStatus.PASS, ())


def test_handshake_pass_needs_a_known_clock_until_the_end() -> None:
    """Ruling out extra transactions needs every later edge, so an x clock
    after the last expected transfer still blocks the pass."""
    text = build_vcd(STREAM, [{"valid": "1", "ready": "1", "data": byte(1)}, {"valid": "0"}], tail_cycles=2)
    clean = VcdWaveformProvider.from_text(text)
    assert compare(clean, golden(HANDSHAKE_HEADER, transactions(1))).status is AnalysisStatus.PASS
    glitched = VcdWaveformProvider.from_text(text + "#72\nxc0\n#74\n0c0\n#80\n")
    result = compare(glitched, golden(HANDSHAKE_HEADER, transactions(1)))
    assert (result.status, codes_of(result)) == (AnalysisStatus.INCONCLUSIVE, ["CLOCK_UNKNOWN_TRANSITION"])
