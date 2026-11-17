# case_01_fifo_wrap

Depth-4, 8-bit synchronous FIFO with a wrap-pointer mutation. The correct and
mutant variants share one external contract and one harness; they are never
compiled together.

- Contract and directed vectors: `docs/track-c/benchmark-spec.md` section 1.
- Timing: clock rises at 5000 ps then every 10000 ps; reset (active low) is
  released at the falling edge at 20000 ps; epoch 0 cycle 0 is the rising edge
  at 25000 ps. Spec phases: `before` and `settled` at the edge time
  (`spec.md` section 5).
- Mutation: the write pointer resets its address at 3 but keeps the old wrap
  bit instead of toggling it. Ground truth lives in `benchmarks/graders/`,
  outside the analyzer's input bundle; the analyzed HDL carries no marker.

## Documented first failure (directed schedule, mutant)

Cycle mode, epoch 0 cycle 3, `settled`, 55000 ps: `tb.dut.full` expected `1`,
actual `0`; `tb.dut.empty` and `tb.dut.out_valid` also mismatch at the same
observation and are kept, in golden declaration order, as additional
mismatches. Handshake mode catches the same bug later: transaction 0 expected
payload `0x11`, observed `0x55` (epoch 0 cycle 5).

## Schedules

| Schedule | Mutant detected | Purpose |
| --- | --- | --- |
| `directed` | yes (cycle 3, `full`) | The documented first observable, including a simultaneous pop/push on a full FIFO |
| `full_hold` | yes (cycle 3, `full`) | Writes against full with no pop must be refused |
| `multi_wrap` | no (masking control) | Two wraps with occupancy never crossing 0/4 in between: the lost wrap bit cancels; detection is schedule-dependent |
| `reset_midway` | no (masking control) | Mid-stream synchronous reset; neither epoch reaches a wrap |

The masking schedules are intentional controls: four authored examples are a
smoke suite, not an accuracy estimate, and a mutant pass here is the expected
result, not a missed check.

## Running

```sh
uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -k case_01
```

Each run builds in its own directory under `build/` (gitignored) and leaves
`trace.vcd`, `observed.jsonl`, golden JSONL, per-mode `ComparisonResult` JSON,
and `provenance.json` (tool version, commands, exit codes, input hashes).

The golden model is a behavioral queue in `golden_model.py`; it reads only the
stimulus and the interface contract. The observed JSONL is written by the
testbench itself (pre-edge values read in the active region, settled values
1 ps after the edge); the checker in `benchmarks/common/benchlib.py` is the
benchmark's independent self-check, not Track A's comparison engine.
