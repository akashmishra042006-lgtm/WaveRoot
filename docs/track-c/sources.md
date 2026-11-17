# Sources, corrections, and assumptions

Official documentation consulted November 16, 2026. Live documentation can change; implementation must record the installed tool versions. These sources support simulator and scheduling facts. They do not validate the product's market, model rankings, or a promise of root-cause accuracy.

| Source | Verified fact | Design consequence |
|---|---|---|
| [Verilator input languages — unknown states and assertions](https://verilator.org/guide/latest/languages.html) | Verilator is mostly two-state; X initialization/assignment options substitute two-state values. Assertion support is partial. | Require a separate four-state run for the X benchmark. Keep assertion generation optional and explicitly validate supported constructs. |
| [Verilator command options](https://verilator.org/guide/latest/exe_verilator.html) | Explicit VCD/FST trace flags and timing support are documented; timing-enabled builds have compiler requirements. | Probe versions/features and use explicit trace format and timing flags in benchmark manifests. |
| [Verilator trace FAQ](https://verilator.org/guide/latest/faq.html) | Tracing requires build-time support and trace dumping; generated `--binary` programs support the documented dump workflow. | Capture real trace artifacts, not just successful compilation; test hierarchy visibility. |
| [Icarus command-line flags](https://steveicarus.github.io/iverilog/usage/command_line_flags.html) | Language generation is selected explicitly; SystemVerilog feature/assertion support has limits. | Use a small supported subset and procedural benchmark checkers; test required features on the installed version. |
| [Icarus simulation workflow](https://steveicarus.github.io/iverilog/usage/simulation.html) | Compilation/elaboration and execution are distinct stages; instance hierarchy is established during elaboration. | Preserve build/run statuses separately and carry an explicit signal map. |
| [cocotb timing model](https://docs.cocotb.org/en/stable/timing_model.html) | A signal edge does not generally imply its dependent HDL has finished updating; end-of-timestep observations are distinct from edge observations. | Define sampling phase explicitly. The ±1 ps controlled harness convention is our design choice, not a cocotb requirement. |

## Corrections to the shared Gemini plan

1. **An X-propagation test cannot be credited from ordinary two-state simulation.** The selected four-state lane must demonstrate unknown bits in a real trace.
2. **A sequential dependency is not always “read the previous clock edge.”** The register consumes values at a specific sampling phase; encode source and target observations.
3. **A waveform slice does not prove a unique root cause.** Preserve uncertainty, unsupported constructs, and incomplete visibility. Candidate ranking is a debugging aid.
4. **Golden models must be independent.** Correct/mutant pairs and independent oracles prevent circular validation.
5. **Transaction comparison alone is incomplete.** Check valid-hold rules, reset epochs, extra outputs, and bounded missing outputs.
6. **A causal graph need not always be a DAG.** Time-unroll sequential feedback; expose same-time cycles and unsupported constructs.
7. **Line numbers require source identity.** A source hash and span prevent navigation into a different revision.
8. **Generic generated SVA is a stretch goal.** Treat candidates as unvalidated until compiler/runtime evidence exists. It is not generally appropriate to call arbitrary assertions synthesizable.
9. **Contracts unblock development, not all integration.** Integrate a real FIFO case early; reserve explicit dependency gates for A/B availability.
10. **No model benchmark claims were relied upon.** The user's model names serve as workflow labels. This package does not validate the shared conversation's benchmark percentages, model identifiers, routing plugins, effort syntax, or commercial claims.

## Assumptions to reconcile with the team

One clock; synchronous active-low reset; explicit pre-edge/post-settle sampling; VCD first; exact integer timestamps; no automatic public hosting; source-aware local UI; ordinal suspect ranking. These defaults intentionally keep the initial implementation testable. Any existing team decision takes precedence after checking its implications consistently across all tracks.
