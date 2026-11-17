# Benchmark specification

All cycles below are active, post-reset cycles. Drivers apply inputs before the rising edge. Tables distinguish inputs consumed at the edge from observations after settling. Use exact-width bit strings for serialization; decimal descriptions here are explanatory. The fixture vector file is normative for the directed examples and is checked independently by `tools/validate.mjs`.

## Common harness

- Top module `tb`, DUT instance `dut`; preserve a signal-map file rather than assuming simulator-generated hierarchy prefixes.
- Default 10 ns clock, 1 ps precision. Store integer time ticks and the tick unit. Record pre-edge sample at `edge_tick-1` and post-settle sample at `edge_tick+1` for these controlled harnesses.
- Reset for two rising edges, then release on a falling edge. Normalize the next rising edge to tick 5000 / cycle 0. Retain the actual trace offset in provenance. A later reset starts a new epoch and clears golden-model and alignment state.
- Set `clk` to a known value from initialization. Do not use an X-to-1 transition as the first functional edge.
- Do not drive inputs or release reset on the sampling rising edge. No zero-delay race-dependent checker code.
- `$dumpfile` and `$dumpvars` capture DUT state needed by the slicer, including intermediate arithmetic/control. If a needed signal is absent, report insufficient evidence.
- Build each variant in its own directory with an explicit top and file list. Correct and buggy variants may share a module name but must never compile together.
- No blanket width-warning suppression. Intentional width behavior must be explicit and reviewed.
- Run at most the declared cycle limit (default 64), drain with no new input for eight cycles, and fail remaining transactions at the declared deadline. Every process also has an independent wall-clock limit.
- Simulator compile/run success is separate from design-check success. The harness should preserve its full trace through the observation window even when the expected mutant mismatch occurs.

## 1. FIFO wrap

### Interface and reference

Depth 4; 8-bit `in_data`/`out_data`; `in_valid`, `in_ready`, `out_valid`, `out_ready`, `full`, `empty`; active-low synchronous reset. State is a queue. `out_valid = occupancy > 0`; `out_data` is the head if nonempty. Invalid data is not compared. `pop = out_valid && out_ready`; `in_ready = occupancy < 4 || pop`; `push = in_valid && in_ready`.

At an edge, consume the old head if `pop`, then append `in_data` if `push`. This permits a full simultaneous pop/push without changing occupancy. When empty, an accepted push is visible only after the edge; there is no same-edge bypass.

Correct RTL uses three-bit read/write pointers (two address bits plus wrap bit). Empty means full-pointer equality; full means equal address bits with unequal wrap bits. The mutation resets the write address at 3 but preserves the old wrap bit rather than toggling it. Keep the change to one semantic location.

| Cycle | in_valid / data | out_ready | Expected queue after edge | full / empty |
|---|---|---|---|---|
| 0 | 1 / 0x11 | 0 | 11 | 0 / 0 |
| 1 | 1 / 0x22 | 0 | 11,22 | 0 / 0 |
| 2 | 1 / 0x33 | 0 | 11,22,33 | 0 / 0 |
| 3 | 1 / 0x44 | 0 | 11,22,33,44 | 1 / 0 |
| 4 | 1 / 0x55 | 1 | 22,33,44,55 | 1 / 0 |
| 5 | 0 / ignored | 1 | 33,44,55 | 0 / 0 |
| 6 | 0 / ignored | 1 | 44,55 | 0 / 0 |
| 7 | 0 / ignored | 1 | 55 | 0 / 0 |
| 8 | 0 / ignored | 1 | empty | 0 / 1 |

Expected transfers at cycles 4..8: `11,22,33,44,55`. At cycle 3 post-settle the faulty pointers both appear zero: `full=0`, `empty=1`, `out_valid=0`. Deterministic same-observation priority is `full`, `empty`, `out_valid`, `out_data`; therefore anchor `full`, expected 1/actual 0. Time is 35001 ps in the normalized timeline. This priority breaks ties; do not claim one status signal physically fails before another.

Additional gates: reset midway; empty read request; full write without pop; simultaneous operations while partially full; multiple wraps with distinct data. The grader should recognize the write-wrap source span, without exposing it to the analyzer.

## 2. Elastic buffer stall (historical name: AXI skid)

One-entry non-fall-through elastic buffer. Use `s_valid/s_ready/s_data` and `m_valid/m_ready/m_data`; 8-bit payload. `s_ready = !full || m_ready`; `m_valid=full`. On `s_ready`, update full from `s_valid` and capture data if valid. Otherwise hold full and data. The source must hold valid and payload until accepted.

Mutation: when `!m_ready` and occupied, clear `full`, dropping the buffered transaction. This violates the stall-hold rule; it is detectable before the next output handshake.

| Cycle | source | m_ready | Correct post-edge state | Output transfer at edge |
|---|---|---|---|---|
| 0 | valid A=0xA1 | 1 | valid A | none |
| 1 | valid B=0xB2 | 0 | valid A | none |
| 2 | hold B=0xB2 | 1 | valid B | A |
| 3 | valid C=0xC3 | 1 | valid C | B |
| 4 | no input | 1 | invalid | C |

