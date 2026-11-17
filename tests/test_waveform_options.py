"""Track A tests: waveform CLI option definitions and ``run_comparison``.

These exercise the option layer with real VCD text generated from the shared
mock fixture. No simulator runs and no CLI command exists here, so this is not
end-to-end coverage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from src.contracts import AnalysisStatus, ComparisonResult, MockFixture, load_mock_fixture
from src.waveform import (
    VcdWaveformProvider,
    WaveformOptions,
    add_waveform_arguments,
    compare,
    compare_files,
    exit_code,
    options_from_namespace,
    run_comparison,
)
from tests.test_waveform import vcd_from_mock, without_detail
from tests.test_waveform_fst import echo_converter

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "mock_fifo_golden.jsonl"


@pytest.fixture(scope="module")
def fixture() -> MockFixture:
    return load_mock_fixture(FIXTURES / "mock_fifo_failure.json")


@pytest.fixture
def fifo_vcd(tmp_path: Path, fixture: MockFixture) -> Path:
    path = tmp_path / "sim.vcd"
    path.write_text(vcd_from_mock(fixture.trace), encoding="utf-8")
    return path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="waveroot", exit_on_error=False)
    add_waveform_arguments(result)
    return result


def parse(*argv: str) -> WaveformOptions:
    return options_from_namespace(parser().parse_args(list(argv)))


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


def test_defaults() -> None:
    options = parse("--trace", "sim.vcd", "--golden", "g.jsonl")
    assert options == WaveformOptions(trace=Path("sim.vcd"), golden=Path("g.jsonl"))
    assert (options.keep, options.cache_dir, options.fst_timeout_s) == ((), None, 120.0)


def test_every_option_and_repeatable_keep() -> None:
    options = parse(
        "--keep", "tb.dut.*",
        "--trace", "run/sim.fst",
        "--fst-timeout", "2.5",
        "--keep", "tb.**.clk",
        "--golden", "golden.jsonl",
        "--cache-dir", ".waveroot-cache",
        "--keep", "tb.dut.*",
    )
    assert options == WaveformOptions(
        trace=Path("run/sim.fst"),
        golden=Path("golden.jsonl"),
        keep=("tb.dut.*", "tb.**.clk", "tb.dut.*"),
        cache_dir=Path(".waveroot-cache"),
        fst_timeout_s=2.5,
    )


def test_options_coexist_with_the_callers_own_arguments() -> None:
    combined = argparse.ArgumentParser(exit_on_error=False)
    combined.add_argument("--json", action="store_true")
    add_waveform_arguments(combined)
    namespace = combined.parse_args(["--json", "--trace", "a.vcd", "--golden", "b.jsonl"])
    assert namespace.json is True
    assert options_from_namespace(namespace).trace == Path("a.vcd")
    help_text = combined.format_help()
    for flag in ("--trace PATH", "--golden PATH", "--keep PATTERN", "--cache-dir PATH", "--fst-timeout SECONDS"):
        assert flag in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["--golden", "g.jsonl"],  # --trace is required
        ["--trace", "sim.vcd"],  # --golden is required
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--fst-timeout", "0"],
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--fst-timeout", "-3"],
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--fst-timeout", "nan"],
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--fst-timeout", "inf"],
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--fst-timeout", "soon"],
        ["--trace", "sim.vcd", "--golden", "g.jsonl", "--keep"],
    ],
)
def test_invalid_arguments_are_rejected_by_argparse(argv: list[str]) -> None:
    with pytest.raises((argparse.ArgumentError, SystemExit)):
        parser().parse_args(argv)


@pytest.mark.parametrize("timeout", [0, -1.0, float("nan"), float("inf")])
def test_options_reject_a_bad_timeout_when_built_directly(timeout: float) -> None:
    with pytest.raises(ValueError):
        WaveformOptions(trace=Path("a.vcd"), golden=Path("b.jsonl"), fst_timeout_s=timeout)


# --------------------------------------------------------------------------
# run_comparison and exit codes
# --------------------------------------------------------------------------


def write_passing_golden(path: Path, fixture: MockFixture, provider: VcdWaveformProvider, cycles: int) -> Path:
    """Golden expectations read from the trace itself for ``cycles`` cycles."""
    header = fixture.golden.header
    lines = [header.model_dump_json(exclude_none=True)]
    for cycle in range(cycles):
        time_ps = min(25000 + 10000 * cycle, provider.get_coverage().end_ps)
        values = {s.name: provider.get_value_at(s.path, time_ps).bits for s in header.signals}
        lines.append(json.dumps({"type": "expected", "epoch": 0, "cycle_index": cycle, "values": values}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_run_comparison_on_the_mock_fifo_trace(
    fixture: MockFixture, fifo_vcd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_comparison(WaveformOptions(trace=fifo_vcd, golden=GOLDEN))
    assert without_detail(result) == without_detail(fixture.comparison)
    assert result == compare_files(fifo_vcd, GOLDEN)
    assert result.failure is not None and result.failure.signal_path == "tb.dut.full"
    assert exit_code(result) == 1

    # Selecting only what the golden file needs, and caching, change nothing.
    keep = ("tb.dut.clk", "tb.dut.rst_n", "tb.dut.full", "tb.dut.empty", "tb.dut.out_*")
    cache_dir = tmp_path / "cache"
    for options in (
        WaveformOptions(trace=fifo_vcd, golden=GOLDEN, keep=keep),
        WaveformOptions(trace=fifo_vcd, golden=GOLDEN, cache_dir=cache_dir),
        WaveformOptions(trace=fifo_vcd, golden=GOLDEN, keep=keep, cache_dir=cache_dir),
        WaveformOptions(trace=fifo_vcd, golden=GOLDEN, keep=keep, cache_dir=cache_dir),  # cache hit
    ):
        assert run_comparison(options) == result
    assert len(list(cache_dir.iterdir())) == 2  # one entry per keep selection
    assert capsys.readouterr() == ("", "")


def test_run_comparison_reads_fst_through_the_converter(
    fixture: MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    echo_converter(tmp_path / "bin", vcd_from_mock(fixture.trace))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    fst = tmp_path / "sim.fst"
    fst.write_bytes(b"opaque")
    expected = compare(VcdWaveformProvider.from_text(vcd_from_mock(fixture.trace)), fixture.golden)
    assert run_comparison(WaveformOptions(trace=fst, golden=GOLDEN)) == expected


def _status_cases(fixture: MockFixture, fifo_vcd: Path, tmp_path: Path) -> dict[str, tuple[WaveformOptions, AnalysisStatus, str | None]]:
    provider = VcdWaveformProvider.from_path(fifo_vcd)
    passing = write_passing_golden(tmp_path / "pass.jsonl", fixture, provider, cycles=8)
    too_long = write_passing_golden(tmp_path / "long.jsonl", fixture, provider, cycles=20)
    bad_golden = tmp_path / "bad.jsonl"
    bad_golden.write_text('{"type":"metadata"}\n', encoding="utf-8")
    bad_trace = tmp_path / "bad.vcd"
    bad_trace.write_text("$timescale 1 ps $end\n$enddefinitions $end\n#0\n0!\n", encoding="utf-8")
    wrong_suffix = tmp_path / "sim.txt"
    wrong_suffix.write_text(fifo_vcd.read_text(encoding="utf-8"), encoding="utf-8")
    fst = tmp_path / "sim.fst"
    fst.write_bytes(b"opaque")

    def options(trace: Path, golden: Path = GOLDEN, **kwargs: object) -> WaveformOptions:
        return WaveformOptions(trace=trace, golden=golden, **kwargs)  # type: ignore[arg-type]

    return {
        "pass": (options(fifo_vcd, passing), AnalysisStatus.PASS, None),
        "fail": (options(fifo_vcd), AnalysisStatus.FAIL, None),
        "golden_invalid": (options(fifo_vcd, bad_golden), AnalysisStatus.INVALID_INPUT, "GOLDEN_INVALID"),
        "trace_invalid": (options(bad_trace), AnalysisStatus.INVALID_INPUT, "TRACE_INVALID"),
        "keep_unmatched": (options(fifo_vcd, keep=("tb.nothing.*",)), AnalysisStatus.INVALID_INPUT, "SELECTION_INVALID"),
        "keep_malformed": (options(fifo_vcd, keep=("tb..clk",)), AnalysisStatus.INVALID_INPUT, "SELECTION_INVALID"),
        "keep_too_narrow": (options(fifo_vcd, keep=("tb.dut.clk", "tb.dut.rst_n")), AnalysisStatus.INVALID_INPUT, "SIGNAL_NOT_FOUND"),
        "trace_missing": (options(tmp_path / "absent.vcd"), AnalysisStatus.ERROR, "FILE_UNREADABLE"),
        "golden_missing": (options(fifo_vcd, tmp_path / "absent.jsonl"), AnalysisStatus.ERROR, "FILE_UNREADABLE"),
        "no_converter": (options(fst), AnalysisStatus.ERROR, "CONVERTER_UNAVAILABLE"),
        "no_converter_cached": (options(fst, cache_dir=tmp_path / "cache"), AnalysisStatus.ERROR, "CONVERTER_UNAVAILABLE"),
        "wrong_suffix": (options(wrong_suffix), AnalysisStatus.UNSUPPORTED, "TRACE_UNSUPPORTED"),
        "trace_too_short": (options(fifo_vcd, too_long), AnalysisStatus.INCONCLUSIVE, "TRACE_ENDED_EARLY"),
    }


CASE_NAMES = [
    "pass", "fail", "golden_invalid", "trace_invalid", "keep_unmatched", "keep_malformed", "keep_too_narrow",
    "trace_missing", "golden_missing", "no_converter", "no_converter_cached", "wrong_suffix", "trace_too_short",
]
EXPECTED_EXIT = {
    AnalysisStatus.PASS: 0,
    AnalysisStatus.FAIL: 1,
    AnalysisStatus.INVALID_INPUT: 2,
    AnalysisStatus.ERROR: 2,
    AnalysisStatus.UNSUPPORTED: 3,
    AnalysisStatus.INCONCLUSIVE: 3,
}


@pytest.mark.parametrize("case", CASE_NAMES)
def test_run_comparison_never_raises_for_input_problems(
    case: str, fixture: MockFixture, fifo_vcd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    options, status, code = _status_cases(fixture, fifo_vcd, tmp_path)[case]
    result = run_comparison(options)
    assert result.status is status
    if code is not None:
        assert [d.code for d in result.diagnostics] == [code]
    assert exit_code(result) == EXPECTED_EXIT[status]


def test_every_status_has_the_spec_exit_code(fixture: MockFixture, fifo_vcd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(EXPECTED_EXIT) == set(AnalysisStatus)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    seen: dict[AnalysisStatus, int] = {}
    for options, _, _ in _status_cases(fixture, fifo_vcd, tmp_path).values():
        result: ComparisonResult = run_comparison(options)
        seen[result.status] = exit_code(result)
    # Every status is produced by a real comparison and mapped per spec.md section 8.4.
    assert seen == EXPECTED_EXIT
