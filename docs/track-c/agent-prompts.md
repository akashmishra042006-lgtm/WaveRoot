# Copyable agent handoffs

These prompts use the roles from the shared plan. Use model names/settings actually present in your account. Each prompt assumes the files have been placed in the repository after conflict review. Do not execute all stages simultaneously: satisfy their prerequisites first.

## Prompt 0 — reconcile with the actual repository

Suggested recipient: shared-contract/architecture agent, with Person 3 supplying this package.

```text
We are building WaveRoot. I am Person 3, owning Track C: reproducible benchmarks,
simulation runner, CLI/local API, and the debugging interface.

First inspect the actual repository, AGENTS.md/CLAUDE.md, git status, spec.md,
shared contracts, existing tests, and dependency/toolchain configuration. Read
docs/brief_track_c.md and docs/track-c/{benchmark-spec,contract-proposal,sources}.md.
Read repository-alignment.md for the inspected baseline. The proposal schema is
not authoritative; current agreed contracts take precedence over draft examples.

Produce a short gap list and map proposed files to real paths. Resolve observation
phase, clock/reset epochs, exact time serialization, four-state values, missing
signals, source identity, report statuses, and graph node identity with the shared
contract owner. Preserve the distinction between pre-edge accepted transactions
and post-settle registered outputs. Do not blindly equate a sequential edge with
subtracting one clock period.

If changes to shared contracts are needed, prepare a focused contract proposal
and compatibility adapter; do not silently overwrite src/contracts.py. Mark any
cross-owner decisions pending while completing independent Track C documentation.
After acceptance, validate a representative fixture against the real Python
contract and generate/validate matching frontend types. Record the schema version.

Stop at the C00 handoff: actual paths, accepted decisions, unresolved dependencies,
commands run with results, and the next single benchmark ticket. Do not implement
Tracks A/B or the full UI. Do not spawn additional agents unless explicitly authorized.
```

## Prompt 1 — build the benchmark foundation

Suggested recipient: Fable benchmark role; prerequisite C00.

```text
Own Track C benchmarks only. Read the accepted spec/contracts, docs/brief_track_c.md,
docs/track-c/benchmark-spec.md, and the latest Track C handoff. Confirm the agreed
clock sampling convention and installed simulators before writing the harness.

Implement C01 first: the depth-4 FIFO correct/mutant pair, identical harness and
directed stimuli, and an independent Python queue oracle. The mutant omits the
write-pointer wrap-bit toggle. Correct full becomes 1 after cycle 3; mutant full
is 0. Verify this with a real VCD and a checker, not comments or an assumed trace.
Exercise the full simultaneous pop/push policy explicitly. Keep ground-truth
mutation spans and patches outside the analyzer's input bundle.

After C01 passes, implement C02: one-entry elastic buffer that loses valid under
stall; pipelined signed MAC that narrows before saturation; and the mux whose
select register is unreset. Follow the specified exact directed vectors and add
independent alternate schedules. Use a genuine four-state Icarus run for the X
case and verify X bits are present; a Verilator randomized-reset run is not a substitute.

For each benchmark provide correct and mutant RTL, manifest, bounded deterministic
driver, independent golden model, expected and observed JSONL, source/input hashes,
tool versions, and tests. Separate process success from intended mutant failure.
Count missing/extra outputs and enforce a drain deadline. Never generate expected
values from observed mutant outputs. Preserve valid held data across stalls.

Keep changes within the agreed benchmark and Track C test directories. Report any
contract blocker to its owner with a minimal example. Do not edit Tracks A/B.
Do not fabricate passed simulator checks when tools are unavailable. Finish with
the actual commands/results, first-failure observations, remaining skips, and a
durable docs/handoffs/track-c-current.md for the platform agent.
```

## Prompt 2 — runner, CLI, and first real integration

Suggested recipient: Opus platform role; prerequisite C01 plus accepted contracts.

