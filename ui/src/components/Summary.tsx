import type { Dispatch, ReactNode } from "react";
import type { Loaded, Mode } from "../model/types";
import { formatTime, KIND_LABELS, PHASE_LABELS, type Radix } from "../model/values";
import type { Action } from "../state/selection";
import { Diagnostics, StatusBadge, Value } from "./common";

export function Header({ doc, children }: { doc: Loaded | null; children: ReactNode }) {
  return (
    <header className="app-header">
      <div className="brand">WaveRoot</div>
      {doc && (
        <div className="run-summary">
          <span className="run-title" data-testid="run-title">{doc.title}</span>
          <StatusBadge status={doc.status} />
          {doc.run?.simulator && (
            <span className="meta">
              {doc.run.simulator}
              {doc.run.tool_version ? ` · ${doc.run.tool_version}` : ""}
            </span>
          )}
          {doc.fixture ? (
            <span className="badge sample" data-testid="sample-badge" title={doc.fixtureNote ?? undefined}>
              Sample data · {doc.fixtureId}
            </span>
          ) : (
            <span className="badge real" data-testid="real-badge">
              {doc.kind === "run_record" ? "Real run data" : "Analysis report"}
            </span>
          )}
        </div>
      )}
      <div className="header-actions">{children}</div>
    </header>
  );
}

/** The immutable first failure, with the main action. */
export function FailureStrip({ doc, radix, dispatch }: { doc: Loaded; radix: Radix; dispatch: Dispatch<Action> }) {
  const failure = doc.failure;
  if (!failure) return null;
  const others = doc.primaryMode ? doc.comparisons[doc.primaryMode]?.additional_mismatches ?? [] : [];
  return (
    <section className="failure-strip" aria-label="First failure" data-testid="failure-strip">
      <div className="failure-head">
        <h2>First failure <span className="hero-eyebrow">/ FAILURE ANCHOR</span></h2>
        <span className={`badge kind-${failure.kind}`} data-testid="failure-kind">{KIND_LABELS[failure.kind]}</span>
        <button type="button" className="primary" onClick={() => dispatch({ type: "jumpToFailure" })}>
          Jump to first failure
        </button>
      </div>
      <dl className="failure-fields">
        <div><dt>Signal</dt><dd><code data-testid="failure-signal">{failure.signal_path}</code></dd></div>
        <div><dt>Observation</dt><dd>epoch {failure.observation.epoch}, cycle {failure.observation.cycle}</dd></div>
        <div><dt>Phase</dt><dd>{failure.observation.phase} ({PHASE_LABELS[failure.observation.phase]})</dd></div>
        <div><dt>Tfail · exact time</dt><dd data-testid="failure-time">{formatTime(failure.observation.time_ps)}</dd></div>
        {failure.transaction_index !== null && (
          <div><dt>Transaction</dt><dd>#{failure.transaction_index}</dd></div>
        )}
        <div><dt>Expected</dt><dd><Value value={failure.expected} radix={radix === "bin" ? "hex" : radix} testId="failure-expected" /></dd></div>
        <div><dt>Actual</dt><dd><Value value={failure.actual} radix={radix === "bin" ? "hex" : radix} testId="failure-actual" /></dd></div>
      </dl>
      {others.length > 0 && (
        <p className="also">
          Also mismatched at this observation (declaration order):{" "}
          {others.map((o) => <code key={o.signal_path}>{o.signal_path}</code>).reduce<ReactNode[]>(
            (acc, el, i) => (i ? [...acc, ", ", el] : [el]), [])}
        </p>
      )}
      <ComparisonCounts doc={doc} />
    </section>
  );
}

function ComparisonCounts({ doc }: { doc: Loaded }) {
  const modes = Object.entries(doc.comparisons) as [Mode, NonNullable<Loaded["comparisons"][Mode]>][];
  if (modes.length === 0) return null;
  return (
    <ul className="comparison-counts" data-testid="comparison-counts">
      {modes.map(([mode, r]) => (
        <li key={mode}>
          {mode} mode: <strong>{r.status}</strong>, {r.compared_values} value{r.compared_values === 1 ? "" : "s"} compared
          {doc.comparisonKind === "benchmark_self_check" && mode === modes[0]?.[0] && (
            <span className="note"> · compared by the Track C benchmark self-check (testbench samples), not waveform analysis</span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Pass, inconclusive, unsupported, invalid input, and infrastructure error. */
export function OutcomePanel({ doc }: { doc: Loaded }) {
  const diagnostics = [...doc.diagnostics, ...(doc.report?.diagnostics ?? [])];
  for (const r of Object.values(doc.comparisons)) if (r) diagnostics.push(...r.diagnostics);
  switch (doc.status) {
    case "pass":
      return (
        <section className="outcome outcome-pass" data-testid="outcome-pass">
          <h2>No failure found</h2>
          <p>Every required expectation was observed and matched. There is no failure and no ranked suspect to show.</p>
          <ComparisonCounts doc={doc} />
        </section>
      );
    case "inconclusive":
    case "unsupported":
      return (
        <section className="outcome outcome-limited" data-testid="outcome-limited">
          <h2>{doc.status === "inconclusive" ? "Inconclusive — not a pass" : "Unsupported input — not a pass"}</h2>
          <p>
            The evidence was not sufficient for a verdict. Nothing below should be read as the design passing.
          </p>
          <Diagnostics items={diagnostics} title="Limits" />
          <ComparisonCounts doc={doc} />
        </section>
      );
    case "error":
    case "invalid_input":
      return (
        <section className="outcome outcome-error" data-testid="outcome-error">
          <h2>{doc.status === "error" ? "The run did not complete" : "The input could not be analyzed"}</h2>
          <p>This is a tooling or input problem, not a verdict about the RTL.</p>
          <Diagnostics items={diagnostics} />
          {doc.run && doc.run.steps.length > 0 && (
            <table className="steps">
              <caption>Run steps</caption>
              <thead><tr><th>Step</th><th>Exit code</th><th>Log</th></tr></thead>
              <tbody>
                {doc.run.steps.map((s) => (
                  <tr key={s.name}>
                    <td>{s.name}</td>
                    <td>{s.exit_code === null ? "did not exit" : s.exit_code}</td>
                    <td><code>{s.log}</code>{s.log_truncated ? " (truncated)" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      );
    case "fail":
      return null;
  }
}

/** A failure with no causal graph: show why localization is unavailable. */
export function Unlocalized({ doc }: { doc: Loaded }) {
  if (doc.status !== "fail" || (doc.report && doc.report.nodes.length > 0)) return null;
  const reasons = [...doc.diagnostics, ...(doc.report?.diagnostics ?? [])];
  return (
    <section className="outcome outcome-limited" data-testid="unlocalized">
      <h2>Not localized</h2>
      <p>The mismatch above was observed, but no causal graph is available for it.</p>
      <Diagnostics items={reasons} title="Why" />
    </section>
  );
}
