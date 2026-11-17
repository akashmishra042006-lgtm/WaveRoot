"""Integration tests: build and run every benchmark variant on its required
simulator and check the result with the independent oracle.

Marked ``integration``: they require Verilator (cases 1-3) and Icarus Verilog
(case 4). A missing simulator fails the corresponding case rather than
skipping it silently — spec.md section 13 treats a skipped four-state run as
incomplete validation, and CI's simulator lanes must fail when the tool is
absent. Run with:

    uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -v
"""

from __future__ import annotations

import shutil

import pytest

from benchmarks.common import benchlib
from benchmarks.common.cases import CASES, Case, Schedule
from src.contracts import AnalysisStatus

pytestmark = pytest.mark.integration


def _require_simulator(case: Case) -> None:
    tool = {"verilator": "verilator", "icarus": "iverilog"}[case.simulator]
    if shutil.which(tool) is None:
        pytest.fail(
            f"{case.case_id} requires {case.simulator}, which is not installed. "
            "This is a hard gate (spec.md section 13), not a skip."
        )


def _run(case: Case, variant: str, schedule: Schedule, tmp_path_factory, tag: str = ""):
    run_dir = tmp_path_factory.mktemp(f"{case.case_id}_{variant}_{schedule.name}{tag}")
    result = benchlib.run_simulation(
        simulator=case.simulator,
        case_dir=case.directory,
        rtl_files=[case.rtl(variant)],
        tb_file=case.tb(),
        stim_file=case.stim(schedule.name),
        run_dir=run_dir,
    )
    cycle, hs = case.golden.goldens_for(case.stim(schedule.name))
    edges = benchlib.parse_observed(result.observed)
    return result, cycle, hs, edges


def _case_schedule_params():
    return [
        pytest.param(case, schedule, id=f"{case.case_id}-{schedule.name}")
        for case in CASES
        for schedule in case.schedules
    ]


@pytest.mark.parametrize(("case", "schedule"), _case_schedule_params())
class TestCorrectVariantPasses:
    def test_cycle_and_handshake_pass(self, case, schedule, tmp_path_factory) -> None:
        _require_simulator(case)
        _, cycle, hs, edges = _run(case, "correct", schedule, tmp_path_factory)
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.PASS, result.model_dump_json()
        assert result.compared_values > 0
        if hs is not None:
            hs_result = benchlib.check_handshake(hs, edges)
            assert hs_result.status is AnalysisStatus.PASS, hs_result.model_dump_json()


@pytest.mark.parametrize(("case", "schedule"), _case_schedule_params())
class TestMutantVerdicts:
    def test_mutant_matches_documented_verdict(self, case, schedule, tmp_path_factory) -> None:
        """A detected schedule must fail at its verified anchor; a masking
        control must pass. A build or simulator error is neither: it raises,
        and is never counted as a found design bug."""
        _require_simulator(case)
        _, cycle, _, edges = _run(case, "mutant", schedule, tmp_path_factory)
        result = benchlib.check_cycle(cycle, edges)
        if not schedule.mutant_detected:
            assert result.status is AnalysisStatus.PASS, (
                f"{schedule.name} is a documented masking control; "
                f"got {result.model_dump_json()}"
            )
            return
        assert result.status is AnalysisStatus.FAIL, result.model_dump_json()
        anchor = schedule.anchor
        assert anchor is not None, "detected schedules must document their anchor"
        failure = result.failure
        assert failure is not None
        assert failure.kind.value == anchor.kind
        assert failure.signal_path == anchor.signal_path
        assert failure.observation.epoch == anchor.epoch
        assert failure.observation.cycle == anchor.cycle
        assert failure.observation.time_ps == anchor.time_ps
        assert failure.expected is not None and failure.expected.bits == anchor.expected_bits
        assert failure.actual is not None and failure.actual.bits == anchor.actual_bits


class TestFifoDirectedAnchorDetail:
    """The documented first observable of the benchmark brief, in full."""

    def test_additional_mismatches_follow_declaration_order(self, tmp_path_factory) -> None:
        case = CASES[0]
        _require_simulator(case)
        _, cycle, hs, edges = _run(case, "mutant", case.schedules[0], tmp_path_factory)
        result = benchlib.check_cycle(cycle, edges)
        assert result.failure is not None
        assert result.failure.signal_path == "tb.dut.full"
        assert [a.signal_path for a in result.additional_mismatches] == [
            "tb.dut.empty",
            "tb.dut.out_valid",
        ]
        # Handshake mode sees the same bug later, as the wrong first payload.
        assert hs is not None
        hs_result = benchlib.check_handshake(hs, edges)
        assert hs_result.status is AnalysisStatus.FAIL
        assert hs_result.failure is not None
        assert hs_result.failure.transaction_index == 0
        assert hs_result.failure.expected is not None
        assert hs_result.failure.expected.bits == "00010001"


