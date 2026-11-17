"""Independent golden model for case_02_axi_skid.

A behavioral one-entry non-fall-through elastic buffer. Derived from the
interface contract only; never reads RTL, simulator output, or grading data.

Reference semantics (docs/track-c/benchmark-spec.md section 2):

* Pre-edge view: ``s_ready = !full || m_ready``; ``m_valid = full``; a transfer
  happens when ``m_valid && m_ready``.
* At the edge: when ``s_ready``, the entry updates from ``s_valid`` (capturing
  ``s_data`` when valid); otherwise it holds. A stalled transaction keeps both
  valid and payload until accepted.
* Cycle mode checks ``m_valid`` every cycle and ``m_data`` whenever ``m_valid``
  is expected high, so a stalled entry's payload stays checked even when no
  handshake occurs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.common import benchlib
from benchmarks.common.benchlib import GoldenSpec, StimRow, bits
from src.contracts import GoldenSignal

COLUMNS = ("s_valid", "s_data", "m_ready")

SPEC = GoldenSpec(
    clock="tb.dut.clk",
    reset="tb.dut.rst_n",
    # Tie-break declaration order: m_valid, then m_data.
    signals=(
        GoldenSignal(name="m_valid", path="tb.dut.m_valid", width=1),
        GoldenSignal(name="m_data", path="tb.dut.m_data", width=8),
    ),
    handshake_valid="tb.dut.m_valid",
    handshake_ready="tb.dut.m_ready",
    handshake_payload=GoldenSignal(name="m_data", path="tb.dut.m_data", width=8),
)


def make_step() -> benchlib.StepFn:
    state = {"full": False, "data": 0}

    def step(row: StimRow) -> tuple[dict[str, str], str | None]:
        s_valid = bool(row.values["s_valid"])
        m_ready = bool(row.values["m_ready"])
        s_ready = (not state["full"]) or m_ready
        transfer = bits(state["data"], 8) if (state["full"] and m_ready) else None
        if s_ready:
            state["full"] = s_valid
            if s_valid:
                state["data"] = row.values["s_data"] & 0xFF
        expected = {"m_valid": bits(int(state["full"]), 1)}
        if state["full"]:
            expected["m_data"] = bits(state["data"], 8)
        return expected, transfer

    return step


def goldens_for(stim_path: Path):
    rows = benchlib.parse_stim(stim_path, COLUMNS)
    return benchlib.build_goldens(rows, make_step, SPEC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stim", type=Path, required=True)
    parser.add_argument("--out-cycle", type=Path, required=True)
    parser.add_argument("--out-handshake", type=Path, required=True)
    args = parser.parse_args()
    cycle, handshake = goldens_for(args.stim)
    benchlib.write_jsonl(cycle, args.out_cycle)
    assert handshake is not None
    benchlib.write_jsonl(handshake, args.out_handshake)


if __name__ == "__main__":
    main()