First fault is cycle 1 post-settle, `m_valid`, expected 1/actual 0 (15001 ps). Data need not change for the bug to be real. The oracle sequence is A,B,C, not merely a comparison of the mutant's observed transfer count. Also check payload stability across stalls, multi-cycle stalls, stall release plus replacement, and drain-to-empty. No claim of full AXI sideband support.

## 3. Pipelined signed MAC saturation

Inputs: signed 8-bit `a`, `b`, `in_valid`; no backpressure in MVP. State: signed 16-bit accumulator reset to 0; signed 16-bit output register and output-valid bit. On a valid edge, update the accumulator with `clamp(acc + a*b, -32768, 32767)`. Independently, output registers the **pre-edge** accumulator; `out_valid` registers the previous cycle's `in_valid`. This gives one additional registered output stage. Invalid inputs do not change the accumulator; output comparisons require `out_valid=1`.

Reference uses unbounded signed Python integers, then clamps. RTL must widen product and accumulator before addition/comparison. Mutation narrows the sum to 16 bits before comparing it to limits, losing the evidence of overflow.

| Cycle | in_valid; a,b | Correct accumulator after edge | Correct out_valid / out_data after edge |
|---|---|---|---|
| 0 | 1; -128,125 | -16000 | 0 / ignored |
| 1 | 1; -128,125 | -32000 | 1 / -16000 |
| 2 | 1; -128,125 | -32768 | 1 / -32000 |
| 3 | 0; 0,0 | -32768 | 1 / -32768 |
| 4 | 0; 0,0 | -32768 | 0 / ignored |

The defective cycle-2 accumulator becomes +17536 (16-bit wrap of -48000), observed on output at cycle 3. Anchor `out_data`, 35001 ps, expected bits `1000000000000000`, actual `0100010010000000`. The earlier internal divergence is diagnostic evidence, not the first declared output mismatch. Add independent positive saturation (`127*127` three times), exact boundaries, mixed signs, zero operands, bubbles, and reset flush. Do not accidentally describe output latency differently in the golden model, UI, and slicer.

## 4. Unreset mux control and four-state propagation

Inputs: 8-bit operands `a`,`b`, `in_valid`, `cfg_we`, `cfg_sel`; state `sel_q`; registered `out_data` and `out_valid`. Correct reset sets `sel_q=0`. Configuration updates it when `cfg_we=1`. On valid input, `out_data <= sel_q ? b : a`; out_valid follows in_valid. If configuration and input coincide, the input uses the **old** selector due to registered state semantics. Reset clears output-valid and output-data in both variants.

Mutation removes only the selector reset assignment; it has no initializer. Crucially use a conditional expression, not procedural `if (sel_q)`, for the X-merging example.

| Cycle | Inputs | Correct post-edge output |
|---|---|---|
| 0 | valid; a=0x00, b=0xFF; no configuration | valid 0x00 |
| 1 | valid; a=0x5A, b=0x5A; no configuration | valid 0x5A |
| 2 | no input; configure sel=1 | invalid |
| 3 | valid; a=0x12, b=0x34 | valid 0x34 |
| 4 | no input | invalid |

In a four-state simulation, unknown selector at cycle 0 yields `xxxxxxxx` because all operand bits differ. At cycle 1 equal bits resolve to `01011010` even with an unknown selector; this checks that unknown propagation is not overgeneralized. The mutation is still present even though some outputs appear correct. Initial anchor: `out_data`, 5001 ps, expected `00000000`, actual `xxxxxxxx`.

Run this on Icarus using explicit SystemVerilog mode and procedural checks supported by the installed version. Confirm the generated VCD actually contains unknown bits. A Verilator randomized-initialization run can be an additional reset sensitivity test; it does not satisfy the four-state gate.

## Build conventions for the implementation agent

These are command shapes, not commands executed for this package. Resolve actual file paths, versions, and environment first. On Windows prefer a tested WSL2/Linux or existing team environment; do not silently install a new system toolchain.

```sh
verilator --binary --timing --trace-vcd --top-module tb --Mdir build/obj tb.sv dut.sv
# Run generated build/obj/Vtb from a per-run working directory.
iverilog -g2012 -s tb -o build/sim.vvp tb.sv dut.sv
vvp build/sim.vvp
```

Use argument arrays in the runner, not shell-joined strings. Version support must be probed; no automatic fallback that changes semantics. Generic assertion synthesis is optional and must not gate baseline diagnosis. Candidate assertions need compilation and positive/negative runtime checks on the chosen simulator before being called validated.

## Evaluation without answer leakage

Store ground-truth source spans and patches outside the HDL/trace input bundle. Match locations to source hashes. The analyzer sees the buggy design, stimulus, trace, and behavioral contract; it must not read the correct variant or mutation manifest as a shortcut to the answer.

Report detection, localization, and replay separately. Detection requires correct anchor/classification. Localization measures top-1/top-2/top-5 recall against allowed bug spans; return `unranked` where unavailable. Replay means the correct variant removes the failure on the same inputs. Four directed examples establish a smoke suite, not a statistically meaningful accuracy estimate. Maintain a held-out deterministic set with alternate data and stalls, then report counts and denominators.
