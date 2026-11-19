"""The causal step's wall-clock limit and cancellation (src/app/bounded.py).

Track B's engine is synchronous, so the service runs it in a child process it
can kill. These tests use real child processes and stub adapters; whether
Track B's engine gives the same report across the process boundary is covered
by the integration test at the end.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from causal_stubs import ChattyCausal, CrashingCausal, DyingCausal, HangingCausal, RefusingCausal

from src.app.adapters import AdapterError, CausalContext, FixtureCausal
from src.app.bounded import CausalCancelled, CausalTimeout, analyze_bounded
from src.app.runner import RunRecord, RunStatus, SimRequest
from src.app.service import AnalysisService
from src.contracts import AnalysisStatus, ComparisonResult, load_mock_fixture

REPO = Path(__file__).resolve().parent.parent.parent
FIFO_MANIFEST = REPO / "benchmarks" / "case_01_fifo_wrap" / "manifest.json"
MOCK_FIXTURE = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"
FAILURE = load_mock_fixture(MOCK_FIXTURE).report.failure
assert FAILURE is not None
CTX = CausalContext("case", REPO, None, None, None, None)


def _gone(pid_file: Path) -> bool:
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_for(path: Path, limit_s: float = 30.0) -> None:
    deadline = time.monotonic() + limit_s
    while not path.exists():
        assert time.monotonic() < deadline, "child never started"
        time.sleep(0.01)


def _record(tmp_path: Path) -> RunRecord:
    return RunRecord(RunStatus.OK, tmp_path, "")


# ----------------------------------------------------------------------
# analyze_bounded
# ----------------------------------------------------------------------


def test_report_crosses_the_process_boundary_unchanged(tmp_path: Path) -> None:
    adapter = FixtureCausal(MOCK_FIXTURE)
    expected = adapter.analyze(FAILURE, _record(tmp_path), [], CTX)
    got = analyze_bounded(adapter, FAILURE, _record(tmp_path), [], CTX, timeout_s=60)
    assert got == expected


def test_works_from_a_caller_without_an_importable_main(tmp_path: Path) -> None:
    """multiprocessing's spawn re-imports the caller's __main__ and fails for
    a `python -c`/stdin caller (and re-runs an unguarded script); the child
    must depend only on src.app.bounded."""
    import subprocess
    import sys

    script = (
        "from pathlib import Path\n"
        "from src.app.adapters import CausalContext, FixtureCausal\n"
        "from src.app.bounded import analyze_bounded\n"
        "from src.app.runner import RunRecord, RunStatus\n"
        "from src.contracts import load_mock_fixture\n"
        f"m = Path({str(MOCK_FIXTURE)!r})\n"
        "f = load_mock_fixture(m).report.failure\n"
        "ctx = CausalContext('c', Path('.'), None, None, None, None)\n"
        "r = analyze_bounded(FixtureCausal(m), f, RunRecord(RunStatus.OK, Path('.'), ''), [], ctx, timeout_s=60)\n"
        "print('suspects', len(r.suspects))\n"
    )
    done = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("suspects ")


def test_engine_output_on_stdout_does_not_corrupt_the_reply(tmp_path: Path) -> None:
    got = analyze_bounded(ChattyCausal(), FAILURE, _record(tmp_path), [], CTX, timeout_s=60)
    assert got == FixtureCausal(MOCK_FIXTURE).analyze(FAILURE, _record(tmp_path), [], CTX)


def test_timeout_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(CausalTimeout, match="limit"):
        # The limit covers interpreter start-up, so it must leave room for it.
        analyze_bounded(HangingCausal(pid_file), FAILURE, _record(tmp_path), [], CTX, timeout_s=5)
    assert time.monotonic() - started < 20
    assert _gone(pid_file)


def test_cancel_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    cancel = threading.Event()

    def cancel_once_running() -> None:
        _wait_for(pid_file)
        cancel.set()

    threading.Thread(target=cancel_once_running, daemon=True).start()
    with pytest.raises(CausalCancelled):
        analyze_bounded(HangingCausal(pid_file), FAILURE, _record(tmp_path), [], CTX,
                        timeout_s=120, cancel=cancel)
    assert _gone(pid_file)


@pytest.mark.parametrize(
    ("adapter", "message"),
    [
        (RefusingCausal(), "refused for the test"),
        (CrashingCausal(), "ZeroDivisionError: engine bug"),
        (DyingCausal(), "exited without a result"),
    ],
    ids=["adapter_refuses", "engine_raises", "child_dies"],
)
def test_child_failures_become_adapter_errors(tmp_path: Path, adapter: object, message: str) -> None:
    with pytest.raises(AdapterError, match=message):
        analyze_bounded(adapter, FAILURE, _record(tmp_path), [], CTX, timeout_s=60)  # type: ignore[arg-type]


def test_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        analyze_bounded(RefusingCausal(), FAILURE, _record(tmp_path), [], CTX, timeout_s=0)
    with pytest.raises(ValueError):
        AnalysisService(object(), object(), causal_timeout_s=-1)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# The service keeps the verdict on a timeout and drops it on a cancel
# ----------------------------------------------------------------------


class _OkRunner:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp

    def run(self, request: SimRequest, *, label: str = "run", cancel: object = None) -> RunRecord:
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        return RunRecord(RunStatus.OK, run_dir, "stub", simulator=request.simulator, trace_scope_prefix="")


class _FailingComparison:
    """Reports the fixture's failure in its mode and a pass in any other."""

    name = "failing"
    kind = "analysis"

    def compare(self, golden: object, run: RunRecord) -> ComparisonResult:
        assert FAILURE is not None
        mode = golden.header.mode  # type: ignore[attr-defined]
        if mode is FAILURE.mode:
            return ComparisonResult(schema_version=1, status=AnalysisStatus.FAIL, mode=mode,
                                    failure=FAILURE, compared_values=1)
        return ComparisonResult(schema_version=1, status=AnalysisStatus.PASS, mode=mode,
                                failure=None, compared_values=1)


def _service(tmp_path: Path, causal: object, timeout_s: float) -> AnalysisService:
    return AnalysisService(_OkRunner(tmp_path), _FailingComparison(), causal,  # type: ignore[arg-type]
                           causal_timeout_s=timeout_s)


def test_service_timeout_keeps_the_fail_verdict_unlocalized(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    outcome = _service(tmp_path, HangingCausal(pid_file), 5).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed")
    assert outcome.status is AnalysisStatus.FAIL
    assert outcome.exit_code == 1
    assert outcome.causal is None
    assert [d.code for d in outcome.diagnostics] == ["CAUSAL_TIMEOUT"]
    assert _gone(pid_file)


def test_service_cancel_during_causal_reports_no_verdict(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    cancel = threading.Event()

    def cancel_once_running() -> None:
        _wait_for(pid_file)
        cancel.set()

    threading.Thread(target=cancel_once_running, daemon=True).start()
    outcome = _service(tmp_path, HangingCausal(pid_file), 120).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed", cancel=cancel)
    assert outcome.status is AnalysisStatus.ERROR
    assert [d.code for d in outcome.diagnostics] == ["CANCELLED"]
    assert outcome.comparisons == {}
    assert outcome.causal is None
    assert _gone(pid_file)


def test_service_child_error_keeps_the_fail_verdict(tmp_path: Path) -> None:
    outcome = _service(tmp_path, CrashingCausal(), 60).analyze(
        FIFO_MANIFEST, variant="mutant", schedule="directed")
    assert outcome.status is AnalysisStatus.FAIL
    assert [d.code for d in outcome.diagnostics] == ["CAUSAL_FAILED"]
