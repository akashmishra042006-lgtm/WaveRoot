# Track C benchmarks

Four correct/mutant RTL pairs with independent Python golden models, shared
per-case harnesses, deterministic stimulus schedules, and a benchmark-local
checker that emits canonical `ComparisonResult` objects from `src/contracts.py`.
Specification: `docs/track-c/benchmark-spec.md`; shared semantics: `spec.md`.

| Case | Defect | Simulator | Documented first observable |
| --- | --- | --- | --- |
| `case_01_fifo_wrap` | Write pointer wraps without toggling its wrap bit | Verilator | `full` expected 1, actual 0 — epoch 0 cycle 3, settled, 55000 ps |
| `case_02_axi_skid` | Elastic buffer clears output valid during a stall | Verilator | `m_valid` expected 1, actual 0 — epoch 0 cycle 1, settled, 35000 ps |
| `case_03_mac_overflow` | Sum narrowed before saturation comparison | Verilator | `out_data` expected -32768, actual +17536 — epoch 0 cycle 3, settled, 55000 ps |
| `case_04_alu_xprop` | Selector register has no reset | Icarus (four-state) | `out_data` expected `00000000`, actual `xxxxxxxx` — epoch 0 cycle 0, settled, 25000 ps |

## Conventions

- Clock rises at 5000 ps and every 10000 ps after; reset (active low) is held
  from time 0 and released by stimulus line 0 at the falling edge at 20000 ps;
  epoch 0 cycle 0 is the rising edge at 25000 ps — identical to the shared mock
  fixture. Inputs are driven on falling edges only.
- Observations use the spec's `before`/`settled` phases at the edge time. The
  testbench samples `before` in the active region at the edge and `settled`
  1 ps later; with inputs stable around the edge these equal the phase queries.
- Golden models (`<case>/golden_model.py`) read only the stimulus and the
  behavioral contract. They never read RTL, traces, mutant output, or grading
  metadata; a unit test enforces this.
- Variants share one module name and are never compiled together; each run
  builds in its own directory with an explicit file list.
- Ground-truth mutation spans live in `benchmarks/graders/`, outside the
  analyzer's input bundle, keyed to source hashes. The analyzed HDL carries no
  marker comments.
- Each run records provenance: tool version, argument-array commands, exit
  codes, input hashes, and the detected VCD scope prefix (Verilator wraps the
  design in `TOP.`; Icarus does not — the mapping is detected, never assumed).
- Golden models emit canonical `tb.dut.*` paths. `benchlib.rebase_golden`
  maps them onto each run's trace hierarchy before comparison (spec §3.1), so
  every anchor resolves in its own trace (§5.1).
- Cross-check: `TestTrackAAgreement` runs Track A's real VCD comparison and the
  benchmark self-check on the same traces; verdicts, anchors, values, and
  counts must be identical (only the prose `detail` may differ).
- Schedules marked `mutant_detected: false` in a manifest are intentional
  masking controls: detection is schedule-dependent, and four directed cases
  are a smoke suite, not an accuracy estimate.

## Running

```sh
uv run pytest tests/track_c/test_benchmark_oracles.py -q     # no simulators needed
uv run pytest tests/track_c/test_benchmarks_sim.py -m integration -q
uv run pytest -m "not integration" -q                        # skip simulator lanes
```

The integration lane requires Verilator and Icarus Verilog and **fails** (not
skips) when the required simulator is missing: a skipped four-state run is not
completion (spec.md section 13).

## Release evidence (C07)

```sh
uv run python -m benchmarks.release_evidence --out /tmp/evidence
```

Runs every case x schedule x variant through the real analysis service with
both Track A's comparison and the benchmark self-check, and writes
`evidence.md` / `evidence.json`: detection, masking controls, and
correct-variant replay as counts with denominators, plus every detected
anchor. Exits non-zero if any documented verdict is contradicted or a required
simulator is missing. Localization (rank of the mutated line from Track B, measured not tuned, with
denominators) is reported for the Track A run; the grader is read only after
analysis. Fixture causal data is never counted.
