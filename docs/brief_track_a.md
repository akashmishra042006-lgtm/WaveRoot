# Track A: waveform queries and first-failure comparison

**Status: proposal for shared-contract review.** This brief supplies Dev 1's
input to the architecture phase. Reconcile it with Tracks B/C before implementing
`src/contracts.py`; the interfaces below are not yet locked.

## Scope and outputs

Own `src/waveform/`: read VCD (FST through a later conversion adapter), index
signal changes, provide time queries, and compare RTL outputs with a golden
trace. Emit the earliest deterministic data mismatch as a `FailureAnchor` and
provide waveform slices for causal analysis and display.

Proposed MVP: one explicitly named clock, one configured reset polarity, digital
signals, in-order output transactions, and explicit hierarchical signal mapping.
Clock/reset autodetection, multiple clock domains, out-of-order protocols, real
signals, and gate-delay reconstruction are outside this initial scope. Report
unsupported input explicitly; do not silently approximate it.

## Parsing and indexing invariants

| Concern | Proposed rule |
| --- | --- |
| Names and aliases | Preserve scope components, escaped identifiers, declared range, width, and identifier-code aliases. Paths must resolve uniquely; do not flatten escaped dots into hierarchy separators. |
| Bit ordering | Store exactly `width` canonical `0/1/X/Z` characters in declared packed-vector order, with range metadata mapping positions to RTL indices. Distinguish `[7:0]` and `[0:7]`; never reverse bytes heuristically. |
| Values | Normalize case; zero-extend shortened known VCD vectors and extend abbreviated leading X/Z per VCD rules. Reject over-width/invalid values. Preserve X/Z masks; signed interpretation requires explicit metadata. Hex in golden logs is converted at the declared width, not treated as native VCD vector syntax. |
| Time | Use integer picoseconds with exact arithmetic. Require a valid timescale; reject any timestamp that cannot be represented exactly in ps (including nonintegral fs conversion). No floating point or rounding. |
| Events | Require nondecreasing time, retain same-time changes for diagnostics, and index final recorded state per timestamp. Repeated identical samples may be coalesced without losing interval boundaries or dump gaps. |
| Initial state | Before a signal's first recorded value, return width-correct X within known trace coverage. An absent signal is an error, not X or zero. No invented initial clock edge. |
| Dump gaps | Track `$dumpoff`/`$dumpon` coverage. Unrecorded intervals are unavailable data, not reliable held values. Truncation and malformed input are errors. |
| Queries | Binary-search sorted per-signal timestamps: O(log transitions) lookup; slices add only returned transitions and a starting value. Avoid expanding every signal at every clock tick. |

