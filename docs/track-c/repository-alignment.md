# Alignment with the current WaveRoot repository

Inspected `main` at commit `0adec0b934825cce94244f37f3ea354d5ebf6611` after repository access was enabled on November 16, 2026.

## Observed state

- Python package skeleton: `src/app/`, `src/causal/`, `src/waveform/`, each with an empty `__init__.py`.
- Placeholder directories: `benchmarks/`, `tests/fixtures/`, and `ui/`.
- Existing planning document: `docs/brief_track_b.md`.
- Existing configuration: `.claude/settings.json`.
- No `AGENTS.md`, `CLAUDE.md`, `spec.md`, `src/contracts.py`, `pyproject.toml`, frontend package manifest, or CI workflow in the inspected tree.
- No implemented benchmark, simulator harness, backend, or frontend in that snapshot.

This confirms that the current deliverable is the Track C pre-contract brief and agent setup. Building the production interface against invented shared types now would preempt the team's next architecture phase. The supplied schema/fixtures remain explicitly provisional until the three track briefs are reconciled.

## Compatibility with Track B

Track B already distinguishes candidate causes from proof, elaborated AST from source syntax, missing samples from unknown values, and pre-edge inputs from post-NBA register updates. Track C preserves those distinctions.

Its `q[k+1] = F(q[k], u[k])` indexing uses sampled pre-edge state. Track C's benchmark cycle k names the physical edge and exposes both `pre_edge` and `post_settle`. These are compatible conventions **only with an explicit adapter**: Track C's post-settle register at edge k corresponds to the new state produced from the pre-edge operands at that edge. Do not identify the two indices by name alone or subtract a period from a post-settle timestamp.

Track B explicitly handles conservative unknown-control paths and ternary merging. The X benchmark's equal/different operands exercise that policy. Any assertion template from Track B must still compile on the chosen simulator and use its preponed-sampling semantics; this is distinct from the post-settle waveform view.

Agree the initial supported construct subset before starting: FIFO status depends on pointer arithmetic/equality; MAC requires signed widening/clamping and registered latency; elastic buffer requires conditional holds; X case requires ternary four-state merging. FIFO storage also introduces indexed memory: either support the relevant memory dependencies or explicitly limit the first localization target to the pointer/full status cone. Do not pretend unsupported memory analysis is complete.

## Proposed repository placement

- `docs/brief_track_c.md`: primary team-facing brief.
- `docs/track-c/*.md`: detailed benchmark, UI, handoff, acceptance, and alignment notes.
- `docs/track-c/proposal/`: isolated draft schema, sample reports, directed vectors, and their dependency-free checks.
- Future implementation remains in the existing `benchmarks/`, `src/app/`, `tests/`, and `ui/` paths after C00.

Existing Track B work and `.claude/settings.json` are preserved. The latter already contains broad command patterns; this handoff does not widen them. No shared production contract is created by this planning change.

## Immediate team action

Person 1 combines the Track A brief (when ready), the existing Track B brief, and this Track C brief to agree the shared contract. Person 3 can then run the benchmark handoff in `agent-prompts.md`. Bring a real FIFO trace into integration before expanding the UI.
