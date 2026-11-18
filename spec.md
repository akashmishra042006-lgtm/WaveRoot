# WaveRoot shared specification

Schema version 1. Status: decided by the contracts coordinator (Dev 1). Changes
from here go through the process in section 2. `src/contracts.py` implements the types named here; where this document
and the code disagree, that is a bug in one of them and must be fixed in both.

This specification reconciles `docs/brief_track_a.md`, `docs/brief_track_b.md`,
and `docs/brief_track_c.md` (with `docs/track-c/contract-proposal.md`). Section
15 records every place the briefs disagreed and what was chosen.

## 1. Scope

WaveRoot compares an RTL simulation waveform with expected values from a Python
golden model, reports the first divergence, and traces candidate causes back to
SystemVerilog source. A suspect is evidence for investigation, not proof.

### 1.1 MVP assumptions

- Local execution. Raw HDL and traces stay on the machine.
- One explicitly named clock. Sampling happens on its rising edges.
- One explicitly named reset with configured polarity, sampled synchronously.
- Synthesizable synchronous RTL: `assign`, `always_comb`, `always_ff`, `if`,
  `case`/`casez`/`casex`, `?:`, packed vectors, part-selects, module instances.
- Digital four-state signals. VCD input. Output transactions are in order.
- Input changes and reset changes never coincide with a sampling clock edge.
- Explicit hierarchical signal paths; nothing is discovered by name guessing.

### 1.2 Exclusions

Multiple clock domains, clock or reset autodetection, asynchronous reset
analysis, latches, real-valued signals, gate-level or delayed timing,
sub-picosecond precision, out-of-order protocols, protocol-violation checks,
don't-care masks in expected values, sample offsets from the clock edge,
escaped identifiers in signal paths, FST input (a later Track A adapter),
automatic RTL patches, and calibrated confidence scores. Assertion synthesis is
optional and never gates a diagnosis.

Unsupported input is reported as `unsupported` or `invalid_input`. It is never
approximated silently.

## 2. Data flow and ownership

```
benchmark + golden model (C) ──▶ simulator run (C) ──▶ VCD + golden JSONL
VCD ──▶ WaveformProvider (A)
golden JSONL + WaveformProvider ──▶ comparison (A) ──▶ ComparisonResult
FailureAnchor + WaveformProvider + SystemVerilog source ──▶ slicing (B) ──▶ CausalReport
ComparisonResult + CausalReport ──▶ CLI, service, UI (C)
```

| Owner | Paths | Produces | Consumes |
| --- | --- | --- | --- |
| Track A | `src/waveform/` | A `WaveformProvider` implementation; `ComparisonResult` | VCD, `GoldenTrace` |
| Track B | `src/causal/` | `CausalReport` | `FailureAnchor`, `WaveformProvider`, RTL source |
| Track C | `src/app/`, `benchmarks/`, `ui/` | VCD, golden JSONL, CLI/service/UI | `ComparisonResult`, `CausalReport` |
| Dev 1 | `spec.md`, `src/contracts.py`, `tests/test_contracts.py`, `tests/fixtures/mock_fifo_*` | Shared contracts | All three briefs |

Rules:

- Tracks exchange only the types in `src/contracts.py`. No track imports another
  track's package; Track C wires A and B together in `src/app/`.
- `src/contracts.py` imports no track package.
- Track A defines waveform options; Track C wires them into the CLI.
- A change to `spec.md` or `src/contracts.py` is proposed in its own pull
  request, reviewed by all three tracks, and updates the spec, code, fixtures,
  and tests together. A change in meaning requires a new schema version even if
  field names stay the same.

## 3. Signals and values

### 3.1 Canonical signal paths

A signal path is the dot-separated hierarchy from the trace's top scope, for
example `tb.dut.wptr`. Each component is a simple identifier
(`[A-Za-z_][A-Za-z0-9_$]*`) optionally followed by `[index]` suffixes for
generate or array instances, for example `tb.gen[2].u.q`.

