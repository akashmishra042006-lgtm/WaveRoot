"""C04 part 2: Track B's engine behind the service's causal adapter.

The unit tests need no simulator. The integration tests run the real pipeline
(simulator -> Track A -> ``TrackBCausal``) through ``AnalysisService`` and read
the grader only after the report exists; it is never an adapter input.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from benchmarks.common.cases import CASES, Case
from src.app import cli
from src.app.adapters import AdapterError, CausalContext, TrackBCausal, WaveformComparison
from src.app.runner import Limits, RunRecord, RunStatus, Runner
from src.app.service import AnalysisService, load_manifest
from src.contracts import AnalysisStatus, ProvenanceKind, load_mock_fixture

REPO = Path(__file__).resolve().parents[2]
MOCK = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"


def _failure():  # type: ignore[no-untyped-def]
    failure = load_mock_fixture(MOCK).report.failure
    assert failure is not None
    return failure


def test_manifest_declares_the_dut_location_for_every_case() -> None:
    for case in CASES:
        m = load_manifest(case.directory / "manifest.json")
        assert (m.top, m.dut_instance) == ("tb", "dut"), case.case_id
        assert m.reset_path and m.reset_active in ("0", "1"), case.case_id


def test_adapter_refuses_when_the_manifest_does_not_locate_the_dut(tmp_path: Path) -> None:
    ctx = CausalContext("c", tmp_path, None, None, None, None)
    with pytest.raises(AdapterError, match="top, dut_instance, reset.path, reset.active"):
        TrackBCausal().analyze(_failure(), RunRecord(RunStatus.OK, tmp_path, ""), [], ctx)


def test_adapter_refuses_a_run_without_a_trace(tmp_path: Path) -> None:
    ctx = CausalContext("c", tmp_path, "tb", "dut", "tb.dut.rst_n", "0")
    with pytest.raises(AdapterError, match="no trace"):
        TrackBCausal().analyze(_failure(), RunRecord(RunStatus.OK, tmp_path, ""), [], ctx)


def _need(case: Case) -> None:
    tool = "iverilog" if case.simulator == "icarus" else "verilator"
    if shutil.which(tool) is None:
        pytest.fail(f"{tool} required (hard gate, not a skip)")


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_service_attaches_a_real_causal_report(case: Case, tmp_path: Path, record_property) -> None:  # type: ignore[no-untyped-def]
    _need(case)
    service = AnalysisService(Runner(tmp_path / "a", Limits(wall_clock_s=180)), WaveformComparison(), TrackBCausal())
    outcome = service.analyze(case.directory / "manifest.json", variant="mutant", schedule="directed")
    assert outcome.status is AnalysisStatus.FAIL
    assert outcome.causal is not None, [d.code for d in outcome.diagnostics]
    report = outcome.causal
    assert report.provenance.kind is ProvenanceKind.ANALYSIS
    assert outcome.causal_adapter == ("track_b_causal", "analysis")
    assert "CAUSAL_UNAVAILABLE" not in [d.code for d in outcome.diagnostics]
    assert outcome.primary is not None and report.failure == outcome.primary.failure
    for source in report.sources:
        assert hashlib.sha256((case.directory / source.path).read_bytes()).hexdigest() == source.sha256
    json.dumps(outcome.to_json())  # the whole record serializes

    # The grader is read only now, after analysis.
    truth = json.loads((REPO / "benchmarks" / "graders" / f"{case.case_id}.json").read_text())
    spans = truth["mutation"]["accepted_spans"]
    by_id = {s.id: (case.directory / s.path).resolve() for s in report.sources}
    ranks = [
        s.rank for s in report.suspects if s.location is not None for sp in spans
        if by_id[s.location.source_id] == (REPO / sp["path"]).resolve()
        and s.location.line <= sp["end_line"] and s.location.end_line >= sp["start_line"]
    ]
    rank = min(ranks) if ranks else None
    record_property("mutated_line_rank", rank if rank is not None else "unranked")
    assert rank is not None and rank <= 2, f"{case.case_id}: rank {rank}"


@pytest.mark.integration
def test_a_passing_run_has_no_causal_report(tmp_path: Path) -> None:
    case = CASES[0]
    _need(case)
    service = AnalysisService(Runner(tmp_path / "a", Limits(wall_clock_s=180)), WaveformComparison(), TrackBCausal())
    outcome = service.analyze(case.directory / "manifest.json", variant="correct", schedule="directed")
    assert outcome.status is AnalysisStatus.PASS and outcome.causal is None


@pytest.mark.integration
def test_cli_analyze_localizes_by_default_and_none_opts_out(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    manifest = str(CASES[0].directory / "manifest.json")
    base = ["analyze", manifest, "--variant", "mutant", "--schedule", "directed",
            "--artifacts", str(tmp_path), "--json"]
    assert cli.main(base) == 1
    rec = json.loads(capsys.readouterr().out)
    assert rec["causal_adapter"] == {"name": "track_b_causal", "kind": "analysis"}
    assert rec["causal"]["suspects"] and rec["fixture_mode"] is False
    assert cli.main([*base, "--causal", "none"]) == 1
    rec = json.loads(capsys.readouterr().out)
    assert rec["causal"] is None and rec["causal_adapter"] is None
    assert "CAUSAL_UNAVAILABLE" in [d["code"] for d in rec["diagnostics"]]
