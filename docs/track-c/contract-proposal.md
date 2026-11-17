# Integration contract proposal

Status: `track-c-proposal-0.1`, not the authoritative team schema. Reconcile with existing `spec.md` and `src/contracts.py` before generating production types. Prefer an adapter over silently changing established field names. A change in sampling meaning requires an explicit contract version, even if JSON field names stay identical.

## Decisions for the shared-contract owner

| Decision | Proposed default | Consequence for other tracks |
|---|---|---|
| Observation identity | `(epoch, cycle, phase, time_tick)` | A must expose sampling phase; B must preserve it on temporal edges |
| Time | Nonnegative integer decimal strings plus `tick_unit` | Frontend parses with BigInt; exact absolute timestamps never become floating-point numbers |
| MVP unit | `ps`, with exact normalization | Reject sub-ps traces in MVP rather than rounding them |
| Value | `{width, bits}`; lowercase binary alphabet `01xz` | Unknown and high impedance remain distinct; width must equal string length |
| Absent signal | Missing diagnostic/null, never fake zero or X | A/B distinguish no evidence from a recorded unknown |
| Comparison | Strict known expected value vs actual; unknown is a failure unless an explicit mask says otherwise | No implicit wildcard X comparison |
| Failure kind | value mismatch, unknown value, missing transaction, extra transaction, protocol violation | Missing transaction uses a deadline observation and nullable actual value |
| Source identity | Source ID + relative path + source SHA-256 + 1-based span | C can verify the source before jumping to a line |
| Causal node identity | Signal + observation, with a unique node ID | Repeated signal across cycles is multiple nodes; sequential feedback is time-unrolled |
| Ranking | Ordinal rank + reason/evidence IDs | Do not display uncalibrated scores as probability or confidence |
| Assertions | Text plus validation status, tool/version, result reference | Candidate is distinct from tested or unsupported |

Do not finalize these decisions on behalf of other owners. The accepting agent records resolution in a repository decision note and updates fixtures and contract tests together.

## Temporal semantics

An edge represents a dependence from a specific **source observation** to a specific **target observation**. `kind=sequential` alone does not imply subtracting one period. For a register's post-settle value at cycle k, the input consumed can be at cycle k pre-edge; a cascaded register's input may be the old upstream register value. Keep those events explicit. `kind=combinational` uses the correct settled/input snapshot for the expression being evaluated. The clock and phase convention must come from the manifest, not guessed from arbitrary signal names.

Track A's required capabilities, expressed independently of Python syntax:

- Resolve canonical signal IDs and metadata, including width and signed display preference.
- Query a value at a declared observation; return value or a structured unavailable reason.
- Return explicit clock edges, reset epochs, and observation-to-tick mapping.
- Return a bounded transition window including each signal's initial value at its left boundary.
- Preserve unknowns, duplicate-time policy, and exact tick units. Report missing or unsupported timing semantics.

Track B's output must identify truncation, unsupported constructs, unobserved predicates, and search depth/time limits. Unknown branch conditions must not be pruned as definitely false. A time-unrolled graph may still contain a same-time combinational cycle; report/collapse it explicitly instead of promising every input produces a DAG.

## Draft report envelope

The accompanying schema covers the fixture-level report: `schema_version`, `provenance`, `status`, `failure`, `nodes`, `edges`, `suspects`, `waveform`, `sources`, `assertions`, and `diagnostics`. Production jobs additionally need immutable run IDs, source/input hashes, simulator versions, sampling offset, and bounded artifact URLs. The fixture includes only explicitly labeled mock provenance; it must not manufacture real compiler versions or artifact hashes.

Statuses:

- `pass`: all required comparisons completed, no divergence; `failure=null`, no suspects. A successful HTTP response alone is not a pass.
- `fail`: a design comparison failed; `failure` identifies it. Missing localization is allowed and must be explained.
- `inconclusive`: insufficient trace/signal/semantic coverage. Preserve any observed failure as partial evidence if available; never claim a complete pass.
- `unsupported`: inputs exceed declared analysis capability. Include the feature and location when available.
- `error`: execution, decoding, or infrastructure failed. Do not rank this as an RTL fault.

A failure has a signal, observation, nullable expected/actual values, kind, and explanation. `missing_transaction` uses the declared deadline and no fabricated actual value. A source-unavailable suspect can have `location=null` but must explain why. Assertion validation values are `candidate`, `validated`, `failed`, or `unsupported`; baseline diagnosis works with an empty assertion list.

## Waveform data

Use sparse transitions with an explicit left-boundary initial value for every available signal. Window has `start_tick`, `end_tick`, and `tick_unit`. Events are ascending and strictly unique by tick within the simplified fixture format. Production ingestion must settle duplicate timestamp policy before reduction. Values apply until the next transition. A missing signal is unavailable, not an all-X trace. Store full exact values and convert only relative pixel offsets after subtracting the window origin with BigInt.

The fixture waveform is a settled-value presentation, not sufficient by itself to reconstruct all pre-edge events. Node observations and benchmark sampling records preserve phase independently. Do not infer proof of a phase-specific read from a pretty waveform.

## Proposed application boundary

One application service performs `run(manifest) -> report` with injected A/B adapters. CLI and HTTP call that service. They do not reimplement diffing or causal slicing. A fixture adapter is explicit test/demo configuration, never an automatic fallback when real analysis fails.

Recommended CLI behavior: `waveroot analyze <manifest> --output <directory>` returns 0 for complete pass, 1 for detected design failure, 2 for input/infrastructure error, and 3 for unsupported/inconclusive analysis. Record status in the report as well. A benchmark harness expecting a mutant failure interprets 1 as its expected outcome without relabeling the report as pass.

Recommended local API, if no existing API governs the repo:

| Method/path | Result |
|---|---|
| GET /api/health | Service/schema versions and available simulator capabilities |
| POST /api/runs | Validate a registered benchmark/run configuration; 202 with run_id |
| GET /api/runs/{id} | queued/running/completed/failed/cancelled and stage |
| GET /api/runs/{id}/report | Versioned report after completion; typed not-ready response beforehand |
| GET /api/runs/{id}/sources/{source_id} | Revision-checked source text by registered source ID |
| POST /api/runs/{id}/cancel | Idempotent cancellation request; terminal state after process cleanup |

Use `benchmark_id`, variant and bounded options, not client-supplied executable paths or arbitrary shell text. Reject path escapes and symlink escapes from the selected project/run roots. Serve source as text, never rendered HTML. Bind the local service to loopback, restrict origins to its own interface, and protect mutation endpoints against cross-site requests. Keep the MVP local, without implying that loopback alone authenticates callers. Logs are capped and scrubbed of credential-like environment values; artifacts use registered IDs.

## Required contract examples

Include pass, localized failure, unlocalized failure, unknown-value failure, missing transaction, unsupported feature, cancelled/infrastructure error, and truncated analysis. The supplied package has eight synthetic reports covering localized fail, unknown fail, pass, inconclusive, large time, an unlocalized missing transaction, unsupported input, and infrastructure error. The implementation agent must add job cancellation and truncated-search examples to the accepted schema's test suite.

Joint acceptance examples: JSON roundtrip; value width mismatch rejected; time larger than 2^53 roundtrips; unknown stays unknown; missing stays missing; dangling graph/suspect/source reference rejected; node selection maps to the same observation in every panel; schema mismatch fails visibly rather than silently dropping fields.