- Paths are case-sensitive and must resolve to exactly one signal.
- Identifier-code aliases in a VCD resolve to every path that declares them.
- Schema 1 cannot represent escaped identifiers. A provider must reject a name
  it cannot represent; it must not flatten or rewrite it.
- Track C supplies the mapping between RTL hierarchy and trace hierarchy in its
  manifest. Track B maps elaborated names to these paths and does not assume a
  simulator-generated prefix.

### 3.2 Declarations, widths, and bit order

`SignalDecl` carries `path`, `width` (at least 1), `bit_range`, and `signed`.

- `bit_range` is the declared packed range `[left:right]` using RTL indices.
  `[7:0]` and `[0:7]` are different declarations and are never reordered. It is
  null only for an unranged one-bit signal.
- `signed` is metadata. No value is interpreted as signed without it.

### 3.3 Four-state values

`LogicValue` is `{ "width": N, "bits": "..." }`.

- `bits` has exactly `width` characters from `0`, `1`, `x`, `z`.
- `bits[0]` is the declared left bit. For `[7:0]`, `bits[0]` is bit 7.
- The canonical form is lowercase. Uppercase input is normalized on load.
- `x` and `z` are distinct and are preserved through every operation.
- There is no implicit integer conversion. `to_unsigned()` raises if any bit is
  `x` or `z`.
- A provider zero-extends shortened known VCD vectors and extends leading `x`/`z`
  per VCD rules before constructing a `LogicValue`. Over-width values are
  rejected.

## 4. Time

- Every timestamp is a non-negative integer number of picoseconds.
- Arithmetic is exact. No floating point and no rounding anywhere.
- `to_picoseconds(count, magnitude, unit)` converts trace ticks. `magnitude` is
  1, 10, or 100. A femtosecond result that is not a whole number of picoseconds
  raises `UnsupportedPrecisionError`.
- A trace without a valid timescale is `invalid_input`.
- Timestamps are trace times. Nothing is renormalized so that cycle 0 lands on a
  fixed time; `Observation` carries the cycle index and the real edge time
  together. Track C may record its own offsets in its run metadata.
- Timestamps are bounded: `0 <= t <= MAX_TIME_PS = 2**128 - 1`. Every legal
  64-bit tick count at every legal timescale fits. `TraceCoverage.end_ps` is
  strictly less than `MAX_TIME_PS`, so an exclusive interval end is always
  representable. A trace with a timestamp above the bound is `unsupported`; a
  contract model rejects it. See section 11 for JSON encoding.

## 5. Sampling phases

A value immediately before a clock edge and the value after sequential updates
are different observations. Every query and every observation names its phase.

| Phase | Meaning at time T |
| --- | --- |
| `before` | The final recorded state at a timestamp strictly less than T. |
| `settled` | The final recorded state at or before T, after every record carrying T. |

- These are conventions about the recorded trace. They do not reconstruct
  simulator scheduling regions, and record order within one timestamp carries no
  meaning.
- No code subtracts or adds a time unit to move between phases. `before` at T is
  exactly the value at T−1 ps, which the provider computes itself.
- Handshake acceptance (ready, valid, payload) samples `before` at the edge.
- Registered-output checks in cycle mode normally sample `settled` at the edge.
  The golden header states the phase.
- A trace that cannot support the required phase (for example, only post-edge
  snapshots) is `unsupported`.

### 5.1 Reproducing a failure

`FailureAnchor` carries `signal_path`, `clock_path`, and an `Observation`
(`epoch`, `cycle`, `phase`, `time_ps`). `time_ps` is the time of the clock edge.

```
provider.get_value_at(anchor.signal_path, anchor.observation.time_ps,
                      phase=anchor.observation.phase) == anchor.actual
```

holds whenever `actual` is present. Track B starts its slice from exactly this
observation.

## 6. Clock edges and sequential semantics

### 6.1 Edge detection

