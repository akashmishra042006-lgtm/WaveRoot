"""Platform tests for the Track C service and CLI (src/app/service.py, cli.py).

Unit tests drive the service with a stub runner so every process status maps
to the right contract status and exit code without a simulator. Integration
tests run the real FIFO benchmark end to end through the CLI.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.app import cli
from src.app.adapters import AdapterError, FixtureCausal, SelfCheckComparison
from src.app.runner import RunRecord, RunStatus, SimRequest
from src.app.service import (
    AnalysisService,
    FixtureModeError,
    load_report,
)
from src.contracts import (
    AnalysisStatus,
    ComparisonMode,
    ComparisonResult,
    FailureAnchor,
    FailureKind,
    LogicValue,
    Observation,
    SamplingPhase,
)

REPO = Path(__file__).resolve().parent.parent.parent
FIFO_MANIFEST = REPO / "benchmarks" / "case_01_fifo_wrap" / "manifest.json"
ALU_MANIFEST = REPO / "benchmarks" / "case_04_alu_xprop" / "manifest.json"
MOCK_FIXTURE = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"


class StubRunner:
    """Returns a canned RunRecord; records the request it was given."""

    def __init__(self, tmp: Path, status: RunStatus, observed_text: str | None = None) -> None:
        self.tmp = tmp
        self.status = status
        self.observed_text = observed_text
        self.requests: list[SimRequest] = []

    def run(self, request: SimRequest, *, label: str = "run", cancel: object = None) -> RunRecord:
        self.requests.append(request)
        run_dir = self.tmp / f"run-{len(self.requests)}"
        run_dir.mkdir()
        record = RunRecord(self.status, run_dir, f"stub {self.status.value}", simulator=request.simulator)
        if self.status is RunStatus.OK:
            observed = run_dir / "observed.jsonl"
            observed.write_text(self.observed_text or "")
            record.observed = observed
            record.trace = run_dir / "trace.vcd"
        return record


def service(runner: object, **kwargs: object) -> AnalysisService:
    return AnalysisService(runner, SelfCheckComparison(), **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Status mapping: process outcomes are never design verdicts
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_status", "status", "exit_code"),
    [
        (RunStatus.TOOL_MISSING, AnalysisStatus.ERROR, 2),
        (RunStatus.BUILD_FAILED, AnalysisStatus.ERROR, 2),
        (RunStatus.RUN_FAILED, AnalysisStatus.ERROR, 2),
        (RunStatus.TIMEOUT, AnalysisStatus.ERROR, 2),
        (RunStatus.CANCELLED, AnalysisStatus.ERROR, 2),
        (RunStatus.ARTIFACT_LIMIT, AnalysisStatus.ERROR, 2),
        (RunStatus.TRACE_MISSING, AnalysisStatus.ERROR, 2),
        (RunStatus.INVALID_REQUEST, AnalysisStatus.INVALID_INPUT, 2),
    ],
)
def test_process_failures_map_to_error_not_fail(
    tmp_path: Path, run_status: RunStatus, status: AnalysisStatus, exit_code: int
) -> None:
    outcome = service(StubRunner(tmp_path, run_status)).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed"
    )
    assert outcome.status is status
    assert outcome.exit_code == exit_code
    assert outcome.comparisons == {}  # nothing was compared, nothing invented
    assert outcome.diagnostics[0].code == run_status.name


def test_malformed_observed_samples_are_invalid_input(tmp_path: Path) -> None:
    outcome = service(StubRunner(tmp_path, RunStatus.OK, "{not json\n")).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed"
    )
    assert outcome.status is AnalysisStatus.INVALID_INPUT
    assert outcome.diagnostics[0].code == "TRACE_MALFORMED"
    assert outcome.comparisons == {}


def test_empty_observed_samples_are_invalid_input(tmp_path: Path) -> None:
    outcome = service(StubRunner(tmp_path, RunStatus.OK, "")).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed"
    )
    assert outcome.status is AnalysisStatus.INVALID_INPUT


def test_variant_selects_rtl_from_manifest(tmp_path: Path) -> None:
    runner = StubRunner(tmp_path, RunStatus.BUILD_FAILED)
    service(runner).analyze(FIFO_MANIFEST, variant="correct", schedule="directed")
    service(runner).analyze(FIFO_MANIFEST, variant="mutant", schedule="directed")
    assert runner.requests[0].sources[1] == Path("rtl/correct/fifo.sv")
    assert runner.requests[1].sources[1] == Path("rtl/mutant/fifo.sv")


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


def write_manifest(tmp_path: Path, **overrides: object) -> Path:
    data = json.loads(FIFO_MANIFEST.read_text())
    data.update(overrides)
    case = tmp_path / "case"
    shutil.copytree(FIFO_MANIFEST.parent, case, ignore=shutil.ignore_patterns("build"))
    path = case / "manifest.json"
    path.write_text(json.dumps(data))
    return path


def test_schedule_path_escape_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "evil.stim").write_text("1 1 00 0\n")
    manifest = write_manifest(tmp_path, schedules={"directed": {"stim": "../evil.stim"}})
    runner = StubRunner(tmp_path, RunStatus.OK)
    outcome = service(runner).analyze(manifest, variant="mutant", schedule="directed")
    assert outcome.status is AnalysisStatus.INVALID_INPUT
    assert outcome.diagnostics[0].code == "PATH_ESCAPE"
    assert runner.requests == []  # nothing was run


def test_golden_module_must_be_a_benchmark_model(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, golden={"module": "os", "modes": ["cycle"], "primary_mode": "cycle"})
    outcome = service(StubRunner(tmp_path, RunStatus.OK)).analyze(
        manifest, variant="mutant", schedule="directed"
    )
    assert outcome.status is AnalysisStatus.INVALID_INPUT
    assert outcome.diagnostics[0].code == "MANIFEST_INVALID"


@pytest.mark.parametrize(
    ("variant", "schedule", "code"),
    [("buggy", "directed", "VARIANT_UNKNOWN"), ("mutant", "nope", "SCHEDULE_UNKNOWN")],
)
def test_unknown_variant_or_schedule(tmp_path: Path, variant: str, schedule: str, code: str) -> None:
    outcome = service(StubRunner(tmp_path, RunStatus.OK)).analyze(
        FIFO_MANIFEST, variant=variant, schedule=schedule
    )
    assert outcome.status is AnalysisStatus.INVALID_INPUT
    assert outcome.diagnostics[0].code == code


def test_four_state_case_refuses_a_substitute_simulator(tmp_path: Path) -> None:
    runner = StubRunner(tmp_path, RunStatus.OK)
    outcome = service(runner).analyze(ALU_MANIFEST, variant="mutant", schedule="directed", simulator="verilator")
    assert outcome.status is AnalysisStatus.UNSUPPORTED
    assert outcome.exit_code == 3
    assert runner.requests == []


# ----------------------------------------------------------------------
# Fixture mode is explicit and honest
# ----------------------------------------------------------------------


def test_fixture_adapter_requires_explicit_fixture_mode(tmp_path: Path) -> None:
    with pytest.raises(FixtureModeError):
        AnalysisService(StubRunner(tmp_path, RunStatus.OK), SelfCheckComparison(), FixtureCausal(MOCK_FIXTURE))  # type: ignore[arg-type]


def test_fixture_causal_refuses_a_different_failure(tmp_path: Path) -> None:
    other = FailureAnchor(
        kind=FailureKind.VALUE_MISMATCH, mode=ComparisonMode.CYCLE,
        signal_path="tb.dut.m_valid", clock_path="tb.dut.clk",
        observation=Observation(epoch=0, cycle=1, phase=SamplingPhase.SETTLED, time_ps=35_000),
        expected=LogicValue(width=1, bits="1"), actual=LogicValue(width=1, bits="0"),
        detail="m_valid dropped",
    )
    record = RunRecord(RunStatus.OK, tmp_path, "")
    with pytest.raises(AdapterError):
        FixtureCausal(MOCK_FIXTURE).analyze(other, record, [])


# ----------------------------------------------------------------------
# Saved records: schema mismatch is rejected before rendering
# ----------------------------------------------------------------------


def saved_record(tmp_path: Path) -> Path:
    outcome = service(StubRunner(tmp_path, RunStatus.BUILD_FAILED)).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed"
    )
    assert outcome.report_path is not None
    return outcome.report_path


def test_saved_record_round_trips(tmp_path: Path) -> None:
    path = saved_record(tmp_path)
    data = load_report(path)
    assert data["status"] == "error"
    assert cli.main(["open", str(path)]) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(record_version=2),
        lambda d: d.update(record="something-else"),
        lambda d: d.update(exit_code=0),
        lambda d: d.update(status="maybe"),
        lambda d: d.update(comparisons={"cycle": {"schema_version": 2}}),
    ],
    ids=["record_version", "record_name", "exit_code", "status", "contract_schema"],
)
def test_schema_mismatch_is_rejected(tmp_path: Path, mutate: object) -> None:
    path = saved_record(tmp_path)
    data = json.loads(path.read_text())
    mutate(data)  # type: ignore[operator]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_report(path)
    assert cli.main(["open", str(path)]) == 2


# ----------------------------------------------------------------------
# Real end-to-end runs through the CLI
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestCliEndToEnd:
    @pytest.fixture(autouse=True)
    def _require_verilator(self) -> None:
        if shutil.which("verilator") is None:
            pytest.fail("the FIFO benchmark requires Verilator (hard gate, not a skip)")

    def test_correct_variant_passes_with_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "correct",
                         "--schedule", "directed", "--artifacts", str(tmp_path), "--json"])
        record = json.loads(capsys.readouterr().out)
        assert code == 0
        assert record["status"] == "pass"
        assert set(record["comparisons"]) == {"cycle", "handshake"}
        assert record["causal"] is None
        assert record["run"]["tool_version"].startswith("Verilator")

    def test_mutant_fails_at_documented_anchor_with_exit_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "mutant",
                         "--schedule", "directed", "--artifacts", str(tmp_path), "--json"])
        record = json.loads(capsys.readouterr().out)
        assert code == 1
        assert record["primary_mode"] == "cycle"
        failure = record["comparisons"]["cycle"]["failure"]
        # Anchors use the trace's own hierarchy (Verilator adds TOP), spec 5.1.
        assert record["run"]["trace_scope_prefix"] == "TOP."
        assert failure["signal_path"] == "TOP.tb.dut.full"
        assert failure["observation"] == {"epoch": 0, "cycle": 3, "phase": "settled", "time_ps": "55000"}
        assert [d["code"] for d in record["diagnostics"]] == ["CAUSAL_UNAVAILABLE"]
        # The saved record re-validates and keeps the same exit code.
        saved = next(tmp_path.glob("*/report.json"))
        assert cli.main(["open", str(saved)]) == 1

    def test_fixture_report_for_another_hierarchy_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The mock report was authored for tb.dut.* paths; this Verilator
        trace declares TOP.tb.dut.*. Attaching it would describe a different
        trace, so it is refused and the run stays labeled as fixture mode."""
        code = cli.main(["analyze", str(FIFO_MANIFEST), "--variant", "mutant", "--schedule", "directed",
                         "--artifacts", str(tmp_path), "--fixture-causal", str(MOCK_FIXTURE), "--json"])
        record = json.loads(capsys.readouterr().out)
        assert code == 1
        assert record["fixture_mode"] is True
        assert record["causal_adapter"]["kind"] == "synthetic_fixture"
        assert record["causal"] is None
        assert [d["code"] for d in record["diagnostics"]] == ["CAUSAL_FAILED"]