```text
Implement Track C tickets C03 and C04 in order using the existing repository stack.
Read the accepted contracts, benchmark manifests, docs/track-c/{execution-plan,
contract-proposal,review-gates}.md, and the latest Track C handoff.

Build one application service with injectable waveform/diff and causal-analysis
adapters. The CLI invokes it; later the local API invokes the same service. Do not
reimplement the algorithms owned by Tracks A/B. Make fixture mode explicit; never
fall back to sample data when a real run fails.

Execute simulator tools with argument arrays and per-run working directories.
Validate file roots/options, bound run time and artifact sizes, cap logs, capture
tool versions/hashes/exit codes, and implement cancellation that cleans up child
processes. Report compiler errors, timeouts, unsupported inputs, observed design
failure, and pass distinctly. Preserve output traces needed for diagnosis.

Test path escapes, cancellation, missing tools, build failure, run failure, timeout,
malformed/missing trace, schema mismatch, and a successful run. Then connect one
actual FIFO trace to the real A/B modules and produce a revision-matched report.
If A/B are unavailable, finish adapter tests with clearly labeled mocks and report
the real integration blocked; do not claim C04 is complete.

Use the repo's actual verification commands. Work within Track C paths; explain
any needed shared change before editing it. Record commands/results, real versus
mock coverage, and next ticket in the handoff. Do not push or merge unless authorized.
```

## Prompt 3 — frontend implementation

Suggested recipient: Astra frontend role; prerequisite accepted schema and fixtures.

```text
Implement Track C's debugging interface using the repository's existing frontend
stack. Read its instructions, the accepted report contract, representative fixtures,
docs/track-c/ui-spec.md, and the current handoff. If contracts are still provisional,
first create a clearly versioned adapter and keep the result labeled fixture-only.

Build the first-failure summary, ranked-suspect list, time-unrolled dependency graph,
waveform viewer, and revision-checked source panel. Share one selection state so
choosing a suspect selects its node, signal, exact observation, and source span.
The first-failure anchor must remain immutable when the user moves the cursor.
Implement jump-to-failure, radix controls, and a pruned-branch toggle.

Keep absolute times and wide values exact; use BigInt/strings and relative pixel
offsets. Display X/Z and missing data distinctly. Provide pass, unlocalized fail,
inconclusive, unsupported, infrastructure error, missing source, and schema-error
states. Use ordinal ranking and evidence, not invented confidence percentages.
Assertions are candidates until validated. Persistent sample-data labeling is required.

Add behavior tests for every acceptance scenario in ui-spec.md. Verify keyboard
operation and inspect desktop/mobile layouts visually. Run the real typecheck,
build, and browser tests. Report measured performance rather than claiming targets
were met without measurement. Keep edits in UI and its adapters/tests. Do not add
causal-analysis logic or silently change shared contracts.

Finish with test results, screenshots where supported, remaining limits, and whether
the interface has consumed a real backend report. Fixture-only UI is not full integration.
```

## Prompt 4 — local service and interface integration

Suggested recipient: Opus integration role; prerequisites C04 and C05.

```text
Complete C06 using the accepted application service and existing API conventions.
Implement run lifecycle, bounded artifact retrieval, cancellation, and revision-
checked source access. Bind locally; enforce allowed origins and cross-site request
protection for state-changing endpoints; reject client-controlled executable paths
and path traversal. Do not expose arbitrary local files.

Connect the UI through a single typed report adapter. Verify one real FIFO mutant
report and its correct control end-to-end, including node/waveform/source selection.
Test offline/error/cancel/schema-mismatch states. Do not substitute a mock if real
analysis fails. Preserve non-public design artifacts locally by default.

Run relevant backend/frontend checks and actual integration tests. Report any A/B
coverage gaps precisely. Keep a durable handoff with versions, hashes, executed
commands, and pending issues. Do not publish the service or merge without authorization.
```

## Prompt 5 — independent cold review

Suggested recipient: Astra reviewer; prerequisite actual changes and test evidence.

```text
Review the actual Track C change against its base commit, accepted spec/contracts,
docs/track-c/review-gates.md, and available test results. Inspect surrounding code,
not only the diff. Do not assume four green examples establish general correctness.

Prioritize concrete defects: pre/post-edge confusion; reference model copying the
mutant; hidden missing transactions; false X coverage with two-state simulation;
source-answer leakage; mismatched source revisions; schema/BigInt loss; stale UI
selection; misleading statuses; unsafe subprocess/path behavior; cancellation leaks;
or unsupported simulator assertions being presented as validated.

For every finding give severity, file/line, a specific trigger, consequence, and
a minimal reproducer or test. Separate confirmed findings from untested risks.
If no actionable defect is found, say exactly which scope was reviewed and what
was not exercised; do not declare the software universally safe to merge.

Summarize actual tests examined/run and remaining simulator or integration gaps.
Do not modify unrelated tracks or merge the PR. If tasked to fix a finding, make
the smallest owned change and rerun the affected verification before closing it.
```
