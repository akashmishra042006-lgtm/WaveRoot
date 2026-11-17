# Review and release gates

This is a checklist for reviewing actual implementation evidence. No item is marked passed solely because it appears in the plan.

## Benchmark correctness

- Correct RTL passes each independent oracle; intended mutant fails at the specified observation.
- Reset release and stimulus changes cannot race the rising-edge sampler.
- Golden state derives from inputs and the behavioral contract; it does not consume mutant outputs or hidden grading data.
- FIFO wrap and full pop/push behavior agree across oracle and RTL.
- Elastic-buffer source holds a stalled transaction; stalled output valid and data remain checked even when no handshake occurs.
- MAC signed widening precedes addition, output latency is one registered stage, and clamping is tested in both directions.
- Four-state case actually records X under its required simulator; equal ternary branches can resolve known bits.
- Missing last output and extra output cannot be lost through a `zip(expected, actual)` comparison.
- Same-event multiple mismatches use a documented stable tie order.
- The analyzed HDL has no planted answer comments; grader spans are revision-matched and outside analysis inputs.

## Runner and integration

- Compile errors, process errors, timeouts, unsupported features, design failures, and pass are distinct states.
- Tool arguments are arrays; working directories are isolated. No arbitrary client shell text is accepted.
- Artifact, source, and include paths stay inside allowed roots, including symlink resolution.
- Child processes terminate on cancellation/time limit; concurrent jobs cannot overwrite one another.
- Logs and output sizes are bounded, partial traces are marked partial, and stale cached results cannot survive source/seed/version changes.
- A/B modules receive real traces in integration tests; mocks are explicit and never catch-all fallback behavior.
- Required CI simulator lanes fail on missing dependencies. Skipped X tests do not count as complete validation.
- Source and inputs can be reconstructed from recorded hashes/manifests; wall-clock metadata is excluded from deterministic comparisons.

## Report and UI

- Width, unknown bits, missing samples, and timestamp precision survive serialization.
- Failure, selected node, selected waveform cursor, and source location have distinct and correct semantics.
- Source hashes are checked before a line is presented as the analyzed code.
- Schema mismatch or dangling graph reference is rejected before normal rendering.
- Unlocalized and incomplete analysis is presented honestly. No ordinal score is formatted as a probability.
- Candidate assertion text is not called validated until compile and runtime checks exist for the selected simulator.
- Pass clears previous failure state. Empty/missing/large datasets do not crash navigation.
- Keyboard and small-screen operation work. Unknown and pruned states have text/shape cues, not color alone.

## Evidence to attach to the implementation review

Base and head commits; accepted contract version; simulator/compiler versions; test commands/results; fixture versus real-run coverage; sample real report; source hashes; relevant desktop/mobile screenshots; known skipped checks; per-case detection and localization results. Include denominators and unsupported/error cases in evaluation reporting.

An independent review reports concrete findings with triggers and consequences. “No actionable findings in this scope” is acceptable when true; “safe to merge” is not a substitute for test and integration evidence.
