# Track C interface specification and frontend handoff

Implementation prerequisite: accepted shared contract or a versioned adapter to it. The proposed JSON fixtures are sufficient for component design, not proof that the actual backend speaks this format. Retain the repository's existing framework and package manager. If there is no frontend, React + TypeScript is the proposed baseline; React Flow and Canvas are optional implementation choices, not requirements to add dependencies blindly.

## Primary journey

Open a saved report → understand the first mismatch → select a suspect → inspect its evidence in the graph, waveform, and source → export the report or copy a clearly labeled assertion candidate. Opening a report must never launch simulation automatically.

## Layout

Desktop: a compact header with run/case name, outcome, simulator, and fixture/real-data label. Below it, a fixed-width suspect list beside the graph and source view. A waveform strip spans the bottom. Show enough source around the selected span to explain the expression. At tablet width, stack source below graph; at narrow width, use accessible panel tabs and preserve the same selection. Avoid a decorative hero that displaces the actual diagnosis.

The first-failure strip displays signal path, cycle, phase, time with units, expected and actual bit patterns, and mismatch class. The main action is “Jump to first failure.” Show full raw unknown bits alongside any hex view; never display an X/Z value as zero.

## Panel behavior

| Panel | Must show | Interaction |
|---|---|---|
| Suspects | Rank, relative file/line or “Source unavailable”, evidence reason | Selects associated node and its observation; does not change the original failure anchor |
| Graph | Signal + cycle/phase, value, dependency kind and pruning status | Select node; pan/zoom; keyboard-accessible equivalent list |
| Waveforms | Signal names, exact values, unit-labeled time, separate failure and selection cursors | Select signal/time; zoom; retain cursor when data window changes |
| Source | Revision-checked text, line numbers, highlighted span | Copy location; show unavailable/revision-mismatch state explicitly |
| Assertions | Candidate text and validation result | Copy; no “verified” badge without tool/result evidence |

Graph direction is cause → effect. Solid lines denote combinational dependencies; dashed lines denote sequential dependencies with explicit source/target observations. Label retained, pruned, and unknown-control edges in text/tooltips so color is never the only distinction. Initially hide pruned branches; a toggle reveals them. Preserve search truncation and unsupported-feature indicators next to the graph.

## Single selection state

Use a shared state object: `selectedSuspectId`, `selectedNodeId`, `selectedSignalId`, `selectedObservation`, `sourceLocation`, `window`, `radix`, and `showPruned`. The immutable `failure` is report data, not mutable selection state.

1. Selecting a suspect chooses its designated node, signal, observation, and source span; recenter window only if that observation lies outside it.
2. Selecting a node updates signal/observation/source. Highlight a suspect only if that node maps to one; clear stale suspect selection otherwise.
3. Moving the waveform cursor updates selected time, not the failure anchor. If no exact node observation matches, clear node/suspect highlight while retaining the selected signal.
4. “Jump to first failure” restores the failure signal/time and reveals its node when present. Do not choose an unrelated node with a nearby timestamp.
5. Loading another report resets graph selection and validates the schema before showing data. Unknown schema versions get an explanatory error.

Absolute tick strings remain exact. Use BigInt for comparisons/subtraction, then convert bounded relative offsets into pixels. Wide bitvectors also remain strings/BigInt; signed decimal is a display transform only. Masked/unknown values display `X`/`Z` or a mixed bit string, not an arbitrary integer.

## States that must be designed

- Empty: choose a saved report or explicitly labeled sample.
- Loading: show stage with cancel where supported; never fake percent complete.
- Pass: show scope/comparison count when available, with no failure or ranked suspects.
- Failed/localized: show first failure and evidence chain.
- Failed/unlocalized: show the observed mismatch and a reason localization is unavailable.
- Inconclusive/unsupported: prominent limits with useful partial evidence retained.
- Infrastructure error: simulator/parser/log information without blaming RTL logic.
- Missing source, missing waveform signal, unknown values, empty graph, oversized graph, timeout, cancellation, and offline API.
- Synthetic fixture mode: persistent “Sample data” badge and separate fixture identifier.

## Acceptance scenarios for the frontend agent

Use the repository's browser test harness; add one if a real frontend is being introduced. Prefer behavior assertions over pixel snapshots that merely mirror implementation.

1. Open the FIFO fixture, select its first suspect, and verify node ID, `tb.dut.wptr`, cycle 3 post-settle, source span, and selected time agree. Failure anchor remains `tb.dut.full`.
2. Drag/click the time cursor elsewhere; the failure marker stays fixed. Jump-to-failure restores it.
3. Open the X fixture and display actual `xxxxxxxx` with an “Unknown value” label.
4. Open a pass report: no stale graph or suspects from the last failure remain.
5. Open inconclusive and source-unavailable data: retain evidence and expose limits; no misleading green pass.
6. Load the large-time fixture (>2^53): changing selected time by one tick remains distinguishable.
7. Invalid width, dangling node ID, and unknown schema fail visibly before rendering.
8. Keyboard-only selection, tab switching, copy controls, zoom controls, and jump-to-failure all work. Announce selection changes politely; do not announce every hover.
9. At 1440, 1024, 768, and 375 px widths, controls remain reachable and text does not overlap. Horizontal waveform scrolling is confined to its panel.
10. Benchmark a bounded viewport: 100 visible signals, 100,000 visible transitions, 1,000 graph nodes. Measure on a named machine; target selection response below 100 ms after initial load. Reduce/window data when limits are exceeded. This is an engineering target, not a measured claim for this package.

## Boundaries

Do not reimplement the causal algorithm in the frontend. Do not infer omitted signals are zero. Never execute assertion text or source code from a report. Do not construct editor links from unchecked arbitrary paths; default to an internal source viewer. Do not claim that the top-ranked suspect is a proven root cause or that a ranking score is statistical confidence.

The frontend deliverable is complete only when it builds, its interaction tests pass, it has been visually inspected, and it can consume at least one real report from the accepted backend contract. A polished fixture demo alone is a milestone, not integration completion.
