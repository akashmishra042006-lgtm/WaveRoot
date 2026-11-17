# Validation record

Executed November 16, 2026 using Node.js v24.22.1 on Windows.

```text
node tools/validate.mjs
28 package checks passed. No HDL simulator or WaveRoot implementation was run.
```

The checks cover eight synthetic report shapes and semantic invariants; twelve invalid-input cases; exact large timestamps; suspect/node/source consistency; four independent reference calculations for the directed benchmark examples and intended mutation behavior; and relative Markdown links.

The arithmetic check corrected the MAC mutant expectation before delivery: signed 16-bit wrapping of -48000 gives **+17536**, binary `0100010010000000`. Correct saturation gives -32768. The output first exposes that difference at cycle 3 under the specified pipeline.

The schema checker implements exactly the keywords used by this draft and rejects unsupported schema keywords. It is not a general JSON Schema engine. When the team's canonical models exist, validate these fixtures with those models and use the selected production schema validator as well.

Not executed: Verilator or Icarus compilation/simulation; Python golden-model implementation; Track A/B integration; React build/browser tests; performance benchmarks; real source localization; assertion compilation or replay. Those are explicitly assigned to later tickets. The current repository contains a skeleton and Track B brief, not those implementations.

The package's fixture graphs and source excerpts are illustrative. Passing these checks establishes internal handoff consistency, not hardware-simulation correctness or product accuracy.