# ----------------------------------------------------------------------
# Cancellation during the comparison step (post-merge review of #17)
# ----------------------------------------------------------------------


def test_cancel_during_comparison_stops_before_any_verdict(tmp_path: Path) -> None:
    """A cancel that arrives while a mode is being compared must not be
    reported as pass or fail: the run stops as error/CANCELLED."""
    import threading

    cancel = threading.Event()

    class SlowComparison:
        name = "slow"
        kind = "analysis"
        calls = 0

        def compare(self, golden: object, run: RunRecord) -> ComparisonResult:
            SlowComparison.calls += 1
            cancel.set()  # Ctrl-C lands while this mode is being compared
            mode = golden.header.mode  # type: ignore[attr-defined]
            return ComparisonResult(schema_version=1, status=AnalysisStatus.PASS, mode=mode,
                                    failure=None, compared_values=3)

    runner = StubRunner(tmp_path, RunStatus.OK)
    svc = AnalysisService(runner, SlowComparison())  # type: ignore[arg-type]
    outcome = svc.analyze(FIFO_MANIFEST, variant="correct", schedule="directed", cancel=cancel)
    assert outcome.status is AnalysisStatus.ERROR
    assert [d.code for d in outcome.diagnostics] == ["CANCELLED"]
    assert outcome.comparisons == {}
    assert outcome.exit_code == 2
    assert SlowComparison.calls == 1  # the second mode was never compared


def test_saved_record_rejects_an_unknown_adapter_kind(tmp_path: Path) -> None:
    runner = StubRunner(tmp_path, RunStatus.TOOL_MISSING)
    outcome = service(runner).analyze(FIFO_MANIFEST, variant="correct", schedule="directed")
    assert outcome.report_path is not None
    data = json.loads(outcome.report_path.read_text())
    load_report(outcome.report_path)
    data["comparison_adapter"]["kind"] = "made_up"
    outcome.report_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="comparison_adapter.kind"):
        load_report(outcome.report_path)
