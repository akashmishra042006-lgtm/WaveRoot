import type { Dispatch, ReactNode } from "react";
import type { CandidateAssertion, Loaded, SourceFile, SourceSpan } from "../model/types";
import { resolveSource, type SourceText } from "../model/sources";
import { formatObservation, shortName } from "../model/values";
import type { Action, Selection } from "../state/selection";
import { CopyButton, Panel } from "./common";

function sourceFor(doc: Loaded, span: SourceSpan | null): SourceFile | undefined {
  return span ? doc.report?.sources.find((s) => s.id === span.source_id) : undefined;
}

export function locationText(doc: Loaded, span: SourceSpan | null): string | null {
  const file = sourceFor(doc, span);
  if (!span || !file) return null;
  return span.line === span.end_line ? `${file.path}:${span.line}` : `${file.path}:${span.line}-${span.end_line}`;
}

export function Suspects({ doc, sel, dispatch }: { doc: Loaded; sel: Selection; dispatch: Dispatch<Action> }) {
  const suspects = [...(doc.report?.suspects ?? [])].sort((a, b) => a.rank - b.rank);
  return (
    <Panel title="Ranked suspects" id="suspects">
      <p className="hint">Ranks are ordinal, not probabilities. A suspect is evidence to investigate, not a proven cause.</p>
      {suspects.length === 0 ? (
        <p className="empty">No ranked suspects in this report.</p>
      ) : (
        <ol className="suspect-list">
          {suspects.map((s) => {
            const node = doc.report?.nodes.find((n) => n.id === s.node_id);
            const where = locationText(doc, s.location);
            return (
              <li key={s.id}>
                <button
                  type="button"
                  className="suspect"
                  aria-pressed={sel.selectedSuspectId === s.id}
                  onClick={() => dispatch({ type: "selectSuspect", id: s.id })}
                  data-testid={`suspect-${s.id}`}
                >
                  <span className="rank">#{s.rank}</span>
                  <span className="suspect-signal"><code>{node?.signal_path}</code></span>
                  <span className="suspect-where">{where ?? "Source unavailable"}</span>
                  {node && <span className="suspect-obs">{formatObservation(node.observation)}</span>}
                  <span className="suspect-reason">{s.reason}</span>
                  <span className="confidence">Confidence: not supplied · rank #{s.rank}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}

export function SourceView({
  doc, sel, sources, onNotice,
}: { doc: Loaded; sel: Selection; sources: readonly SourceText[]; onNotice: (m: string) => void }) {
  const span = sel.sourceLocation;
  const file = sourceFor(doc, span);
  const location = locationText(doc, span);
  let body: ReactNode;
  if (!span || !file) {
    body = <p className="empty" data-testid="source-none">{sel.selectedNodeId ? "This node has no source location." : "Select a suspect or node to see its source."}</p>;
  } else {
    const state = resolveSource(file, sources);
    if (state.kind === "unavailable") {
      body = (
        <p className="empty" data-testid="source-unavailable">
          Source unavailable: <code>{file.path}</code> (SHA-256 <code className="hash">{file.sha256.slice(0, 12)}…</code>) has not been
          provided. Use “Add source files” to supply it; it is shown only if its hash matches.
        </p>
      );
    } else if (state.kind === "mismatch") {
      body = (
        <p className="warning" data-testid="source-mismatch">
          Revision mismatch: the supplied <code>{state.supplied.name}</code> has SHA-256{" "}
          <code className="hash">{state.supplied.sha256.slice(0, 12)}…</code>, but the report analyzed{" "}
          <code className="hash">{file.sha256.slice(0, 12)}…</code>. It is not shown as the analyzed code.
        </p>
      );
    } else {
      const lines = state.text.text.split("\n");
      if (lines.at(-1) === "") lines.pop();
      body = (
        <>
          <p className="verified" data-testid="source-verified">✓ Revision checked: SHA-256 matches the report.</p>
          <ol className="code" aria-label={`${file.path} source`}>
            {lines.map((line, i) => {
              const n = i + 1;
              const hit = n >= span.line && n <= span.end_line;
              return (
                <li key={n} value={n} className={hit ? "hit" : undefined} data-line={n} ref={hit && n === span.line ? scrollIntoView : undefined}>
                  <code>{highlightSV(line)}</code>
                </li>
              );
            })}
          </ol>
        </>
      );
    }
  }
  return (
    <Panel
      title="Source"
      id="source"
      actions={location ? <CopyButton text={location} label="location" onResult={onNotice} /> : undefined}
    >
      {location && <p className="location" data-testid="source-location"><code>{location}</code></p>}
      {body}
    </Panel>
  );
}

/** Token decoration only: source bytes and line numbering stay unchanged. */
function highlightSV(line: string): ReactNode {
  return line.split(/(\/\/.*$|\b(?:module|endmodule|input|output|logic|wire|assign|always_ff|always_comb|begin|end|if|else|posedge|negedge|parameter|localparam)\b|\b\d+'[bdh][0-9a-fxz_]+\b)/gi).map((token,i) =>
    <span key={i} className={token.startsWith("//") ? "sv-comment" : /^(module|endmodule|input|output|logic|wire|assign|always_ff|always_comb|begin|end|if|else|posedge|negedge|parameter|localparam)$/.test(token) ? "sv-keyword" : /^\d+'/.test(token) ? "sv-number" : undefined}>{token || ""}</span>
  );
}

/** Scroll the highlighted line into view inside the code box only; moving the
 * whole page would hide content under the sticky header. */
function scrollIntoView(el: HTMLLIElement | null) {
  const box = el?.closest<HTMLElement>(".code");
  if (el && box) box.scrollTop = Math.max(0, el.offsetTop - box.clientHeight / 3);
}

const VALIDATION_TEXT: Record<CandidateAssertion["validation"], string> = {
  candidate: "Candidate — not validated",
  validated: "Validated",
  failed: "Failed validation",
  unsupported: "Unsupported by the selected simulator",
};

export function Assertions({ doc, onNotice }: { doc: Loaded; onNotice: (m: string) => void }) {
  const items = doc.report?.assertions ?? [];
  return (
    <Panel title="Assertion candidates" id="assertions">
      {items.length === 0 ? (
        <p className="empty">No candidate assertions in this report.</p>
      ) : (
        <ul className="assertions">
          {items.map((a) => {
            const header = a.validation === "candidate" ? "// Candidate assertion from WaveRoot — not validated\n" : `// ${VALIDATION_TEXT[a.validation]}${a.tool ? ` (${a.tool})` : ""}\n`;
            return (
              <li key={a.id} data-testid={`assertion-${a.id}`}>
                <span className={`badge validation-${a.validation}`}>
                  {VALIDATION_TEXT[a.validation]}
                  {a.tool && (a.validation === "validated" || a.validation === "failed") ? ` by ${a.tool}` : ""}
                </span>
                {/* Displayed as text only; assertion text is never executed. */}
                <pre><code>{a.text}</code></pre>
                <CopyButton text={header + a.text} label="assertion" onResult={onNotice} />
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

export function Limits({ doc }: { doc: Loaded }) {
  const report = doc.report;
  if (!report) return null;
  const items = report.diagnostics;
  return (
    <div className="limits" data-testid="graph-limits">
      {report.truncated && (
        <p className="warning" data-testid="truncated">
          Slice truncated: the analysis stopped at a depth, time, or capability limit. Absent nodes are not evidence of irrelevance.
        </p>
      )}
      {items.length > 0 && (
        <ul>
          {items.map((d, i) => (
            <li key={`${d.code}-${i}`}><code>{d.code}</code> {d.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export { shortName };
