# Track C handoff — current state

Date: November 16, 2026. Role: Fable benchmark role (tickets C01 and C02).
Branch: `feat/track-c-benchmarks`, based on `main` at `b5d096d` (merge of
`feat/shared-contracts`, PR #3). Not pushed or merged.

## Scope completed

C01 and C02: all four benchmark pairs, built against the accepted shared
contracts (`spec.md` schema 1, `src/contracts.py`). The proposal drafts under
`docs/track-c/proposal/` were treated as superseded by `src/contracts.py`, per
the team note; goldens are emitted through the canonical `GoldenTrace` models
and results through `ComparisonResult`.

## Changed files

- `benchmarks/common/benchlib.py` — stimulus parsing, golden emission through
  contract models, epoch mapping (spec §7), the benchmark-local independent
  checker (cycle §8.2 and handshake §8.3, declaration-order tie-break §10),
  subprocess runners (argument arrays, per-run directories, wall-clock limit,
  provenance with hashes and detected VCD scope prefix), VCD header/x scans.
  This is deliberately not Track A's engine: observed values come from the
  testbench's own phase-sampled JSONL, not from VCD value parsing.
- `benchmarks/common/cases.py` — registry with verified per-schedule verdicts.
- `benchmarks/case_0{1,2,3,4}_*/` — correct+mutant RTL, shared TB, stimulus
  schedules, independent golden model, manifest, README each.
- `benchmarks/graders/*.json` — hash-keyed mutation spans, outside analyzer
  inputs.
- `tests/track_c/test_benchmark_oracles.py` — 35 unit tests, no simulators.
- `tests/track_c/test_benchmarks_sim.py` — 38 integration tests.

## Commands and results (this environment)

- `uv sync --locked`; `uv run mypy src/` → clean (no src/ changes).
- `uv run pytest -q` → 189 passed (116 pre-existing contract tests unchanged).
- `uv run pytest -m "not integration" -q` → 151 passed, 38 deselected.
- Simulators: Verilator 5.020 (Debian 5.020-1), Icarus Verilog 12.0 (stable),
  installed via apt in the working container. Both required: the integration
  lane **fails** (not skips) when its simulator is missing.

## Verified first failures (mutant, directed; real simulator runs)

| Case | Anchor | Observation |
| --- | --- | --- |
| case_01_fifo_wrap | `tb.dut.full` 1→0, +[empty, out_valid] | e0 c3 settled 55000 ps (Verilator) |
| case_02_axi_skid | `tb.dut.m_valid` 1→0 | e0 c1 settled 35000 ps (Verilator) |
| case_03_mac_overflow | `tb.dut.out_data` -32768→+17536 | e0 c3 settled 55000 ps (Verilator) |
| case_04_alu_xprop | `tb.dut.out_data` `00000000`→`xxxxxxxx`, unknown_value | e0 c0 settled 25000 ps (Icarus, VCD contains real x) |

Correct variants pass every schedule in both modes. Masking controls
(mutant passes by design): case_01 `multi_wrap`, `reset_midway`; case_03
`mixed_bubbles`, `reset_flush` — documented in the manifests.

## Notes for the next agent

- Timing matches the shared mock fixture: epoch 0 cycle 0 at 25000 ps; trace
  time, no renormalization; phases `before`/`settled` at the edge time.
- Verilator prefixes VCD scopes with `TOP.`; Icarus does not. The per-run
  prefix is detected and recorded in `provenance.json` (`trace_scope_prefix`);
  Track A's provider should consume the mapping, not assume a prefix.
- Spec §8.1 requires every epoch to hold at least one golden record; a
  handshake schedule therefore needs at least one transfer per epoch
  (`benchlib.build_goldens` raises otherwise). Worth confirming with Dev 1
  whether a transfer-less epoch should instead drop handshake mode.
- Icarus 12 limitations encountered: no `break` statements, `$fgets` needs a
  reg buffer (not `string`). The case_04 TB reflects both.
- No changes to `src/`, `spec.md`, Track A/B paths, `pyproject.toml`, or
  `.claude/settings.json`.

## Failing or skipped checks

None in this environment. Not executed anywhere: Track A/B integration (C04),
runner/CLI (C03), UI (C05+) — those are the next tickets.

## Next single ticket

C03 (Opus platform role): application service with injectable waveform/diff
and causal adapters, CLI, per-run isolation, cancellation — building on
`benchlib.run_simulation`'s conventions. Prompt 2 in
`docs/track-c/agent-prompts.md`.