class TestVcdEvidence:
    def test_declared_paths_match_manifest_hierarchy(self, tmp_path_factory) -> None:
        """Canonical `tb.dut.*` paths resolve in the VCD through the recorded
        per-run scope prefix (Verilator wraps the design in `TOP`; Icarus does
        not); the prefix is detected and recorded, never assumed."""
        case = CASES[0]
        _require_simulator(case)
        result, _, _, _ = _run(case, "correct", case.schedules[0], tmp_path_factory)
        prefix = result.provenance["trace_scope_prefix"]
        paths = benchlib.vcd_declared_paths(result.vcd)
        for name in ("clk", "rst_n", "full", "empty", "out_valid", "out_data"):
            assert f"{prefix}tb.dut.{name}" in paths, (
                f"tb.dut.{name} (prefix {prefix!r}) not declared in the VCD"
            )

    def test_four_state_case_really_records_x(self, tmp_path_factory) -> None:
        """Spec section 13: the Icarus run must put actual unknown bits in the
        VCD, and the anchor itself must be unknown. A two-state run cannot
        satisfy this gate."""
        case = next(c for c in CASES if c.case_id == "case_04_alu_xprop")
        _require_simulator(case)
        result, cycle, _, edges = _run(case, "mutant", case.schedules[0], tmp_path_factory)
        assert benchlib.vcd_body_has_unknown(result.vcd)
        check = benchlib.check_cycle(cycle, edges)
        assert check.failure is not None
        assert check.failure.actual is not None
        assert "x" in check.failure.actual.bits
        # Equal operand bits still resolve under an unknown selector: the
        # cycle-1 settled output is fully known even though sel_q is x.
        settled = {e.time_ps: e.settled for e in edges}
        assert settled[35000]["out_data"] == "01011010"
        assert settled[35000]["sel_q"] == "x"


class TestDeterminism:
    def test_two_runs_produce_identical_results(self, tmp_path_factory) -> None:
        """Spec section 10: same inputs, identical contract objects. Wall-clock
        and host metadata stay outside the compared models."""
        case = CASES[0]
        _require_simulator(case)
        schedule = case.schedules[0]
        _, cycle_a, hs_a, edges_a = _run(case, "mutant", schedule, tmp_path_factory, tag="_a")
        _, cycle_b, hs_b, edges_b = _run(case, "mutant", schedule, tmp_path_factory, tag="_b")
        assert cycle_a == cycle_b
        ra = benchlib.check_cycle(cycle_a, edges_a)
        rb = benchlib.check_cycle(cycle_b, edges_b)
        assert ra == rb
        assert ra.model_dump_json() == rb.model_dump_json()
        assert hs_a is not None and hs_b is not None
        assert benchlib.check_handshake(hs_a, edges_a) == benchlib.check_handshake(hs_b, edges_b)


class TestProcessFailuresAreNotDesignBugs:
    def test_build_failure_raises_distinctly(self, tmp_path, tmp_path_factory) -> None:
        case = CASES[0]
        _require_simulator(case)
        broken = tmp_path / "broken.sv"
        broken.write_text("module fifo(; endmodule\n")
        with pytest.raises(benchlib.BuildFailure):
            benchlib.run_simulation(
                simulator=case.simulator,
                case_dir=case.directory,
                rtl_files=[broken],
                tb_file=case.tb(),
                stim_file=case.stim("directed"),
                run_dir=tmp_path_factory.mktemp("broken_build"),
            )

    def test_provenance_records_tools_and_hashes(self, tmp_path_factory) -> None:
        case = CASES[0]
        _require_simulator(case)
        result, _, _, _ = _run(case, "correct", case.schedules[0], tmp_path_factory)
        prov = result.provenance
        assert prov["simulator"] == "verilator"
        assert prov["version"]
        assert prov["build_exit"] == 0 and prov["run_exit"] == 0
        assert isinstance(prov["build_cmd"], list)  # argument arrays, not shell text
        assert all(len(h) == 64 for h in prov["inputs_sha256"].values())
