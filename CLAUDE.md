# WaveRoot development rules

## Ownership
- Track A: `src/waveform/`, `tests/test_waveform.py`, waveform fixtures,
  `docs/brief_track_a.md`, and `docs/plan_track_a.md`.
- Track B: `src/causal/`, `tests/test_causal.py`, causal fixtures, and Track B docs.
- Track C: `src/app/`, `benchmarks/`, `ui/`, platform tests, and Track C docs.
- Dev 1 coordinates shared contracts and final end-to-end integration.
- Coordinate shared files (`pyproject.toml`, `uv.lock`, shared fixtures, and
  configuration) through a focused PR. Track A defines waveform options; Track C
  wires them into `src/app/cli.py`.

## Shared contracts
- `spec.md` and `src/contracts.py` are the cross-track source of truth. The
  briefs in `docs/` are background; where they differ, the spec wins.
- Build against `MockWaveformProvider` and `tests/fixtures/mock_fifo_failure.json`
  until the real engines exist. Fixture causal data is hand-authored, not analysis.
- Propose interface changes explicitly and coordinate them with the team before
  changing dependent modules. Keep the spec, fixtures, and tests consistent.
- Do not silently convert X/Z to integers, round timestamps, assume a fixed
  clock period, or infer simulator scheduling phases from VCD record order.
- Never report placeholder/import checks as functional or end-to-end coverage.

## Verification (run from repository root)
- Setup: `uv sync --locked`
- Typecheck: `uv run mypy src/`
- Unit suite: `uv run pytest`
- Contracts, once present: `uv run pytest tests/test_contracts.py -v`
- Track A, once present: `uv run pytest tests/test_waveform.py -v`
- Track B, once present: `uv run pytest tests/test_causal.py -v`
- End-to-end, once present: `uv run pytest tests/test_e2e_benchmarks.py -v`
- Frontend, once scaffolded: `npm --prefix ui run build`

The contract tests cover shared models and the mock provider only. Do not add
trivial tests to conceal absent engine coverage.
