"""Shared Track C benchmark tooling.

This module is benchmark infrastructure owned by Track C. It deliberately does
not parse VCD value changes and does not implement Track A's comparison engine:
observed values come from the testbench's own sampled JSONL, and the checker
here is the benchmark's independent self-check required by the review gates.
Contract objects (``GoldenTrace``, ``ComparisonResult``) come from
``src.contracts`` and are never redefined.

Timing convention (spec.md sections 5-7, matching the mock fixture): the clock
rises at 5000 ps and every 10000 ps after; reset is low from 0 until the
falling edge at 20000 ps; epoch 0 cycle 0 is the rising edge at 25000 ps.
Stimulus line ``i`` is applied at the falling edge at ``20000 + 10000*i`` and
sampled at the rising edge at ``25000 + 10000*i``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from src.contracts import (
    AnalysisStatus,
    ComparisonMode,
    ComparisonResult,
    Diagnostic,
    FailureAnchor,
    FailureKind,
    GoldenHeader,
    GoldenRecord,
    GoldenSignal,
    GoldenTrace,
    LogicValue,
    Observation,
    SamplingPhase,
)

PERIOD_PS = 10_000
FIRST_EDGE_PS = 5_000
CYCLE_LIMIT = 64
WALL_CLOCK_LIMIT_S = 180

# --------------------------------------------------------------------------
# Stimulus
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StimRow:
    """One applied input line. ``rst_n`` is the active-low reset level."""

    rst_n: int
    values: Mapping[str, int]


def parse_stim(path: Path, columns: Sequence[str]) -> list[StimRow]:
    """Parse a whitespace-separated stimulus file.

    The first column is always ``rst_n`` (0/1); the remaining columns are the
    case's input fields, in hexadecimal. ``#`` starts a comment.
    """
    rows: list[StimRow] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != len(columns) + 1:
            raise ValueError(f"{path}:{raw!r}: expected {len(columns) + 1} fields")
        rst = int(fields[0], 2)
        if rst not in (0, 1):
            raise ValueError(f"{path}: rst_n must be 0 or 1")
        vals = {name: int(tok, 16) for name, tok in zip(columns, fields[1:])}
        rows.append(StimRow(rst_n=rst, values=vals))
    if not rows:
        raise ValueError(f"{path}: empty stimulus")
    if len(rows) > CYCLE_LIMIT:
        raise ValueError(f"{path}: {len(rows)} lines exceed the cycle limit {CYCLE_LIMIT}")
    return rows


def edge_time_ps(line_index: int) -> int:
    """Trace time of the rising edge that samples stimulus line ``line_index``."""
    return 25_000 + PERIOD_PS * line_index


def bits(value: int, width: int) -> str:
    if value < 0:
        value += 1 << width
    if not 0 <= value < (1 << width):
        raise ValueError(f"value {value} does not fit in {width} bits")
    return format(value, f"0{width}b")


# --------------------------------------------------------------------------
# Golden emission
# --------------------------------------------------------------------------

# A model step: (state-updating callable) takes a StimRow and returns
#   expected: mapping of declared name -> exact-width bit string for the
#             settled phase at this edge (omit a name to skip it this cycle),
#   transfer: payload bit string if the reference interface accepts an output
#             transfer at this edge (pre-edge valid&ready), else None.
StepFn = Callable[[StimRow], tuple[dict[str, str], str | None]]


@dataclass
class GoldenSpec:
    clock: str
    reset: str
    signals: Sequence[GoldenSignal]  # cycle-mode declaration order = tie-break
    handshake_valid: str | None = None
    handshake_ready: str | None = None
    handshake_payload: GoldenSignal | None = None


def build_goldens(
    rows: Sequence[StimRow],
    make_step: Callable[[], StepFn],
    spec: GoldenSpec,
) -> tuple[GoldenTrace, GoldenTrace | None]:
    """Walk the stimulus once per mode and emit canonical golden traces.

    Epochs follow spec.md section 7: a row with ``rst_n`` active ends the
    current epoch (its edge has pre-edge reset active) and resets the model;
    the next inactive row starts the next epoch at cycle 0.
    """
    cycle_records: list[GoldenRecord] = []
    hs_records: list[GoldenRecord] = []
    step = make_step()
    epoch = -1
    cycle = 0
    txn = 0
    in_epoch = False
    last_cycle_of_final_epoch: int | None = None
    for row in rows:
        if row.rst_n == 0:
            in_epoch = False
            step = make_step()  # synchronous reset: model state restarts
            continue
        if not in_epoch:
            epoch += 1
            cycle = 0
            txn = 0
            in_epoch = True
        else:
            cycle += 1
        expected, transfer = step(row)
        if expected:
            cycle_records.append(
                GoldenRecord(type="expected", epoch=epoch, cycle_index=cycle, values=expected)
            )
        if transfer is not None and spec.handshake_payload is not None:
            hs_records.append(
                GoldenRecord(
                    type="expected",
                    epoch=epoch,
                    transaction_index=txn,
                    values={spec.handshake_payload.name: transfer},
                )
            )
            txn += 1
        last_cycle_of_final_epoch = cycle
    if epoch < 0:
        raise ValueError("stimulus never releases reset")

    cycle_header = GoldenHeader(
        type="metadata",
        schema_version=1,
        mode=ComparisonMode.CYCLE,
        clock=spec.clock,
        reset=spec.reset,
        reset_active="0",
        sampling=SamplingPhase.SETTLED,
        signals=tuple(spec.signals),
    )
    cycle_trace = GoldenTrace(header=cycle_header, records=tuple(cycle_records))

    hs_trace: GoldenTrace | None = None
    if spec.handshake_payload is not None:
        assert spec.handshake_valid and spec.handshake_ready
        epochs_with_txn = {r.epoch for r in hs_records}
        if epochs_with_txn != set(range(epoch + 1)):
            raise ValueError(
                "handshake golden would leave an epoch without a record "
                f"(epochs with transfers: {sorted(epochs_with_txn)}, epochs: {epoch + 1}); "
                "schedule at least one transfer per epoch or skip handshake mode"
            )
        hs_header = GoldenHeader(
            type="metadata",
            schema_version=1,
            mode=ComparisonMode.HANDSHAKE,
            clock=spec.clock,
            reset=spec.reset,
            reset_active="0",
            sampling=SamplingPhase.BEFORE,
            signals=(spec.handshake_payload,),
            valid=spec.handshake_valid,
            ready=spec.handshake_ready,
            deadline_cycle=last_cycle_of_final_epoch,
        )
        hs_trace = GoldenTrace(header=hs_header, records=tuple(hs_records))
    return cycle_trace, hs_trace


# --------------------------------------------------------------------------
# Observed samples and the independent self-check live in src/app/selfcheck.py
# (strictly typed, shared with the Track C service). Re-exported here so the
# benchmark tests keep one implementation.
# --------------------------------------------------------------------------

from src.app.selfcheck import (  # noqa: E402
    MappedEdge,
    ObservedEdge,
    ObservedFormatError,
    check_cycle,
    check_handshake,
    map_epochs,
    parse_observed,
)


# --------------------------------------------------------------------------
# Simulator runs
# --------------------------------------------------------------------------


class SimulatorUnavailable(RuntimeError):
    pass


class BuildFailure(RuntimeError):
    pass


class RunFailure(RuntimeError):
    pass


def _tool_version(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    out = (proc.stdout or proc.stderr).strip().splitlines()
    return out[0] if out else "unknown"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class RunResult:
    run_dir: Path
    vcd: Path
    observed: Path
    provenance: dict = field(default_factory=dict)


def run_simulation(
    *,
    simulator: str,
    case_dir: Path,
    rtl_files: Sequence[Path],
    tb_file: Path,
    stim_file: Path,
    run_dir: Path,
    cycle_limit: int = CYCLE_LIMIT,
    wall_clock_s: int = WALL_CLOCK_LIMIT_S,
) -> RunResult:
    """Build and run one variant in its own directory; argument arrays only."""
    if shutil.which({"verilator": "verilator", "icarus": "iverilog"}[simulator]) is None:
        raise SimulatorUnavailable(f"{simulator} is not installed")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir = run_dir.resolve()
    vcd = run_dir / "trace.vcd"
    observed = run_dir / "observed.jsonl"
    sources = [tb_file, *rtl_files]
    plusargs = [
        f"+stim={stim_file.resolve()}",
        f"+obs={observed.resolve()}",
        f"+vcd={vcd.resolve()}",
        f"+cycle_limit={cycle_limit}",
    ]
    if simulator == "verilator":
        obj = run_dir / "obj"
        build_cmd = [
            "verilator", "--binary", "--timing", "--trace",
            "--top-module", "tb", "--Mdir", str(obj),
            *[str(s.resolve()) for s in sources],
        ]
        run_cmd = [str(obj / "Vtb"), *plusargs]
        version = _tool_version(["verilator", "--version"])
    else:
        sim = run_dir / "sim.vvp"
        build_cmd = [
            "iverilog", "-g2012", "-s", "tb", "-o", str(sim),
            *[str(s.resolve()) for s in sources],
        ]
        run_cmd = ["vvp", str(sim), *plusargs]
        version = _tool_version(["iverilog", "-V"])

    build = subprocess.run(
        build_cmd, cwd=run_dir, capture_output=True, text=True, timeout=wall_clock_s
    )
    (run_dir / "build.log").write_text(build.stdout + build.stderr)
    if build.returncode != 0:
        raise BuildFailure(f"build failed ({build.returncode}); see {run_dir / 'build.log'}")

    run = subprocess.run(
        run_cmd, cwd=run_dir, capture_output=True, text=True, timeout=wall_clock_s
    )
    (run_dir / "sim.log").write_text(run.stdout + run.stderr)
    if run.returncode != 0:
        raise RunFailure(f"simulation failed ({run.returncode}); see {run_dir / 'sim.log'}")
    if not vcd.exists() or not observed.exists():
        raise RunFailure(f"simulation produced no trace/observed output in {run_dir}")

    provenance = {
        "simulator": simulator,
        "version": version,
        "build_cmd": build_cmd,
        "run_cmd": run_cmd,
        "build_exit": build.returncode,
        "run_exit": run.returncode,
        "cycle_limit": cycle_limit,
        "inputs_sha256": {str(p.relative_to(case_dir.parent.parent)): sha256_file(p)
                          for p in (*sources, stim_file)},
        "vcd_sha256": sha256_file(vcd),
        "trace_scope_prefix": detect_trace_prefix(vcd),
    }
    (run_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return RunResult(run_dir=run_dir, vcd=vcd, observed=observed, provenance=provenance)


# --------------------------------------------------------------------------
# VCD sanity (header scopes and value-change x scan; not a waveform parser)
# --------------------------------------------------------------------------


def vcd_declared_paths(vcd: Path) -> set[str]:
    """Signal paths declared in the VCD header, as dotted hierarchy names."""
    paths: set[str] = set()
    stack: list[str] = []
    with vcd.open() as fh:
        for line in fh:
            tok = line.split()
            if not tok:
                continue
            if tok[0] == "$scope" and len(tok) >= 3:
                stack.append(tok[2])
            elif tok[0] == "$upscope":
                if stack:
                    stack.pop()
            elif tok[0] == "$var" and len(tok) >= 5:
                name = tok[4]
                paths.add(".".join([*stack, name]))
            elif tok[0] == "$enddefinitions":
                break
    return paths


def detect_trace_prefix(vcd: Path, canonical: str = "tb.dut.clk") -> str:
    """The scope prefix the simulator prepended to the canonical hierarchy.

    Verilator wraps the design in a ``TOP`` scope (prefix ``"TOP."``); Icarus
    dumps ``tb.dut.*`` directly (prefix ``""``). The canonical ``tb.dut.*``
    paths in manifests and golden headers map to trace paths by prepending the
    returned prefix; the mapping is recorded per run, never assumed.
    """
    declared = vcd_declared_paths(vcd)
    suffix = canonical
    matches = sorted(p for p in declared if p == suffix or p.endswith("." + suffix))
    if not matches:
        raise ValueError(f"{vcd}: no declared path matches {canonical!r}")
    path = matches[0]
    return "" if path == suffix else path[: -len(suffix)]


def vcd_body_has_unknown(vcd: Path) -> bool:
    """True when any dumped value change carries an x/z bit."""
    in_body = False
    with vcd.open() as fh:
        for line in fh:
            if not in_body:
                if line.startswith("$enddefinitions"):
                    in_body = True
                continue
            s = line.strip().lower()
            if not s or s.startswith("#") or s.startswith("$"):
                continue
            value = s[1:].split()[0] if s[0] == "b" else s[:1]
            if "x" in value or "z" in value:
                return True
    return False


def rebase_golden(trace: GoldenTrace, prefix: str) -> GoldenTrace:
    """Map a golden trace's canonical ``tb.dut.*`` paths onto a run's trace
    hierarchy (spec.md section 3.1: Track C supplies this mapping).

    Golden models stay simulator-independent and emit canonical paths; each
    run detects its scope prefix (``detect_trace_prefix``) — ``"TOP."`` for
    Verilator, ``""`` for Icarus — and the comparison uses the rebased paths,
    so every failure anchor resolves in the trace it came from (section 5.1).
    """
    if not prefix:
        return trace
    if not prefix.endswith(".") or prefix.startswith("."):
        raise ValueError(f"invalid scope prefix {prefix!r}")
    h = trace.header
    signals = tuple(s.model_copy(update={"path": prefix + s.path}) for s in h.signals)
    header = h.model_copy(
        update={
            "clock": prefix + h.clock,
            "reset": prefix + h.reset,
            "signals": signals,
            "valid": prefix + h.valid if h.valid else None,
            "ready": prefix + h.ready if h.ready else None,
        }
    )
    # Round-trip through the canonical parser so the result is fully validated.
    from src.contracts import parse_golden_jsonl

    return parse_golden_jsonl(GoldenTrace(header=header, records=trace.records).to_jsonl())


def write_jsonl(trace: GoldenTrace, path: Path) -> None:
    path.write_text(trace.to_jsonl())