- A rising edge at T: `before` is known `0` and `settled` is known `1`. A
  falling edge is the reverse.
- A transition from or to `x`/`z` is not an edge. After reset has first been
  observed active, such a transition means an edge may have been missed: the
  comparison stops as `inconclusive` with diagnostic `CLOCK_UNKNOWN_TRANSITION`
  at the first rising edge after it, unless a failure was already found at an
  earlier edge or the comparison had already completed. Before reset is first
  observed active, such transitions are ignored. Consequently every observation
  in a `ComparisonResult`, from the first reset-active edge to the failing or
  final edge, lies on a clock with no unknown transitions.
- A clock must be one bit wide.
- Several transitions of the clock at one timestamp are ambiguous. A provider
  that retains same-timestamp history raises `IndeterminateClockError`.
- Glitches are kept in the trace and are never filtered globally.
- The edge list comes from the trace. A paused clock simply has no edges during
  the pause. Nothing computes an edge from an assumed period.

### 6.2 Graph semantics

The causal graph is time-unrolled: a `CausalNode` is a signal (or part-select)
at one `Observation`. The same signal at two cycles is two nodes. A `CausalEdge`
runs from the node that is depended on (`source`) to the dependent node
(`target`).

| `edge_type` | Rule |
| --- | --- |
| `sequential` | One storage boundary at one actual clock edge. `source` is sampled `before` that edge; `target` is `settled` at the same edge. Both share `epoch`, `cycle`, and `time_ps`. |
| `combinational` | No storage boundary. Either both nodes share one observation, or the edge is a carry: the same signal, from `settled` at an earlier cycle to `before` at a later cycle of the same epoch, with an unchanged value. |

Consequences:

- A register's `settled` value at edge k depends sequentially on its next-state
  operands sampled `before` edge k. This matches Track B's
  `q[k+1] = F(q[k], u[k])` with pre-edge sampling.
- To continue backward, a carry edge links a signal's `before` value at edge k to
  its `settled` value at the earlier edge that produced it. A register that held
  for several cycles may carry across several cycles in one edge.
- No edge implies "one clock period earlier". Every hop names real observations.
- A testbench-driven input sampled `before` edge k is a leaf; it is not carried
  back to an earlier `settled` observation unless its value is unchanged.
- Each edge has a `role` (`data` or `control`) and a `branch` status
  (`retained`, `pruned`, or `unknown`). An unknown control value never prunes a
  branch.
- Same-observation combinational cycles are legal in the model. Track B reports
  them in diagnostics; the report is not promised to be acyclic.

## 7. Reset, epochs, and indexing

- The golden header names the reset signal and its active level (`"0"` or `"1"`).
- Comparison requires an observed asserted reset. A design with no reset is
  `unsupported` in schema 1.
- Reset is sampled `before` each rising clock edge.
- Epoch 0 begins at the first rising edge whose pre-edge reset is known
  inactive, after reset was observed active. That edge is cycle 0. Cycles count
  rising edges from 0 within the epoch.
- An edge whose pre-edge reset is active ends the current epoch immediately.
  Expectations of that epoch not yet observed are cancelled: they are never
  matched against a later epoch. They are not waived: if any remain, the
  comparison stops at that edge as `inconclusive` (`EPOCH_ENDED_EARLY` in cycle
  mode, `EXPECTATIONS_CANCELLED_BY_RESET` in handshake mode). The golden model
  must list, per epoch, only expectations it guarantees are observed before the
  reset that ends it. The next inactive edge starts the next epoch at cycle 0.
- Once reset has first been observed active, an `x`/`z` reset at a sampling
  edge, or a reset change at the edge timestamp, makes the comparison
  `inconclusive`. Before that, `x`/`z` reset values and reset changes at edges
  are ignored, as for the clock (section 6.1): epoch 0 needs a known active
  reset followed by a known inactive one, so they cannot move its start, and
  no value is compared before it.
- Handshake transactions are indexed from 0 within each epoch. A stall consumes
  no index.

