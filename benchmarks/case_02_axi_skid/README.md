# case_02_axi_skid

One-entry, non-fall-through elastic ready/valid buffer (historical name
"axi_skid"; it claims only this elastic contract, not AXI compliance). The
mutation adds an else-branch that clears `full_q` when the buffer is occupied
and the sink stalls, dropping the stalled transaction's valid — a violation of
the stall-hold rule, detectable before the next output handshake.

- Contract and directed vectors: `docs/track-c/benchmark-spec.md` section 2.
- Documented first failure (directed, mutant): cycle mode, epoch 0 cycle 1,
  `settled`, 35000 ps: `tb.dut.m_valid` expected `1`, actual `0`. The payload
  need not change for the bug to be real; the oracle also checks `m_data`
  whenever `m_valid` is expected high, so a stalled payload stays checked even
  when no handshake occurs.
- Handshake mode sees the dropped entry as the wrong first payload (B where A
  was expected). On `reset_midway` handshake mode alone passes — the surviving
  transfers still arrive in order — which is exactly why cycle observations
  accompany transaction expectations.
- Schedules: `directed`, `long_stall` (three-cycle stall hold),
  `stall_release_replace` (release coincides with a replacement offer),
  `reset_midway` (a buffered-but-unsent entry is cancelled by reset). All four
  detect the mutant in cycle mode.
- Ground truth: `benchmarks/graders/case_02_axi_skid.json` (not an analyzer
  input).

Run: `uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -k case_02`
