# Track C — start here

Person 3's pre-contract handoff for benchmarks, orchestration, and the debugging interface.

1. Read [the Track C brief](../brief_track_c.md).
2. Reconcile [the contract proposal](contract-proposal.md) with all three track briefs, using [the repository alignment note](repository-alignment.md).
3. Use [the agent prompts](agent-prompts.md) in order. The first implementation is the FIFO correct/mutant pair.
4. Follow [the execution plan](execution-plan.md) and [review gates](review-gates.md).

Detailed requirements: [benchmarks](benchmark-spec.md), [interface](ui-spec.md), and [official sources/corrections](sources.md).

The `proposal/` directory contains a draft report schema, eight synthetic reports, directed benchmark vectors, and dependency-free checks. These are preparation artifacts, not simulation evidence or shared production contracts. The schema is intentionally named `track-c-proposal-0.1`.

From the repository root:

```sh
node docs/track-c/proposal/tools/validate.mjs
```

All 28 package checks pass. See [validation scope](proposal/VALIDATION.md): no RTL simulator, production application, or frontend has been run by this handoff. Existing Track B work and configuration are preserved. The shared contract, real benchmark implementation, and production UI follow through the documented gates.