## 8. Golden JSONL and comparison

### 8.1 File format

One JSON object per line, no blank lines. The first line is the metadata
record; every other line is an expected record. Models: `GoldenHeader`,
`GoldenRecord`, `GoldenTrace`; parser: `parse_golden_jsonl`.

```json
{"type":"metadata","schema_version":1,"mode":"cycle","clock":"tb.dut.clk","reset":"tb.dut.rst_n","reset_active":"0","sampling":"settled","signals":[{"name":"full","path":"tb.dut.full","width":1}]}
{"type":"expected","epoch":0,"cycle_index":0,"values":{"full":"0"}}
```

Metadata fields: `type`, `schema_version`, `mode` (`cycle` or `handshake`),
`clock`, `reset`, `reset_active`, `sampling`, `signals` (an ordered list of
`name`, `path`, `width`), and for handshake mode `valid`, `ready`, and optional
`deadline_cycle`.

Expected fields: `type`, `epoch`, exactly one of `cycle_index` or
`transaction_index`, and `values` mapping a declared name to its expected bits.

Validation:

- Expected values are exactly `width` characters of `0`/`1`, left bit first.
  Hex, shortened values, and `x`/`z` are rejected.
- Epochs start at 0 and never skip or decrease. Within an epoch the index starts
  at 0 and increases by one. Every epoch has at least one record.
- Cycle-mode records may omit signals that are not compared in that cycle (for
  example `out_data` while `out_valid` is expected low). Handshake records must
  give every declared signal.
- Mixed modes, undeclared names, duplicate declarations, unknown fields, and
  unsupported schema versions are rejected. A trace with no expected value at
  all is rejected.

### 8.2 Cycle mode

At each cycle that has a record, compare every listed signal at the header's
sampling phase against the trace. The comparison is complete when every listed
cycle of every epoch was observed inside trace coverage.

### 8.3 Handshake mode

`sampling` must be `before`. At each rising edge of an active epoch:

1. Check reset and coverage before consuming any expectation.
2. Evaluate `valid` and `ready` with four-state logic. A known `0` on either
   means no transfer. Both known `1` means a transfer. Anything else is
   `inconclusive`; transfers are never guessed.
3. No transfer consumes no expectation.
4. A transfer compares the pre-edge payload with the next expected record of the
   epoch. A mismatch stops the comparison at this edge. The comparison never
   searches ahead for a later matching payload.
5. A transfer with no expectations left is `extra_transaction`.

Completion: when `deadline_cycle` is set and that cycle of the final epoch is
observed with expectations outstanding, the result is `missing_transaction`
anchored at that edge. If the trace ends before the deadline, or no deadline is
set and expectations remain, the result is `inconclusive`. A non-final epoch is
complete only if all of its transactions transferred before the reset that
ended it; otherwise section 7 applies.

### 8.4 Result

`ComparisonResult.status`:

| Status | Meaning | `failure` |
| --- | --- | --- |
| `pass` | Every required expectation was observed and matched; at least one value was compared. | null |
| `fail` | A divergence was found. | the first divergence |
| `inconclusive` | Coverage, sampling, clock, or reset evidence was insufficient. | null |
| `unsupported` | Input exceeds the MVP capability. | null |
| `invalid_input` | The trace or golden data is malformed. | null |
| `error` | Tooling or infrastructure failed. | null |

Every status other than `pass` and `fail` must carry at least one diagnostic.
A comparison in which no value was compared is `inconclusive`
(`NOTHING_COMPARED`).
`compared_values` counts expected values checked, including those at a failing
observation. Track C's CLI maps `pass` to exit 0, `fail` to 1, `invalid_input`
and `error` to 2, and `unsupported` and `inconclusive` to 3.

`FailureKind`:

| Kind | Mode | `expected` | `actual` |
| --- | --- | --- | --- |
| `value_mismatch` | either | known | known and different |
| `unknown_value` | either | known | contains `x`/`z` |
| `missing_transaction` | handshake | known | null |
| `extra_transaction` | handshake | null | observed payload |

