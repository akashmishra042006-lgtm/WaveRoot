# UI usability and readability plan (Track C)

Goal: someone new to WaveRoot answers three questions in under a minute,
without scrolling past the first screen: what failed and when; which line most
likely caused it; what evidence supports that.

Constraints (from `docs/track-c/playbook.md`): times stay `BigInt`, bits stay
strings and x/z are never numbers; waveform tracks stay a few memoized paths;
panels scroll internally; honesty labels (real vs synthetic, "not validated",
"ranks are ordinal") stay; fixtures are regenerated, never hand-edited.

Baseline (real FIFO report, 1440x900, `main` at c0d3dd9): page 4,959 px tall;
waveform starts 4,385 px down; 10 suspect cards at 45-64 words each, each with
"Confidence: not supplied"; graph draws 51 nodes at 32% zoom (12 px labels
render at about 4 px); long source lines run off the source panel.

## Phase 1: wording and noise (done)
- Removed "Confidence: not supplied" (the contract has no confidence field).
- Suspect cards lead with the verified source line (only when the file's
  SHA-256 matches the report); the reason is two lines until the card is
  selected (full text stays in the DOM).
- Plain labels: "Sampled: after the clock edge settles", "Time (exact)".
- Source lines wrap instead of running off the panel.
- Default radix "Auto": single bits binary, buses hex, exact bits always shown.
- Hosted page: one welcome block; one line once a report is open.
- Result on the same report: page 4,273 px (-14%), suspect list -24%, cards
  -31% each, no sideways overflow in the source panel.

## Phase 2: one-screen layout
Failure card, suspect #1 with its source line, and a waveform strip centred on
the failure all visible at 1440x900. Top 3 suspects in full, the rest behind
"Show more". Assertions, pruned branches and "also mismatched" collapsed by
default. A calmer header with actions grouped at narrow widths.

## Phase 3: a readable graph
Default to the causal path from the selected suspect to the failing signal;
"Show full graph" for everything. Minimum zoom keeps labels at least 11 px.
Selecting a suspect highlights its path and moves source and waveform; the
keyboard node list stays.

## Phase 4: first-time guidance
A dismissible "how to read this" strip; plain-language sample names; "mutant"
and "correct" explained where they appear; a three-step "run it yourself" box
on the hosted site.

## Phase 5: accessibility and polish
WCAG AA contrast for secondary text, an optional light theme following the
system setting, visible focus on new controls, consistent number format.

## How we know it is better
CI layout assertions (one-screen target, minimum graph label size, no clipped
source), axe-core contrast checks, the existing tests, and a small usability
test (3-5 people, at least one outside the team; three timed tasks on the
hosted FIFO example, before and after). Nobody has been observed using the UI
yet; the problems above come from screenshots and measurements.
