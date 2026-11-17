// The single shared selection state (ui-spec "Single selection state").
//
// The failure anchor is report data and never lives here: no action can
// change what the first failure is. Rules implemented, by ui-spec number:
//  1. Selecting a suspect selects its node, signal, observation, and source
//     span; the window recenters only if the observation is outside it.
//  2. Selecting a node updates signal/observation/source; a suspect stays
//     highlighted only if it maps to that node.
//  3. Moving the cursor changes the selected time only. With no exact node
//     observation at that time, node and suspect highlights clear while the
//     selected signal is kept.
//  4. Jump-to-failure restores the failure signal and time and reveals the
//     anchor node when present — never a different node with a nearby time.
//  5. Loading another report resets everything (see initialSelection).

import { sameObservation } from "../model/validate";
import type { CausalNode, Loaded, Observation, SourceSpan } from "../model/types";
import type { Radix } from "../model/values";
import { formatObservation, formatTime, shortName } from "../model/values";
import { ensureVisible, tenCycleWindow, type TimeWindow, zoomWindow } from "../model/waveform";

export interface Selection {
  selectedSuspectId: string | null;
  selectedNodeId: string | null;
  selectedSignalId: string | null;
  selectedObservation: Observation | null;
  sourceLocation: SourceSpan | null;
  cursorTime: bigint | null;
  window: TimeWindow | null;
  radix: Radix;
  showPruned: boolean;
  announcement: string;
}

export type Action =
  | { type: "selectSuspect"; id: string }
  | { type: "selectNode"; id: string }
  | { type: "setCursor"; time: bigint; signal?: string }
  | { type: "stepCursor"; ticks: bigint }
  | { type: "jumpToFailure" }
  | { type: "setRadix"; radix: Radix }
  | { type: "togglePruned" }
  | { type: "zoom"; factor: number }
  | { type: "setWindow"; window: TimeWindow };

function bounds(doc: Loaded): TimeWindow | null {
  const w = doc.report?.waveform;
  return w ? { start: w.start_ps, end: w.end_ps } : null;
}

function nodeById(doc: Loaded, id: string | null): CausalNode | undefined {
  return id ? doc.report?.nodes.find((n) => n.id === id) : undefined;
}

function suspectForNode(doc: Loaded, nodeId: string): string | null {
  const suspects = doc.report?.suspects.filter((s) => s.node_id === nodeId) ?? [];
  suspects.sort((a, b) => a.rank - b.rank);
  return suspects[0]?.id ?? null;
}

function reveal(state: Selection, doc: Loaded, t: bigint): TimeWindow | null {
  const b = bounds(doc);
  if (!b || !state.window) return state.window;
  return ensureVisible(state.window, t, b);
}

function focusFailure(state: Selection, doc: Loaded, announce: boolean): Selection {
  const failure = doc.failure;
  if (!failure) return { ...state, selectedSuspectId: null, selectedNodeId: null, selectedObservation: null, sourceLocation: null };
  const anchor = nodeById(doc, doc.report?.anchor_node_id ?? null);
  const t = failure.observation.time_ps;
  return {
    ...state,
    selectedSignalId: failure.signal_path,
    selectedObservation: failure.observation,
    cursorTime: t,
    selectedNodeId: anchor?.id ?? null,
    selectedSuspectId: anchor ? suspectForNode(doc, anchor.id) : null,
    sourceLocation: anchor?.location ?? null,
    window: reveal(state, doc, t),
    announcement: announce
      ? `First failure: ${shortName(failure.signal_path)}, ${formatObservation(failure.observation)}`
      : state.announcement,
  };
}

export function initialSelection(doc: Loaded | null): Selection {
  const base: Selection = {
    selectedSuspectId: null,
    selectedNodeId: null,
    selectedSignalId: null,
    selectedObservation: null,
    sourceLocation: null,
    cursorTime: null,
    window: doc?.report?.waveform && doc.failure ? tenCycleWindow(doc.report.waveform, doc.failure.clock_path, doc.failure.observation.time_ps) : doc ? bounds(doc) : null,
    radix: "bin",
    showPruned: false,
    announcement: "",
  };
  return doc ? focusFailure(base, doc, false) : base;
}

