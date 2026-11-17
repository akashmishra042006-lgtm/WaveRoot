# Track C technical brief — benchmarks, orchestration, and interface

Status: implementation-ready requirements proposal; pending reconciliation with the real repository's shared contracts.

## Goal

Give a hardware engineer a reproducible route from a failing simulation to the first observed divergence, a bounded set of upstream suspects, and the corresponding waveform and RTL source. A suspect is evidence for investigation, not a proven unique cause. Track C makes the workflow reproducible and inspectable; Tracks A/B supply waveform alignment and causal analysis.

Person 3 owns benchmark inputs and independent expected outputs, simulator process management, the CLI/local service, and the interface. Person 1 owns waveform parsing and first-failure comparison. Person 2 owns elaboration, structural dependencies, temporal slicing, and candidate assertions. Shared schemas and clock semantics are jointly agreed.

## MVP scope

- Local execution, one explicitly declared clock, active-low synchronous reset, fixed integer time ticks, synthesizable synchronous RTL, and a documented supported construct subset.
- VCD first. FST remains an adapter extension after correctness gates pass.
- Four benchmark pairs: FIFO wrap, elastic buffer stall, signed MAC saturation, and unreset mux control. Each pair has one intentional semantic mutation and a correct variant.
- Verilator for the first three; Icarus with a supported SystemVerilog subset for true `X` propagation. Record actual versions and reject unsupported simulator/case combinations.
- One CLI command analyzes a manifest; the interface can open its saved report. A local service can later expose the same application layer.
- A report is readable without cloud access. Raw HDL and traces stay local by default.
- Return `inconclusive`, `unsupported`, or `error` explicitly when evidence is missing. Never turn an incomplete run into a pass.

Defer arbitrary multi-clock designs, automatic protocol discovery, universal SV support, gate-level timing, automatic RTL patches, cloud collaboration, mandatory SVA synthesis, and guaranteed root-cause identification.

## Benchmarks

| ID | Contract | Intentional defect | First required observable |
|---|---|---|---|
| case_01_fifo_wrap | Depth 4, 8-bit FIFO; accepts replacement on a full pop/push edge | Write pointer wraps its address but fails to toggle its wrap bit | At cycle 3 after the fourth accepted write, `full` should be 1 but is 0 |
| case_02_axi_skid | One-entry ready/valid elastic buffer; payload and valid held through stall | Clears output valid during a stall | At cycle 1 post-edge, pending output valid drops despite no transfer |
| case_03_mac_overflow | Signed int8 product into a signed 16-bit saturating accumulator, one output register stage | Narrows the wide sum before overflow detection | At cycle 3 post-edge, expected -32768, defective result +17536 |
| case_04_alu_xprop | 8-bit mux/ALU control resets to select operand A | Reset omits the select register | At cycle 0 post-edge, expected `00000000`, actual `xxxxxxxx` under a four-state simulation |

The second case keeps the historical `axi_skid` name but only claims a one-entry elastic ready/valid contract, not complete AXI compliance or a two-entry registered-ready skid architecture. Detailed cycle tables and variant requirements are in `track-c/benchmark-spec.md`.

## Non-negotiable timing contract

For these fixtures, `timeunit=1ns`, `timeprecision=1ps`, period 10 ns. Drive inputs on the falling edge; deassert reset on a falling edge after two complete reset cycles. Define the first subsequent rising edge as epoch 0, cycle 0 at 5000 ps in the normalized fixture timeline. Store any offset from simulator time explicitly.

Capture `pre_edge` at one precision tick before the rising edge and `post_settle` one precision tick after it. Inputs/control must be stable across that interval for these benchmarks. Acceptance uses `pre_edge` ready/valid/data. Registered-result comparison uses `post_settle`. Same-cycle pre/post samples must never be collapsed into an unqualified `value_at(T)` query.

This is a constrained benchmark convention, not a universal VCD delta-cycle reconstruction algorithm. Reject traces that cannot establish the required sampling discipline. Track B must query the inputs consumed by the failing register update; blindly subtracting a clock period can select the wrong values.

## Golden-model and benchmark independence

Golden models are simple Python behavioral specifications: queue operations, accepted transaction lists, unbounded integer arithmetic with explicit clamping, and a specified reset state. They must not read the buggy output, the causal report, mutant source, or grading metadata. Feed both correct and buggy RTL the same stimulus schedule. When handshake behavior diverges, advance expected state according to the reference interface and record expected versus observed acceptances separately.

Emit cycle observations for ready/valid/status as well as ordered transaction expectations. A pure transaction matcher can miss a dropped final output or an illegally deasserted valid. Every run has a bounded drain window and classifies missing/extra outputs explicitly.

Keep bug locations and mutation patches in a grader-only directory. Do not place `BUG HERE` comments in the analyzed HDL. Grade exact source spans from a revision-matched manifest, not assumed line numbers. Add alternate schedules and non-buggy controls so success on four authored examples is not presented as general accuracy.

## Required artifacts per benchmark

`README.md`, common interface/harness, correct/buggy variants, deterministic stimulus, independent `golden_model.py`, a simulator manifest, and tests. Generated output includes VCD, expected and observed JSONL, stdout/stderr, build/run exit codes, simulator/compiler versions, source and input hashes, seed, sampling convention, cycle count, and the final status. Grader-only files remain separate from analysis inputs.

## Interface outcome

The user sees the first failure, ranked suspects with reasons, a time-unrolled dependency graph, relevant waveforms, and revision-matched source. Choosing a suspect selects its node, signal, cycle/phase, and source span. Waveform panning cannot silently change the failure anchor. All screens label synthetic fixtures, incomplete analysis, unknown values, missing signals, and unvalidated assertions accurately.

## Gates before the next phase

1. Team contract accepted with a representative fixture and clock-phase tests.
2. Each correct variant passes; each mutant fails the intended independent checker at the documented first observation. No simulator/build error counts as a found design bug.
3. One real FIFO trace passes through A → B → C before the full dashboard is built.
4. Interface handles pass/fail/inconclusive/error and selection synchronization; no invented certainty percentages.
5. Repeated runs produce semantically identical results after excluding wall-clock/runtime metadata. All four required simulator jobs execute; a skip is not completion.

For detailed tasks and prompts, read `track-c/execution-plan.md` and `track-c/agent-prompts.md`. Official source links and the rationale for corrections appear in `track-c/sources.md`.
