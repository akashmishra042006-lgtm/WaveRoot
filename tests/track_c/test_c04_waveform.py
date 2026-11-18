"""C04 part 1: Track A's waveform comparison wired into the Track C service/CLI.

Unit tests need no simulator. Integration tests run the real benchmarks and
check that the default comparison is Track A's engine (kind ``analysis``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.app import adapters, cli
from src.app.adapters import AdapterError, SelfCheckComparison, WaveformComparison
from src.app.runner import RunRecord, RunStatus
from src.contracts import ComparisonMode, ComparisonResult, GoldenTrace
from src.waveform.options import DEFAULT_FST_TIMEOUT_S, WaveformOptions

REPO = Path(__file__).resolve().parent.parent.parent
FIFO_MANIFEST = REPO / "benchmarks" / "case_01_fifo_wrap" / "manifest.json"
ALU_MANIFEST = REPO / "benchmarks" / "case_04_alu_xprop" / "manifest.json"
MOCK_VCD = REPO / "tests" / "fixtures" / "waveform" / "mock_fifo_verilator.vcd"


# ----------------------------------------------------------------------
# Unit tests (no simulator)
# ----------------------------------------------------------------------


def _record(run_dir: Path | None, trace: Path | None) -> RunRecord:
    return RunRecord(status=RunStatus.OK, run_dir=run_dir, message="ok", trace=trace)


def test_adapter_refuses_a_run_without_a_trace(tmp_path: Path) -> None:
    golden = _any_golden()
    with pytest.raises(AdapterError, match="trace"):
        WaveformComparison().compare(golden, _record(tmp_path, None))
    with pytest.raises(AdapterError, match="run directory"):
        WaveformComparison().compare(golden, _record(None, MOCK_VCD))


def test_adapter_writes_golden_and_forwards_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[WaveformOptions] = []
    sentinel = object()

    def fake_run_comparison(options: WaveformOptions) -> object:
        seen.append(options)
        return sentinel

    monkeypatch.setattr(adapters, "run_comparison", fake_run_comparison)
    golden = _any_golden()
    cache = tmp_path / "cache"
    adapter = WaveformComparison(keep=("tb.**",), cache_dir=cache, fst_timeout_s=7.5)
    result = adapter.compare(golden, _record(tmp_path, MOCK_VCD))
    assert result is sentinel
    (options,) = seen
    assert options.trace == MOCK_VCD
    assert options.golden == tmp_path / f"golden_{golden.header.mode.value}.jsonl"
    assert options.golden.read_text() == golden.to_jsonl()
    assert options.keep == ("tb.**",)
    assert options.cache_dir == cache
    assert options.fst_timeout_s == 7.5


def test_adapter_defaults_match_track_a() -> None:
    adapter = WaveformComparison()
    assert adapter.kind == "analysis"
    assert adapter.keep == ()
    assert adapter.cache_dir is None
    assert adapter.fst_timeout_s == DEFAULT_FST_TIMEOUT_S


def test_analyze_defaults_to_track_a_and_accepts_track_a_options() -> None:
    args = cli._parser().parse_args(
        ["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed"]
    )
    assert args.comparison == "track-a"
    assert args.keep is None and args.cache_dir is None
    assert args.fst_timeout_s is None  # resolved to DEFAULT_FST_TIMEOUT_S when the adapter is built
    args = cli._parser().parse_args(
        ["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed",
         "--comparison", "self-check", "--keep", "a.*", "--keep", "b.**",
         "--cache-dir", "/tmp/c", "--fst-timeout", "9"]
    )
    assert args.comparison == "self-check"
    assert args.keep == ["a.*", "b.**"]
    assert args.cache_dir == Path("/tmp/c")
    assert args.fst_timeout_s == 9.0


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "soon"])
def test_analyze_rejects_bad_fst_timeout(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parser().parse_args(
            ["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed",
             "--fst-timeout", bad]
        )
    assert exc.value.code == 2


def test_compare_requires_trace_and_golden() -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parser().parse_args(["compare"])
    assert exc.value.code == 2


def test_compare_reports_unreadable_trace_as_error_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden = tmp_path / "g.jsonl"
    golden.write_text(_any_golden().to_jsonl())
    code = cli.main(["compare", "--trace", str(tmp_path / "missing.vcd"), "--golden", str(golden)])
    result = ComparisonResult.model_validate_json(capsys.readouterr().out)
    assert result.status.value == "error"
    assert [d.code for d in result.diagnostics] == ["FILE_UNREADABLE"]
    assert code == 2


def test_analyze_passes_track_a_options_to_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[WaveformComparison] = []

    class SpyService:
        def __init__(self, runner: object, comparison: object, causal: object, *, fixture_mode: bool) -> None:
            assert isinstance(comparison, WaveformComparison)
            built.append(comparison)

        def analyze(self, *args: object, **kwargs: object) -> object:
            raise SystemExit(0)

    monkeypatch.setattr(cli, "AnalysisService", SpyService)
    with pytest.raises(SystemExit):
        cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed",
                  "--keep", "TOP.**", "--cache-dir", "/tmp/c", "--fst-timeout", "4"])
    (adapter,) = built
    assert adapter.keep == ("TOP.**",)
    assert adapter.cache_dir == Path("/tmp/c")
    assert adapter.fst_timeout_s == 4.0


@pytest.mark.parametrize("option", [["--keep", "TOP.**"], ["--cache-dir", "/tmp/c"],
                                    ["--fst-timeout", str(DEFAULT_FST_TIMEOUT_S)]])
def test_self_check_refuses_track_a_only_options(option: list[str]) -> None:
    """Even an explicit value equal to Track A's default is refused."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed",
                  "--comparison", "self-check", *option])
    assert exc.value.code == 2


