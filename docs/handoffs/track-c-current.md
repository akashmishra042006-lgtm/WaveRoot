# Track C handoff — current state

Date: November 16, 2026. Latest role: Opus platform role (ticket C03).
Branch: `feat/track-c-platform`, stacked on `feat/track-c-benchmarks`
(PR #5, C01+C02), based on `main` at `b5d096d`. Not merged.

## Team state at handoff

- Track A: PR #4 was merged, then **reverted** (PR #7) pending contract rulings
  on spec sections 4, 6.1, 7, 9. `main` has no Track A code. It returns in a new
  rebased PR.
- Track B: PR #6 open (`feat/track-b-causal`), not merged.
- Contracts: PR #8 open (`contracts/rulings-a-d`). VCD-tail evidence for its
  decision 11 was posted on the PR.
- Conflict check (Nov 16): #5, #6, #8 touch disjoint files; pairwise and
  combined merges are clean; `main + #5 + #8` passes the fast suite and mypy.

## C03 — completed

Application service, adapters, CLI, and hardened runner in `src/app/`:

- `runner.py` — argument-array execution; unique per-run directories; every
  input resolved (following symlinks) inside the manifest's directory before
  any process starts; wall-clock timeout, trace-size limit, and caller
  cancellation kill the whole process group (SIGTERM, then SIGKILL); capped,
  reported logs; tool version, argv, exit codes, input hashes recorded.
  Distinct statuses: ok, invalid_request, tool_missing, build_failed,
  run_failed, timeout, artifact_limit, cancelled, trace_missing.
- `selfcheck.py` — the benchmark self-check, moved here from `benchlib` and
  strictly typed (benchlib re-exports it). Implements PR #8's stricter reset
  rules (`EPOCH_ENDED_EARLY`, `EXPECTATIONS_CANCELLED_BY_RESET`,
  `NOTHING_COMPARED`); compatible with the merged spec text.
- `adapters.py` — `ComparisonAdapter`/`CausalAdapter` protocols, each labeled
  `analysis` | `benchmark_self_check` | `synthetic_fixture`. Available now:
  `SelfCheckComparison`, `FixtureCausal` (refuses any failure it was not
  authored for).
- `service.py` — manifest-driven `AnalysisService`; process failures map to
  `error`/`invalid_input`, never `fail`; synthetic adapters require explicit
  fixture mode; missing causal engine is the diagnostic `CAUSAL_UNAVAILABLE`,
  never sample data. Writes `report.json` (run record
  `waveroot-track-c-run` v1 wrapping contract objects); `load_report`
  re-validates every contract object.
- `cli.py` — `python -m src.app.cli analyze MANIFEST --variant V --schedule S`
  and `open REPORT`; exit codes per spec 8.4; Ctrl-C cancels and reaps.

Also: testbench errors now use `$fatal` — previously `$display("TB_ERROR")` +
`$finish` exited 0, so a malformed stimulus line looked like a clean shorter
run. The runner additionally treats any `TB_ERROR` output as a run failure.
Manifests gained a `golden` block (module, modes, primary mode).

## Commands and results

- `uv run mypy src/` → clean (10 files, strict).
- `uv run pytest -q` → 237 passed (116 contract, 35+4 oracle/checker, 38
  benchmark integration, 17 runner, 27 service/CLI).
- `uv run pytest -m "not integration" -q` → 186 passed, 51 deselected.
- Simulators: Verilator 5.020, Icarus Verilog 12.0. Timeout and cancellation
  are tested against real hanging `vvp` processes, with a guard test proving
  the process probe sees full command lines (`ps -eww`).

## C04 — blocked, not complete

Requires real Track A and Track B modules on `main`; neither is present. The
service is ready for them: implement `ComparisonAdapter` over Track A's
provider/diff engine and `CausalAdapter` over Track B's slicer, using only
`src/contracts.py` types, then run case_01 mutant/directed through both. The
benchmark self-check is labeled `benchmark_self_check` and must not be
reported as Track A coverage.

## Open questions

- Dev 1: spec §8.1 forbids epochs without golden records, so handshake
  schedules need ≥1 transfer per epoch (`build_goldens` enforces this).
- A console-script entry point would touch shared `pyproject.toml`; deferred to
  a focused PR. Track A's waveform options get wired into `cli.py` when Track A
  re-lands.
- `benchmarks/common/benchlib.run_simulation` still has its own simple runner
  for the benchmark tests; it could delegate to `src.app.runner.Runner`.

## Next single ticket

C05 (Astra frontend role, Prompt 3): UI against `ComparisonResult` and
`CausalReport` using the mock fixture plus real run records from
`python -m src.app.cli analyze ... --json`. C04 follows when A and B land.
