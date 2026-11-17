"""Contract tests for ``src/contracts.py`` and the mock FIFO fixture.

These tests exercise shared data models, validation, serialization, and the
mock provider's query semantics. They do not exercise a waveform parser, a
comparison engine, or a causal engine, and are not end-to-end coverage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.contracts import (
    AnalysisStatus,
    BitRange,
    CausalReport,
    ClockEdge,
    ComparisonMode,
    ComparisonResult,
    DataUnavailableError,
    Diagnostic,
    EdgeType,
    FailureAnchor,
    FailureKind,
    GoldenTrace,
    InvalidQueryError,
    LogicValue,
    MockFixture,
    MockSignal,
    MockTrace,
    MockWaveformProvider,
    Observation,
    OutOfRangeError,
    Provenance,
    ProvenanceKind,
    SamplingPhase,
    SignalDecl,
    SignalNotFoundError,
    SignalSample,
    TimeInterval,
    TimeUnit,
    TraceCoverage,
    UnsupportedPrecisionError,
    WaveformProvider,
    WaveformWindow,
    load_mock_fixture,
    parse_golden_jsonl,
    to_picoseconds,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent
FIXTURE_PATH = FIXTURES / "mock_fifo_failure.json"
GOLDEN_PATH = FIXTURES / "mock_fifo_golden.jsonl"

CLK = "tb.dut.clk"
RST = "tb.dut.rst_n"
WPTR = "tb.dut.wptr"
FULL = "tb.dut.full"
BEFORE = SamplingPhase.BEFORE
SETTLED = SamplingPhase.SETTLED
CYCLE_EDGES = tuple(25000 + 10000 * k for k in range(8))


@pytest.fixture(scope="module")
def fixture() -> MockFixture:
    return load_mock_fixture(FIXTURE_PATH)


@pytest.fixture(scope="module")
def provider(fixture: MockFixture) -> MockWaveformProvider:
    return MockWaveformProvider(fixture.trace)


@pytest.fixture()
def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data


def lv(bits: str) -> LogicValue:
    return LogicValue(width=len(bits), bits=bits)


def scalar(path: str, *samples: tuple[int, str]) -> MockSignal:
    return MockSignal(
        decl=SignalDecl(path=path, width=1),
        samples=tuple(SignalSample(time_ps=t, value=lv(v)) for t, v in samples),
    )


def gap_provider() -> MockWaveformProvider:
    """A trace covering [0, 100] with dumping off during [40, 60)."""
    trace = MockTrace(
        coverage=TraceCoverage(start_ps=0, end_ps=100),
        unavailable=(TimeInterval(start_ps=40, end_ps=60),),
        signals=(
            scalar("top.clk", (0, "0"), (10, "1"), (20, "0"), (30, "1"), (60, "1"), (70, "0")),
            scalar("top.d", (0, "x"), (15, "1"), (60, "1"), (80, "z")),
        ),
    )
    return MockWaveformProvider(trace)


def cycle_anchor(**overrides: Any) -> FailureAnchor:
    fields: dict[str, Any] = {
        "kind": FailureKind.VALUE_MISMATCH,
        "mode": ComparisonMode.CYCLE,
        "signal_path": FULL,
        "clock_path": CLK,
        "observation": Observation(epoch=0, cycle=3, phase=SETTLED, time_ps=55000),
        "expected": lv("1"),
        "actual": lv("0"),
        "detail": "expected high",
    }
    fields.update(overrides)
    return FailureAnchor(**fields)


# --------------------------------------------------------------------------
# Fixture loading and JSON round-trip
# --------------------------------------------------------------------------


def test_fixture_loads_and_is_labeled_synthetic(fixture: MockFixture) -> None:
    assert fixture.schema_version == 1
    assert fixture.provenance.kind is ProvenanceKind.SYNTHETIC_FIXTURE
    assert fixture.report.provenance.kind is ProvenanceKind.SYNTHETIC_FIXTURE
    assert {d.code for d in fixture.report.diagnostics} >= {"SYNTHETIC_FIXTURE"}
    assert len(fixture.trace.signals) == 14
    assert fixture.trace.coverage == TraceCoverage(start_ps=0, end_ps=100000)


def test_fixture_json_round_trip_is_lossless(fixture: MockFixture) -> None:
    text = fixture.model_dump_json()
    again = MockFixture.model_validate_json(text)
    assert again == fixture
    assert again.model_dump_json() == text


def test_fixture_round_trips_through_plain_json_objects(fixture: MockFixture) -> None:
    as_objects = json.loads(fixture.model_dump_json())
    assert MockFixture.model_validate(as_objects) == fixture


def test_timestamps_serialize_as_decimal_strings_and_stay_exact() -> None:
    huge = 2**70 + 1
    sample = SignalSample(time_ps=huge, value=lv("1"))
    encoded = json.loads(sample.model_dump_json())
    assert encoded["time_ps"] == str(huge)
    assert SignalSample.model_validate_json(sample.model_dump_json()).time_ps == huge
    assert SignalSample.model_validate({"time_ps": huge, "value": lv("1")}).time_ps == huge
    assert sample.model_dump()["time_ps"] == huge


@pytest.mark.parametrize("bad", [-1, 1.0, 1.5, True, "1.0", "01", "-1", "1e3", " 1", "", None])
def test_invalid_timestamps_are_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        SignalSample.model_validate({"time_ps": bad, "value": {"width": 1, "bits": "1"}})


def test_unknown_fields_and_schema_versions_are_rejected(raw: dict[str, Any]) -> None:
    raw["unexpected"] = 1
    with pytest.raises(ValidationError):
        MockFixture.model_validate(raw)
    del raw["unexpected"]
    raw["schema_version"] = 2
    with pytest.raises(ValidationError):
        MockFixture.model_validate(raw)


# --------------------------------------------------------------------------
# Four-state values and widths
# --------------------------------------------------------------------------


def test_logic_value_preserves_x_and_z() -> None:
    value = LogicValue.model_validate_json('{"width": 4, "bits": "1xz0"}')
    assert value.bits == "1xz0"
    assert not value.is_fully_known
    assert json.loads(value.model_dump_json()) == {"width": 4, "bits": "1xz0"}
    with pytest.raises(ValueError, match="contains x/z"):
        value.to_unsigned()


def test_logic_value_normalizes_case_and_keeps_leading_zeros() -> None:
    assert LogicValue(width=4, bits="0XZ1").bits == "0xz1"
    assert lv("00101010").to_unsigned() == 0x2A
    assert LogicValue.from_unsigned(5, 8).bits == "00000101"
    assert LogicValue.unknown(3).bits == "xxx"
    assert lv("x") != lv("z") != lv("0")


@pytest.mark.parametrize(
    "payload",
    [
        {"width": 3, "bits": "01"},
        {"width": 2, "bits": "012"},
        {"width": 0, "bits": ""},
        {"width": "2", "bits": "01"},
        {"width": 2.0, "bits": "01"},
        {"width": 2, "bits": 1},
        {"width": 4, "bits": "0x2a"},
    ],
)
def test_malformed_logic_values_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LogicValue.model_validate(payload)


def test_from_unsigned_rejects_values_that_do_not_fit() -> None:
    with pytest.raises(ValueError):
        LogicValue.from_unsigned(16, 4)
    with pytest.raises(ValueError):
        LogicValue.from_unsigned(-1, 4)


@given(st.text(alphabet="01xzXZ", min_size=1, max_size=64))
def test_logic_value_round_trip_property(bits: str) -> None:
    value = LogicValue(width=len(bits), bits=bits)
    assert value.bits == bits.lower()
    assert LogicValue.model_validate_json(value.model_dump_json()) == value
    assert value.is_fully_known == (set(value.bits) <= {"0", "1"})


def test_signal_declarations_validate_width_range_and_path() -> None:
    descending = SignalDecl(path="tb.dut.bus", width=8, bit_range=BitRange(left=7, right=0))
    ascending = SignalDecl(path="tb.dut.bus", width=8, bit_range=BitRange(left=0, right=7))
    assert descending != ascending
    assert SignalDecl(path="tb.gen[2].u.q", width=1).path == "tb.gen[2].u.q"
    with pytest.raises(ValidationError):
        SignalDecl(path="tb.dut.bus", width=8)
    with pytest.raises(ValidationError):
        SignalDecl(path="tb.dut.bus", width=8, bit_range=BitRange(left=3, right=0))
    for bad_path in ["", ".tb", "tb..dut", "tb.dut.", "tb.\\esc.aped ", "tb dut", "9tb"]:
        with pytest.raises(ValidationError):
            SignalDecl(path=bad_path, width=1)


# --------------------------------------------------------------------------
# Exact time conversion
# --------------------------------------------------------------------------


def test_to_picoseconds_is_exact() -> None:
    assert to_picoseconds(7, 1, TimeUnit.NS) == 7000
    assert to_picoseconds(7000, 1, TimeUnit.PS) == 7000
    assert to_picoseconds(3, 100, TimeUnit.US) == 300_000_000
    assert to_picoseconds(5000, 1, TimeUnit.FS) == 5
    assert to_picoseconds(50, 100, TimeUnit.FS) == 5
    big = 10**30 + 7
    assert to_picoseconds(big, 1, TimeUnit.S) == big * 10**12


def test_to_picoseconds_rejects_sub_picosecond_results_without_rounding() -> None:
    with pytest.raises(UnsupportedPrecisionError):
        to_picoseconds(1, 1, TimeUnit.FS)
    with pytest.raises(UnsupportedPrecisionError):
        to_picoseconds(1999, 1, TimeUnit.FS)
    with pytest.raises(InvalidQueryError):
        to_picoseconds(1, 5, TimeUnit.NS)
    with pytest.raises(InvalidQueryError):
        to_picoseconds(-1, 1, TimeUnit.NS)


# --------------------------------------------------------------------------
# Provider: phases, boundaries, coverage, errors
# --------------------------------------------------------------------------


def test_mock_provider_satisfies_the_protocol(provider: MockWaveformProvider) -> None:
    assert isinstance(provider, WaveformProvider)
    assert provider.get_coverage() == TraceCoverage(start_ps=0, end_ps=100000)
    assert provider.get_signal_decl(WPTR).bit_range == BitRange(left=2, right=0)


def test_before_and_settled_differ_exactly_at_a_transition(provider: MockWaveformProvider) -> None:
    # wptr changes 011 -> 000 at the cycle-3 clock edge (55000 ps).
    assert provider.get_value_at(WPTR, 55000, phase=BEFORE) == lv("011")
    assert provider.get_value_at(WPTR, 55000, phase=SETTLED) == lv("000")
    assert provider.get_value_at(WPTR, 55000) == lv("000")
    # One picosecond either side there is no transition, so the phases agree.
    for time_ps, bits in [(54999, "011"), (55001, "000")]:
        assert provider.get_value_at(WPTR, time_ps, phase=BEFORE) == lv(bits)
        assert provider.get_value_at(WPTR, time_ps, phase=SETTLED) == lv(bits)


def test_values_before_the_first_known_record_are_x_at_declared_width(
    provider: MockWaveformProvider,
) -> None:
    assert provider.get_value_at(WPTR, 0) == lv("xxx")
    assert provider.get_value_at(WPTR, 4999) == lv("xxx")
    assert provider.get_value_at(WPTR, 5000, phase=BEFORE) == lv("xxx")
    assert provider.get_value_at(WPTR, 5000, phase=SETTLED) == lv("000")
    # Nothing is recorded before the trace start, so `before` there is x, not 0.
    assert provider.get_value_at(CLK, 0, phase=SETTLED) == lv("0")
    assert provider.get_value_at(CLK, 0, phase=BEFORE) == lv("x")


def test_trace_bounds_are_inclusive_and_enforced(provider: MockWaveformProvider) -> None:
    assert provider.get_value_at(CLK, 100000) == lv("0")
    with pytest.raises(OutOfRangeError):
        provider.get_value_at(CLK, 100001)
    with pytest.raises(OutOfRangeError):
        provider.get_window_slice([CLK], 0, 100002)
    with pytest.raises(OutOfRangeError):
        provider.get_clock_edges(CLK, 50000, 200000)
    assert provider.get_window_slice([CLK], 99999, 100001).signals[0].samples == (
        SignalSample(time_ps=100000, value=lv("0")),
    )


def test_missing_signals_raise_and_are_never_reported_as_values(
    provider: MockWaveformProvider,
) -> None:
    with pytest.raises(SignalNotFoundError):
        provider.get_value_at("tb.dut.mem", 55000)
    with pytest.raises(SignalNotFoundError):
        provider.get_signal_decl("tb.dut.absent")
    with pytest.raises(SignalNotFoundError):
        provider.get_clock_edges("tb.clk", 0, 100)
    with pytest.raises(SignalNotFoundError):
        provider.get_window_slice([CLK, "tb.dut.absent"], 0, 100)


def test_malformed_queries_are_rejected(provider: MockWaveformProvider) -> None:
    bad_calls: list[Callable[[], object]] = [
        lambda: provider.get_value_at(CLK, -1),
        lambda: provider.get_value_at(CLK, 5.0),  # type: ignore[arg-type]
        lambda: provider.get_value_at(CLK, True),
        lambda: provider.get_value_at(CLK, 5, phase="settled"),  # type: ignore[arg-type]
        lambda: provider.get_window_slice([CLK], 100, 100),
        lambda: provider.get_window_slice([CLK], 200, 100),
        lambda: provider.get_window_slice([], 0, 100),
        lambda: provider.get_window_slice([CLK, CLK], 0, 100),
        lambda: provider.get_window_slice(CLK, 0, 100),
        lambda: provider.get_clock_edges(CLK, 100, 100),
        lambda: provider.get_clock_edges(WPTR, 0, 100),
        lambda: provider.get_clock_edges(CLK, 0, 100, edge="rising"),  # type: ignore[arg-type]
    ]
    for call in bad_calls:
        with pytest.raises(InvalidQueryError):
            call()


def test_malformed_request_is_reported_before_a_missing_signal(
    provider: MockWaveformProvider,
) -> None:
    with pytest.raises(InvalidQueryError):
        provider.get_value_at("tb.dut.absent", -1)
    with pytest.raises(SignalNotFoundError):
        provider.get_value_at("tb.dut.absent", 10**9)


# --------------------------------------------------------------------------
# Provider: clock edges
# --------------------------------------------------------------------------


def test_clock_edges_use_half_open_intervals(provider: MockWaveformProvider) -> None:
    rising = provider.get_clock_edges(CLK, 0, 100001)
    assert rising == tuple(range(5000, 100000, 10000))
    assert provider.get_clock_edges(CLK, 25000, 35000) == (25000,)
    assert provider.get_clock_edges(CLK, 25001, 35000) == ()
    assert provider.get_clock_edges(CLK, 25000, 35001) == (25000, 35000)
    falling = provider.get_clock_edges(CLK, 0, 100001, edge=ClockEdge.FALLING)
    assert falling == tuple(range(10000, 100001, 10000))


def test_x_to_one_is_not_a_clock_edge() -> None:
    trace = MockTrace(
        coverage=TraceCoverage(start_ps=0, end_ps=100),
        signals=(scalar("top.clk", (0, "x"), (10, "1"), (20, "0"), (30, "1"), (40, "x"), (50, "0")),),
    )
    edges = MockWaveformProvider(trace)
    assert edges.get_clock_edges("top.clk", 0, 101) == (30,)
    assert edges.get_clock_edges("top.clk", 0, 101, edge=ClockEdge.FALLING) == (20,)


def test_clock_pause_does_not_invent_edges() -> None:
    trace = MockTrace(
        coverage=TraceCoverage(start_ps=0, end_ps=1000),
        signals=(scalar("top.clk", (0, "0"), (10, "1"), (20, "0"), (500, "1"), (510, "0")),),
    )
    assert MockWaveformProvider(trace).get_clock_edges("top.clk", 0, 1001) == (10, 500)


def test_cycle_zero_is_the_first_edge_with_reset_inactive_before_it(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    header = fixture.golden.header
    inactive = "1" if header.reset_active == "0" else "0"
    edges = provider.get_clock_edges(header.clock, 0, 100001)
    active_cycle_edges = tuple(
        t for t in edges if provider.get_value_at(header.reset, t, phase=BEFORE).bits == inactive
    )
    assert active_cycle_edges == CYCLE_EDGES
    assert edges[:2] == (5000, 15000)  # two edges with reset asserted precede cycle 0


# --------------------------------------------------------------------------
# Provider: windows
# --------------------------------------------------------------------------


def test_window_starting_value_and_interior_transitions(provider: MockWaveformProvider) -> None:
    window = provider.get_window_slice([WPTR, FULL], 45000, 65000)
    wptr, full = window.signals
    assert (window.start_ps, window.end_ps) == (45000, 65000)
    assert [s.signal_path for s in window.signals] == [WPTR, FULL]
    # The change exactly at the window start is folded into the initial value.
    assert wptr.initial == lv("011")
    # The change exactly at the window end is excluded.
    assert wptr.samples == (SignalSample(time_ps=55000, value=lv("000")),)
    assert full.initial == lv("0")
    assert full.samples == ()
    assert window.unavailable == ()


def test_window_agrees_with_point_queries(provider: MockWaveformProvider) -> None:
    paths = [s.decl.path for s in provider._trace.signals]
    window = provider.get_window_slice(paths, 20000, 100001)
    for signal in window.signals:
        value = signal.initial
        changes = {sample.time_ps: sample.value for sample in signal.samples}
        for time_ps in range(20000, 100001, 2500):
            value = changes.get(time_ps, value)
            assert value == provider.get_value_at(signal.signal_path, time_ps)


def test_report_waveform_matches_the_provider(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    waveform = fixture.report.waveform
    assert waveform is not None
    paths = [signal.signal_path for signal in waveform.signals]
    assert waveform == provider.get_window_slice(paths, waveform.start_ps, waveform.end_ps)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w["signals"][0].update(initial=None),
        lambda w: w["signals"][0]["samples"].append(dict(w["signals"][0]["samples"][-1])),
        lambda w: w["signals"][0]["samples"].reverse(),
        lambda w: w["signals"][0]["samples"][0].update(time_ps=w["start_ps"]),
        lambda w: w["signals"][0]["samples"][0].update(value={"width": 2, "bits": "01"}),
        lambda w: w["signals"].append(dict(w["signals"][0])),
        lambda w: w.update(signals=[]),
        lambda w: w.update(end_ps=w["start_ps"]),
        lambda w: w.update(unavailable=[{"start_ps": "0", "end_ps": "10"}]),
    ],
)
def test_malformed_windows_are_rejected(provider: MockWaveformProvider, mutate: Any) -> None:
    window = json.loads(provider.get_window_slice([CLK], 20000, 60000).model_dump_json())
    WaveformWindow.model_validate(window)
    mutate(window)
    with pytest.raises(ValidationError):
        WaveformWindow.model_validate(window)


# --------------------------------------------------------------------------
# Provider: unavailable data
# --------------------------------------------------------------------------


def test_dump_gap_is_unavailable_not_a_held_value() -> None:
    gap = gap_provider()
    assert gap.get_value_at("top.d", 39) == lv("1")
    for time_ps in (40, 50, 59):
        with pytest.raises(DataUnavailableError):
            gap.get_value_at("top.d", time_ps)
    # At the resume time the settled value is known but the pre-edge value is not.
    assert gap.get_value_at("top.d", 60, phase=SETTLED) == lv("1")
    with pytest.raises(DataUnavailableError):
        gap.get_value_at("top.d", 60, phase=BEFORE)
    assert gap.get_value_at("top.d", 40, phase=BEFORE) == lv("1")
    assert gap.get_value_at("top.d", 61, phase=BEFORE) == lv("1")
    assert gap.get_value_at("top.d", 100) == lv("z")


def test_clock_edges_are_not_certified_across_a_dump_gap() -> None:
    gap = gap_provider()
    assert gap.get_clock_edges("top.clk", 0, 40) == (10, 30)
    assert gap.get_clock_edges("top.clk", 61, 101) == ()
    for start_ps, end_ps in [(0, 41), (30, 70), (50, 55), (60, 101)]:
        with pytest.raises(DataUnavailableError):
            gap.get_clock_edges("top.clk", start_ps, end_ps)


def test_window_reports_gaps_and_marks_resume_samples() -> None:
    gap = gap_provider()
    window = gap.get_window_slice(["top.d"], 10, 90)
    assert window.unavailable == (TimeInterval(start_ps=40, end_ps=60),)
    signal = window.signals[0]
    assert signal.initial == lv("x")
    # The sample at 60 restates the value after the gap; 15 and 80 are changes.
    assert [(s.time_ps, s.value.bits) for s in signal.samples] == [(15, "1"), (60, "1"), (80, "z")]

    inside = gap.get_window_slice(["top.d"], 45, 70)
    assert inside.unavailable == (TimeInterval(start_ps=45, end_ps=60),)
    assert inside.signals[0].initial is None
    assert WaveformWindow.model_validate_json(inside.model_dump_json()) == inside


def test_mock_trace_rejects_inconsistent_data() -> None:
    coverage = TraceCoverage(start_ps=0, end_ps=100)
    gap = (TimeInterval(start_ps=40, end_ps=60),)
    bad_signal_sets = [
        (scalar("top.a", (0, "0"), (0, "1")),),  # duplicate timestamp
        (scalar("top.a", (10, "0"), (5, "1")),),  # descending time
        (scalar("top.a", (0, "0"), (10, "0")),),  # repeated value is not a change
        (scalar("top.a", (0, "0"), (101, "1")),),  # outside coverage
        (scalar("top.a", (0, "0")), scalar("top.a", (0, "1"))),  # duplicate path
        (MockSignal(decl=SignalDecl(path="top.a", width=1), samples=(SignalSample(time_ps=0, value=lv("00")),)),),
    ]
    for signals in bad_signal_sets:
        with pytest.raises(ValidationError):
            MockTrace(coverage=coverage, signals=signals)
    with pytest.raises(ValidationError):  # sample recorded while dumping was off
        MockTrace(coverage=coverage, unavailable=gap, signals=(scalar("top.a", (0, "0"), (50, "1"), (60, "1")),))
    with pytest.raises(ValidationError):  # no value restated where dumping resumed
        MockTrace(coverage=coverage, unavailable=gap, signals=(scalar("top.a", (0, "0")),))
    with pytest.raises(ValidationError):
        TraceCoverage(start_ps=10, end_ps=5)
    with pytest.raises(ValidationError):
        TimeInterval(start_ps=5, end_ps=5)


# --------------------------------------------------------------------------
# Failure anchor and comparison outcomes
# --------------------------------------------------------------------------


def test_failure_anchor_is_the_documented_first_divergence(fixture: MockFixture) -> None:
    failure = fixture.comparison.failure
    assert failure is not None
    assert fixture.comparison.status is AnalysisStatus.FAIL
    assert failure.kind is FailureKind.VALUE_MISMATCH
    assert failure.signal_path == FULL
    assert failure.clock_path == CLK
    assert failure.observation == Observation(epoch=0, cycle=3, phase=SETTLED, time_ps=55000)
    assert (failure.expected, failure.actual) == (lv("1"), lv("0"))
    assert fixture.report.failure == failure


def test_failure_anchor_actual_value_is_reproducible(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    failure = fixture.comparison.failure
    assert failure is not None
    for anchor in (failure, *fixture.comparison.additional_mismatches):
        observed = provider.get_value_at(
            anchor.signal_path, anchor.observation.time_ps, phase=anchor.observation.phase
        )
        assert observed == anchor.actual
        assert observed != anchor.expected
    # The anchor's time is a real rising edge of the anchor's clock.
    assert failure.observation.time_ps in provider.get_clock_edges(failure.clock_path, 0, 100001)
    assert CYCLE_EDGES[failure.observation.cycle] == failure.observation.time_ps
    # full does not toggle at the failing edge; the pre-edge value was correct then.
    assert provider.get_value_at(FULL, 55000, phase=BEFORE) == lv("0")


def test_golden_and_trace_agree_until_the_anchor_and_tie_break_is_declaration_order(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    golden = fixture.golden
    assert golden.header.sampling is SETTLED
    declared = [signal.name for signal in golden.header.signals]
    paths = {signal.name: signal.path for signal in golden.header.signals}
    mismatches: list[tuple[int, str]] = []
    compared = 0
    for record in golden.records:
        assert record.cycle_index is not None
        for name in declared:
            if name not in record.values:
                continue
            compared += 1
            actual = provider.get_value_at(paths[name], CYCLE_EDGES[record.cycle_index])
            if actual.bits != record.values[name]:
                mismatches.append((record.cycle_index, name))
        if mismatches:
            break
    assert mismatches == [(3, "full"), (3, "empty"), (3, "out_valid")]
    assert compared == fixture.comparison.compared_values == 16
    assert paths[mismatches[0][1]] == fixture.comparison.failure.signal_path  # type: ignore[union-attr]
    others = [a.signal_path for a in fixture.comparison.additional_mismatches]
    assert others == [paths["empty"], paths["out_valid"]]


def test_outcomes_are_distinguished() -> None:
    def result(status: AnalysisStatus, **fields: Any) -> ComparisonResult:
        base: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "mode": ComparisonMode.CYCLE,
            "failure": None,
            "compared_values": 0,
        }
        base.update(fields)
        return ComparisonResult(**base)

    why = (Diagnostic(code="TRACE_ENDED_EARLY", message="Trace ended before cycle 7."),)
    passed = result(AnalysisStatus.PASS, compared_values=32)
    failed = result(AnalysisStatus.FAIL, failure=cycle_anchor(), compared_values=13)
    incomplete = result(AnalysisStatus.INCONCLUSIVE, compared_values=12, diagnostics=why)
    invalid = result(AnalysisStatus.INVALID_INPUT, diagnostics=why)
    statuses = {r.status for r in (passed, failed, incomplete, invalid)}
    assert len(statuses) == 4
    assert passed.failure is None and failed.failure is not None and incomplete.failure is None
    for one in (passed, failed, incomplete, invalid):
        assert ComparisonResult.model_validate_json(one.model_dump_json()) == one

    with pytest.raises(ValidationError):  # comparing nothing is not a pass
        result(AnalysisStatus.PASS, compared_values=0)
    with pytest.raises(ValidationError):  # a pass cannot carry a failure
        result(AnalysisStatus.PASS, failure=cycle_anchor(), compared_values=5)
    with pytest.raises(ValidationError):  # a fail needs its anchor
        result(AnalysisStatus.FAIL, compared_values=5)
    for status in (
        AnalysisStatus.INCONCLUSIVE,
        AnalysisStatus.UNSUPPORTED,
        AnalysisStatus.INVALID_INPUT,
        AnalysisStatus.ERROR,
    ):
        with pytest.raises(ValidationError):  # must be explained
            result(status)
        with pytest.raises(ValidationError):  # and never carries a failure
            result(status, failure=cycle_anchor(), diagnostics=why)


def test_failure_kinds_never_invent_payloads() -> None:
    handshake = {
        "mode": ComparisonMode.HANDSHAKE,
        "transaction_index": 4,
        "observation": Observation(epoch=0, cycle=9, phase=BEFORE, time_ps=115000),
    }
    missing = cycle_anchor(kind=FailureKind.MISSING_TRANSACTION, expected=lv("00101010"), actual=None, **handshake)
    extra = cycle_anchor(kind=FailureKind.EXTRA_TRANSACTION, expected=None, actual=lv("0010x010"), **handshake)
    assert missing.actual is None and extra.expected is None
    unknown = cycle_anchor(kind=FailureKind.UNKNOWN_VALUE, expected=lv("0000"), actual=lv("xxxx"))
    assert FailureAnchor.model_validate_json(unknown.model_dump_json()).actual == lv("xxxx")

    invalid: list[dict[str, Any]] = [
        {"kind": FailureKind.MISSING_TRANSACTION, "actual": lv("0"), **handshake},
        {"kind": FailureKind.EXTRA_TRANSACTION, "actual": None, **handshake},
        {"kind": FailureKind.MISSING_TRANSACTION, "actual": None},  # needs handshake mode
        {"expected": lv("1"), "actual": lv("1")},  # not a mismatch
        {"expected": lv("1"), "actual": lv("x")},  # x/z must be unknown_value
        {"kind": FailureKind.UNKNOWN_VALUE, "expected": lv("1"), "actual": lv("0")},
        {"expected": lv("11"), "actual": lv("0")},  # width disagreement
        {"expected": lv("x"), "actual": lv("0")},  # expected must be known
        {"actual": None},
        {"transaction_index": 2},  # cycle mode has no transaction index
        {"mode": ComparisonMode.HANDSHAKE, "transaction_index": 0},  # handshake samples before
    ]
    for overrides in invalid:
        with pytest.raises(ValidationError):
            cycle_anchor(**overrides)


# --------------------------------------------------------------------------
# Golden JSONL
# --------------------------------------------------------------------------


def test_golden_jsonl_matches_the_fixture(fixture: MockFixture) -> None:
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    parsed = parse_golden_jsonl(text)
    assert parsed == fixture.golden
    assert parsed.to_jsonl() == text
    assert [record.cycle_index for record in parsed.records] == list(range(8))


def _golden_lines() -> list[dict[str, Any]]:
    return [json.loads(line) for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines()]


def _parse(lines: list[dict[str, Any]]) -> GoldenTrace:
    return parse_golden_jsonl("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g.pop(3),  # skipped cycle index
        lambda g: g.insert(3, dict(g[3])),  # duplicated cycle index
        lambda g: g[1].update(epoch=1),  # epoch does not start at 0
        lambda g: g[2]["values"].update(full="x"),  # expected must be known
        lambda g: g[2]["values"].update(out_data="0x11"),  # hex is not accepted
        lambda g: g[2]["values"].update(out_data="10001"),  # not zero-extended silently
        lambda g: g[2]["values"].update(bogus="1"),  # undeclared signal
        lambda g: g[2].update(transaction_index=1),  # mixed modes
        lambda g: g[2].pop("cycle_index"),
        lambda g: g[0].update(schema_version=2),
        lambda g: g[0].update(valid="tb.dut.out_valid"),  # handshake field in cycle mode
        lambda g: g[0]["signals"].append(dict(g[0]["signals"][0])),  # duplicate declaration
        lambda g: g[0].update(signals=[]),
        lambda g: g[0].update(mode="handshake"),  # handshake needs valid/ready/before
        lambda g: g[0].update(extra_field=True),
        lambda g: g.reverse(),  # metadata must come first
    ],
)
def test_malformed_golden_traces_are_rejected(mutate: Any) -> None:
    lines = _golden_lines()
    _parse(lines)
    mutate(lines)
    with pytest.raises(ValueError):
        _parse(lines)


def test_golden_text_level_errors_are_rejected() -> None:
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    for bad in ["", "\n", text.replace("\n", "\n\n", 1), "not json\n"]:
        with pytest.raises(ValueError):
            parse_golden_jsonl(bad)
    header_only = text.split("\n", 1)[0] + "\n"
    with pytest.raises(ValueError, match="at least one value"):
        parse_golden_jsonl(header_only)


def test_handshake_golden_trace_validates_and_supports_epochs() -> None:
    header = {
        "type": "metadata", "schema_version": 1, "mode": "handshake", "clock": "tb.dut.clk",
        "reset": "tb.dut.rst_n", "reset_active": "0", "sampling": "before",
        "valid": "tb.dut.out_valid", "ready": "tb.dut.out_ready", "deadline_cycle": 16,
        "signals": [{"name": "data", "path": "tb.dut.out_data", "width": 8}],
    }  # fmt: skip

    def record(epoch: int, index: int, bits: str = "00101010") -> dict[str, Any]:
        return {"type": "expected", "epoch": epoch, "transaction_index": index, "values": {"data": bits}}

    trace = _parse([header, record(0, 0), record(0, 1), record(1, 0)])
    assert trace.header.deadline_cycle == 16
    assert [(r.epoch, r.index) for r in trace.records] == [(0, 0), (0, 1), (1, 0)]
    with pytest.raises(ValueError):
        _parse([header, record(0, 0), record(2, 0)])  # skipped epoch
    with pytest.raises(ValueError):
        _parse([header, record(0, 0), record(1, 1)])  # epoch must restart at 0
    with pytest.raises(ValueError):
        _parse([{**header, "sampling": "settled"}, record(0, 0)])
    with pytest.raises(ValueError):  # handshake payloads must be complete
        _parse([header, {"type": "expected", "epoch": 0, "transaction_index": 0, "values": {}}])


# --------------------------------------------------------------------------
# Causal report
# --------------------------------------------------------------------------


def test_causal_graph_references_are_valid(fixture: MockFixture) -> None:
    report = fixture.report
    node_ids = {node.id for node in report.nodes}
    assert len(node_ids) == len(report.nodes) == 10
    assert all(edge.source in node_ids and edge.target in node_ids for edge in report.edges)
    assert report.anchor_node_id in node_ids
    assert [suspect.rank for suspect in report.suspects] == [1, 2]
    for suspect in report.suspects:
        assert suspect.node_id in node_ids
        assert set(suspect.evidence_node_ids) <= node_ids
    assert {edge.edge_type for edge in report.edges} == {EdgeType.COMBINATIONAL, EdgeType.SEQUENTIAL}
    assert report.truncated


def test_causal_node_values_are_reproducible_from_the_trace(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    for node in fixture.report.nodes:
        obs = node.observation
        assert CYCLE_EDGES[obs.cycle] == obs.time_ps
        assert node.value == provider.get_value_at(node.signal_path, obs.time_ps, phase=obs.phase)


def test_sequential_edges_cross_one_real_clock_edge(
    fixture: MockFixture, provider: MockWaveformProvider
) -> None:
    nodes = {node.id: node for node in fixture.report.nodes}
    clock_edges = provider.get_clock_edges(CLK, 0, 100001)
    sequential = [e for e in fixture.report.edges if e.edge_type is EdgeType.SEQUENTIAL]
    assert sequential
    for edge in sequential:
        source, target = nodes[edge.source].observation, nodes[edge.target].observation
        assert (source.phase, target.phase) == (BEFORE, SETTLED)
        assert source.time_ps == target.time_ps
        assert target.time_ps in clock_edges
    carry = next(e for e in fixture.report.edges if e.id == "e10")
    assert nodes[carry.source].observation.cycle + 1 == nodes[carry.target].observation.cycle
    assert nodes[carry.source].value == nodes[carry.target].value


def test_source_locations_reference_the_fixture_source_file(fixture: MockFixture) -> None:
    report = fixture.report
    sources = {source.id: source for source in report.sources}
    assert len(sources) == 1
    source = sources["fifo-src"]
    text = (REPO_ROOT / source.path).read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == source.sha256
    lines = text.split("\n")
    spans = [node.location for node in report.nodes] + [s.location for s in report.suspects]
    assert all(span is not None for span in spans)
    for span in spans:
        assert span is not None
        assert span.source_id in sources
        assert 1 <= span.line <= span.end_line <= len(lines)
    top = next(s for s in report.suspects if s.rank == 1)
    assert top.location is not None
    top_text = "\n".join(lines[top.location.line - 1 : top.location.end_line])
    assert "wptr <= {wptr[2], 2'b00};" in top_text
    anchor = next(n for n in report.nodes if n.id == report.anchor_node_id)
    assert anchor.location is not None
    assert "assign full" in lines[anchor.location.line - 1]


def _report(raw: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = raw["report"]
    return report


def _node(report: dict[str, Any], node_id: str) -> dict[str, Any]:
    node: dict[str, Any] = next(n for n in report["nodes"] if n["id"] == node_id)
    return node


def _edge(report: dict[str, Any], edge_id: str) -> dict[str, Any]:
    edge: dict[str, Any] = next(e for e in report["edges"] if e["id"] == edge_id)
    return edge


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: _edge(r, "e01").update(source="no-such-node"),
        lambda r: _edge(r, "e01").update(target="no-such-node"),
        lambda r: _edge(r, "e01").update(source="full.c3.settled"),  # self-loop
        lambda r: r["edges"].append(dict(r["edges"][0])),  # duplicate edge id
        lambda r: r["nodes"].append(dict(r["nodes"][0])),  # duplicate node id
        lambda r: r["suspects"][0].update(node_id="no-such-node"),
        lambda r: r["suspects"][0].update(evidence_node_ids=["no-such-node"]),
        lambda r: r["suspects"][1].update(rank=1),  # tied ranks
        lambda r: r["suspects"][1].update(rank=3),  # gap in ranks
        lambda r: r["suspects"][0]["location"].update(source_id="no-such-source"),
        lambda r: _node(r, "push.c3.before")["location"].update(source_id="no-such-source"),
        lambda r: r["sources"][0].update(path="../outside.sv"),
        lambda r: r["sources"][0].update(path="/abs/fifo.sv"),
        lambda r: r["sources"][0].update(sha256="abc"),
        lambda r: r.update(anchor_node_id=None),
        lambda r: r.update(anchor_node_id="wptr.c3.settled"),  # not the failing signal
        lambda r: _node(r, "full.c3.settled").update(value={"width": 1, "bits": "1"}),
        # A sequential edge must run before -> settled at one clock edge.
        lambda r: _edge(r, "e01").update(edge_type="sequential"),
        lambda r: _edge(r, "e10").update(edge_type="sequential"),
        # A combinational edge may not cross a clock edge between different signals.
        lambda r: _edge(r, "e03").update(edge_type="combinational"),
        lambda r: _edge(r, "e05").update(edge_type="combinational"),
        # A carry requires an unchanged value.
        lambda r: _node(r, "wptr.c2.settled").update(value={"width": 3, "bits": "010"}),
        # One cycle cannot map to two edge times.
        lambda r: _node(r, "in_valid.c3.before")["observation"].update(time_ps="56000"),
        lambda r: r.update(status="pass"),
        lambda r: r.update(failure=None),
        lambda r: r.update(diagnostics=[]),  # truncated must be explained
        lambda r: r.update(schema_version=2),
        lambda r: _edge(r, "e01").update(edge_type="temporal"),
    ],
)
def test_inconsistent_causal_reports_are_rejected(raw: dict[str, Any], mutate: Any) -> None:
    report = _report(raw)
    CausalReport.model_validate(report)
    mutate(report)
    with pytest.raises(ValidationError):
        CausalReport.model_validate(report)


def test_report_statuses_are_distinguished(fixture: MockFixture) -> None:
    fixture_note = Provenance(kind=ProvenanceKind.SYNTHETIC_FIXTURE, note="contract test")
    why = (Diagnostic(code="SIGNAL_NOT_OBSERVED", message="A required signal is absent."),)

    def report(status: AnalysisStatus, **fields: Any) -> CausalReport:
        base: dict[str, Any] = {"schema_version": 1, "provenance": fixture_note, "status": status, "failure": None}
        base.update(fields)
        return CausalReport(**base)

    passed = report(AnalysisStatus.PASS)
    unlocalized = report(AnalysisStatus.FAIL, failure=fixture.report.failure, diagnostics=why)
    inconclusive = report(AnalysisStatus.INCONCLUSIVE, diagnostics=why)
    unsupported = report(AnalysisStatus.UNSUPPORTED, diagnostics=why)
    for one in (passed, unlocalized, inconclusive, unsupported):
        assert CausalReport.model_validate_json(one.model_dump_json()) == one
    assert passed.failure is None and not passed.nodes and not passed.suspects
    assert unlocalized.failure is not None and not unlocalized.nodes

    with pytest.raises(ValidationError):  # an incomplete run is never a silent pass
        report(AnalysisStatus.INCONCLUSIVE)
    with pytest.raises(ValidationError):  # a pass carries no failure
        report(AnalysisStatus.PASS, failure=fixture.report.failure)
    with pytest.raises(ValidationError):  # an unlocalized failure must be explained
        report(AnalysisStatus.FAIL, failure=fixture.report.failure)
    with pytest.raises(ValidationError):  # a pass carries no graph
        report(AnalysisStatus.PASS, nodes=fixture.report.nodes, anchor_node_id="full.c3.settled")
    with pytest.raises(ValidationError):  # truncation applies to a failing slice
        report(AnalysisStatus.PASS, truncated=True, diagnostics=why)


def test_fixture_rejects_disagreement_between_comparison_and_report(raw: dict[str, Any]) -> None:
    raw["comparison"]["failure"]["detail"] = "a different failure description"
    with pytest.raises(ValidationError):
        MockFixture.model_validate(raw)


def test_fixture_must_be_labeled_synthetic(raw: dict[str, Any]) -> None:
    raw["report"]["provenance"]["kind"] = "analysis"
    with pytest.raises(ValidationError):
        MockFixture.model_validate(raw)
