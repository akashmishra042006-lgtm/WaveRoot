"""Independent golden model for case_01_fifo_wrap.

A behavioral queue specification: it derives expectations from the stimulus and
the interface contract only. It never reads RTL, simulator output, mutation
metadata, or grading data.

Reference semantics (docs/track-c/benchmark-spec.md section 1):

* State is a queue of at most four 8-bit values.
* Pre-edge view: ``out_valid = occupancy > 0``; ``out_data`` is the head when
  nonempty; ``pop = out_valid && out_ready``; ``in_ready = occupancy < 4 ||
  pop``; ``push = in_valid && in_ready``.
* At the edge, consume the old head if ``pop``, then append if ``push``; a
  simultaneous pop/push on a full FIFO is accepted and keeps occupancy at 4.
* When empty, an accepted push is visible only after the edge (no bypass).
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from benchmarks.common import benchlib
from benchmarks.common.benchlib import GoldenSpec, StimRow, bits
from src.contracts import GoldenSignal

DEPTH = 4
COLUMNS = ("in_valid", "in_data", "out_ready")

SPEC = GoldenSpec(
    clock="tb.dut.clk",
    reset="tb.dut.rst_n",
    # Declaration order is the deterministic same-observation tie-break:
    # full, then empty, then out_valid, then out_data.
    signals=(
        GoldenSignal(name="full", path="tb.dut.full", width=1),
        GoldenSignal(name="empty", path="tb.dut.empty", width=1),
        GoldenSignal(name="out_valid", path="tb.dut.out_valid", width=1),
        GoldenSignal(name="out_data", path="tb.dut.out_data", width=8),
    ),
    handshake_valid="tb.dut.out_valid",
    handshake_ready="tb.dut.out_ready",
    handshake_payload=GoldenSignal(name="out_data", path="tb.dut.out_data", width=8),
)


def make_step() -> benchlib.StepFn:
    queue: deque[int] = deque()

    def step(row: StimRow) -> tuple[dict[str, str], str | None]:
        occupancy = len(queue)
        out_valid_pre = occupancy > 0
        pop = out_valid_pre and bool(row.values["out_ready"])
        in_ready_pre = occupancy < DEPTH or pop
        push = bool(row.values["in_valid"]) and in_ready_pre
        transfer = bits(queue[0], 8) if pop else None
        if pop:
            queue.popleft()
        if push:
            queue.append(row.values["in_data"] & 0xFF)
        occupancy = len(queue)
        expected = {
            "full": bits(int(occupancy == DEPTH), 1),
            "empty": bits(int(occupancy == 0), 1),
            "out_valid": bits(int(occupancy > 0), 1),
        }
        if occupancy > 0:
            expected["out_data"] = bits(queue[0], 8)
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