A known expected value against an actual value with `x`/`z` is always a failure.
`x` is never a wildcard. No payload is invented for a missing transfer.

## 9. Coverage, gaps, and missing data

- `TraceCoverage` is the inclusive range `[start_ps, end_ps]`. A signal's last
  change does not extend or limit it. VCD has no end-of-file
  marker, so a timestep is known complete only when a later timestamp follows
  it: for VCD, `end_ps` is the final timestamp minus 1 ps, and records at the
  final timestamp are not exposed. A trailing timestamp with no records (as
  Icarus and Verilator write at `$finish`) therefore costs nothing. A
  `$dumpoff` that is still in effect at the final timestamp makes
  `[a, end_ps + 1)` unavailable. Benchmarks must dump at least one timestep
  after the last compared edge.
- A `$dumpoff`/`$dumpon` span is an unavailable interval `[a, b)`. Values inside
  it are unknown data, not held values. Each signal restates its value at `b`.
- A malformed or detectably truncated trace is `invalid_input`: an
  unterminated block or `$comment`, an incomplete record, a file that does not
  end with a newline, or fewer than two distinct timestamps. Truncation at a
  timestep boundary is not detectable in VCD; it shortens coverage and can only
  make a result `inconclusive`, never change a compared value.

| Situation | Behavior |
| --- | --- |
| Path not in the trace | `SignalNotFoundError`. Never `x`, never `0`. |
| Time outside coverage | `OutOfRangeError`. |
| Time inside a dump gap | `DataUnavailableError`. |
| Signal has no record yet at the observed instant | all-`x` at the declared width. This includes `before` at the trace start. |
| `before` at the time dumping resumes | `DataUnavailableError`; `settled` there is known. |
| Malformed request | `InvalidQueryError`. |

An explicit first record that is all-`x` is not a change and never appears as a
window sample, except as the restatement where a dump gap ends.

Errors are raised in this order: malformed request, missing signal, out of
range, unavailable. No method returns a placeholder for a failed query.

`WaveformProvider` methods:

| Method | Returns | Interval and errors |
| --- | --- | --- |
| `get_coverage()` | `TraceCoverage` | none |
| `get_signal_decl(signal_path)` | `SignalDecl` | `SignalNotFoundError` |
| `get_value_at(signal_path, time_ps, *, phase=settled)` | `LogicValue` | `time_ps` must be inside coverage |
| `get_clock_edges(clock_path, start_ps, end_ps, *, edge=rising)` | ascending tuple of edge times | `[start_ps, end_ps)`; every picosecond covered; raises `DataUnavailableError` if the interval, or the instant before it, touches a dump gap |
| `get_window_slice(signal_paths, start_ps, end_ps)` | `WaveformWindow` | `[start_ps, end_ps)`; every picosecond covered; gaps are returned, not raised |

`WaveformWindow`:

- Signals appear in request order. The request must be a non-empty list of
  distinct paths. Empty intervals are rejected.
- `initial` is the settled value at `start_ps`. A change exactly at `start_ps`
  is folded into it. It is null only when `start_ps` lies in a dump gap.
- `samples` are strictly inside `(start_ps, end_ps)`, ascending, one per
  timestamp. Each is a real change, except a sample exactly where a dump gap
  ends, which restates the value and does not assert a change.
- `unavailable` lists dump gaps clipped to the window.
- A value holds until the next sample or the next unavailable interval.

## 10. Determinism

- The first failure is the earliest failing clock edge in trace order.
- When several signals fail at the same observation, the anchor is the one
  declared first in the golden header's `signals` list. The others are kept, in
  declaration order, in `additional_mismatches`. This order breaks ties; it does
  not claim one signal failed physically before another.
- Clock edge lists are ascending with no duplicates.
- Suspect ranks are exactly 1..N with no ties. Rank is ordinal and is not a
  probability or confidence.
