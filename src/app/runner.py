"""Hardened simulator execution for Track C (ticket C03).

The runner builds and runs one benchmark variant in its own run directory and
reports *process* outcomes. It never decides whether the design is correct;
that is the comparison adapter's job. Keeping the two apart is what lets a
build error, a timeout, or a cancelled job never be mistaken for a found bug.

Guarantees:

* Tools are invoked with argument arrays; no shell, no client-supplied
  executable paths. The simulator is one of a fixed set.
* Every input path must resolve (following symlinks) inside an allowed root.
  The check happens before any process starts.
* Each run gets a fresh, uniquely named directory, so concurrent runs cannot
  overwrite one another.
* A wall-clock limit, an output-trace size limit, and caller cancellation all
  terminate the whole process group (SIGTERM, then SIGKILL), so no simulator
  child outlives its job.
* Captured logs are capped; the cap is reported, never silently applied.
* Tool versions, commands, exit codes, and input hashes are recorded.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Final, Literal

from src.app.tracepaths import TraceHierarchyError, detect_trace_prefix

Simulator = Literal["verilator", "icarus"]

SIMULATOR_TOOLS: Final[Mapping[Simulator, tuple[str, ...]]] = {
    "verilator": ("verilator",),
    "icarus": ("iverilog", "vvp"),
}

_TB_ERROR: Final = "TB_ERROR"
_RUN_DIR_NAME: Final = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_POLL_S: Final = 0.05
_KILL_GRACE_S: Final = 2.0


class RunStatus(StrEnum):
    """Process-level outcome of one build-and-run job."""

    OK = "ok"
    INVALID_REQUEST = "invalid_request"
    TOOL_MISSING = "tool_missing"
    BUILD_FAILED = "build_failed"
    RUN_FAILED = "run_failed"
    TIMEOUT = "timeout"
    ARTIFACT_LIMIT = "artifact_limit"
    CANCELLED = "cancelled"
    TRACE_MISSING = "trace_missing"


@dataclass(frozen=True)
class Limits:
    wall_clock_s: float = 180.0
    max_log_bytes: int = 1_000_000
    max_trace_bytes: int = 256_000_000


@dataclass(frozen=True)
class SimRequest:
    """One variant build and run. Every path is checked against ``allowed_root``."""

    simulator: Simulator
    allowed_root: Path
    sources: tuple[Path, ...]  # testbench first, then RTL
    stimulus: Path
    top: str = "tb"
    cycle_limit: int = 64


@dataclass(frozen=True)
class StepRecord:
    name: str
    argv: tuple[str, ...]
    exit_code: int | None
    duration_s: float
    log: str  # file name inside the run directory
    log_bytes_total: int
    log_truncated: bool


@dataclass
class RunRecord:
    status: RunStatus
    run_dir: Path | None
    message: str
    simulator: Simulator | None = None
    tool_version: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    inputs_sha256: dict[str, str] = field(default_factory=dict)
    trace: Path | None = None
    observed: Path | None = None
    trace_sha256: str | None = None
    trace_scope_prefix: str | None = None

    def to_json(self) -> dict[str, object]:
        """Provenance as JSON-ready data. Wall-clock durations are kept here,
        outside the contract models, so they never break determinism."""
        return {
            "status": self.status.value,
            "message": self.message,
            "simulator": self.simulator,
            "tool_version": self.tool_version,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "steps": [
                {
                    "name": s.name,
                    "argv": list(s.argv),
                    "exit_code": s.exit_code,
                    "duration_s": round(s.duration_s, 3),
                    "log": s.log,
                    "log_bytes_total": s.log_bytes_total,
                    "log_truncated": s.log_truncated,
                }
                for s in self.steps
            ],
            "inputs_sha256": dict(sorted(self.inputs_sha256.items())),
            "trace": self.trace.name if self.trace else None,
            "trace_sha256": self.trace_sha256,
            "trace_scope_prefix": self.trace_scope_prefix,
        }


class PathEscapeError(ValueError):
    """An input path resolves outside its allowed root."""


def resolve_inside(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` (relative to ``root`` unless absolute), following
    symlinks, and require the result to exist inside ``root``."""
    base = root.resolve(strict=True)
    target = candidate if candidate.is_absolute() else base / candidate
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathEscapeError(f"{candidate}: does not exist") from exc
    if not resolved.is_relative_to(base):
        raise PathEscapeError(f"{candidate}: resolves outside {base}")
    if not resolved.is_file():
        raise PathEscapeError(f"{candidate}: not a regular file")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Runner:
    """Executes ``SimRequest`` jobs under ``artifacts_root``.

    ``search_path`` overrides the PATH used to locate simulator executables
    (tests use it to model a missing tool); it never accepts a path to an
    executable from the client.
    """

    def __init__(
        self,
        artifacts_root: Path,
        limits: Limits | None = None,
        *,
        search_path: str | None = None,
    ) -> None:
        # Tools run with cwd=run_dir, so every path handed to them must be absolute.
        self.artifacts_root = artifacts_root.resolve()
        self.limits = limits or Limits()
        self.search_path = search_path

    # -- public ---------------------------------------------------------

    def run(
        self,
        request: SimRequest,
        *,
        label: str = "run",
        cancel: threading.Event | None = None,
    ) -> RunRecord:
        cancel = cancel or threading.Event()
        try:
            sources = tuple(resolve_inside(request.allowed_root, p) for p in request.sources)
            stimulus = resolve_inside(request.allowed_root, request.stimulus)
        except PathEscapeError as exc:
            return RunRecord(RunStatus.INVALID_REQUEST, None, str(exc))
        if not _RUN_DIR_NAME.fullmatch(label):
            return RunRecord(RunStatus.INVALID_REQUEST, None, f"invalid run label {label!r}")
        if not 1 <= request.cycle_limit <= 4096:
            return RunRecord(RunStatus.INVALID_REQUEST, None, "cycle_limit out of range")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", request.top):
            return RunRecord(RunStatus.INVALID_REQUEST, None, f"invalid top {request.top!r}")

        tools = {name: shutil.which(name, path=self.search_path) for name in SIMULATOR_TOOLS[request.simulator]}
        missing = sorted(name for name, found in tools.items() if found is None)
        if missing:
            return RunRecord(
                RunStatus.TOOL_MISSING,
                None,
                f"{request.simulator} requires {', '.join(missing)}, not found",
                simulator=request.simulator,
            )

        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.artifacts_root / f"{label}-{uuid.uuid4().hex[:12]}"
        run_dir.mkdir()
        record = RunRecord(RunStatus.OK, run_dir, "", simulator=request.simulator)
        root = request.allowed_root.resolve()
        record.inputs_sha256 = {
            str(p.relative_to(root)): sha256_file(p) for p in (*sources, stimulus)
        }
        record.tool_version = self._tool_version(request.simulator, tools)

        trace = run_dir / "trace.vcd"
        observed = run_dir / "observed.jsonl"
        plusargs = (
            f"+stim={stimulus}",
            f"+obs={observed}",
            f"+vcd={trace}",
            f"+cycle_limit={request.cycle_limit}",
        )
        build_argv, run_argv = self._commands(request, tools, sources, run_dir, plusargs)

        deadline = time.monotonic() + self.limits.wall_clock_s
        build = self._execute("build", build_argv, run_dir, deadline, cancel, None)
        record.steps.append(build.step)
        if build.outcome is not None:
            record.status, record.message = build.outcome
            return record
        if build.step.exit_code != 0:
            record.status = RunStatus.BUILD_FAILED
            record.message = f"build exited {build.step.exit_code}; see {build.step.log}"
            return record

        sim = self._execute("simulate", run_argv, run_dir, deadline, cancel, trace)
        record.steps.append(sim.step)
        if sim.outcome is not None:
            record.status, record.message = sim.outcome
            return record
        if sim.step.exit_code != 0 or sim.tb_error:
            record.status = RunStatus.RUN_FAILED
            reason = "testbench reported TB_ERROR" if sim.tb_error else f"exited {sim.step.exit_code}"
            record.message = f"simulation {reason}; see {sim.step.log}"
            return record
        if not trace.is_file() or not observed.is_file():
            record.status = RunStatus.TRACE_MISSING
            record.message = "simulation finished but produced no trace or observed samples"
            return record
        if trace.stat().st_size > self.limits.max_trace_bytes:
            record.status = RunStatus.ARTIFACT_LIMIT
            record.message = f"trace exceeds {self.limits.max_trace_bytes} bytes"
            return record
        try:
            record.trace_scope_prefix = detect_trace_prefix(trace)
        except (TraceHierarchyError, OSError) as exc:
            record.status = RunStatus.TRACE_MISSING
            record.message = f"trace hierarchy not usable: {exc}"
            return record
        record.trace = trace
        record.observed = observed
        record.trace_sha256 = sha256_file(trace)
        record.message = "build and simulation completed"
        return record

    # -- internals ------------------------------------------------------

    def _commands(
        self,
        request: SimRequest,
        tools: Mapping[str, str | None],
        sources: Sequence[Path],
        run_dir: Path,
        plusargs: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if request.simulator == "verilator":
            exe = tools["verilator"]
            assert exe is not None
            obj = run_dir / "obj"
            build = (
                exe, "--binary", "--timing", "--trace",
                "--top-module", request.top, "--Mdir", str(obj),
                *(str(s) for s in sources),
            )
            return build, (str(obj / f"V{request.top}"), *plusargs)
        iverilog, vvp = tools["iverilog"], tools["vvp"]
        assert iverilog is not None and vvp is not None
        image = run_dir / "sim.vvp"
        build = (iverilog, "-g2012", "-s", request.top, "-o", str(image), *(str(s) for s in sources))
        return build, (vvp, str(image), *plusargs)

    def _tool_version(self, simulator: Simulator, tools: Mapping[str, str | None]) -> str:
        exe = tools["verilator" if simulator == "verilator" else "iverilog"]
        assert exe is not None
        flag = "--version" if simulator == "verilator" else "-V"
        try:
            proc = subprocess.run((exe, flag), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        return lines[0] if lines else "unknown"

    @dataclass
    class _Execution:
        step: StepRecord
        outcome: tuple[RunStatus, str] | None
        tb_error: bool

    def _execute(
        self,
        name: str,
        argv: tuple[str, ...],
        cwd: Path,
        deadline: float,
        cancel: threading.Event,
        watch_trace: Path | None,
    ) -> Runner._Execution:
        log_name = f"{name}.log"
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group, so killpg reaps children
            )
        except OSError as exc:
            step = StepRecord(name, argv, None, 0.0, log_name, 0, False)
            (cwd / log_name).write_text(f"failed to start: {exc}\n")
            return Runner._Execution(step, (RunStatus.RUN_FAILED, f"{name} failed to start: {exc}"), False)

        assert proc.stdout is not None
        capture = _CappedCapture(proc.stdout, cwd / log_name, self.limits.max_log_bytes)
        capture.start()
        outcome: tuple[RunStatus, str] | None = None
        while not _exited(proc):
            if cancel.is_set():
                outcome = (RunStatus.CANCELLED, f"{name} cancelled")
            elif time.monotonic() > deadline:
                outcome = (RunStatus.TIMEOUT, f"{name} exceeded {self.limits.wall_clock_s:g} s")
            elif (
                watch_trace is not None
                and watch_trace.exists()
                and watch_trace.stat().st_size > self.limits.max_trace_bytes
            ):
                outcome = (RunStatus.ARTIFACT_LIMIT, f"trace exceeded {self.limits.max_trace_bytes} bytes")
            if outcome is not None:
                _terminate_group(proc)
                break
            time.sleep(_POLL_S)
        # Nothing the step spawned may outlive it. With waitid the leader is
        # still unreaped here, so this cannot signal an unrelated group (see
        # _exited for the macOS case); reap only afterwards.
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        capture.join()
        step = StepRecord(
            name=name,
            argv=argv,
            exit_code=proc.returncode,
            duration_s=time.monotonic() - started,
            log=log_name,
            log_bytes_total=capture.total,
            log_truncated=capture.truncated,
        )
        return Runner._Execution(step, outcome, capture.saw_tb_error)


# Python exposes os.waitid (and WNOWAIT) on Linux but not on macOS.
_HAVE_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


def _exited(proc: subprocess.Popen[bytes]) -> bool:
    """True once the step's leader process has exited.

    Where ``os.waitid`` exists this does not reap it: leaving the leader an
    unreaped zombie keeps its process-group ID reserved, so a later ``killpg``
    can never reach an unrelated group that reused it.

    Without it (macOS) the leader is reaped here instead. The group ID is then
    still reserved while any group member lives, and the only exposure is the
    few microseconds between this call and the final ``killpg`` in which the
    whole group has died and the kernel has recycled the ID; PIDs are handed out
    sequentially, so that does not happen in practice.
    """
    if _HAVE_WAITID:
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the whole process group, then SIGKILL whatever survives.

    Never reaps the leader: the caller kills the group once more and only then
    reaps, so the group ID stays reserved throughout.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + _KILL_GRACE_S
    while not _exited(proc) and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    _signal_group(proc.pid, signal.SIGKILL)


class _CappedCapture(threading.Thread):
    """Drains a pipe to a file, keeping at most ``cap`` bytes but counting all.

    Draining continues past the cap so the child never blocks on a full pipe.
    """

    def __init__(self, stream: IO[bytes], path: Path, cap: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.path = path
        self.cap = cap
        self.total = 0
        self.truncated = False
        self.saw_tb_error = False
        self._tail = b""

    def run(self) -> None:
        marker = _TB_ERROR.encode()
        with self.path.open("wb") as out:
            for chunk in iter(lambda: self.stream.read(65536), b""):
                window = self._tail + chunk
                if marker in window:
                    self.saw_tb_error = True
                self._tail = window[-len(marker):]
                room = self.cap - self.total
                if room > 0:
                    out.write(chunk[:room])
                if len(chunk) > room:
                    self.truncated = True
                self.total += len(chunk)
            if self.truncated:
                out.write(f"\n[log truncated: {self.total} bytes total, cap {self.cap}]\n".encode())
