"""Independent golden model for case_04_alu_xprop.

Behavioral specification of the correct design: the selector resets to 0
(operand A). Expectations are always fully known ``0``/``1`` strings — spec.md
section 8.1 forbids ``x``/``z`` in expected values — and the mutant's unknown
output is caught as a ``unknown_value`` failure against the known expectation.
Never reads RTL, simulator output, or grading data.

Reference semantics (docs/track-c/benchmark-spec.md section 4):

* ``sel`` resets to 0; ``cfg_we`` updates it at the edge.
* On a valid input the output registers ``sel ? b : a`` using the pre-edge
  (old) selector, so a coinciding configuration applies to the next input.
* ``out_valid`` follows ``in_valid`` with one register stage.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.common import benchlib
from benchmarks.common.benchlib import GoldenSpec, StimRow, bits
from src.contracts import GoldenSignal

COLUMNS = ("in_valid", "a", "b", "cfg_we", "cfg_sel")

SPEC = GoldenSpec(
    clock="tb.dut.clk",
    reset="tb.dut.rst_n",
    # Tie-break declaration order: out_valid, then out_data.
    signals=(
        GoldenSignal(name="out_valid", path="tb.dut.out_valid", width=1),
        GoldenSignal(name="out_data", path="tb.dut.out_data", width=8),
    ),
)


def make_step() -> benchlib.StepFn:
    state = {"sel": 0, "out": 0}

    def step(row: StimRow) -> tuple[dict[str, str], str | None]:
        in_valid = bool(row.values["in_valid"])
        if in_valid:
            operand = row.values["b"] if state["sel"] else row.values["a"]
            state["out"] = operand & 0xFF
        if row.values["cfg_we"]:
            state["sel"] = row.values["cfg_sel"] & 1
        expected = {"out_valid": bits(int(in_valid), 1)}
        if in_valid:
            expected["out_data"] = bits(state["out"], 8)
        return expected, None

    return step


def goldens_for(stim_path: Path):
    rows = benchlib.parse_stim(stim_path, COLUMNS)
    return benchlib.build_goldens(rows, make_step, SPEC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stim", type=Path, required=True)
    parser.add_argument("--out-cycle", type=Path, required=True)
    args = parser.parse_args()
    cycle, _ = goldens_for(args.stim)
    benchlib.write_jsonl(cycle, args.out_cycle)


if __name__ == "__main__":
    main()
