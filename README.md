# WaveRoot

WaveRoot is a local-first debugger that compares RTL simulation waveforms with
golden-model outputs, locates the first divergence, and traces candidate causes
back to SystemVerilog source locations.

## Current status

All three tracks are implemented on `main`:

- **Track A** (`src/waveform/`): VCD/FST parsing, time queries, and golden-model
  comparison.
- **Track B** (`src/causal/`): SystemVerilog dependency graph, backward slice,
  ranked suspects, and candidate assertions.
- **Track C** (`src/app/`, `benchmarks/`, `ui/`): simulator runner, CLI, local
  API, React UI, the four-case benchmark corpus, and release evidence.

The cross-track contract is [spec.md](spec.md) and `src/contracts.py`. The
draft 0.1.0 release notes, with results and known limits, are in
[docs/release-notes.md](docs/release-notes.md). The four benchmarks are a smoke
suite, not an accuracy estimate.

Supported platforms: Linux (verified in CI) and macOS. Windows is supported only
through WSL2; the simulator runner uses POSIX-only process calls.

## Development setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/akashmishra042006-lgtm/WaveRoot.git
cd WaveRoot
uv sync --locked
uv run mypy src/
uv run pytest -m "not integration"
```

`.python-version` selects Python 3.12; uv can download it if missing. The
committed `uv.lock` pins the resolved dependencies.

The simulator gate needs Verilator, Icarus Verilog, and GTKWave (for
`vcd2fst`/`fst2vcd`) on `PATH`; they are not installed by `uv sync`:

```sh
uv run pytest -m integration      # real simulators, about 12-15 minutes
npm --prefix ui ci && npm --prefix ui test && npm --prefix ui run build
```

Run commands from the repository root. The project keeps the team's
`src.waveform`, `src.causal`, and `src.app` imports; `uv sync` installs it in
editable mode with a `waveroot` command (`uv run waveroot --help`), equivalent
to `uv run python -m src.app.cli`. It is meant for a checkout: the benchmark
golden models are imported from the repository.

## Usage

```sh
uv run waveroot analyze benchmarks/case_01_fifo_wrap/manifest.json \
    --variant mutant --schedule directed
uv run waveroot serve --dev        # local API; prints a UI link with its access token
npm --prefix ui ci && npm --prefix ui run dev
```

To reproduce the release numbers, see "Reproducing the evidence" in
[docs/release-notes.md](docs/release-notes.md).

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

## Changing shared contracts

A change to `spec.md` or `src/contracts.py` goes in its own pull request,
reviewed by all three tracks, and updates the spec, code, fixtures, and tests
together (spec §2). The cross-track decisions in spec §15.1 are final for
schema 1; a change in meaning needs a new schema version.

Keep each branch within its track. Keep generated simulations, traces, and
reports in `artifacts/`; keep small, deterministic test inputs in
`tests/fixtures/`.
