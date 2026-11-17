import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import { Assertions, SourceView, Suspects } from "./components/Evidence";
import { ApiClient, apiBaseFromLocation } from "./api/client";
import { Graph } from "./components/Graph";
import { RunPanel } from "./components/RunPanel";
import { FailureStrip, Header, OutcomePanel, Unlocalized } from "./components/Summary";
import { Waveform } from "./components/Waveform";
import { useMediaQuery } from "./components/common";
import { makeSourceText, type SourceText } from "./model/sources";
import type { Loaded } from "./model/types";
import { loadDocument, parseDocumentText, ValidationError } from "./model/validate";
import { RADIX_LABELS, type Radix } from "./model/values";
import { BUNDLED_SOURCES, GROUP_LABELS, SAMPLES } from "./samples";
import { initialSelection, reduce, type Action, type Selection } from "./state/selection";

/** Blob.text() where available; FileReader otherwise (older browsers, jsdom). */
function readText(file: Blob): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

type View =
  | { kind: "empty" }
  | { kind: "loading"; stage: string }
  | { kind: "error"; message: string }
  | { kind: "ready" };

interface State {
  doc: Loaded | null;
  sel: Selection;
  view: View;
}

type AppAction = Action | { type: "loaded"; doc: Loaded } | { type: "view"; view: View };

function appReducer(state: State, action: AppAction): State {
  if (action.type === "loaded") {
    // Rule 5: a new report replaces all selection state.
    return { doc: action.doc, sel: initialSelection(action.doc), view: { kind: "ready" } };
  }
  if (action.type === "view") {
    // A failed load clears the previous report so no stale data stays visible.
    const clear = action.view.kind === "error";
    return { doc: clear ? null : state.doc, sel: clear ? initialSelection(null) : state.sel, view: action.view };
  }
  return { ...state, sel: reduce(state.doc, state.sel, action) };
}

const TABS = [
  { id: "suspects", label: "Suspects" },
  { id: "graph", label: "Graph" },
  { id: "source", label: "Source" },
  { id: "waveform", label: "Waveforms" },
  { id: "details", label: "Assertions" },
] as const;
type TabId = (typeof TABS)[number]["id"];

