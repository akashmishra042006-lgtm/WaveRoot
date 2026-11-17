# case_03_mac_overflow

Pipelined signed MAC: int8 × int8 products accumulate into a signed 16-bit
saturating accumulator; the output registers the pre-edge accumulator and
`out_valid` registers the previous cycle's `in_valid` (one extra output stage).
The mutation compares a sign-extended 16-bit narrowing of the sum against the
clamp limits instead of the 17-bit wide sum, so overflow evidence is lost and
the accumulator wraps.

- Contract and directed vectors: `docs/track-c/benchmark-spec.md` section 3.
- Documented first failure (directed, mutant): cycle mode, epoch 0 cycle 3,
  `settled`, 55000 ps: `tb.dut.out_data` expected `1000000000000000` (-32768),
  actual `0100010010000000` (+17536, the 16-bit wrap of -48000). The internal
  accumulator diverges one cycle earlier; that is diagnostic evidence in the
  trace (`acc_q` is dumped), not the first declared output mismatch.
- The golden model uses unbounded Python integers, then clamps; it never
  mirrors RTL width behavior.
- Schedules: `directed` (negative saturation), `pos_saturation` (127×127 three
  times; the mutant wraps 48387 to -17149), `neg_boundary` (exactly -32768
  without clamping — both variants agree — then one step beyond),
  `mixed_bubbles` and `reset_flush` (masking controls: no overflow, mutant
  passes by design).
- Ground truth: `benchmarks/graders/case_03_mac_overflow.json`.

Run: `uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -k case_03`
