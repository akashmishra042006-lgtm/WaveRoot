# WaveRoot 0.1.0 — release notes

Released November 20, 2026. Drafted by Track C; reviewed and finalized by
Dev 1. Every number below comes from a command listed under "Reproducing the
evidence"; nothing was adjusted after it was observed.

## What it is

A local-first debugger for RTL simulations. Given a benchmark manifest, it
runs the design under Verilator or Icarus Verilog, compares the waveform with a
golden model's expected values, reports the first divergence exactly (signal,
epoch, cycle, sampling phase, picosecond time; X and Z kept as X and Z), and
ranks candidate SystemVerilog source lines that could have caused it.

- **Track A** (`src/waveform/`): VCD/FST parsing, time queries, comparison.
- **Track B** (`src/causal/`): dependency graph, backward slice, ranked
  suspects, candidate assertion.
- **Track C** (`src/app/`, `benchmarks/`, `ui/`): simulator runner, CLI, local
  API, React UI, benchmark corpus, release evidence.

```sh
uv sync --locked
uv run python -m src.app.cli analyze benchmarks/case_01_fifo_wrap/manifest.json \
    --variant mutant --schedule directed
uv run python -m src.app.cli serve --dev     # local API; prints a UI link with its access token
npm --prefix ui ci && npm --prefix ui run dev
```

