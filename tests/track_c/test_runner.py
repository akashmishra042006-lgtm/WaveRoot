"""Platform tests for the hardened runner (src/app/runner.py).

Unit tests need no simulator. Tests marked ``integration`` run real Icarus
processes — including deliberately hanging ones — to prove timeouts and
cancellation actually reap the simulator, not just a mocked handle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import src.app.runner as runner_module

from src.app.runner import Limits, Runner, RunStatus, SimRequest, resolve_inside, PathEscapeError

STIM = "1 1 11 0\n"

HANG_TB = """
module tb;
  initial begin
    forever #1;
  end
endmodule
"""

FATAL_TB = """
module tb;
  initial begin
    #5;
    $fatal(1, "deliberate failure");
  end
endmodule
"""

TB_ERROR_EXIT0_TB = """
module tb;
  initial begin
    #5;
    $display("TB_ERROR: reported but exits 0");
    $finish;
  end
endmodule
"""

FLOOD_TB = """
module tb;
  initial begin
    repeat (20000) $display("0123456789012345678901234567890123456789");
    $finish;
  end
endmodule
"""

NO_TRACE_TB = """
module tb;
  initial begin
    #5;
    $finish;
  end
endmodule
"""

BIG_TRACE_TB = """
module tb;
  reg [63:0] r;
  string vcd;
  initial begin
    r = 0;
    void'($value$plusargs("vcd=%s", vcd));
    $dumpfile(vcd);
    $dumpvars(0, tb);
    forever begin
      #1 r = r + 64'h0123456789abcdef;
    end
  end
endmodule
"""


def make_case(root: Path, tb_text: str) -> SimRequest:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tb.sv").write_text(tb_text)
    (root / "s.stim").write_text(STIM)
    return SimRequest(
        simulator="icarus",
        allowed_root=root,
        sources=(Path("tb.sv"),),
        stimulus=Path("s.stim"),
    )


def processes_mentioning(fragment: str) -> list[str]:
    """Live processes whose full command line contains ``fragment``.

    ``ww`` disables ps's column truncation; without it a long run-directory
    path is cut off and "no process remains" would pass vacuously.
    """
    out = subprocess.run(["ps", "-eww", "-o", "pid,args"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if fragment in line and "ps -eww" not in line]


def test_process_probe_sees_long_command_lines(tmp_path: Path) -> None:
    """Guard for the helper itself: it must find a process by a long path."""
    marker = tmp_path / ("x" * 150)
    probe = subprocess.Popen(["sh", "-c", f"sleep 30 # {marker}"])
    try:
        time.sleep(0.2)
        assert processes_mentioning(str(marker)), "ps output is truncated"
    finally:
        probe.kill()
        probe.wait()


needs_icarus = pytest.mark.skipif(
    shutil.which("iverilog") is None, reason="unit-level guard; the integration lane requires Icarus"
)


# ----------------------------------------------------------------------
# Path validation (no simulator needed; nothing may start)
# ----------------------------------------------------------------------


class TestPathValidation:
    def test_dotdot_escape_rejected_before_any_process(self, tmp_path: Path) -> None:
        case = tmp_path / "case"
        req = make_case(case, NO_TRACE_TB)
        (tmp_path / "outside.sv").write_text("module x; endmodule\n")
        bad = SimRequest(req.simulator, req.allowed_root, (Path("../outside.sv"),), req.stimulus)
        artifacts = tmp_path / "artifacts"
        record = Runner(artifacts).run(bad)
        assert record.status is RunStatus.INVALID_REQUEST
        assert "outside" in record.message
        assert record.run_dir is None
        assert not artifacts.exists()  # nothing was created, nothing ran

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        case = tmp_path / "case"
        req = make_case(case, NO_TRACE_TB)
        secret = tmp_path / "secret.sv"
        secret.write_text("module x; endmodule\n")
        os.symlink(secret, case / "link.sv")
        bad = SimRequest(req.simulator, req.allowed_root, (Path("link.sv"),), req.stimulus)
        record = Runner(tmp_path / "a").run(bad)
        assert record.status is RunStatus.INVALID_REQUEST

    def test_absolute_path_outside_rejected(self, tmp_path: Path) -> None:
        case = tmp_path / "case"
        req = make_case(case, NO_TRACE_TB)
        bad = SimRequest(req.simulator, req.allowed_root, req.sources, Path("/etc/hostname"))
        assert Runner(tmp_path / "a").run(bad).status is RunStatus.INVALID_REQUEST

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathEscapeError):
            resolve_inside(tmp_path, Path("nope.sv"))

    def test_bad_label_and_top_rejected(self, tmp_path: Path) -> None:
        req = make_case(tmp_path / "case", NO_TRACE_TB)
        runner = Runner(tmp_path / "a")
        assert runner.run(req, label="../x").status is RunStatus.INVALID_REQUEST
        bad_top = SimRequest(req.simulator, req.allowed_root, req.sources, req.stimulus, top="tb; rm")
        assert runner.run(bad_top).status is RunStatus.INVALID_REQUEST


class TestMissingTool:
    def test_missing_simulator_is_reported_not_skipped(self, tmp_path: Path) -> None:
        req = make_case(tmp_path / "case", NO_TRACE_TB)
        empty_path = tmp_path / "empty-bin"
        empty_path.mkdir()
        record = Runner(tmp_path / "a", search_path=str(empty_path)).run(req)
        assert record.status is RunStatus.TOOL_MISSING
        assert "iverilog" in record.message and "vvp" in record.message


# ----------------------------------------------------------------------
# Real process behavior (Icarus)
# ----------------------------------------------------------------------


@pytest.fixture(params=["waitid", "poll"])
def exit_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run process-control tests with both exit detectors. ``poll`` is what
    macOS uses (no os.waitid); forcing it here exercises it on Linux."""
    if request.param == "waitid" and not hasattr(os, "waitid"):
        pytest.skip("os.waitid is not available on this platform")
    monkeypatch.setattr(runner_module, "_HAVE_WAITID", request.param == "waitid")
    return str(request.param)


