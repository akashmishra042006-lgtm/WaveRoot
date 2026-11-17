# Track B: dynamic backward cone of influence

`src/causal/` builds an elaborated SystemVerilog dependency graph with `pyslang`
and specializes backward slices using four-state VCD values. A slice identifies
candidate causes, not proof of causality or of intended behavior.

## 1. AST → directed graph

Visit the **elaborated semantic AST**, resolving instances, parameters, generates,
ports, symbols, widths, and signedness. `ContinuousAssign` and `ProceduralBlock`
are semantic symbol kinds; distinguish procedural kinds `AlwaysComb` and
`AlwaysFF`. Check concrete binding names against the pinned `pyslang` version.

Vertices represent signal bits/ranges and intermediate expression/assignment
versions. Edges run **source → dependent destination**; backward slicing follows
incoming edges. Store timing class, data/control role, path guard, source location,
and clock domain/polarity. Include dependencies from selects, dynamic indices,
called functions, and port connections.

| AST construct | Mapping |
| --- | --- |
| `ContinuousAssign`: `assign y = expr;` | Expression reads → `y`: **COMBINATIONAL**, backward offset **0 cycles**. Mux selectors contribute control edges. |
| `ProceduralBlock` / `always_comb` | Lower ordered statements to expression versions and guarded merges. Reads and controls → results: **COMBINATIONAL**, **0 cycles**. Resolve blocking assignments in execution order and preserve last-executed-write priority. |
| `ProceduralBlock` / `always_ff` | Construct combinational next-state expression `D_q`, then `D_q → q`: **SEQUENTIAL**, backward offset **−1 clock cycle**. Enables/reset guards feed `D_q`; a non-writing path contributes `D_q = q`. |

In `always_ff`, resolve blocking temporaries in order and evaluate nonblocking
RHSs at their execution points; preserve last-executed nonblocking write priority.
Latency belongs to the storage boundary, not to each statement. Track partial
writes and conservatively retain all possibly selected bits for unknown indices.
Inline pure functions or use conservative dependency summaries.

**Time contract:** for pre-edge sampled state and inputs,
`q[k+1] = F(q[k], u[k])`; backward traversal from sampled `q[k+1]` reaches `D_q[k]`.
For a **post-NBA VCD query at T**, locate the clock event that produced the value
and evaluate operands immediately **before that event**. Do not subtract one VCD
time unit or blindly read inputs at the previous clock edge. Holds may require
several backward cycle steps to find the last write.

Asynchronous resets require event edges, not a −1-clock approximation. Explicitly
model or flag latches, delays, multiple drivers, and cross-domain paths requiring
richer timing. Never silently label storage combinational. Clocks are event
dependencies. Key traversal visits by vertex, time/event, and bit mask; handle
zero-cycle loops with a fixed point.

## 2. Exact branch-pruning rules at T

Evaluate the **whole control expression** using SystemVerilog four-state,
width, signedness, and cast rules. Map elaborated names/ranges to VCD identifiers,
honor aliases/timescale, and use the last value at or before the selected event.
Sequential guards use the pre-update event above; combinational guards use their
relevant evaluation at T. Missing signals/history are **unavailable**, not zero.
VCD may omit scheduling-region and delta-cycle detail: reconstruct procedural
temporaries from the AST; if execution ordering remains ambiguous, keep all
feasible paths. Settled values at T need not equal the values a process read.

Default to **conservative unknown-control slicing**. Keep control dependencies
even after selection. Literal four-state simulation replay is a separate policy.

| Construct | Selection and pruning |
| --- | --- |
| `if (c) A; else B;` | Integral truth: any bit 1 → true; all bits 0 → false; otherwise X/Z → unknown. True keeps A; false keeps B. Unknown/unavailable keeps both conservatively. Literal standard simulation takes B for an unknown condition (X optimism); label this replay policy explicitly. Missing `else` uses the previous procedural definition, or hold if none exists. |
| Ordinary `case (s)` | Literal replay matches exact four-state values, including X/Z, after language-defined sizing. Select the first matching item in source order; otherwise `default`, otherwise the non-writing path. X is not a wildcard. |
| `casez` / `casex` | Same first-match priority. `casez` masks Z/`?` bits in either operand; `casex` masks X/Z/`?` bits in either operand. Overlapping items still obey source order. `unique`/`priority` add checking obligations, not justification for pruning feasible paths. |
| Case controls containing unknowns | Conservatively retain literal replay outcomes **and** all first-match outcomes feasible when uncertain X/Z bits resolve to 0/1, using the actual case flavor. Include default/non-writing only if some feasible resolution matches no item. Use symbolic feasibility or enumeration, preserving correlations between repeated reads. Unavailable values retain all feasible outcomes. |
| Integral `c ? a : b` | True keeps a; false keeps b. Unknown control requires both arms: after sizing/coercion, equal four-state bits survive and unequal bits merge to X. Never implement an unknown ternary as an `if` choosing b. Unsupported operand types or unavailable controls retain both arms. |

Apply rules recursively with enclosing guards. Preserve previous definitions on
non-writing paths. Equal observed arm values alone do not prove their dependencies
irrelevant; further removal requires an expression-level or bit-specific proof.

## 3. SVA templates from a root transition

Generate assertions in or bind a checker to the relevant domain. Substitute
resolved signals, width-correct old/new values, path guards, and a specified
expected result. Trace-derived expected behavior is a **candidate property**,
not an independent correctness oracle.

These templates assume active-low reset and scalar `root`; declare typed
`EXPECTED_VALUE` and integer `K >= 1` parameters for the target checker.

```systemverilog
bit past_valid = 1'b0;
always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) past_valid <= 1'b0;
  else        past_valid <= 1'b1;
end

// Exact known 0 -> 1 transition, avoiding startup/reset history artifacts.
sequence cause;
  past_valid && ($past(root) === 1'b0) && (root === 1'b1)
             && (path_guard === 1'b1);
endsequence

// Zero storage boundaries: same sampled cycle.
a_comb: assert property (@(posedge clk) disable iff (rst_n !== 1'b1)
  cause |-> (!$isunknown(sink) && (sink == EXPECTED_VALUE)));

// K storage boundaries with fixed latency in this clock domain.
a_delayed: assert property (@(posedge clk) disable iff (rst_n !== 1'b1)
  cause |-> ##K (!$isunknown(sink) && (sink == EXPECTED_VALUE)));

// Example for q <= d when enabled: capture d at the launching sample.
a_register: assert property (@(posedge clk) disable iff (rst_n !== 1'b1)
  ((en === 1'b1) && !$isunknown(d))
  |=> (!$isunknown(q) && (q == $past(d))));

// Detect vacuous success due to an unreachable antecedent.
c_cause: cover property (@(posedge clk) disable iff (rst_n !== 1'b1) cause);
```

For falling/vector transitions, compare `$past(root)` and `root` to exact old/new
values; `$rose(vector)` observes only its least significant bit. For X-related
causes, explicitly use `$isunknown(root)`. Replay checks may use `===` for an
expected X/Z pattern; functional checks should normally require known outputs.

Concurrent SVA samples in the preponed region. An NBA register update is visible
at the next sampled edge, hence `|=>` above. `|->` checks a settled combinational
relation at the sample, not a post-NBA change on that edge. Reconcile VCD events
with this contract before generating latency. Between-edge glitches need an
explicit observation event/checker. Later-cycle enables must appear at their
respective cycles; holds and variable latency cannot be inferred from graph
depth alone. Multiple clock domains require explicit multiclock semantics.