export function reduce(doc: Loaded | null, state: Selection, action: Action): Selection {
  if (!doc) return state;
  switch (action.type) {
    case "selectSuspect": {
      const suspect = doc.report?.suspects.find((s) => s.id === action.id);
      const node = suspect ? nodeById(doc, suspect.node_id) : undefined;
      if (!suspect || !node) return state;
      return {
        ...state,
        selectedSuspectId: suspect.id,
        selectedNodeId: node.id,
        selectedSignalId: node.signal_path,
        selectedObservation: node.observation,
        sourceLocation: suspect.location ?? node.location,
        cursorTime: node.observation.time_ps,
        window: reveal(state, doc, node.observation.time_ps),
        announcement: `Suspect ${suspect.rank}: ${shortName(node.signal_path)}, ${formatObservation(node.observation)}`,
      };
    }
    case "selectNode": {
      const node = nodeById(doc, action.id);
      if (!node) return state;
      return {
        ...state,
        selectedNodeId: node.id,
        selectedSuspectId: suspectForNode(doc, node.id),
        selectedSignalId: node.signal_path,
        selectedObservation: node.observation,
        sourceLocation: node.location,
        cursorTime: node.observation.time_ps,
        window: reveal(state, doc, node.observation.time_ps),
        announcement: `Node ${shortName(node.signal_path)}, ${formatObservation(node.observation)}`,
      };
    }
    case "setCursor":
      return moveCursor(doc, state, action.time, action.signal ?? state.selectedSignalId);
    case "stepCursor": {
      const from = state.cursorTime ?? doc.failure?.observation.time_ps ?? state.window?.start;
      if (from === undefined || from === null) return state;
      let t = from + action.ticks;
      const b = bounds(doc);
      if (b) t = t < b.start ? b.start : t >= b.end ? b.end - 1n : t;
      return moveCursor(doc, state, t, state.selectedSignalId);
    }
    case "jumpToFailure":
      return focusFailure(state, doc, true);
    case "setRadix":
      return { ...state, radix: action.radix };
    case "togglePruned":
      return {
        ...state,
        showPruned: !state.showPruned,
        announcement: state.showPruned ? "Pruned branches hidden" : "Pruned branches shown",
      };
    case "zoom": {
      const b = bounds(doc);
      if (!b || !state.window) return state;
      const center = state.cursorTime !== null && state.cursorTime >= state.window.start && state.cursorTime < state.window.end
        ? state.cursorTime
        : state.window.start + (state.window.end - state.window.start) / 2n;
      return { ...state, window: zoomWindow(state.window, center, action.factor, b) };
    }
    case "setWindow":
      return { ...state, window: action.window };
  }
}

function moveCursor(doc: Loaded, state: Selection, t: bigint, signal: string | null): Selection {
  // Keep the phase the user is looking at; an exact node match needs signal,
  // time, and phase to agree. Never snap to a node at a nearby time.
  const phase = state.selectedObservation?.phase ?? "settled";
  const node = signal
    ? doc.report?.nodes.find((n) => n.signal_path === signal && n.observation.time_ps === t && n.observation.phase === phase)
    : undefined;
  const window = reveal(state, doc, t);
  if (node) {
    return {
      ...state,
      cursorTime: t,
      selectedSignalId: signal,
      selectedNodeId: node.id,
      selectedSuspectId: suspectForNode(doc, node.id),
      selectedObservation: node.observation,
      sourceLocation: node.location,
      window,
      announcement: `Cursor ${formatTime(t)}: node ${shortName(node.signal_path)}`,
    };
  }
  const keepObservation = state.selectedObservation && sameTime(state.selectedObservation, t) ? state.selectedObservation : null;
  return {
    ...state,
    cursorTime: t,
    selectedSignalId: signal,
    selectedNodeId: null,
    selectedSuspectId: null,
    selectedObservation: keepObservation,
    sourceLocation: null,
    window,
    announcement: `Cursor ${formatTime(t)}`,
  };
}

function sameTime(o: Observation, t: bigint): boolean {
  return o.time_ps === t;
}

export { sameObservation };
