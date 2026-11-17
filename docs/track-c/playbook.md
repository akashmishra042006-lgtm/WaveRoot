# Track C playbook: continuing with any model

This is how to keep Track C correct after the heavy-model sessions end. The
judgment that caught real defects is now encoded in **CI gates** and in the
**rules below**; the remaining tickets are specified to the level of file,
function, and acceptance test. A session that follows this file, keeps CI
green, and does not weaken a test is very hard to steer wrong.

Read first: `CLAUDE.md`, `spec.md`, `docs/handoffs/track-c-current.md`.

## 1. The loop for every ticket

1. **Sync.** `git fetch`; check open PRs; merge `main` into your branch.
   Teammates land contract and engine changes often (§2 of the handoff).
2. **Branch** from the top of the Track C stack (or `main` once it merges).
3. **Write the acceptance tests first**, from the ticket spec in §4 below.
   Run them and watch them fail for the right reason.
4. **Implement** the smallest change that passes them.
5. **Run the local gates** (§2). All of them, every time.
6. **Push and open a PR.** CI must be green. Never merge red; never "fix" a
   red gate by skipping, loosening, or deleting a test.
7. **Update** `docs/handoffs/track-c-current.md`: commands actually run,
   results, what is real versus fixture, the next single ticket.

If a contract (`spec.md`, `src/contracts.py`) is wrong for your ticket, open a
small contract-change PR; do not work around it locally.

## 2. Gates

Run from the repository root. CI (`.github/workflows/ci.yml`) runs the same.

| Gate | Command | What it has caught |
|---|---|---|
| Types | `uv run mypy src/` | strict typing across `src/` |
| Unit | `uv run pytest -m "not integration"` | contracts, oracles, runner/service logic |
| Simulators | `uv run pytest -m integration` (needs verilator, iverilog, gtkwave) | the `TOP.` path bug via Track A agreement; real timeouts and cancellation |
| UI | `npm --prefix ui test && npm --prefix ui run build` | selection rules, exactness, validation |
| Layout | `uv run --with playwright python ui/scripts/visual_check.py /tmp/out` (after build) | page overflow at 375 px; content hidden under the header |

After the layout check, **open the screenshots and look at them**. The
automated check finds overflow and overlaps, not clutter or nonsense.

## 3. Rules learned the hard way

Each of these was a real defect or near miss. Do not relearn them.

**Benchmarks and simulators**
- Verilator wraps the design in a `TOP` scope; Icarus does not. Golden models
  emit canonical `tb.dut.*`; always map them with
  `src/app/tracepaths.rebase_golden` onto the run's detected prefix. Never
  compare canonical paths against a trace.
- `TestTrackAAgreement` (Track A's engine vs the self-check on the same real
  trace) is the strongest test in the repo. If it fails, one of the two
  checkers or the benchmark is wrong; find out which. Never relax it.
- Testbench errors must use `$fatal`. `$display` + `$finish` exits 0, so a
  malformed stimulus looks like a clean, shorter run.
- Icarus 12: no `break` statements; `$fgets` needs a `reg` buffer, not
  `string`.
- Benchmarks must dump past the last compared edge (spec §9, decision 11):
  the final VCD timestep is never compared.
- The self-check reads testbench samples, not the VCD. It is labeled
  `benchmark_self_check` and is never reported as Track A coverage.

**Processes**
- Never reap a child before the final `killpg`: observe exit with
  `waitid(WNOWAIT)`, kill the group while the leader is an unreaped zombie,
  then reap. Otherwise a reused group ID can receive SIGKILL.
- Process-existence tests must use `ps -eww`; truncated command lines make
  "no process remains" pass vacuously.

**Data honesty**
- Never hand-edit UI fixtures; regenerate with
  `uv run python ui/scripts/make_fixtures.py`. Invalid fixtures must also be
  rejected by `src/contracts.py` (`tests/track_c/test_ui_fixtures.py`).
- Fixture data is never a fallback. `FixtureCausal` refuses any failure it
  was not authored for, including the same failure under another hierarchy.
- A process failure (build, timeout, cancel, missing tool) maps to `error` or
  `invalid_input`, never `fail`.

**UI**
- Times are `BigInt` end to end; only window-relative offsets become pixels.
  Bits stay strings; never convert x/z to numbers.