After `uv sync`, `uv run waveroot ...` is the same command as
`uv run python -m src.app.cli ...` (console script added in #37).

## Results

### The benchmark suite is a smoke suite

Four designs, one injected mutation each:

| Case | Simulator | Mutation |
| --- | --- | --- |
| FIFO wrap | Verilator | write pointer keeps its wrap bit instead of toggling it |
| AXI skid buffer | Verilator | a stall clears the stored transaction's valid |
| MAC overflow | Verilator | overflow is checked on the narrowed sum, so it is lost before clamping |
| ALU X-propagation | Icarus (four-state) | the selector register is never reset and starts X |

These four are not an accuracy estimate. They show that the pipeline
works end to end on real simulators. They don't show how often it finds a
defect.

### Detection (16 authored schedules per engine)

Both comparison engines (Track A's waveform engine, and Track C's independent
benchmark self-check) match all 32 documented verdicts:

- mutant detected on the 12 schedules documented to expose it: 12/12;
- mutant passes the 4 documented masking controls (no false detection): 4/4;
- the correct design passes all 16 schedules: 16/16.

### Localization (Track B, on Track A's first failure)

Rank of the mutated line among the ranked suspects:

| Set | Detected | Ranked | Top-1 | Top-2 | Top-5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Authored schedules | 12/12 | 12/12 | 6/12 | 12/12 | 12/12 |
| Held-out schedules | 17/20 | 17/17 | 6/17 | 12/17 | 15/17 |
| Same schedules after B-12 (post-change; ALU no longer held out) | 17/20 | 17/17 | 6/17 | 14/17 | 15/17 |
| Profiles 06-08 (#39, written by the Track B Claude session) | 12/12 | 12/12 | 5/12 | 11/12 | 12/12 |

- **Authored:** the mutated line ranks 1st for FIFO and AXI skid and 2nd for MAC
  and ALU. Suspects repeat across a case's schedules, so these 12 rows are about
  4 independent observations.
- **Held-out:** 20 seeded schedules, 5 per case. They were generated once from
  fixed seeds without consulting a mutant, run once, and reported as found.
  - The correct design passed all 40 runs (both engines).
  - Track A and the self-check agree on status and first failure 20/20.
  - Errored runs: 0/20.
  - The 3 undetected mutants are FIFO held_out_03 and MAC held_out_01 and 02.
    On those schedules the mutant's compared outputs matched the golden model.
    Why the mutation stayed hidden has not been analyzed.
- **After B-12 (post-change):** Track B's ticket B-12 (#34) was prompted by
  the ALU held-out rows, so those rows are no longer held out. Rerun on the
  same seeds after it merged, exactly two rows changed: ALU held_out_01 and
  held_out_03 moved from rank 3 to rank 2. Detection, agreement and every other
  rank were identical. The held-out row above stays the held-out result.
- **Profiles 06-08:** 12 more schedules, 3 per case, appended by the Track B
  Claude session (#39). That author knew the four mutations, had studied the
  rankings of held_out_01-05 and wrote B-12, so these rows are held out from
  tuning only. No mutant was run on them before Track C's single run on
  November 19, 2026 (`main` at c03a73a, with B-12), reported here as found:
  - the correct design passed all 24 runs (both engines);
  - the mutant was detected on all 12, with no errored runs;
  - Track A and the self-check agree on status and first failure 12/12;
  - mutated-line ranks: FIFO 1, 1, 5; AXI skid 1, 1, 1; MAC 2, 2, 2; ALU 2, 2, 2.

  The same run reproduced the authored matrix and rows 01-05 of the post-B-12
  rerun exactly.
- **The only claim we make:** the mutated line is in the top 2 for all four
  authored cases, and for 12 of the 17 detected held-out mutants (14 of 17
  after B-12, where the 2 changed rows are no longer held out), and for 11
  of the 12 on profiles 06-08, whose author knew the mutations.

### Why localization drops on held-out schedules

Track B checked these explanations on FIFO held_out_01 and 05, ALU held_out_01
and MAC held_out_03:

- **FIFO (ranks 10, 5, 10 on three schedules): a known limit.** The defect is
  a missing toggle of the pointer's wrap bit, and later increments carry the
  stale bit forward. The faulty line never changes the bit, and neither do
  the later ones. So no sound rule singles it out once more recent writes
  come between it and the failure. Track B records this as a limit and
  doesn't tune ranking for it.
- **ALU (rank 3 on two schedules):** Track B agreed this ordering was wrong. A
  hold that only keeps an X should not outrank the missing reset assignment.
  Fixed by Track B ticket B-12 (#34), tested on a made-up design; after it both
  rows rank 2nd. Because the fix was prompted by these schedules, the ALU
  held-out schedules no longer count as held out, and the post-B-12 numbers are
  reported beside the held-out ones, never replacing them.
- **MAC (always rank 2): deliberate.** The clamp is one step closer to the
  failure than the mutated comparison.

## Limits

- **Small suite.** 4 designs and 4 mutations.
- **Held out from tuning, not from authorship.** The held-out schedules were
  written by the same session that built the tools, and they reuse the same
  designs. Independently authored profiles (`PROFILES` in
  `benchmarks/held_out/generate.py`) or new designs are needed before any
  accuracy claim. New profiles are run once and reported alongside these
  numbers, and nothing is ever re-seeded. Profiles 06-08 per case (#39) do not
  meet this bar either: they were written by the Track B Claude session, which
  knew the four mutations and had studied the per-row rankings of
  held_out_01-05.
- **No human review.** The independent reviews were by models only: a
  fresh-session cold review of Track C, and a separate model's audit of
  Track B. Neither is a substitute for a human review. Dev 1 decided to release
  0.1.0 without one; a human review is still needed before any stronger claim.
- **Handshake goldens need a transfer in every reset epoch.** Spec §8.1 forbids
  an epoch with no golden records, so a handshake-mode golden file cannot
  describe a reset epoch in which nothing transfers (for example a mid-run
  reset with nothing in flight); `build_goldens` refuses such schedules. Dev 1
  confirmed this rule for schema 1 on November 19, 2026. Allowing empty epochs
  changes the meaning of the golden format, so it waits for a new schema
  version (spec §2).
- **Candidate assertions can assume latency.** When the cause is more than
  one clock edge before the failure, the generated assertion assumes a fixed
  latency and says so (`SVA_LATENCY_ASSUMED`). It doesn't encode conditions in
  the cycles between. This is shown on every MAC run.
- **Local API.**
  - It binds to loopback only.
  - It has Host and Origin guards and a per-start access token.
  - The token stops other local users and stray clients. It doesn't stop your
    own processes, which can read the terminal or the token file.
  - Job records are kept in memory only.
- **Platforms.**
  - Linux (Ubuntu 24.04, Verilator 5.020, Icarus 12.0) is verified, and CI
    runs everything on it.
  - macOS (one machine: arm64, macOS 26, Verilator 5.054, Icarus 13.0):
    `mypy src/` clean, unit tests passed, and on November 20, 2026
    `uv run pytest -m integration` gave 98 passed, 0 failed, including Track A's
    FST round trip, using the `poll()` runner path. The 11 skipped tests cover
    the Linux-only `os.waitid` path. Homebrew no longer packages GTKWave, so
    `vcd2fst`/`fst2vcd` were built from GTKWave 3.3.116's source; on non-Linux
    builds they accept short options only, which Track A handles.
  - Windows natively is not supported for 0.1.0 (Dev 1, November 19, 2026);
    use WSL2. The simulator runner uses POSIX-only
    process and signal calls (`mypy src/` reports them on Windows), and
    Verilator and Icarus were run only under WSL. Under WSL2 (Ubuntu 26.04,
    Verilator 5.032, Icarus 12.0) on November 19, 2026: on `main` at 77c6f22
    plus B-12, `mypy src/` was clean and 700 unit tests and 183 causal,
    adapter and end-to-end tests passed; on `main` at 432d37a, a UI served
    from Windows ran four real analyses against the WSL API in Chrome. One
    machine, one run each.
- **Not evidence.** Fixture causal data (`--fixture-causal`) is hand-authored
  and labeled synthetic in every output. It is never reported as analysis.

## Reproducing the evidence

```sh
uv run mypy src/
uv run pytest -m "not integration"
uv run pytest -m integration                               # needs Verilator, Icarus, GTKWave (~12 min)
uv run python -m benchmarks.release_evidence               # authored matrix
uv run python -m benchmarks.release_evidence --held-out    # plus held-out schedules (~15 min)
npm --prefix ui ci && npm --prefix ui test && npm --prefix ui run build
```

The held-out run is deterministic. A second run on November 19, 2026, with the
same seeds and tools, reproduced every number above. That makes it a
reproduction, not a second observation.

## Release decisions (Dev 1)

- The spec §8.1 rule stays for schema 1 (see Limits). November 19, 2026.
- Windows is unsupported natively; use WSL2. November 19, 2026.
- Profiles 06-08 (#39) were run once by Track C (#42) and are reported as
  their own row. November 20, 2026.
- 0.1.0 is released without a human review of Tracks B and C (see Limits).
  November 20, 2026.