- Node, edge, suspect, source, and assertion identifiers are unique within a
  report.
- Two runs on the same inputs produce identical contract objects. Wall-clock and
  host metadata are kept outside these models.

## 11. JSON conventions

- Field names are snake_case. Enumerations are lowercase strings.
- Unknown fields are rejected. Missing required fields are rejected.
- Timestamps serialize as canonical decimal strings (`"55000"`), so values above
  2^53 survive JavaScript. Input accepts an integer or a canonical decimal
  string. Floats, booleans, signs, leading zeros, and exponents are rejected.
  A timestamp string is at most 39 digits and is checked for length before
  conversion.
- Counts, widths, indices, and line numbers are JSON integers. Floats and
  booleans are rejected.
- Absent or inapplicable data is `null`. It is never a fabricated value.
- Every top-level document (`GoldenHeader`, `ComparisonResult`, `CausalReport`,
  `MockFixture`) carries `schema_version`, currently the integer `1`. Any other
  version is rejected rather than read loosely.
- Models are immutable. A JSON round-trip reproduces an equal object.

## 12. Causal report

`CausalReport` contains `provenance`, `status`, `failure`, `anchor_node_id`,
`nodes`, `edges`, `suspects`, `sources`, `assertions`, `waveform`, `truncated`,
and `diagnostics`.

- `pass`: no failure and no graph.
- `fail`: the failure is present. The graph may be empty when localization was
  not possible; diagnostics must say why.
- Any other status: no failure, no graph, at least one diagnostic.
- When nodes exist, `anchor_node_id` names the node whose signal, observation,
  and value equal the failure's.
- `CausalNode.value` is the observed value, or null when the signal was not
  observed. `location` may be null with the reason given in diagnostics.
- `truncated` means the slice stopped at a depth, time, or capability limit.
  Absent nodes are then not evidence of irrelevance.
- `SourceFile` is `id`, a normalized relative POSIX `path`, and the file's
  SHA-256. `SourceSpan` is an inclusive 1-based line range in that file. Track C
  checks the hash before showing a line as the analyzed code.
- `SourceFile.path` is relative to the analysis source root: the directory the
  caller passes to Track B as `root`. Every analyzed file lies inside it. For a
  benchmark case run, the source root is the case directory (the directory that
  holds the case manifest). For the mock fixture of section 16, it is the
  repository root. The report does not record its root; a consumer resolves
  `path` against the root the analysis used, refuses a path that resolves
  outside it, and never shows a file whose hash does not match as the analyzed
  code.
- `CandidateAssertion.validation` is `candidate`, `validated`, `failed`, or
  `unsupported`. `validated` and `failed` must name the tool. Text alone is
  never presented as validated.
- `provenance.kind` is `analysis` or `synthetic_fixture`. Fixture data is always
  labeled.

## 13. Four-state validation

Literal X propagation cannot be validated with ordinary Verilator runs, because
Verilator is mostly two-state. Its initialization options are not evidence of
literal X propagation.

- Contract level: `tests/test_contracts.py` checks that `x`/`z` survive models,
  JSON, and the mock provider. The mock FIFO trace contains real `x` values
  before reset.
- Track A: handcrafted four-state VCD fixtures cover `x`/`z` parsing, extension,
  clocks, and comparison.
- Track C: `case_04_alu_xprop` runs on Icarus Verilog in SystemVerilog mode. The
  run must confirm the produced VCD actually contains unknown bits. A Verilator
  randomized-initialization run may be added but does not satisfy this gate.
- A missing Icarus installation fails the gate. A skipped X test is not
  completion, and all-four end-to-end coverage is not claimed without it.

## 14. Acceptance criteria

### 14.1 Shared foundation (this change)

- `uv run pytest tests/test_contracts.py` and `uv run mypy src/` pass.
- The mock FIFO fixture loads, round-trips through JSON, and its failure is
  reproducible from the mock provider.
