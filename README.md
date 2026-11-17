# WaveRoot

WaveRoot is a planned local-first debugger that compares RTL simulation
waveforms with golden-model outputs, locates the first divergence, and traces
candidate causes back to SystemVerilog source locations.

## Current status

Repository bootstrap and the draft Track A requirements brief. The waveform
engine, shared contracts, causal tracer, CLI, and UI are not implemented yet.

## Development setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/akashmishra042006-lgtm/WaveRoot.git
cd WaveRoot
uv sync --locked
uv run mypy src/
uv run pytest
```

`.python-version` selects Python 3.12; uv can download it if missing. The
committed `uv.lock` pins the resolved dependencies. The development group
includes pytest, mypy, and Hypothesis; Pydantic v2 is available for the shared
contracts. Parser, simulator, platform, and frontend dependencies will be added
by their owners when implemented.

There are currently no test cases, so `uv run pytest` exits with code 5
(no tests collected). An empty suite is not a passing functional check.
The first substantive suite will be `tests/test_contracts.py`.

Run commands from the repository root. This bootstrap uses an unpackaged uv
project and preserves the team's `src.waveform`, `src.causal`, and `src.app`
imports. Track C will add packaging and the console entry point when the CLI
exists. There is no runnable `waveroot` command yet.

## Team ownership

| Owner | Files | Responsibility |
| --- | --- | --- |
| Dev 1 / Track A | `src/waveform/` | Waveform parsing, time queries, golden-model comparison |
| Dev 2 / Track B | `src/causal/` | SystemVerilog dependency graph and causal analysis |
| Dev 3 / Track C | `src/app/`, `benchmarks/`, `ui/` | Simulator runner, CLI/API, benchmark corpus, UI |
| Coordinated by Dev 1 | `spec.md`, `src/contracts.py`, shared fixtures | Cross-track interfaces and integration |

See [CLAUDE.md](CLAUDE.md) for boundaries and verification commands. The
existing `.claude/settings.json` contains the team's shared command allowlist;
individual overrides belong in the ignored `.claude/settings.local.json`.
These settings do not select or pin subagent models.

## Next handoff

1. Review [Track A's brief](docs/brief_track_a.md); Devs 2 and 3 add their briefs
   to `docs/brief_track_b.md` and `docs/brief_track_c.md`.
2. Reconcile timing, reset, golden-trace, and failure-result decisions together.
3. Create `spec.md`, `src/contracts.py`, `tests/fixtures/mock_fifo_failure.json`,
   a mock provider, and meaningful contract tests in a shared foundation PR.
4. After that PR merges, branch from the same `main` for each track.

Use pull requests for changes and keep each branch within its track. The latest
project plan targets four benchmarks; exact fixtures and acceptance criteria
must be recorded in the shared spec. Verilator and GTKWave's `fst2vcd` are later
external dependencies, not installed by `uv sync`. The X-propagation case needs
a four-state simulation fixture or equivalent explicit test data; see the brief.

Keep generated simulations, traces, and reports in `artifacts/`; keep small,
deterministic test inputs in `tests/fixtures/`.
