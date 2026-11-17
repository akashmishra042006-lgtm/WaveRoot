"""Track A tests: FST input through GTKWave's ``fst2vcd``.

Unit tests replace the converter with small Python scripts written to
``tmp_path``; they exercise process handling, not real FST decoding. The single
``integration`` test converts a real FST and needs GTKWave's ``vcd2fst`` and
``fst2vcd`` on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.contracts import AnalysisStatus, MockFixture, load_mock_fixture
from src.waveform import (
    ConversionFailedError,
    ConverterUnavailableError,
    UnsupportedTraceError,
    VcdWaveformProvider,
    compare,
    compare_files,
    convert_fst_to_vcd_text,
    load_trace,
)
from tests.test_waveform import vcd_from_mock

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "mock_fifo_golden.jsonl"


@pytest.fixture(scope="module")
def fixture() -> MockFixture:
    return load_mock_fixture(FIXTURES / "mock_fifo_failure.json")


@pytest.fixture(scope="module")
def fifo_vcd(fixture: MockFixture) -> str:
    return vcd_from_mock(fixture.trace)


def fake_converter(directory: Path, body: str, *, name: str = "fst2vcd") -> Path:
    """An executable Python script standing in for the converter."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!{sys.executable}\nimport sys, time\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)
    return script


def echo_converter(directory: Path, vcd_text: str) -> Path:
    """A converter that logs its argv and prints ``vcd_text``."""
    log = directory / "argv.json"
    return fake_converter(
        directory,
        f"""
        import json
        with open({str(log)!r}, "w") as handle:
            json.dump(sys.argv[1:], handle)
        sys.stdout.write({vcd_text!r})
        """,
    )


@pytest.fixture
def input_fst(tmp_path: Path) -> Path:
    path = tmp_path / "trace dir" / "sim.fst"
    path.parent.mkdir()
    path.write_bytes(b"not really an FST; the fake converter ignores it")
    return path


# --------------------------------------------------------------------------
# convert_fst_to_vcd_text
# --------------------------------------------------------------------------


def test_successful_conversion_returns_stdout_and_writes_nothing_beside_the_input(
    tmp_path: Path, input_fst: Path, fifo_vcd: str
) -> None:
    script = echo_converter(tmp_path / "bin", fifo_vcd)
    before = sorted(input_fst.parent.iterdir())
    assert convert_fst_to_vcd_text(input_fst, executable=str(script)) == fifo_vcd
    assert sorted(input_fst.parent.iterdir()) == before
    # One argument, passed as a list element: the space in the path survives intact.
    assert json.loads((tmp_path / "bin" / "argv.json").read_text()) == [str(input_fst)]


def test_non_zero_exit_reports_status_and_truncated_stderr(tmp_path: Path, input_fst: Path) -> None:
    script = fake_converter(
        tmp_path / "bin", 'sys.stderr.write("E" * 5000 + "TAIL")\nsys.stdout.write("$date")\nsys.exit(3)\n'
    )
    with pytest.raises(ConversionFailedError) as caught:
        convert_fst_to_vcd_text(input_fst, executable=str(script))
    error = caught.value
    assert error.returncode == 3
    assert error.stderr == "E" * 2000
    assert "exit status 3" in str(error)
    assert "E" * 2000 in str(error) and "E" * 2001 not in str(error) and "TAIL" not in str(error)


def test_timeout_is_a_conversion_failure(tmp_path: Path, input_fst: Path) -> None:
    script = fake_converter(tmp_path / "bin", 'sys.stderr.write("working"); sys.stderr.flush()\ntime.sleep(30)\n')
    with pytest.raises(ConversionFailedError, match="timed out") as caught:
        convert_fst_to_vcd_text(input_fst, executable=str(script), timeout_s=0.5)
    assert caught.value.returncode is None
    assert "exit status" in str(caught.value)


@pytest.mark.parametrize("output", ["", "  \n\t\n"])
def test_empty_output_is_a_conversion_failure(tmp_path: Path, input_fst: Path, output: str) -> None:
    script = fake_converter(tmp_path / "bin", f"sys.stdout.write({output!r})\n")
    with pytest.raises(ConversionFailedError, match="no output") as caught:
        convert_fst_to_vcd_text(input_fst, executable=str(script))
    assert caught.value.returncode == 0


