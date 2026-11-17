"""Unit tests for the Track C golden models and the benchmark self-checker.

These run without simulators: they check the oracles against the directed
tables in docs/track-c/benchmark-spec.md, validate the emitted JSONL against
the canonical contract parser, and exercise the checker's determinism rules on
synthetic observations. They are benchmark-package coverage, not engine or
end-to-end coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.common import benchlib
from benchmarks.common.benchlib import ObservedEdge
from benchmarks.common.cases import CASES, case_by_id
from src.contracts import AnalysisStatus, FailureKind, parse_golden_jsonl

REPO = Path(__file__).resolve().parent.parent.parent


def directed_cycle_golden(case_id: str):
    case = case_by_id(case_id)
    cycle, _ = case.golden.goldens_for(case.stim("directed"))
    return cycle


def record(trace, epoch: int, cycle: int):
    for rec in trace.records:
        if rec.epoch == epoch and rec.index == cycle:
            return rec
    raise AssertionError(f"no record for epoch {epoch} cycle {cycle}")


# ----------------------------------------------------------------------
# Golden models against the benchmark-spec directed tables
# ----------------------------------------------------------------------


class TestFifoOracle:
    def test_directed_cycle_table(self) -> None:
        cycle = directed_cycle_golden("case_01_fifo_wrap")
        assert record(cycle, 0, 0).values == {
            "full": "0", "empty": "0", "out_valid": "1", "out_data": "00010001"
        }
        assert record(cycle, 0, 3).values == {
            "full": "1", "empty": "0", "out_valid": "1", "out_data": "00010001"
        }
        # Simultaneous pop/push on a full FIFO keeps occupancy at 4.
        assert record(cycle, 0, 4).values == {
            "full": "1", "empty": "0", "out_valid": "1", "out_data": "00100010"
        }
        assert record(cycle, 0, 8).values == {"full": "0", "empty": "1", "out_valid": "0"}

    def test_directed_transfers(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        payloads = [r.values["out_data"] for r in hs.records]
        assert payloads == ["00010001", "00100010", "00110011", "01000100", "01010101"]
        assert hs.header.deadline_cycle == 16

    def test_reset_midway_epochs(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        cycle, _ = case.golden.goldens_for(case.stim("reset_midway"))
        epochs = {r.epoch for r in cycle.records}
        assert epochs == {0, 1}
        # Epoch 1 restarts from an empty FIFO at cycle 0.
        assert record(cycle, 1, 0).values["empty"] == "0"  # one write accepted
        assert record(cycle, 1, 0).values["out_data"] == "11010001"


class TestElasticOracle:
    def test_directed_cycle_table(self) -> None:
        cycle = directed_cycle_golden("case_02_axi_skid")
        assert record(cycle, 0, 0).values == {"m_valid": "1", "m_data": "10100001"}
        # The stalled entry holds valid and payload.
        assert record(cycle, 0, 1).values == {"m_valid": "1", "m_data": "10100001"}
        assert record(cycle, 0, 2).values == {"m_valid": "1", "m_data": "10110010"}
        assert record(cycle, 0, 3).values == {"m_valid": "1", "m_data": "11000011"}
        assert record(cycle, 0, 4).values == {"m_valid": "0"}

    def test_directed_transfers(self) -> None:
        case = case_by_id("case_02_axi_skid")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        payloads = [r.values["m_data"] for r in hs.records]
        assert payloads == ["10100001", "10110010", "11000011"]  # A, B, C in order


class TestMacOracle:
    def test_directed_cycle_table(self) -> None:
        cycle = directed_cycle_golden("case_03_mac_overflow")
        assert record(cycle, 0, 0).values == {"out_valid": "0"}
        assert record(cycle, 0, 1).values == {"out_valid": "1", "out_data": bits16(-16000)}
        assert record(cycle, 0, 2).values == {"out_valid": "1", "out_data": bits16(-32000)}
        # Correct saturation gives -32768; the mutant's wrap would be +17536.
        assert record(cycle, 0, 3).values == {"out_valid": "1", "out_data": bits16(-32768)}
        assert record(cycle, 0, 4).values == {"out_valid": "0"}

    def test_unbounded_then_clamp(self) -> None:
        cycle = directed_cycle_golden("case_03_mac_overflow")
        assert record(cycle, 0, 3).values["out_data"] == "1000000000000000"
        wrapped = (-48000) & 0xFFFF
        assert wrapped == 17536  # the documented defective value
        assert format(wrapped, "016b") == "0100010010000000"

    def test_positive_saturation(self) -> None:
        case = case_by_id("case_03_mac_overflow")
        cycle, _ = case.golden.goldens_for(case.stim("pos_saturation"))
        assert record(cycle, 0, 3).values["out_data"] == bits16(32767)


class TestAluOracle:
    def test_directed_cycle_table(self) -> None:
        cycle = directed_cycle_golden("case_04_alu_xprop")
        assert record(cycle, 0, 0).values == {"out_valid": "1", "out_data": "00000000"}
        assert record(cycle, 0, 1).values == {"out_valid": "1", "out_data": "01011010"}
        assert record(cycle, 0, 2).values == {"out_valid": "0"}
        # After configuring sel=1, the next input selects operand B.
        assert record(cycle, 0, 3).values == {"out_valid": "1", "out_data": "00110100"}

    def test_expected_values_fully_known(self) -> None:
        # Spec 8.1: expected values are exact-width 0/1 strings, never x/z.
        for sched in ("directed", "partial_overlap", "reset_midway"):
            case = case_by_id("case_04_alu_xprop")
            cycle, _ = case.golden.goldens_for(case.stim(sched))
            for rec in cycle.records:
                for value in rec.values.values():
                    assert set(value) <= {"0", "1"}


def bits16(value: int) -> str:
    return format(value & 0xFFFF, "016b")


# ----------------------------------------------------------------------
# JSONL round-trips through the canonical parser
# ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_goldens_validate_against_contracts(case) -> None:
    for sched in case.schedules:
        cycle, hs = case.golden.goldens_for(case.stim(sched.name))
        reparsed = parse_golden_jsonl(cycle.to_jsonl())
        assert reparsed == cycle
        if case.has_handshake:
            assert hs is not None
            assert parse_golden_jsonl(hs.to_jsonl()) == hs
        else:
            assert hs is None


# ----------------------------------------------------------------------
# Checker behavior on synthetic observations
# ----------------------------------------------------------------------


def synthetic_edges(case_id: str, overrides: dict[tuple[int, str], dict[str, str]] | None = None):
    """Build observed edges that match the directed golden exactly, then apply
    per-(time, phase) overrides. The pre phase carries reset and handshake
    inputs; both phases carry the compared signals."""
    case = case_by_id(case_id)
    cycle, _ = case.golden.goldens_for(case.stim("directed"))
    rows = benchlib.parse_stim(case.stim("directed"), case.golden.COLUMNS)
    edges = []
    for t in (5000, 15000):
        edges.append(ObservedEdge(time_ps=t, pre={"rst_n": "0"}, settled={}))
    # DUT outputs start at their post-reset values (all zero for these cases).
    last: dict[str, str] = {s.name: "0" * s.width for s in cycle.header.signals}
    recs = {(r.epoch, r.index): r for r in cycle.records}
    for i, row in enumerate(rows):
        t = benchlib.edge_time_ps(i)
        # Pre-edge: reset and stimulus inputs, plus the outputs' previous
        # settled values (what `before` at this edge sees).
        pre = {"rst_n": str(row.rst_n), **last}
        for name, value in row.values.items():
            width = 8 if name.endswith("data") else 1
            pre[name] = format(value, f"0{width}b") if width > 1 else str(value & 1)
        rec = recs.get((0, i))
        settled = dict(last)
        if rec:
            settled.update(rec.values)
        last = dict(settled)
        if overrides:
            pre.update(overrides.get((t, "pre"), {}))
            settled.update(overrides.get((t, "settled"), {}))
        edges.append(ObservedEdge(time_ps=t, pre=pre, settled=settled))
    return cycle, edges


class TestChecker:
    def test_matching_trace_passes(self) -> None:
        cycle, edges = synthetic_edges("case_01_fifo_wrap")
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.PASS
        assert result.compared_values > 0

    def test_tie_break_is_declaration_order(self) -> None:
        # Flip full, empty, and out_valid at cycle 3: the anchor must be the
        # first declared signal (full); the others follow in declaration order.
        cycle, edges = synthetic_edges(
            "case_01_fifo_wrap",
            {(55000, "settled"): {"full": "0", "empty": "1", "out_valid": "0"}},
        )
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.FAIL
        assert result.failure is not None
        assert result.failure.signal_path == "tb.dut.full"
        assert result.failure.observation.time_ps == 55000
        assert [a.signal_path for a in result.additional_mismatches] == [
            "tb.dut.empty", "tb.dut.out_valid"
        ]

    def test_unknown_actual_is_unknown_value(self) -> None:
        cycle, edges = synthetic_edges(
            "case_01_fifo_wrap", {(25000, "settled"): {"out_data": "0001000x"}}
        )
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.FAIL
        assert result.failure is not None
        assert result.failure.kind is FailureKind.UNKNOWN_VALUE

    def test_missing_cycle_is_inconclusive_not_pass(self) -> None:
        cycle, edges = synthetic_edges("case_01_fifo_wrap")
        result = benchlib.check_cycle(cycle, edges[:-4])
        assert result.status is AnalysisStatus.INCONCLUSIVE
        assert result.diagnostics

    def test_x_reset_is_inconclusive(self) -> None:
        cycle, edges = synthetic_edges(
            "case_01_fifo_wrap", {(45000, "pre"): {"rst_n": "x"}}
        )
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.INCONCLUSIVE

    def test_handshake_missing_transaction(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        _, edges = synthetic_edges("case_01_fifo_wrap")
        # Suppress the last transfer: out_valid low from cycle 8 on.
        patched = []
        for e in edges:
            if e.time_ps >= 105000 and "out_valid" in e.pre:
                pre = dict(e.pre)
                pre["out_valid"] = "0"
                e = ObservedEdge(time_ps=e.time_ps, pre=pre, settled=e.settled)
            patched.append(e)
        result = benchlib.check_handshake(hs, patched)
        assert result.status is AnalysisStatus.FAIL
        assert result.failure is not None
        assert result.failure.kind is FailureKind.MISSING_TRANSACTION
        assert result.failure.transaction_index == 4
        assert result.failure.actual is None  # no payload is invented

    def test_handshake_extra_transaction(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        _, edges = synthetic_edges("case_01_fifo_wrap")
        patched = []
        for e in edges:
            if e.time_ps == 115000:  # after all five real transfers
                pre = dict(e.pre)
                pre["out_valid"] = "1"
                pre["out_ready"] = "1"
                pre["out_data"] = "11111111"
                e = ObservedEdge(time_ps=e.time_ps, pre=pre, settled=e.settled)
            patched.append(e)
        result = benchlib.check_handshake(hs, patched)
        assert result.status is AnalysisStatus.FAIL
        assert result.failure is not None
        assert result.failure.kind is FailureKind.EXTRA_TRANSACTION
        assert result.failure.expected is None

    def test_unknown_handshake_is_inconclusive(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        _, edges = synthetic_edges(
            "case_01_fifo_wrap", {(65000, "pre"): {"out_valid": "x"}}
        )
        result = benchlib.check_handshake(hs, edges)
        assert result.status is AnalysisStatus.INCONCLUSIVE

    def test_checker_is_deterministic(self) -> None:
        cycle, edges = synthetic_edges(
            "case_01_fifo_wrap",
            {(55000, "settled"): {"full": "0", "empty": "1", "out_valid": "0"}},
        )
        first = benchlib.check_cycle(cycle, edges)
        second = benchlib.check_cycle(cycle, edges)
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()


# ----------------------------------------------------------------------
# Independence and leakage guards
# ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_golden_models_do_not_read_rtl_or_graders(case) -> None:
    """The oracle derives expectations from the stimulus and the contract only:
    its code must not open RTL, traces, observed output, or grader data.
    (Prose in docstrings is ignored; this scans executable code.)"""
    import ast

    tree = ast.parse((case.directory / "golden_model.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            ):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    for token in (".sv", "graders", ".vcd", "observed", "subprocess", "open("):
        assert token not in code, f"golden model code references {token!r}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_no_answer_markers_in_analyzed_hdl(case) -> None:
    for variant in ("correct", "mutant"):
        text = case.rtl(variant).read_text().lower()
        assert "bug here" not in text
        assert "bug:" not in text
    mutant = case.rtl("mutant").read_text()
    assert "Never compile the two variants together." in mutant


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_grader_spans_match_hashes(case) -> None:
    grader_path = REPO / "benchmarks" / "graders" / f"{case.case_id}.json"
    if not grader_path.exists():
        pytest.skip(f"no grader file yet for {case.case_id}")
    grader = json.loads(grader_path.read_text())
    for span in grader["mutation"]["accepted_spans"]:
        path = REPO / span["path"]
        assert benchlib.sha256_file(path) == span["sha256"], (
            "grader hash is stale; re-run the hash update against the mutant source"
        )
        lines = path.read_text().splitlines()
        snippet = "\n".join(lines[span["start_line"] - 1 : span["end_line"]])
        assert snippet.strip(), "span points at empty lines"


class TestResetCancellation:
    """Expectations cancelled by a reset are not waived (PR #8 decision 10)."""

    def _reset_at_cycle_4(self):
        return synthetic_edges("case_01_fifo_wrap", {(65000, "pre"): {"rst_n": "0"}})

    def test_cycle_mode_epoch_ended_early(self) -> None:
        cycle, edges = self._reset_at_cycle_4()
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.INCONCLUSIVE
        assert [d.code for d in result.diagnostics] == ["EPOCH_ENDED_EARLY"]

    def test_handshake_mode_expectations_cancelled(self) -> None:
        case = case_by_id("case_01_fifo_wrap")
        _, hs = case.golden.goldens_for(case.stim("directed"))
        assert hs is not None
        _, edges = self._reset_at_cycle_4()
        result = benchlib.check_handshake(hs, edges)
        assert result.status is AnalysisStatus.INCONCLUSIVE
        assert [d.code for d in result.diagnostics] == ["EXPECTATIONS_CANCELLED_BY_RESET"]

    def test_failure_before_the_reset_still_fails(self) -> None:
        cycle, edges = synthetic_edges(
            "case_01_fifo_wrap",
            {(55000, "settled"): {"full": "0"}, (65000, "pre"): {"rst_n": "0"}},
        )
        result = benchlib.check_cycle(cycle, edges)
        assert result.status is AnalysisStatus.FAIL
        assert result.failure is not None and result.failure.observation.time_ps == 55000


def test_nothing_compared_is_inconclusive() -> None:
    from src.app.selfcheck import _pass_or_nothing
    from src.contracts import ComparisonMode

    result = _pass_or_nothing(ComparisonMode.CYCLE, 0)
    assert result.status is AnalysisStatus.INCONCLUSIVE
    assert result.diagnostics[0].code == "NOTHING_COMPARED"