def test_exit_detection_works_without_waitid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform without os.waitid must not crash on the first step."""
    monkeypatch.setattr(runner_module, "_HAVE_WAITID", False)
    proc = subprocess.Popen(["sleep", "0.2"], start_new_session=True)
    assert runner_module._exited(proc) is False
    proc.wait()
    assert runner_module._exited(proc) is True


@pytest.mark.integration
@needs_icarus
@pytest.mark.usefixtures("exit_backend")
class TestProcessOutcomes:
    def test_build_failure_is_distinct(self, tmp_path: Path) -> None:
        req = make_case(tmp_path / "case", "module tb(; endmodule\n")
        record = Runner(tmp_path / "a").run(req)
        assert record.status is RunStatus.BUILD_FAILED
        assert record.steps[0].exit_code not in (0, None)
        assert len(record.steps) == 1  # simulation never attempted

    def test_fatal_is_run_failure(self, tmp_path: Path) -> None:
        record = Runner(tmp_path / "a").run(make_case(tmp_path / "case", FATAL_TB))
        assert record.status is RunStatus.RUN_FAILED
        assert record.steps[-1].exit_code == 1

    def test_tb_error_with_exit_zero_is_run_failure(self, tmp_path: Path) -> None:
        """vvp exits 0 after $display + $finish; the marker must still fail the run."""
        record = Runner(tmp_path / "a").run(make_case(tmp_path / "case", TB_ERROR_EXIT0_TB))
        assert record.steps[-1].exit_code == 0
        assert record.status is RunStatus.RUN_FAILED
        assert "TB_ERROR" in record.message

    def test_missing_trace_is_distinct(self, tmp_path: Path) -> None:
        record = Runner(tmp_path / "a").run(make_case(tmp_path / "case", NO_TRACE_TB))
        assert record.status is RunStatus.TRACE_MISSING

    def test_log_is_capped_and_reported(self, tmp_path: Path) -> None:
        runner = Runner(tmp_path / "a", Limits(max_log_bytes=10_000))
        record = runner.run(make_case(tmp_path / "case", FLOOD_TB))
        sim = record.steps[-1]
        assert sim.log_truncated
        assert sim.log_bytes_total > 10_000
        assert record.run_dir is not None
        log = (record.run_dir / sim.log).read_bytes()
        assert len(log) < 10_200  # cap plus the truncation note
        assert b"log truncated" in log

    def test_timeout_kills_the_simulator(self, tmp_path: Path) -> None:
        runner = Runner(tmp_path / "a", Limits(wall_clock_s=2.0))
        started = time.monotonic()
        record = runner.run(make_case(tmp_path / "case", HANG_TB), label="hang")
        assert record.status is RunStatus.TIMEOUT
        assert time.monotonic() - started < 10
        assert record.run_dir is not None
        assert processes_mentioning(str(record.run_dir)) == []

    def test_timeout_kills_grandchildren(self, tmp_path: Path) -> None:
        """The whole process group dies, including children of the step."""
        runner = Runner(tmp_path / "a", Limits(wall_clock_s=1.0))
        marker = "61.4321"
        deadline = time.monotonic() + 1.0
        step = runner._execute(
            "probe", ("sh", "-c", f"sleep {marker} & sleep {marker}; wait"),
            tmp_path, deadline, threading.Event(), None,
        )
        assert step.outcome is not None and step.outcome[0] is RunStatus.TIMEOUT
        time.sleep(0.2)
        assert processes_mentioning(f"sleep {marker}") == []

    def test_cancellation_kills_the_simulator(self, tmp_path: Path) -> None:
        runner = Runner(tmp_path / "a", Limits(wall_clock_s=60.0))
        req = make_case(tmp_path / "case", HANG_TB)
        cancel = threading.Event()
        result: list[object] = []
        worker = threading.Thread(target=lambda: result.append(runner.run(req, label="cancel", cancel=cancel)))
        worker.start()
        # Wait until the simulator process itself is running, then cancel.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not processes_mentioning("sim.vvp"):
            time.sleep(0.05)
        assert processes_mentioning("sim.vvp"), "simulator never started"
        cancel.set()
        worker.join(timeout=15)
        assert not worker.is_alive()
        record = result[0]
        assert getattr(record, "status") is RunStatus.CANCELLED
        assert processes_mentioning(str(getattr(record, "run_dir"))) == []

    def test_trace_size_limit_stops_a_runaway_dump(self, tmp_path: Path) -> None:
        runner = Runner(tmp_path / "a", Limits(wall_clock_s=60.0, max_trace_bytes=200_000))
        record = runner.run(make_case(tmp_path / "case", BIG_TRACE_TB), label="big")
        assert record.status is RunStatus.ARTIFACT_LIMIT
        assert record.run_dir is not None
        assert processes_mentioning(str(record.run_dir)) == []

    def test_concurrent_runs_get_isolated_directories(self, tmp_path: Path) -> None:
        runner = Runner(tmp_path / "a")
        req = make_case(tmp_path / "case", NO_TRACE_TB)
        records: list[object] = []
        threads = [
            threading.Thread(target=lambda: records.append(runner.run(req, label="same")))
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dirs = {getattr(r, "run_dir") for r in records}
        assert len(dirs) == 3

    def test_provenance_records_argv_version_and_hashes(self, tmp_path: Path) -> None:
        record = Runner(tmp_path / "a").run(make_case(tmp_path / "case", NO_TRACE_TB))
        data = record.to_json()
        assert data["tool_version"] and "Icarus" in str(data["tool_version"])
        steps = data["steps"]
        assert isinstance(steps, list) and all(isinstance(s["argv"], list) for s in steps)
        hashes = data["inputs_sha256"]
        assert isinstance(hashes, dict) and set(hashes) == {"tb.sv", "s.stim"}