def test_empty_output_keeps_the_converters_stderr(tmp_path: Path, input_fst: Path) -> None:
    script = fake_converter(tmp_path / "bin", 'sys.stderr.write("bad FST magic " + "M" * 3000)\n')
    with pytest.raises(ConversionFailedError, match="no output") as caught:
        convert_fst_to_vcd_text(input_fst, executable=str(script))
    assert caught.value.stderr == ("bad FST magic " + "M" * 3000)[:2000]
    assert "bad FST magic" in str(caught.value)


def test_leading_dash_file_name_is_never_read_as_an_option(
    tmp_path: Path, fifo_vcd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = echo_converter(tmp_path / "bin", fifo_vcd)
    work = tmp_path / "work"
    work.mkdir()
    (work / "-fother.fst").write_bytes(b"opaque")
    monkeypatch.chdir(work)
    convert_fst_to_vcd_text(Path("-fother.fst"), executable=str(script))
    argv = json.loads((tmp_path / "bin" / "argv.json").read_text())
    assert argv == [str(Path.cwd() / "-fother.fst")]
    assert not argv[0].startswith("-")


def test_non_utf8_output_is_a_conversion_failure(tmp_path: Path, input_fst: Path) -> None:
    script = fake_converter(tmp_path / "bin", 'sys.stdout.buffer.write(b"\\xff\\xfe$date")\n')
    with pytest.raises(ConversionFailedError, match="UTF-8"):
        convert_fst_to_vcd_text(input_fst, executable=str(script))


def test_missing_tool_names_the_tool_and_how_to_install_it(
    tmp_path: Path, input_fst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ConverterUnavailableError) as caught:
        convert_fst_to_vcd_text(input_fst)
    assert "fst2vcd" in str(caught.value) and "GTKWave" in str(caught.value)
    with pytest.raises(ConverterUnavailableError, match="no-such-converter"):
        convert_fst_to_vcd_text(input_fst, executable="no-such-converter")


def test_unreadable_input_is_reported_before_running_the_converter(tmp_path: Path, fifo_vcd: str) -> None:
    script = echo_converter(tmp_path / "bin", fifo_vcd)
    with pytest.raises(FileNotFoundError):
        convert_fst_to_vcd_text(tmp_path / "absent.fst", executable=str(script))
    assert not (tmp_path / "bin" / "argv.json").exists()


@pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_positive_and_finite(input_fst: Path, timeout_s: float) -> None:
    with pytest.raises(ValueError):
        convert_fst_to_vcd_text(input_fst, timeout_s=timeout_s)


# --------------------------------------------------------------------------
# load_trace and compare_files
# --------------------------------------------------------------------------


def test_load_trace_dispatches_on_suffix(
    tmp_path: Path, input_fst: Path, fifo_vcd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = VcdWaveformProvider.from_text(fifo_vcd)
    vcd_path = tmp_path / "sim.vcd"
    vcd_path.write_text(fifo_vcd, encoding="utf-8")
    assert load_trace(vcd_path).trace == reference.trace

    echo_converter(tmp_path / "bin", fifo_vcd)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert load_trace(input_fst).trace == reference.trace
    upper = input_fst.with_name("SIM.FST")
    input_fst.rename(upper)
    assert load_trace(upper).trace == reference.trace

    for name in ("sim.txt", "sim.vcd.gz", "sim", "sim.ghw"):
        other = tmp_path / name
        other.write_text(fifo_vcd, encoding="utf-8")
        with pytest.raises(UnsupportedTraceError, match="suffix"):
            load_trace(other)


def test_compare_files_reads_fst_and_maps_converter_problems_to_error(
    tmp_path: Path, input_fst: Path, fixture: MockFixture, fifo_vcd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(bin_dir))

    echo_converter(bin_dir, fifo_vcd)
    converted = compare_files(input_fst, GOLDEN)
    assert converted == compare(VcdWaveformProvider.from_text(fifo_vcd), fixture.golden)
    assert converted.status is AnalysisStatus.FAIL

    fake_converter(bin_dir, 'sys.stderr.write("corrupt FST")\nsys.exit(1)\n')
    failed = compare_files(input_fst, GOLDEN)
    assert failed.status is AnalysisStatus.ERROR
    assert [d.code for d in failed.diagnostics] == ["CONVERSION_FAILED"]
    assert "corrupt FST" in failed.diagnostics[0].message

    fake_converter(bin_dir, "pass\n")
    empty = compare_files(input_fst, GOLDEN)
    assert (empty.status, [d.code for d in empty.diagnostics]) == (AnalysisStatus.ERROR, ["CONVERSION_FAILED"])

    (bin_dir / "fst2vcd").unlink()
    missing = compare_files(input_fst, GOLDEN)
    assert (missing.status, [d.code for d in missing.diagnostics]) == (
        AnalysisStatus.ERROR,
        ["CONVERTER_UNAVAILABLE"],
    )

    fake_converter(bin_dir, 'sys.stdout.write("$timescale 1 ps $end\\n#0\\n")\n')
    invalid = compare_files(input_fst, GOLDEN)
    assert (invalid.status, [d.code for d in invalid.diagnostics]) == (AnalysisStatus.INVALID_INPUT, ["TRACE_INVALID"])

    absent = compare_files(tmp_path / "absent.fst", GOLDEN)
    assert (absent.status, [d.code for d in absent.diagnostics]) == (AnalysisStatus.ERROR, ["FILE_UNREADABLE"])

    wrong_suffix = tmp_path / "sim.txt"
    wrong_suffix.write_text(fifo_vcd, encoding="utf-8")
    unsupported = compare_files(wrong_suffix, GOLDEN)
    assert (unsupported.status, [d.code for d in unsupported.diagnostics]) == (
        AnalysisStatus.UNSUPPORTED,
        ["TRACE_UNSUPPORTED"],
    )


# --------------------------------------------------------------------------
# Real conversion (integration)
# --------------------------------------------------------------------------


def _explicitly_requested(request: pytest.FixtureRequest) -> bool:
    """True unless this is an unfiltered run.

    Any ``-m`` or ``-k`` selection that still includes this test, or naming it
    by node ID, counts as asking for it, so it fails instead of skipping.
    """
    config = request.config
    if config.option.markexpr or config.option.keyword:
        return True
    name = request.node.name
    return any(name in str(arg) for arg in config.args)


@pytest.mark.integration
def test_real_fst_converts_to_the_equivalent_vcd(
    tmp_path: Path, fixture: MockFixture, fifo_vcd: str, request: pytest.FixtureRequest
) -> None:
    """Round-trip the mock FIFO trace through vcd2fst and fst2vcd.

    Fails (never skips) when selected with ``-m``, ``-k``, or its node ID and the
    GTKWave tools are absent; only in an unfiltered run does it skip with the reason.
    """
    missing = [tool for tool in ("vcd2fst", "fst2vcd") if shutil.which(tool) is None]
    if missing:
        message = f"{', '.join(missing)} not on PATH; install GTKWave to run this integration test"
        if _explicitly_requested(request):
            pytest.fail(message)
        pytest.skip(message + " (select it with -m integration to require it)")

    vcd_path = tmp_path / "fifo.vcd"
    fst_path = tmp_path / "fifo.fst"
    vcd_path.write_text(fifo_vcd, encoding="utf-8")
    subprocess.run(["vcd2fst", str(vcd_path), str(fst_path)], check=True, capture_output=True, timeout=60)

    reference = VcdWaveformProvider.from_text(fifo_vcd)
    converted = load_trace(fst_path)
    paths = list(reference.trace.signal_paths)
    assert list(converted.trace.signal_paths) == paths
    assert converted.get_coverage() == reference.get_coverage()
    for path in paths:
        assert converted.get_signal_decl(path) == reference.get_signal_decl(path)
    end = reference.get_coverage().end_ps + 1
    assert converted.get_window_slice(paths, 0, end) == reference.get_window_slice(paths, 0, end)
    assert compare_files(fst_path, GOLDEN) == compare_files(vcd_path, GOLDEN)