- These are contract tests. They are not engine or end-to-end coverage.

### 14.2 Track A

- The real provider passes the same query, phase, interval, and error behavior
  the mock provider demonstrates, on real VCD input.
- Equivalent 1 ns and 1 ps traces give identical results; fractional
  picoseconds are rejected; large timestamps stay exact.
- Aliases, both range directions, shortened vectors, and mixed-case `x`/`z`
  normalize without losing bit positions.
- Clock pauses, falling edges, `x` clocks, and same-time glitches are handled as
  section 6.1 requires.
- With and without stalls, handshake comparison gives the same matched sequence.
  First wrong, extra, or missing transaction, reset mid-stream, and repeated
  payloads cannot be hidden by alignment.
- Several mismatches at one edge yield the deterministic anchor of section 10.
- Lookups and slices agree with a linear-scan oracle under property tests.
- A clock that becomes `x` mid-epoch makes the comparison `inconclusive`; a
  failure found before it is still `fail`.
- A reset that ends an epoch with expectations outstanding is `inconclusive` in
  both modes, never `pass`.
- A timestamp above `MAX_TIME_PS` is `unsupported`, never a crash.
- Every byte-prefix of a valid VCD is either `invalid_input` or agrees with the
  full trace on its covered range.

### 14.3 Track B

- Developed against `MockWaveformProvider` and `mock_fifo_failure.json` before
  Track A's provider exists.
- Every emitted report validates as a `CausalReport`, including the edge rules
  of section 6.2.
- Unknown control values keep both branches. Missing signals and unsupported
  constructs appear in diagnostics, and limits set `truncated`.
- Suspect locations reference real source files with matching hashes.

### 14.4 Track C

- Each correct benchmark variant passes; each mutant fails at its documented
  first observation. A build or simulator error is never counted as a found bug.
- Golden models are independent of the RTL under test and emit valid golden
  JSONL.
- CLI, service, and UI consume `ComparisonResult` and `CausalReport` without
  redefining them, and render every status, null value, and unknown value
  honestly.
- The four-state gate of section 13 is met.

### 14.5 Integration

- One real FIFO trace passes through A, then B, then C before the full UI is
  built.
- All four benchmarks run end to end with real simulator output.
- Detection, localization, and replay are reported separately, with counts and
  denominators. Four directed cases are a smoke suite, not an accuracy estimate.

## 15. Reconciliation of the briefs

| Topic | Track A | Track B | Track C | Decision |
| --- | --- | --- | --- | --- |
| Phase names | `before` / `settled` | pre-edge / post-NBA | `pre_edge` / `post_settle` | `before` / `settled`. |
| Phase time | Query at T with a phase | Do not subtract a time unit | Sample at edge −1 ps and +1 ps | Query at the edge time T with a phase. Observations store T. With inputs stable around the edge, C's ±1 ps samples equal these. |
| Timestamps in JSON | Integers | — | Decimal strings | Integers in Python, decimal strings in JSON, both accepted on input. |
| Time origin | Trace time | — | Normalized so cycle 0 is 5000 ps | Trace time. Cycle index plus edge time are stored together. |
| Bit characters | `0/1/X/Z`, normalize case | — | Lowercase `01xz` | Lowercase canonical, uppercase accepted. |
| Golden values | Hex at declared width | — | Exact-width bit strings | Exact-width `0`/`1` strings. |
| Tie-break | Canonical path order | — | Per-benchmark priority list | Declaration order in the golden header, which the benchmark author controls. |
| Hierarchy root | `top.*` | Elaborated names | `tb.dut.*` | Whatever the trace declares. Fixtures use `tb.dut.*`. |
| Sequential edge | Do not shift blindly | −1 cycle, evaluated before the producing event | Explicit source and target observations | `before` to `settled` at one real clock edge, plus carry edges. |
| Failure kinds | Mismatch, extra, missing, invalid, incomplete | — | Adds `unknown_value`, `protocol_violation` | Four kinds in section 8.4. `protocol_violation` is deferred; C's stall benchmark is caught as a cycle-mode `value_mismatch` on `m_valid`. |
| Statuses | Typed outcomes | — | pass/fail/inconclusive/unsupported/error | Those five plus `invalid_input`. |
| Ranking | — | Score and rank | Ordinal rank, no probabilities | Ordinal rank only. |
| Reset | Configured polarity | Async needs event edges | Active-low synchronous | Configured polarity, synchronous sampling. Async is excluded. |
| Missing signal | Error | Unavailable, not zero | Missing, never fake X | `SignalNotFoundError`. |
| Assertions | — | Templates | Optional, with validation status | Optional `CandidateAssertion` with status. |
| Source in report | — | File and line | Embedded content plus hash | Path plus hash. Track C serves text by source id. |

