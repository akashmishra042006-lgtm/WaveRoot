# November 17, 2026 — dark dashboard refresh

Branch `feat/track-c-ui` fast-forwarded onto merged main `96e2593` before this
refresh. React 19 + TypeScript, Tailwind CSS, React Flow, and high-DPI Canvas
replace the earlier React 18/SVG presentation. Shared schema/fixtures unchanged.

The failure hero shows exact Tfail, raw binary and hex; ranked suspects explicitly
state confidence is not supplied. Copyable report-level assertions retain their
validation labels. Graphs distinguish cyan combinational / amber dashed sequential
edges, actual previous-cycle carries, retained active paths, unknown controls, and
dimmed pruned branches. A shared selection synchronizes nodes, waveform rows,
exact before/settled cursor values, and revision-checked, highlighted SV source.
A bounded ps ruler and observed-clock ten-cycle fit avoid assumed clock periods;
short/gapped traces retain available coverage. Dump gaps are not drawn as held
values. The fixture remains explicitly synthetic, with no invented assertions.

Validation in this cloud workspace:
- Frontend: 54 tests pass; strict typecheck + production build pass.
- Browser: Chromium 151, 1440/1024/768/375 px, no page overflow/visible control
  overlaps/page errors. Actual React Flow node clicks synchronize waveform/source.
  Screenshots inspected. On Intel Xeon Platinum 8573C, 100 signals × 1,000
  transitions + 1,000 nodes: selection median 17.2 ms, max 143.2 ms; cursor median
  17.0 ms. The occasional selection maximum exceeds the 100 ms target. Initial
  load ~3.5 s. Graph drawing still capped at 400 nodes (accessible list above it).
- `uv sync --locked`; `uv run mypy src/`: 17 files clean.
- `uv run pytest -m "not integration" -q`: 421 passed, 69 deselected.
- `uv run pytest tests/track_c/test_ui_fixtures.py -q`: 13 passed.
- Full simulator gate could not pass locally: Verilator/Icarus are absent
  (421 passed, 12 skipped, 54 failed, 3 errors). CI must run those real tool gates;
  this is not full A → B → C integration coverage.

Next: verify CI simulator gates, then continue C04 integration. Previous handoff
below is historical; its statement that Track C PRs are unmerged is superseded.

---

# Track C handoff — current state

