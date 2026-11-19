# Track B plan: next tickets for `src/causal/`

Audience: Claude Opus 5.5 (or any engineer) picking up Track B after the first
engine landed on `feat/track-b-causal`. Do the tickets in order. Each one is
written to be started cold: it says what exists, what to build, how to test
it, and what to leave alone.

`spec.md` and `src/contracts.py` are the source of truth. `docs/brief_track_b.md`
is background. Where they differ, the spec wins.

## 1. What exists

| File | Role |
| --- | --- |
| `src/causal/fourstate.py` | Four-state vector arithmetic (`Vec`, `Tri`, operators, `case_match`). |
| `src/causal/expr.py` | Immutable expression IR, `evaluate`, `Guard`, `collect_reads`, printing. |
| `src/causal/ast_graph.py` | `build_design_graph`: pyslang elaboration lowered to a `DesignGraph` (signals, per-signal `Driver` lists of `GuardedAssign`, flat `StructuralEdge` list). |
| `src/causal/backward_slicer.py` | `analyze_failure` (the single entry point Track C calls, C04 part 2), `slice_failure`, `analyze_comparison`: time-unrolled, value-pruned backward BFS that returns a `CausalReport`. `SourceFile.path` is relative to the `root` the caller passes (spec decision 13). |
| `src/causal/sva_synth.py` | `synthesize_assertion`: candidate checker module plus `bind` for the top suspect. |
| `src/causal/graph_io.py` | `dump_design_graph`, `load_design_graph`, `verify_sources` (B-04): deterministic JSON form of a `DesignGraph`, format version 1, internal to Track B. |
| `tests/test_causal.py` | Engine tests against `MockWaveformProvider`: 73 for the first engine, 121 after B-01 to B-04, 165 after the PR #6 review rounds, `analyze_failure`, and B-12. |
| `tests/test_e2e_benchmarks.py` | Real-simulator benchmark runs (Verilator, Icarus) that pin the mutated line's rank per case. Needs an LF checkout (see section 3). |

The engine models an **allowlist** (PR #6 review round 4). Every driver
outside it is an unsupported driver: the slice stops there with `truncated`
and `UNSUPPORTED_DRIVER`, and nothing is pruned. A gap in the subset therefore
costs coverage, never a wrong answer (spec §12). The subset:

- Scopes: the top module and module instances connected through input and
  output ports (B-01; signals are named by path from the top instance, such as
  `u_core.q`, and clocks are resolved through plain port connections).
- Processes: continuous `assign` and net declaration assignments;
  `always_ff` and `always @(posedge|negedge clk)` on one one-bit clock with
  nonblocking assignments; `always_comb` and `always @*` with blocking
  assignments only. A plain level-sensitive list (`@(a or b)`, no `iff`, whole
  signals only) that names every signal the process reads counts as
  `always @*`.
- Statements: blocks, `if` / `else` (including `unique` and `priority`),
  `case`, `casez`, `casex`, assignments to signals of the design.
- Expressions: signals, parameters, and enum values; operators; `?:`; packed
  vectors; constant and indexed part-selects; packed struct field reads;
  concatenations and concatenated targets; casts; `$signed`, `$unsigned`, and
  the pure system functions in `ast_graph._PURE_SYSTEM_CALLS`.
- Values: fixed-size unpacked arrays of integral elements, up to 4096
  elements (B-02; one signal per element, `mem[2]` or `grid[1][0]`, reads and
  writes selected by `index` guards); unpacked structs flattened to one signal
  per leaf field, `s.f` or `o.inner.arr[1]`, with whole copies and assignment
  patterns lowered field by field (B-03); bit-precise partial writes through
  constant part-selects, packed struct fields, and packed union members (B-03).

Refused, with the reason in `Driver.unsupported`: `inout`, `ref`, and
interface ports; generate blocks, instance arrays, gate primitives, and
checkers (every signal they may write gets a `scope` driver); asynchronous
reset or several edge events; `iff`; `always_latch`; blocking assignments in a
clocked process; nonblocking assignments in a combinational process;
intra-assignment delays; incomplete event lists; user-defined function and
task calls; other system calls (`$past`, `$random`, ...); loops, waits, and
every other statement; pattern conditions and `case ... inside`; local and
automatic variables and other names that are not signals of the design.
Undriven internal signals and unconnected input ports also end the slice.

Inside the subset, operators that are not evaluated (for example `inside`
or `**`) and pure system functions keep every operand as a dependence; the
value is unknown and no guard that contains them prunes. The same holds for
array slices, sub-arrays, multi-dimensional and replicated assignment
patterns, whole-value assignments from anything but a same-shaped value or a
pattern, arrays above the element cap or with negative indices (one aggregate
signal, `ARRAY_NOT_EXPANDED`), and struct and array ports.

Bit demand (B-03). Nodes stay whole-signal, but the slicer records which bits
of each node it must explain. A constant part-select or packed field read
demands only its bits, an assignment that writes none of the demanded bits is
pruned, an assignment is overwritten only when later definite writes cover
every demanded bit it writes, and the hold edge appears only for demanded bits
that no definite write covers. When a node is reached again for more bits it is
expanded again; demand only grows, so this terminates. Carry edges still
compare whole values, as the contract requires.

B-02 deviates from its ticket in one place. The ticket asked for an unknown
read index to give the bitwise merge of all elements. IEEE 1800 section 7.4.6
says an index with an `x`/`z` bit or out of range reads all-`x`, and a merge
could claim known bits the simulator never produces, which could then prune a
branch the simulator took. `ArrayRead` therefore evaluates such a read to
all-`x`. Pruning is unaffected: an unknown index keeps every element through
its `index` guards.

### 1.1 How the slicer treats time

The original task statement described sequential edges as "the previous clock
edge `T - T_clk`". The spec forbids assuming a clock period (sections 6.1,
6.2, 15.1 item 6), so the slicer does this instead:

- It reads the real rising edges with `WaveformProvider.get_clock_edges` and
  maps cycle numbers of the failure's epoch to those times.
- A register that is `settled` at edge k depends on operands sampled `before`
  edge k: one `sequential` edge at one real clock edge.
- A register read `before` edge k carries back to the earlier edge that last
  wrote it: one `combinational` carry edge. Cycles where every write guard is
  false are skipped; `SliceOptions.expand_holds` disables the skipping.
- `before` at cycle 0 is the epoch boundary and ends the slice.
- A carry must start from a `settled` value, so the hold at the cycle 0 edge
  cannot be skipped like later ones. Instead, a register `settled` at cycle 0
  whose demanded bits provably held through that edge takes the origin of the
  value before cycle 0: the reset branch when it never assigns the register,
  otherwise the epoch boundary (B-12). Before B-12 that one hold added a
  storage boundary, so its unmet condition outranked the missing reset
  assignment whenever the failure came after cycle 0.

Do not reintroduce period arithmetic anywhere.

### 1.2 How suspects are ranked

The task statement asked for ranking of leaf nodes. Leaves of a complete slice
are only module inputs and reset-boundary state, so ranking leaves alone could
never name a faulty assignment. Every reached node is therefore a candidate,
candidates collapse to one per source location, and the order is: RTL
statements that compute a value, then statements that only forward one (the
right-hand side is a signal, a constant or run-time select of it, or one
element of an unpacked array), then slice ends;
fewer storage boundaries from the failure; state-changing register write or
unreset register, then combinational logic, then a write that changed
nothing, then a hold; fewer hops; a node reached through a control edge before
one reached through data; then signal path. See `_Slicer._rank`. The rank is
ordinal. Do not add scores or probabilities to the contract.