### 15.1 Decisions with cross-track impact

These change behavior for more than one track. They are final for schema 1. A
track that finds one unworkable during implementation raises it through the
change process in section 2 rather than working around it locally:

1. Observations use the clock edge time with a phase, not edge ±1 ps.
2. Timestamps are decimal strings in JSON.
3. The same-observation tie-break is golden declaration order.
4. Golden expected values are exact-width binary strings only.
5. `protocol_violation` is not a failure kind in schema 1.
6. Sequential edges are defined per clock edge, with carry edges for held values.
7. A design without an observed reset is unsupported in schema 1.
8. Timestamps are bounded by `MAX_TIME_PS = 2**128 - 1`; JSON timestamp strings
   are at most 39 digits.
9. A clock transition to or from `x`/`z` after reset is first observed makes the
   comparison `inconclusive`.
10. Expectations cancelled by reset are not waived: an epoch ended with
    expectations outstanding is `inconclusive` in both modes, and a comparison
    with nothing compared is `inconclusive`.
11. VCD coverage ends 1 ps before the final timestamp; a VCD must end with a
    newline and contain at least two distinct timestamps. Verified against real
    output from Icarus Verilog 13.0 and Verilator 5.054 and 5.020: both end
    files with a newline and write a bare timestamp at `$finish`, so a normal
    `$finish` loses no records.
12. An `x`/`z` reset, or a reset change at an edge, is ignored until reset has
    first been observed active, matching the clock rule of decision 9. After
    that it makes the comparison `inconclusive` (section 7).
13. `SourceFile.path` is relative to the analysis source root that the caller
    passes to Track B: the case directory for benchmark runs and the repository
    root for the mock fixture (section 12). This records existing behavior of
    Track B (`build_design_graph(root=...)`) and Track C (the API resolves
    sources inside the job's case directory); no code changes.

## 16. Mock fixture

`tests/fixtures/mock_fifo_failure.json` is a synthetic eight-cycle synchronous
FIFO wrap failure. `tests/fixtures/mock_fifo_golden.jsonl` is its golden trace
and `tests/fixtures/mock_fifo_wrap.sv` is the source its locations point at.
Its source paths are relative to the repository root (decision 13), so a
consumer that resolves sources against a case directory does not find them.

- Clock `tb.dut.clk` rises at 5000 ps and every 10000 ps after. Reset
  `tb.dut.rst_n` is low until 20000 ps. Cycles 0 to 7 are the edges at 25000 ps
  to 95000 ps. Coverage is `[0, 100000]`.
- Four writes fill the depth-4 FIFO. At cycle 3 the write pointer wraps to `000`
  without toggling its wrap bit, so it equals the read pointer.
- First divergence: `tb.dut.full`, cycle 3, `settled`, 55000 ps, expected `1`,
  actual `0`. `empty` and `out_valid` also differ at that observation.
- The trace values were produced by stepping the fixture RTL with a script, not
  by an HDL simulator. The comparison result, causal graph, suspects, and
  ranking are hand-authored fixture data. They are not the output of a working
  comparison or causal engine and must not be cited as analysis results.
