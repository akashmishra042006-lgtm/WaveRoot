"""Real simulator -> Track A -> Track B smoke tests for the four directed cases.

The grader is read only after analysis, never passed to the causal engine.
Run with --junitxml=work/causal-e2e.xml to retain measured ranks and diagnostics.
This is public engine integration, not coverage of a future service causal adapter.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Callable

import pytest

from benchmarks.common.cases import CASES, Case
from src.app.adapters import WaveformComparison
from src.app.runner import Limits, Runner
from src.app.service import AnalysisService
from src.causal import SliceOptions, analyze_failure
from src.contracts import AnalysisStatus, CausalReport, ProvenanceKind
from src.waveform import VcdWaveformProvider, parse_vcd

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
@pytest.mark.parametrize("variant", ["correct", "mutant"])
def test_real_directed_causal_pipeline(
    case: Case, variant: str, tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    tool = "iverilog" if case.simulator == "icarus" else "verilator"
    if shutil.which(tool) is None:
        pytest.fail(f"{tool} required for real causal integration (hard gate)")
    service = AnalysisService(Runner(tmp_path / "runs", Limits(wall_clock_s=180)), WaveformComparison())
    outcome = service.analyze(case.directory / "manifest.json", variant=variant, schedule="directed")
    assert outcome.status is (AnalysisStatus.PASS if variant == "correct" else AnalysisStatus.FAIL), outcome.to_json()
    assert outcome.primary is not None
    if variant == "correct":
        assert outcome.primary.failure is None
        return
    failure = outcome.primary.failure
    run = outcome.run
    assert failure is not None and run is not None and run.trace is not None
    assert run.trace_scope_prefix is not None
    expected = next(s.anchor for s in case.schedules if s.name == "directed")
    assert expected is not None
    assert failure.signal_path == run.trace_scope_prefix + expected.signal_path
    assert failure.observation.time_ps == expected.time_ps
    assert failure.observation.cycle == expected.cycle
    manifest = json.loads((case.directory / "manifest.json").read_text())
    provider = VcdWaveformProvider(parse_vcd(run.trace.read_text()))
    sources = [case.directory / p for p in manifest["analysis_inputs"]["rtl"]]
    report = analyze_failure(
        failure, provider, sources, root=case.directory,
        options=SliceOptions(
            trace_scope=run.trace_scope_prefix + manifest["top"] + "." + manifest["dut_instance"],
            reset_path=run.trace_scope_prefix + manifest["reset"]["path"],
            reset_active=manifest["reset"]["active"], case_id=case.case_id,
        ),
    )
    CausalReport.model_validate_json(report.model_dump_json())
    assert report.provenance.kind is ProvenanceKind.ANALYSIS
    assert report.failure == failure
    assert report.anchor_node_id is not None
    anchor = next(n for n in report.nodes if n.id == report.anchor_node_id)
    assert anchor.signal_path == failure.signal_path and anchor.observation == failure.observation
    assert anchor.value == failure.actual
    assert report.sources
    for source in report.sources:
        assert hashlib.sha256((case.directory / source.path).read_bytes()).hexdigest() == source.sha256

    # Only the test grader sees mutation locations, after the report is complete.
    truth = json.loads((REPO / "benchmarks" / "graders" / f"{case.case_id}.json").read_text())
    accepted = truth["mutation"]["accepted_spans"]
    for span in accepted:
        assert hashlib.sha256((REPO / span["path"]).read_bytes()).hexdigest() == span["sha256"]
    by_id = {s.id: s for s in report.sources}
    ranks = [s.rank for s in report.suspects if s.location is not None for span in accepted
             if (case.directory / by_id[s.location.source_id].path).resolve() == (REPO / span["path"]).resolve()
             and s.location.line <= span["end_line"] and s.location.end_line >= span["start_line"]]
    rank = min(ranks) if ranks else None
    record_property("mutated_line_rank", rank if rank is not None else "unranked")
    record_property("truncated", report.truncated)
    record_property("diagnostics", json.dumps([d.code for d in report.diagnostics]))
    # Pin the published four-case smoke result, not a general accuracy claim.
    assert rank is not None and rank <= 2, f"{case.case_id}: mutated line rank {rank}; diagnostics={report.diagnostics}"