- Never render one SVG element per waveform segment: tracks are a few memoized
  paths, and cursors live in an overlay. (Per-segment rendering measured
  303 ms per selection on the large dataset; paths measure ~17 ms.)
- Scroll inside panels, never the page (the sticky header hides content).
- A `<select>` grows with its longest option: constrain it.
- jsdom differences: no `File.text()` (use the `readText` fallback), no
  `crypto.subtle` (set in `src/test/setup.ts`), `userEvent.setup()` installs
  its own clipboard (read back with `navigator.clipboard.readText()`), and
  Testing Library cleanup is wired explicitly.

## 4. Remaining tickets, specified

### C04 part 1: Track A comparison adapter (unblocked now)

- `src/app/adapters.py`: add `WaveformComparison` with `kind = "analysis"`.
  `compare(golden, run)` writes the (already rebased) golden to
  `run.run_dir / f"golden_{mode}.jsonl"` and returns
  `src.waveform.options.run_comparison(WaveformOptions(trace=run.trace,
  golden=that_path, keep=..., cache_dir=..., fst_timeout_s=...))`.
  Import only from `src.waveform`'s public modules.
- `src/app/cli.py`, `analyze`: add `--comparison {track-a,self-check}`
  (default `track-a`) plus `--keep`, `--cache-dir`, `--fst-timeout` with
  Track A's names, types, and help. Do **not** call `add_waveform_arguments`
  there: it requires `--trace`/`--golden`, which `analyze` produces itself.
- `src/app/cli.py`: add a `compare` subcommand that calls
  `add_waveform_arguments` unchanged and prints `run_comparison`'s result.
- Acceptance tests (integration): (1) FIFO mutant/directed through the CLI
  with the default comparison fails at `TOP.tb.dut.full`, epoch 0 cycle 3,
  settled, 55000 ps, exit 1, `comparison_adapter.kind == "analysis"`;
  (2) FIFO correct passes with exit 0; (3) ALU mutant under Icarus fails with
  `unknown_value`; (4) `--comparison self-check` gives a semantically equal
  result; (5) `compare` on a saved trace + golden reproduces the same anchor.
- Regenerate UI fixtures. The UI already hides the self-check note when the
  adapter kind is `analysis`; add a test that asserts it.

### C04 part 2: Track B causal adapter (after Track B merges)

- `CausalAdapter` over Track B's public entry point (read `spec.md` §6 and
  Track B's README at that time), `kind = "analysis"`, sources = the variant's
  RTL from the manifest.
- Acceptance: FIFO mutant/directed end to end yields a `CausalReport` whose
  `anchor_node_id` matches the comparison failure, whose sources' SHA-256 match
  the RTL files, and whose provenance is `analysis`. Record what rank the
  mutated line gets (`benchmarks/graders/`) **without** adjusting anything to
  improve it; a poor rank is a finding, not a bug to hide.
- The UI then shows a real graph; source text comes from "Add source files"
  until C06 serves it.

### C06: local API

- One process wrapping `AnalysisService`; bind to `127.0.0.1` only; strict
  `Origin` check; manifests restricted to a configured benchmarks root (today
  the CLI accepts any manifest path, acceptable locally, not for an API).
- Jobs: create (returns id), status, cancel (sets the runner's cancel event;
  test that the simulator process is gone), result (the run record).
- Sources served by source id after verifying the SHA-256 against the report;
  never arbitrary paths.
- UI: an API adapter alongside file loading; the offline/API-error state from
  `ui-spec.md` becomes testable here.

### C07: release evidence

- All simulator jobs run in CI (they do); correct-vs-mutant controls and
  masking schedules documented (they are, in manifests); cold review by a
  fresh session using `docs/track-c/agent-prompts.md` Prompt 5; every finding
  fixed and rechecked.

## 5. Choosing a model

- **Routine tickets** that follow §4 (adapters, CLI flags, fixture
  regeneration, API endpoints): any capable coding model, as long as it runs
  the gates and CI is green.
- **Anything touching contracts, the comparison semantics, or a failing
  agreement test**: use your strongest available model, and get a fresh-session
  review.
- **Reviews**: a fresh session that has not seen the work (Prompt 5). Same-
  session self-review is useful but not independent.
