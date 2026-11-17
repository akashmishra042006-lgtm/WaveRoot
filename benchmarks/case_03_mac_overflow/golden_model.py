"""Independent golden model for case_03_mac_overflow.

Behavioral specification using unbounded Python integers with explicit
clamping. Never reads RTL, simulator output, or grading data.

Reference semantics (docs/track-c/benchmark-spec.md section 3):

* State: a signed 16-bit accumulator, reset to 0.
* On a valid edge the accumulator becomes ``clamp(acc + a*b, -32768, 32767)``,
  computed with unbounded integers before clamping.
* The output registers the pre-edge accumulator and ``out_valid`` registers
  the previous cycle's ``in_valid``: one extra registered output stage.
* Invalid inputs leave the accumulator unchanged. ``out_data`` is compared
  only when ``out_valid`` is expected high.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.common import benchlib
from benchmarks.common.benchlib import GoldenSpec, StimRow, bits
from src.contracts import GoldenSignal

COLUMNS = ("in_valid", "a", "b")

SPEC = GoldenSpec(
    clock="tb.dut.clk",
    reset="tb.dut.rst_n",
    # Tie-break declaration order: out_valid, then out_data.
    signals=(
        GoldenSignal(name="out_valid", path="tb.dut.out_valid", width=1),
        GoldenSignal(name="out_data", path="tb.dut.out_data", width=16),
    ),
)

INT16_MIN = -32768
INT16_MAX = 32767


def _to_int8(value: int) -> int:
    value &= 0xFF
    return value - 256 if value > 127 else value


def make_step() -> benchlib.StepFn:
    state = {"acc": 0, "prev_valid": False}

    def step(row: StimRow) -> tuple[dict[str, str], str | None]:
        out_valid = state["prev_valid"]
        out_data = state["acc"]  # pre-edge accumulator
        if row.values["in_valid"]:
            total = state["acc"] + _to_int8(row.values["a"]) * _to_int8(row.values["b"])
            state["acc"] = min(max(total, INT16_MIN), INT16_MAX)
        state["prev_valid"] = bool(row.values["in_valid"])
        expected = {"out_valid": bits(int(out_valid), 1)}
        if out_valid:
            expected["out_data"] = bits(out_data, 16)
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
