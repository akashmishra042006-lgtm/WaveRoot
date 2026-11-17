# case_04_alu_xprop

8-bit two-operand mux with a configurable registered selector. The mutation
removes only the selector's reset assignment; `sel_q` has no initializer and
starts unknown in a four-state simulation. The mux is a conditional expression
(not a procedural `if`), so the simulator merges per bit: operand bits that
agree resolve even under an unknown selector.

- Contract and directed vectors: `docs/track-c/benchmark-spec.md` section 4.
- **Requires Icarus Verilog** (`-g2012`): the four-state gate of `spec.md`
  section 13. The integration test asserts the produced VCD actually contains
  unknown bits and that the anchor value itself carries `x`. A Verilator
  randomized-initialization run does not satisfy this gate, and a missing
  Icarus installation fails the test rather than skipping it.
- Documented first failure (directed, mutant): cycle mode, epoch 0 cycle 0,
  `settled`, 25000 ps: `tb.dut.out_data` expected `00000000`, actual
  `xxxxxxxx` (`unknown_value`). At cycle 1 the equal operands resolve to
  `01011010` with `sel_q` still `x` — unknown propagation is not
  overgeneralized, and the mutation is present even where outputs look right.
- Schedules: `directed`; `partial_overlap` (actual `0101xxxx`: only the
  disagreeing nibble is unknown); `reset_midway` (after reset the mutant holds
  a stale *known* selector — a `value_mismatch` in epoch 1, not an
  `unknown_value`).
- Ground truth: `benchmarks/graders/case_04_alu_xprop.json`.

Run: `uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -k case_04`