[pyvcd's reader](https://pyvcd.readthedocs.io/en/stable/vcd.reader.html) exposes
declarations, identifier codes, timescales, and changes, including vector values
represented as either integers or strings. The adapter must retain declared
width even when the reader returns an integer.

Use [GTKWave's fst2vcd](https://gtkwave.github.io/gtkwave/man/fst2vcd.1.html) for
the later FST wrapper. Pass an argument list, check exit status, bound execution,
and clean up temporary output. Missing converters must produce a useful error.
Validate converted traces against equivalent VCD fixtures. Caches must include
input identity/content, parser/converter version, and normalization options.

## Clock sampling: settle this before contracts

Values immediately before a clock edge and values after sequential updates are
different observations. [cocotb's timing model](https://docs.cocotb.org/en/stable/timing_model.html)
illustrates why detecting a rising edge does not mean all downstream values have
settled. A waveform's text order is not a portable simulator-region identifier.

Proposed explicit query phases:

- `before`: final recorded state at a timestamp strictly less than T.
- `settled`: final recorded state at or before T, after consuming all records
  carrying T. This is a trace convention, not a reconstruction of HDL regions.

For handshake comparison, sample ready, valid, and payload with `before` at the
accepted edge. Track C must drive inputs/reset away from that rising edge and
record pre-edge values. If a trace records only post-edge snapshots or lacks
needed history, report unsupported sampling/insufficient coverage.

For cycle comparison, the golden header explicitly chooses `before` or
`settled`. Default proposal: `settled` for registered-output checks. Delayed
clock-to-Q observations require an explicit sample offset and matching golden
semantics; MVP rejects them rather than guessing.

Count only known `0 -> 1` rising and `1 -> 0` falling transitions. X/Z transitions
are diagnostics, not accepted clocks. Reject ambiguous same-timestamp multiple
clock transitions. Preserve glitches in the trace; do not globally filter them.
Use the actual edge list, including clock pauses; never compute the previous
edge by subtracting an assumed period.

Track B must distinguish a register output after edge T from its input sampled
before the *same* edge T. Blindly shifting every sequential dependency to
T minus one period can be wrong. Its traversal and the failure-anchor sampling
phase must use the same observation convention.

## Reset and handshake state machine

Proposed reset convention: active polarity is configured; comparison requires
an observed asserted reset. Reset assertion ends the active epoch immediately
and cancels pending comparisons in that epoch. Deassertion must occur away from
the rising edge. The first rising edge with reset known inactive in the pre-edge
sample begins the next epoch, cycle 0. Unknown reset or assertion/deassertion
exactly at the sampling edge is indeterminate input. A no-reset design requires
an explicitly declared initialization mode in the shared spec.

At each eligible rising edge, in order:

1. Check reset and trace coverage before consuming any golden record.
2. Evaluate one-bit ready/valid using four-state logic. A known zero on either
   side means no transfer; both known one means a transfer. Other combinations
   are indeterminate and stop alignment with a diagnostic, never guessed counts.
3. With no transfer, consume no expected transaction. Variable latency and
   downstream stalls do not shift the expected transaction index.
4. With a transfer, compare the pre-edge payload to the next expected record
   for this epoch and channel. Match -> increment the index; mismatch -> stop
   at this edge and emit a failure. Do not resynchronize by searching for a
   later matching payload, which can hide dropped or duplicated words.
5. Known expected bits versus actual X/Z is a mismatch. MVP golden expected
   values must be fully known and width-valid; don't-care masks are deferred.

If golden records are exhausted but a transfer occurs, report an extra
transaction. If expected records remain at EOF, report incomplete coverage unless
the golden metadata supplies a deadline that was observed and missed. Never
invent an actual payload for a missing transfer. Reset epochs are explicit in
golden data; reset never silently rewinds one global expected stream.

Cycle mode compares each eligible configured sample by epoch and zero-based
cycle index. Multiple mismatches at the same edge are ordered by canonical
signal path for deterministic results; retain other same-edge mismatches as
diagnostics. Failing to compare anything is not a pass.

## Proposed golden JSONL shape

Track C produces one metadata record followed by expected records. JSONL is
the MVP format; CSV conversion can follow once a schema is agreed.

```json
{"type":"metadata","schema_version":1,"mode":"handshake","clock":"top.clk","reset":"top.rst_n","reset_active":"0","sampling":"before","channel":"out","valid":"top.out_valid","ready":"top.out_ready","signals":{"data":{"path":"top.out_data","width":8}}}
{"type":"expected","epoch":0,"transaction_index":0,"values":{"data":"0x2a"}}
{"type":"expected","epoch":0,"transaction_index":1,"values":{"data":"0x35"}}
```

Cycle-mode records use `cycle_index` instead of `transaction_index`. Metadata
must also define expected completion coverage (last cycle or deadline) when a
complete pass is required. Reject duplicate/skipped indices, unsupported schema
versions, invalid widths, unresolved mappings, and mixed modes. The displayed
example proposes structure only; it is not the shared FIFO fixture.

## Minimal proposed interface for Track B

These signatures illustrate the needed information; shared types and exceptions
will be defined in the contract phase, not imported from this Markdown.

```python
class WaveformProvider(Protocol):
    def get_value_at(
        self, signal_path: str, time_ps: int, *,
        phase: Literal["before", "settled"] = "settled",
    ) -> LogicValue: ...

    def get_clock_edges(
        self, clock_signal: str, start_ps: int, end_ps: int, *,
        edge: Literal["rising", "falling"] = "rising",
    ) -> Sequence[int]: ...

    def get_window_slice(
        self, signals: Sequence[str], start_ps: int, end_ps: int,
    ) -> WaveformWindow: ...
```

- `LogicValue`: width and canonical bits, preserving X/Z.
- Edge ranges are `[start_ps, end_ps)` and sorted with no duplicates.
- Window ranges are `[start_ps, end_ps)` with the settled starting value plus
  subsequent transitions strictly inside the interval, range/width metadata,
  and unavailable spans. Distinguish the boundary value from a real transition.
- Queries outside known trace coverage, missing paths, and unavailable samples
  raise typed errors. Empty windows are rejected. Trace end is explicit metadata;
  a signal's last transition alone does not establish coverage of later times.
- `FailureAnchor` needs signal path, accepted edge/time, clock, expected/actual
  values, epoch/index, and sampling phase. If sample time differs from edge time,
  preserve both. Track B must be able to reproduce the exact failing observation.
- The comparison result must distinguish pass, data mismatch, extra/missing
  transactions, invalid input, and incomplete/indeterminate analysis. The shared
  contract must decide how non-data failures are represented; an optional
  `FailureAnchor` alone is insufficient.

## Acceptance cases for implementation

1. Equivalent 1 ns and 1 ps traces yield identical normalized queries; fractional
   ps input is rejected, large timestamps stay exact.
2. Aliases, escaped names, ascending/descending ranges, abbreviated vectors,
   leading zeros, and mixed-case X/Z normalize without losing bit positions.
3. Queries at a transition exercise both phases; pre-initialization, absent
   signals, dump gaps, duplicate timestamps, and trace bounds are distinguished.
4. Clock pauses and falling edges are indexed correctly; X clocks and same-time
   glitches do not create silently accepted transfer edges.
5. No stalls and arbitrary stalls yield the same matched transaction sequence.
   First wrong/extra/missing transaction, reset mid-stream, and repeated payloads
   cannot be hidden by alignment.
6. Multiple differing outputs choose a deterministic first anchor; the provider
   reproduces its actual value using the anchor's phase.
7. Generated traces are checked against a simple linear-scan oracle for lookup
   and slicing; Hypothesis covers time ordering, values, and stall schedules.

For X/Z tests use handcrafted four-state VCD fixtures and a four-state simulator
fixture for end-to-end validation. [Verilator is mostly two-state](https://verilator.org/guide/latest/languages.html#unknown-states);
its initialization options are not proof of literal X propagation. Track C must
resolve this for the fourth benchmark before claiming all-four E2E coverage.

## Decisions to carry into the shared-spec session

- Confirm the single-clock scope, reset epochs, phases, and trace-coverage rules
  with both the causal algorithm and benchmark harness.
- Agree the golden schema, completion deadlines, and non-data result variants.
- Freeze path mapping and required signal metadata with Track B.
- Confirm the fourth benchmark's four-state simulation approach with Track C.
- Record the agreed types in `src/contracts.py` and exercise the mock FIFO through
  contract tests before any track starts depending on them.

After the core exists, `docs/plan_track_a.md` will contain the four scoped
follow-up tickets: FST conversion, hierarchy glob filtering, caching, and waveform
CLI options. Track A owns callable behavior and option definitions; Track C owns
the shared CLI wiring. That implementation plan is deliberately deferred until
the common contracts and core are established.