def test_default_fst_timeout_reaches_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[WaveformComparison] = []

    class Spy:
        def __init__(self, runner: object, comparison: WaveformComparison, causal: object, *, fixture_mode: bool) -> None:
            built.append(comparison)

        def analyze(self, *a: object, **k: object) -> object:
            raise SystemExit(0)

    monkeypatch.setattr(cli, "AnalysisService", Spy)
    with pytest.raises(SystemExit):
        cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed"])
    assert built[0].fst_timeout_s == DEFAULT_FST_TIMEOUT_S


def test_adapter_reports_an_unwritable_run_dir_as_adapter_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "not-a-dir"
    run_dir.write_text("a file, not a directory")
    with pytest.raises(AdapterError, match="cannot write the golden"):
        WaveformComparison().compare(_any_golden(), _record(run_dir, MOCK_VCD))


def test_summary_prints_comparison_diagnostics() -> None:
    from src.app.service import AnalysisOutcome
    from src.contracts import AnalysisStatus, Diagnostic

    result = ComparisonResult(
        schema_version=1, status=AnalysisStatus.INVALID_INPUT, mode=ComparisonMode.CYCLE,
        compared_values=0, failure=None,
        diagnostics=[Diagnostic(code="SELECTION_INVALID", message="pattern 'x' matches no signal path")],
    )
    outcome = AnalysisOutcome(
        status=AnalysisStatus.INVALID_INPUT, case_id="c", variant="mutant", schedule="directed",
        fixture_mode=False, comparison_adapter=("track_a_waveform", "analysis"), causal_adapter=None,
        comparisons={ComparisonMode.CYCLE: result},
    )
    assert "SELECTION_INVALID: pattern 'x' matches no signal path" in cli._summary(outcome)


def _any_golden() -> GoldenTrace:
    """A small real golden produced by the FIFO benchmark's model."""
    import importlib

    module = importlib.import_module("benchmarks.case_01_fifo_wrap.golden_model")
    stim = REPO / "benchmarks" / "case_01_fifo_wrap" / "stim" / "directed.stim"
    cycle, _handshake = module.goldens_for(stim)
    assert cycle.header.mode is ComparisonMode.CYCLE
    return cycle


# ----------------------------------------------------------------------
# Integration tests (real simulators)
# ----------------------------------------------------------------------


def _analyze(
    manifest: Path, variant: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, object]]:
    code = cli.main(["analyze", str(manifest), "--variant", variant, "--schedule", "directed",
                     "--artifacts", str(tmp_path), "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def _semantic(result: dict[str, object]) -> dict[str, object]:
    """Verdict, anchor, values and counts; prose ``detail`` may differ."""
    data = json.loads(json.dumps(result))
    failure = data.get("failure")
    if isinstance(failure, dict):
        failure.pop("detail", None)
    for extra in data.get("additional_mismatches") or []:
        if isinstance(extra, dict):
            extra.pop("detail", None)
    return data  # type: ignore[no-any-return]


@pytest.mark.integration
class TestTrackAThroughTheCli:
    @pytest.fixture(autouse=True)
    def _require_tools(self) -> None:
        for tool in ("verilator", "iverilog"):
            if shutil.which(tool) is None:
                pytest.fail(f"{tool} is required (hard gate, not a skip)")

    def test_default_comparison_is_track_a_and_fails_at_the_anchor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, record = _analyze(FIFO_MANIFEST, "mutant", tmp_path, capsys)
        assert code == 1
        assert record["comparison_adapter"]["kind"] == "analysis"  # type: ignore[index]
        failure = record["comparisons"]["cycle"]["failure"]  # type: ignore[index]
        assert failure["signal_path"] == record["run"]["trace_scope_prefix"] + "tb.dut.full"
        assert failure["observation"] == {"epoch": 0, "cycle": 3, "phase": "settled", "time_ps": "55000"}

    def test_correct_variant_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code, record = _analyze(FIFO_MANIFEST, "correct", tmp_path, capsys)
        assert code == 0
        assert record["status"] == "pass"
        assert record["comparison_adapter"]["kind"] == "analysis"  # type: ignore[index]

    def test_alu_mutant_under_icarus_fails_with_unknown_value(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, record = _analyze(ALU_MANIFEST, "mutant", tmp_path, capsys)
        assert code == 1
        kinds = {r["failure"]["kind"] for r in record["comparisons"].values() if r["failure"]}  # type: ignore[attr-defined]
        assert "unknown_value" in kinds

    def test_self_check_gives_a_semantically_equal_result(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code_a, track_a = _analyze(FIFO_MANIFEST, "mutant", tmp_path / "a", capsys)
        code_s, selfcheck = _analyze(FIFO_MANIFEST, "mutant", tmp_path / "s", capsys,
                                     "--comparison", "self-check")
        assert code_a == code_s == 1
        assert selfcheck["comparison_adapter"]["kind"] == "benchmark_self_check"  # type: ignore[index]
        for mode, result in track_a["comparisons"].items():  # type: ignore[attr-defined]
            assert _semantic(result) == _semantic(selfcheck["comparisons"][mode])  # type: ignore[index]

    def test_compare_on_a_saved_trace_reproduces_the_anchor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _code, record = _analyze(FIFO_MANIFEST, "mutant", tmp_path, capsys)
        run_dir = next(tmp_path.glob("*/report.json")).parent
        trace = run_dir / record["run"]["trace"]  # type: ignore[index]
        golden = run_dir / "golden_cycle.jsonl"
        code = cli.main(["compare", "--trace", str(trace), "--golden", str(golden)])
        again = json.loads(capsys.readouterr().out)
        assert code == 1
        assert _semantic(again) == _semantic(record["comparisons"]["cycle"])  # type: ignore[index]
