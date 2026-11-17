import { useEffect, useState, type ReactNode, type RefObject } from "react";
import type { Diagnostic, LogicValue, Status } from "../model/types";
import { describeValue, formatValue, hasUnknown, type Radix } from "../model/values";

export const STATUS_TEXT: Record<Status, string> = {
  pass: "Pass",
  fail: "Fail",
  inconclusive: "Inconclusive",
  unsupported: "Unsupported",
  invalid_input: "Invalid input",
  error: "Infrastructure error",
};

// A shape glyph accompanies every status so color is never the only cue.
const STATUS_GLYPH: Record<Status, string> = {
  pass: "✓",
  fail: "✕",
  inconclusive: "?",
  unsupported: "⊘",
  invalid_input: "!",
  error: "⚠",
};

export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`badge status-${status}`} data-testid="status-badge">
      <span aria-hidden="true">{STATUS_GLYPH[status]}</span> {STATUS_TEXT[status]}
    </span>
  );
}

/** Exact bits always; the radix view beside them; unknowns labeled. */
export function Value({ value, radix, testId }: { value: LogicValue | null; radix: Radix; testId?: string }) {
  if (value === null) return <span className="value missing" data-testid={testId}>not observed</span>;
  const view = radix === "bin" ? null : formatValue(value, radix);
  const unknown = hasUnknown(value);
  return (
    <span className={`value${unknown ? " unknown" : ""}`} data-testid={testId} title={describeValue(value, radix)}>
      <code className="bits">{value.bits}</code>
      {view !== null && <span className="radix-view"> = {view}</span>}
      {radix !== "bin" && view === null && <span className="radix-view"> ({radix}: n/a)</span>}
      {unknown && <span className="unknown-tag">Unknown bits</span>}
    </span>
  );
}

export function Diagnostics({ items, title = "Diagnostics" }: { items: Diagnostic[]; title?: string }) {
  if (items.length === 0) return null;
  return (
    <div className="diagnostics">
      <h3>{title}</h3>
      <ul>
        {items.map((d, i) => (
          <li key={`${d.code}-${i}`}>
            <code>{d.code}</code> {d.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Panel({ title, id, children, actions }: { title: string; id: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="panel" aria-labelledby={`${id}-title`} id={id}>
      <div className="panel-head">
        <h2 id={`${id}-title`}>{title}</h2>
        {actions && <div className="panel-actions">{actions}</div>}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function CopyButton({ text, label, onResult }: { text: string; label: string; onResult: (message: string) => void }) {
  return (
    <button
      type="button"
      onClick={async () => onResult((await copyText(text)) ? `Copied ${label}` : `Copy failed; select the text manually`)}
    >
      Copy {label}
    </button>
  );
}

export function useMediaQuery(query: string): boolean {
  const get = () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false);
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (!window.matchMedia) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener?.("change", onChange);
    return () => mql.removeEventListener?.("change", onChange);
  }, [query]);
  return matches;
}

export function useWidth(ref: RefObject<HTMLElement | null>, fallback: number): number {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && w > 0) setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}