Date: November 16, 2026. Latest work: ticket C05 (interface), done in the
Opus session (the plan's "Astra" role is not a selectable model).
Branch: `feat/track-c-ui`, stacked on `feat/track-c-platform` (PR #10, C03),
which is stacked on `feat/track-c-benchmarks` (PR #5, C01+C02). Not merged.

## Team state at handoff (Nov 16, late)

- `main` now has the contract rulings (#8) and **Track A re-landed (#9)**.
  #11/#12 (decision 11 firm, stacked) are open. Track B (#6) is open.
- All three Track C branches have current `main` merged in; the stack is
  #5 → #10 → #13.

## Review of #5, #10, #13 (Nov 16)

Same-session review (not independent; a cold review in a fresh session is
still recommended). Findings, all fixed on the PR they belong to:

1. High (#5): golden paths ignored Verilator's `TOP.` scope, so Track A
   reported SIGNAL_NOT_FOUND on 3 of 4 benchmarks and anchors did not
   resolve in the trace (spec 3.1, 5.1). Fix: `rebase_golden` onto the
   detected prefix. New `TestTrackAAgreement`: Track A's real engine and the
   self-check agree on every case, schedule, variant, and mode (16 tests).
2. Medium (#10): runner reaped the simulator before the final group kill, so
   a reused group ID could in principle receive SIGKILL. Fix: observe exit
   with waitid(WNOWAIT), kill the group while the leader is unreaped.
3. Medium (#10): the service compared and anchored unmapped paths; the mock
   fixture's causal report only "matched" a Verilator run because both lacked
   the prefix. Fix: runner records `trace_scope_prefix`; service rebases.
   That fixture is now correctly refused for Verilator runs.
4. Low (#13): UI validator lacked anchor-value, failure-mode,
   additional-mismatch, edge-rule, and edge-time checks. Added, with tests.

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

## C05 — fixture-and-real-record milestone (not full integration)

`ui/`: React + TypeScript (Vite), Vitest + Testing Library. See `ui/README.md`.

- Reads CLI run records, mock fixtures, and bare `CausalReport`s; validates
  schema 1 before rendering (unknown fields, widths, dangling references,
  ranks, schema version).
- Panels: header with real/sample labeling, first-failure strip with
  jump-to-failure, ranked suspects, time-unrolled SVG graph with a keyboard
  node list and pruned toggle, BigInt waveform with separate failure and
  selection cursors, SHA-256-checked source viewer, assertion candidates.
  Tabs below 700 px with shared selection; polite live announcements.
- Designed states: empty, loading, pass, fail localized, fail unlocalized,
  inconclusive/unsupported, infrastructure error (real TIMEOUT record),
  validation error, missing source, revision mismatch, missing waveform
  signal, unknown values, empty and oversized graphs.
- Fixtures in `ui/fixtures/` are generated through `src/contracts.py`; five are
  real CLI run records (Verilator FIFO pass/fail, Icarus X, real timeout).

Evidence:
- `npm --prefix ui test` → 47 passed (acceptance scenarios 1–8 plus model
  unit tests). `npm --prefix ui run build` → clean strict typecheck and build.
- `python3 ui/scripts/visual_check.py` in Chrome 153: 1440/1024/768/375 px
  have no page-level horizontal overflow and no overlapping controls; the
  waveform scrolls inside its panel. Screenshots were inspected by eye.
- Scenario 10, measured on this cloud container (Intel Xeon @ 2.30GHz,
  headless Chrome 153): 100 signals × 1,000 transitions plus 1,000 nodes →
  suspect selection 16.6 ms median (17.5 max), cursor step ~16 ms, initial
  load ~0.4 s. The graph is not drawn above 400 nodes (list only), by design.
  First measurement was 303 ms; fixed with path-based memoized tracks and a
  cursor overlay.
- Fixed during inspection: selecting a suspect scrolled the whole page under
  the sticky header; source now scrolls inside its own box.

Not done: the offline-API state (C06 has no API yet); causal graphs and
waveform windows from real engines (Tracks A/B). Fixture-only UI is not full
integration.

## C04 — partly unblocked

Track A is on `main` and already agrees with the self-check on every real
benchmark trace. Next: a `ComparisonAdapter` over `src.waveform.compare`
(kind `analysis`) as the CLI default, and Track A's waveform options wired
into `cli.py`. The causal half still waits for Track B (#6). The self-check
stays labeled `benchmark_self_check` and is not Track A coverage.

## Open questions

- Dev 1: spec §8.1 forbids epochs without golden records, so handshake
  schedules need ≥1 transfer per epoch (`build_goldens` enforces this).
- A console-script entry point would touch shared `pyproject.toml`; deferred to
  a focused PR. Track A's waveform options get wired into `cli.py` when Track A
  re-lands.
- `benchmarks/common/benchlib.run_simulation` still has its own simple runner
  for the benchmark tests; it could delegate to `src.app.runner.Runner`.

## Continuing without a heavy model

`docs/track-c/playbook.md` is the operating manual: the per-ticket loop, the
gates (now automated in CI, PR #16), rules learned from real defects, and
file-level specs with acceptance tests for C04 part 1, C04 part 2, C06, and
C07. All three Track C branches have current `main` merged (decision 11,
pyslang); the top branch also carries the CI workflow, so #13's checks run
every gate against the whole stack.

## Next single ticket

C04 part 1: the Track A comparison adapter (see above). Independent cold
review of C03+C05 (Prompt 5) in a fresh session. Then C04 part 2 when Track B
merges, and C06 (local API).