The forwarding tier and the unreset rule came from the end-to-end run on the
four benchmarks (review of PR #6). In case 03 two register copies outranked
the line that computed the wrong value. In case 04 a register that the reset
branch never assigns was located at its declaration. When `SliceOptions`
names the reset, such a register is now located at the reset branch: the
condition line through the branch's last assignment. Nothing is detected by
name, so without `reset_path` and `reset_active` the value stays an epoch
boundary. Track C must pass both from the manifest.

#### Known limit: a defect that fails to change a bit, seen much later

Ranking prefers the most recent logic (fewer storage boundaries). A defect
whose effect stays latent through later, correct writes therefore drops. The
held-out schedules of C07 (#30) show it on case 01: the mutant
`wptr <= {wptr[2], 2'b00}` fails to toggle `wptr[2]`. When later `wptr + 1`
writes come between the wrap and the failure, line 40 (the last write) ranks
first and line 38 drops to 5th or 10th. Crediting each bit to the assignment
that last changed it does not help: line 38 never changed `wptr[2]` (that is the
bug), and the later `+1` writes only copy it, so per-bit crediting points back
to reset. In the mutant, lines 38 and 40 both copy `wptr[2]`; no data-flow rule
singles out 38 without knowing an internal reference value, which the golden
model does not provide. Do not tune the ranking to the held-out schedules. A
real fix would need a new idea (for example, which statements could have
written a different value to the wrong bit) and its own ticket.

Held-out localization before B-12 (Track C, #30, run once): over 17 detected
mutants, top-1 6, top-2 12, top-5 15. B-12 changes case 04's ordering, so case
04's held-out rows are no longer held out for B-12; report any rerun as
post-change.

### 1.3 What the slicer refuses to guess

- A guard is opaque, and so unknown, when an operand reads a signal the trace
  cannot supply or contains a construct whose value is not computed (an
  unsupported call such as `$countones`). Either one evaluates to a fabricated
  `x`, which `===` or a two-state cast would turn into a known value. A
  recorded `x` is still evaluated.
- A value read before the last blocking write to it in the same combinational
  process is not the traced value. An `if` or `case` reads its condition
  before every statement it controls, so a write inside its own branch counts.
  Such reads prune nothing, are not followed, and set `truncated`
  (`INTERMEDIATE_VALUE`). B-06 is the real fix.
- A combinational process in which no assignment executes (an inferred latch,
  or an undriven path) ends the slice with `truncated` and
  `NO_ACTIVE_ASSIGNMENT`.
- A combinational process (not a continuous assignment) may keep bits from
  an earlier evaluation when it writes them on some paths but cannot be shown
  to write them on every path. If such a bit is one the slice follows, the
  slice ends there with `truncated` and `PARTIAL_ASSIGNMENT`. A bit is covered
  by its exact writes (bits known at elaboration): an unconditional one, one
  in an arm known to be taken, or an `if` with an `else` or a `case` with a
  `default` whose every possible arm writes it. Bits the process never writes
  belong to other drivers and are not checked against it.
- Any driver outside the modeled subset (section 1) is an unsupported
  driver; the slice stops there. Widen the subset only together with tests
  that check the new construct against simulation.
- `max_cycles_back` bounds the hold scan as well as the slice, and
  `max_nodes` bounds the report: no node is added past it.
- A hold scan is remembered per signal, demanded bits, and start cycle, so
  tracing a held value from every cycle of a long window stays linear.

## 2. Rules for every ticket

- Own only `src/causal/`, `tests/test_causal.py`, causal fixtures, and Track B
  docs. Do not edit `src/contracts.py`, `spec.md`, `pyproject.toml`,
  `uv.lock`, or shared fixtures. If a ticket needs a contract change, stop and
  raise it through the process in `spec.md` section 2.
- Do not import `src.waveform` or `src.app`.
- Never drop a construct silently. Anything not modeled becomes a diagnostic
  and either an `Unsupported` operand (dependencies kept, value unknown) or a
  driver with `unsupported` set (slice stops, `truncated` is set).
- An unknown or unobserved control value never prunes a branch.
- Never convert `x` or `z` to an integer, round a timestamp, or assume a clock
  period.
- Every report must construct as a valid `CausalReport`. Add a JSON round-trip
  assertion to at least one test per new scenario.
- Keep `test_evaluator_agrees_with_slang_constant_evaluation` passing, and
  extend its expression grammar whenever a ticket adds an evaluated construct.
  It caught a real sizing bug during the first implementation.
- Tests must exercise the engine. No import-only or placeholder tests.
- Verification, from the repository root, for every ticket:

  ```
  uv run mypy src/
  uv run pytest tests/test_causal.py -v
  uv run pytest
  ```

- Report results as they are. If something is skipped or still failing, say so.

## 3. Open items that need other tracks

These are not tickets for Track B to execute alone. Raise them with Dev 1.

| Item | Why it matters | Owner |
| --- | --- | --- |
| Line endings and source hashes | `SourceFile.sha256` is the hash of the file's bytes. On a Windows checkout with `core.autocrlf=true`, `tests/fixtures/mock_fifo_wrap.sv` hashes to `54b129d2...`; the LF form hashes to `76726bf9...`, which is what the fixture JSON records. The benchmark RTL has the same problem, so the mutant cases of `tests/test_e2e_benchmarks.py` fail on such a checkout and pass on an LF clone. A `.gitattributes` rule (`*.sv text eol=lf`) would make hashes identical on every platform. Still open. | Dev 1 |
| Trace names for array elements and unpacked struct fields | Tickets B-02 and B-03 need to know the path a provider gives `mem[2]` or `s.field`. The spec's path grammar allows `mem[2]`; simulators differ in what they dump. | Track A with Dev 1 |
| No wall-clock limit or cancellation in the causal step | Under 10 ms on the benchmarks, but a trace near the 256 MB cap could block the API's single worker (Track C handoff). A deadline in `SliceOptions` would be Track B's part. | Track C with Track B |

Resolved: `pyslang` is a locked runtime dependency (#14). Track C passes
`trace_scope`, `reset_path`, and `reset_active` from the manifest (#27).
`SourceFile.path` is relative to the analysis source root (spec decision 13,
#29). `SVA_LATENCY_ASSUMED` stays a diagnostic; the manifest gets no latency
field (agreed with Track C on #30).

## 4. Tickets

### B-01 Module instantiation

Goal: follow dependencies through module instances, so a failure on a top
output can be traced into a child's registers and back out to top inputs.

Do this first. It defines the hierarchical signal naming that every later
ticket builds on.

Current behavior: `_Lowerer._lower_instance` emits `UNSUPPORTED_INSTANCE` and
gives every signal connected to an instance output a `Driver` with
`process=INSTANCE` and `unsupported` set. Signals are named relative to the
top instance, and `_Lowerer._local_name` rejects anything in a nested scope.

Design:

1. Name signals by their path relative to the top instance: `u_core.q`,
   `u_core.u_fifo.wptr`. With `trace_scope="tb.dut"` these map to
   `tb.dut.u_core.q`, which is the spec's canonical form (section 3.1).
2. Lower each elaborated instance separately, by recursion over the instance
   body with a name prefix. Do not cache by module definition: two instances
   can differ by parameter.
3. Model each port connection as a combinational `Driver`:
   - input port: target is the child's internal signal, RHS is the connection
     expression lowered in the parent scope;
   - output port: target is the parent signal or signals, RHS is a `SignalRef`
     to the child's internal signal;
   - `inout`, `ref`, interface ports: keep the current unsupported driver;
   - unconnected input: no driver, and a diagnostic.
4. Clocks. `_Slicer._clock_matches` compares a register's clock with the
   failure's clock by trace path. A child's clock is its own signal
   (`u_core.clk`) driven from the parent. Add an alias map to `DesignGraph`,
   built from port connections whose expression is a plain whole-signal
   reference, and resolve clocks through it. A clock that passes through
   logic is not an alias and stays `UNSUPPORTED_CLOCK`.
5. Child port signals are `SignalKind.INTERNAL` with drivers. Only top-level
   inputs are slice leaves.

Tests to add:

- Two-level design in one file: failure on a top output, slice reaches a
  child register, crosses the clock edge, and ends at a top input. Assert
  node paths, edge types, and that the report round-trips.
- Same module instantiated twice with different parameter values and widths.
- Clock passed through two levels resolves; a gated clock does not, and the
  report is `truncated` with `UNSUPPORTED_CLOCK`.
- A child signal missing from the trace gives `SIGNAL_NOT_OBSERVED` and keeps
  both branches of any decision that reads it.
- The generated assertion binds with hierarchical references and elaborates
  with pyslang (see `compile_errors` in the tests).
- Multi-file design: `sources` lists both files with correct hashes, and
  suspect locations point into the child's file.

Out of scope: instance arrays, generate blocks (B-05), interfaces.

### B-02 Unpacked arrays

Goal: trace through memories element by element, so a wrong `out_data` in the
FIFO leads to the one write that stored the wrong word.

Current behavior: an unpacked array is one `SignalInfo` with
`is_aggregate=True`. A read lowers to `ir.ArrayElement`, which evaluates to
all-`x`. An element write is a `GuardedAssign` with `full_write=False`, so the
slicer keeps every write and the hold at every cycle. See
`test_unobserved_array_is_reported_and_its_writes_are_kept`.

Design:

1. Declare one signal per element for fixed-size unpacked arrays of integral
   elements, named `mem[0]` .. `mem[N-1]` (nested for more dimensions). Cap the
   element count (start with 4096); above the cap keep today's aggregate
   behavior and emit a diagnostic.
2. Read `mem[idx]`: add an IR node that holds the element references, the
   index expression, and the declared range. Evaluation: known index gives
   that element; unknown index gives the bitwise merge of all elements
   (`fourstate.merge` semantics); out-of-range gives `x`.
3. Pruning: add `GuardKind.INDEX` ("index equals i"). `collect_reads` returns
   the index operands as control reads and each element as a data read under
   its `INDEX` guard. The existing guard evaluation then prunes unselected
   elements, and an unknown index keeps them all.
4. Write `mem[idx] <= v`: emit one `GuardedAssign` per element with
   `full_write=True` and an extra `INDEX` guard. A constant index emits one.
   Last-write-wins, hold, and carry then work per element with no slicer
   change.
5. Array slices, whole-array copies, and assignment patterns stay
   `Unsupported` in this ticket, with diagnostics.

Depends on the cross-track answer about element paths (section 3). Until it
is settled, build traces in the tests with paths like `tb.dut.mem[2]`, which
the path grammar already accepts.

Tests to add:

- Extend the mock FIFO trace in the test (not the shared fixture) with
  `tb.dut.mem[0..3]`. Slice the `out_data` failure at cycle 4. Assert that
  only the selected element is retained, the others are pruned edges, and the
  slice reaches `in_data` at the cycle whose `wptr` addressed that element.
- Unknown read index keeps every element as `unknown`.
- Unknown write index keeps both the write and the hold for every element.
- Elements missing from the trace are reported and nothing is pruned on them.
- Element count above the cap falls back with a diagnostic.
- Add array reads with known indices to the differential evaluator test.

### B-03 Structs

Goal: field-precise dependencies for packed and unpacked structs.

Current behavior: a packed struct field read lowers to `ir.PartSelect` and
evaluates correctly. A field write is a partial write (`full_write=False`), so
it never overrides earlier writes and always keeps the hold edge. Unpacked
structs are aggregates with unknown value.

Design:

1. Bit-precise partial writes. Give `GuardedAssign` the written bit range
   (offset and width within the target) when it is constant. In
   `_Slicer._verdicts`, replace the single `blocker` with a covered-bits mask:
   an assignment is overwritten only when later definite writes cover all of
   its bits, and the hold edge appears only when some bit is left unwritten.
   This also improves ordinary part-select writes such as `q[3:0] <= a`.
2. Keep graph nodes at whole-signal granularity in this ticket. Put the field
   name in edge reasons and suspect reasons. Emitting part-select nodes with
   `CausalNode.bit_range` is a separate decision; raise it before doing it,
   because Track C renders nodes.
3. Unpacked structs: flatten to one signal per field, named `s.field`,
   reusing the element-signal approach from B-02. Lower whole-struct copies
   and assignment patterns field by field.
4. Packed unions: reads already work as part-selects; treat writes through
   one member as partial writes of the overlapping bits.

Tests to add:

- Packed struct register with two fields written under different enables:
  slicing a failure on one field's consumer does not retain the other
  field's write.
- Two partial writes that together cover the signal remove the hold edge;
  one alone keeps it.
- Unpacked struct copied whole, then one field read: only that field's
  source is retained.
- Add packed field reads and part-select writes to the differential test
  where slang can evaluate them.

### B-04 Graph serialization

Goal: save a `DesignGraph` to JSON and load it back, so elaboration can be
cached and a graph can be inspected or attached to a bug report.

Do this after B-01 to B-03, so the IR node set is settled before it is frozen
into a format.

Design:

1. New module `src/causal/graph_io.py` with `dump_design_graph(graph) -> str`
   and `load_design_graph(text) -> DesignGraph`.
2. JSON only. No pickle. Each IR node is a tagged object, for example
   `{"k": "binary", "w": 4, "s": false, "op": "+", "l": {...}, "r": {...}}`.
   Use one explicit encoder and decoder table keyed by node class, so adding a
   node class without serialization fails a test.
3. Top-level fields: `format_version` (start at 1), `pyslang_version`,
   `top_module`, `sources`, `signals`, `drivers`, `diagnostics`. Derive
   `edges` on load with `_structural_edges` rather than storing them.
4. Follow the contract's JSON conventions (section 11): snake_case, unknown
   fields rejected, missing fields rejected, any other `format_version`
   rejected.
5. Deterministic output: stable key order, signals and drivers in
   declaration order. `Guard.decision` numbers are already assigned in
   traversal order and are stable.
6. Staleness: add `verify_sources(graph, root) -> tuple[Diagnostic, ...]` that
   re-hashes each source file and reports a mismatch. Never slice silently
   against a stale graph.
7. This is an internal Track B format. It is not a shared contract. If Track
   C wants to consume it, propose that through `spec.md` section 2.

Tests to add:

- `load(dump(g)) == g` for the FIFO, the test `DUT_SV`, and one design per
  construct added in B-01 to B-03.
- Two independent builds of the same sources dump byte-identical text.
- Slicing with the loaded graph gives a report whose JSON equals the report
  from the original graph.
- Unknown `format_version`, an unknown field, and an unknown node tag are
  each rejected with a clear error.
- A test that enumerates every `ir.Expr` subclass and fails if one has no
  encoder.
- `verify_sources` reports an edited file.

## 5. Backlog after B-04

Listed in suggested order. Write a full ticket before starting any of them.

| Id | Item | Note |
| --- | --- | --- |
| B-05 | Generate blocks and instance arrays | Names like `gen[2].u.q` are already valid paths. Today `UNSUPPORTED_SCOPE` is repeated in every report of the design. |
| B-06 | Statement-ordered lowering inside a process | Blocking temporaries in clocked blocks and reads of intermediate values in `always_comb` (`IMPRECISE_DRIVER` today). Needs versioned values per statement. |
| B-07 | Exact case feasibility | With an unknown selector, tri-state evaluation keeps the default arm even when every resolution matches an item. Enumerate resolutions of the unknown bits, with a cap. |
| B-08 | Constant-controlling-value pruning | For example `a && b` with `a` known 0. The brief requires a bit-specific proof before removing an operand; today nothing is removed. |
| B-09 | Functions | Inline pure functions, or keep a conservative summary. |
| B-10 | Assertion validation | Run the candidate in a simulator and set `validated` or `failed` with the tool name. Needs Track C's simulator runs. |
| B-11 | Slice past a dump gap | `CLOCK_EDGES_UNAVAILABLE` confines the slice to the failing cycle. Query edges after the last gap instead. |
| B-12 | Hold at the cycle 0 edge (done) | A register settled at cycle 0 that provably held through that edge takes the origin of its value before cycle 0, as holds at later edges already did through the carry skip. Found from Track C's held-out run of case 04 (#30); tested on a made-up design (section 1.1). |

Out of scope for schema 1, per `spec.md` section 1.2: several clock domains,
asynchronous reset analysis, latches, delays.