export function App({ client }: { client?: ApiClient } = {}) {
  const api = useMemo(
    () => client ?? new ApiClient(apiBaseFromLocation(typeof window === "undefined" ? "" : window.location.search)),
    [client],
  );
  const [state, dispatch] = useReducer(appReducer, { doc: null, sel: initialSelection(null), view: { kind: "empty" } });
  const [sources, setSources] = useState<SourceText[]>([]);
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState<TabId>("suspects");
  const narrow = useMediaQuery("(max-width: 700px)");
  const fileInput = useRef<HTMLInputElement>(null);
  const sourceInput = useRef<HTMLInputElement>(null);
  const { doc, sel, view } = state;

  useEffect(() => {
    let live = true;
    Promise.all(BUNDLED_SOURCES.map((s) => makeSourceText(s.name, s.text))).then((texts) => {
      if (live) setSources((prev) => [...texts, ...prev]);
    }, () => setNotice("Bundled source text could not be hashed; sources will show as unavailable."));
    return () => {
      live = false;
    };
  }, []);

  const openDocument = useCallback((value: unknown) => {
    try {
      dispatch({ type: "view", view: { kind: "loading", stage: "Validating schema" } });
      dispatch({ type: "loaded", doc: loadDocument(value) });
      setTab("suspects");
    } catch (err) {
      const message = err instanceof ValidationError ? err.message : `Unexpected error: ${String(err)}`;
      dispatch({ type: "view", view: { kind: "error", message } });
    }
  }, []);

  const onApiResult = useCallback((record: unknown, texts: SourceText[], problems: string[]) => {
    setSources((prev) => [...prev.filter((p) => !texts.some((t) => t.sha256 === p.sha256)), ...texts]);
    openDocument(record);
    if (problems.length > 0) setNotice(`Some sources could not be loaded: ${problems.join("; ")}`);
  }, [openDocument]);

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    dispatch({ type: "view", view: { kind: "loading", stage: `Reading ${file.name}` } });
    try {
      const text = await readText(file);
      dispatch({ type: "view", view: { kind: "loading", stage: "Validating schema" } });
      dispatch({ type: "loaded", doc: parseDocumentText(text) });
      setTab("suspects");
    } catch (err) {
      const message = err instanceof ValidationError ? err.message : `Could not read the file: ${String(err)}`;
      dispatch({ type: "view", view: { kind: "error", message } });
    }
  };

  const onSources = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = [...(e.target.files ?? [])];
    e.target.value = "";
    const texts = await Promise.all(files.map(async (f) => makeSourceText(f.name, await readText(f))));
    setSources((prev) => [...prev, ...texts]);
    setNotice(`Added ${texts.length} source file${texts.length === 1 ? "" : "s"}; each is shown only if its SHA-256 matches the report.`);
  };

  const exportReport = () => {
    if (!doc) return;
    const blob = new Blob([JSON.stringify(doc.raw, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${doc.title.replace(/[^A-Za-z0-9_.-]+/g, "_")}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Only when there is evidence to inspect; an unlocalized failure is fully
  // explained by the failure strip and the "Not localized" panel.
  const hasWorkspace = doc !== null && doc.status === "fail" && doc.report !== null &&
    (doc.report.nodes.length > 0 || doc.report.waveform !== null);
  const panels: Record<TabId, ReactNode> = doc
    ? {
        suspects: <Suspects doc={doc} sel={sel} dispatch={dispatch} />,
        graph: <Graph doc={doc} sel={sel} dispatch={dispatch} />,
        source: <SourceView doc={doc} sel={sel} sources={sources} onNotice={setNotice} />,
        waveform: <Waveform doc={doc} sel={sel} dispatch={dispatch} />,
        details: <Assertions doc={doc} onNotice={setNotice} />,
      }
    : ({} as Record<TabId, ReactNode>);

  return (
    <div className="app">
      <Header doc={doc}>
        <button type="button" onClick={() => fileInput.current?.click()}>Open report…</button>
        <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={onFile} data-testid="file-input" />
        <label className="select">
          <span className="sr-only">Load a bundled document</span>
          <select
            value=""
            onChange={(e) => {
              const sample = SAMPLES.find((s) => s.id === e.target.value);
              if (sample) openDocument(sample.document);
            }}
            data-testid="sample-select"
          >
            <option value="">Bundled documents…</option>
            {(Object.keys(GROUP_LABELS) as (keyof typeof GROUP_LABELS)[]).map((g) => (
              <optgroup key={g} label={GROUP_LABELS[g]}>
                {SAMPLES.filter((s) => s.group === g).map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        {doc && (
          <>
            <button type="button" onClick={() => sourceInput.current?.click()}>Add source files…</button>
            <input ref={sourceInput} type="file" multiple hidden onChange={onSources} data-testid="source-input" />
            <label className="select">
              <span>Radix</span>
              <select value={sel.radix} onChange={(e) => dispatch({ type: "setRadix", radix: e.target.value as Radix })} data-testid="radix">
                {(Object.keys(RADIX_LABELS) as Radix[]).map((r) => (
                  <option key={r} value={r}>{RADIX_LABELS[r]}</option>
                ))}
              </select>
            </label>
            <button type="button" onClick={exportReport}>Export report</button>
          </>
        )}
      </Header>

      <main>
        <RunPanel client={api} onResult={onApiResult} />
        {view.kind === "empty" && (
          <section className="empty-state" data-testid="empty-state">
            <h1>Open a saved WaveRoot report</h1>
            <p>
              Choose a run record from <code>python -m src.app.cli analyze … --json</code> (or its saved{" "}
              <code>report.json</code>), or pick a bundled document. Opening a report never runs a simulation.
            </p>
            <button type="button" className="primary" onClick={() => fileInput.current?.click()}>Open report…</button>{" "}
            <button type="button" onClick={() => openDocument(SAMPLES[0]?.document)}>Load sample data (mock FIFO)</button>
          </section>
        )}
        {view.kind === "loading" && (
          <p className="loading" role="status" data-testid="loading">{view.stage}…</p>
        )}
        {view.kind === "error" && (
          <section className="outcome outcome-error" role="alert" data-testid="load-error">
            <h2>This document cannot be displayed</h2>
            <p>{view.message}</p>
            <p>Nothing from it has been rendered.</p>
          </section>
        )}
        {view.kind === "ready" && doc && (
          <>
            {doc.fixture && (
              <p className="fixture-note" data-testid="fixture-note">
                Sample data: {doc.fixtureNote}
              </p>
            )}
            <FailureStrip doc={doc} radix={sel.radix} dispatch={dispatch} />
            <OutcomePanel doc={doc} />
            <Unlocalized doc={doc} />
            {hasWorkspace &&
              (narrow ? (
                <div className="tabs">
                  <div role="tablist" aria-label="Panels">
                    {TABS.map((t) => (
                      <button
                        key={t.id}
                        role="tab"
                        id={`tab-${t.id}`}
                        aria-selected={tab === t.id}
                        aria-controls={`tabpanel-${t.id}`}
                        tabIndex={tab === t.id ? 0 : -1}
                        onClick={() => setTab(t.id)}
                        onKeyDown={(e) => {
                          const i = TABS.findIndex((x) => x.id === tab);
                          const next = e.key === "ArrowRight" ? TABS[(i + 1) % TABS.length] : e.key === "ArrowLeft" ? TABS[(i + TABS.length - 1) % TABS.length] : undefined;
                          if (next) {
                            setTab(next.id);
                            document.getElementById(`tab-${next.id}`)?.focus();
                          }
                        }}
                      >
                        {t.label}
                      </button>
                    ))}
                  </div>
                  <div role="tabpanel" id={`tabpanel-${tab}`} aria-labelledby={`tab-${tab}`}>
                    {panels[tab]}
                  </div>
                </div>
              ) : (
                <div className="workspace">
                  <div className="col-suspects">
                    {panels.suspects}
                    {panels.details}
                  </div>
                  <div className="col-graph">{panels.graph}</div>
                  <div className="col-source">{panels.source}</div>
                  <div className="col-wave">{panels.waveform}</div>
                </div>
              ))}
          </>
        )}
      </main>
      <div className="sr-only" aria-live="polite" data-testid="announcer">
        {sel.announcement}
      </div>
      <div className="sr-only" aria-live="polite">{notice}</div>
      {notice && <p className="toast" role="status">{notice}</p>}
    </div>
  );
}
