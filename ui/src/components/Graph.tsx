import { memo, useMemo, useRef, useState, type Dispatch, type PointerEvent, type WheelEvent } from "react";
import type { CausalEdge, CausalNode, Loaded } from "../model/types";
import { describeValue, hasUnknown, shortName, type Radix } from "../model/values";
import type { Action, Selection } from "../state/selection";
import { Panel } from "./common";
import { Limits } from "./Evidence";

/** Above this many nodes the diagram is not drawn; the list is windowed. */
export const GRAPH_DRAW_LIMIT = 400;
const LIST_LIMIT = 500;
const COL_W = 190;
const ROW_H = 58;
const NODE_W = 150;
const NODE_H = 40;

interface Placed {
  node: CausalNode;
  x: number;
  y: number;
}

/** Columns by time (epoch, cycle, then before < settled): cause → effect
 * reads left to right. Deterministic: rows are ordered by node id. */
function layout(nodes: CausalNode[]): { placed: Map<string, Placed>; width: number; height: number } {
  const key = (n: CausalNode) => [n.observation.epoch, n.observation.cycle, n.observation.phase === "before" ? 0 : 1] as const;
  const columns = new Map<string, CausalNode[]>();
  for (const n of nodes) {
    const k = key(n).join(":");
    columns.set(k, [...(columns.get(k) ?? []), n]);
  }
  const ordered = [...columns.entries()].sort(([a], [b]) => {
    const pa = a.split(":").map(Number);
    const pb = b.split(":").map(Number);
    for (let i = 0; i < 3; i++) if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) - (pb[i] ?? 0);
    return 0;
  });
  const placed = new Map<string, Placed>();
  let tallest = 0;
  ordered.forEach(([, column], ci) => {
    column.sort((a, b) => a.id.localeCompare(b.id));
    tallest = Math.max(tallest, column.length);
    column.forEach((node, ri) => placed.set(node.id, { node, x: 20 + ci * COL_W, y: 30 + ri * ROW_H }));
  });
  return { placed, width: 40 + ordered.length * COL_W, height: 50 + tallest * ROW_H };
}

function edgeLabel(e: CausalEdge, nodes: Map<string, Placed>): string {
  const s = nodes.get(e.source)?.node;
  const t = nodes.get(e.target)?.node;
  const branch = e.branch === "unknown" ? "unknown control (both branches kept)" : e.branch;
  return `${s ? shortName(s.signal_path) : e.source} → ${t ? shortName(t.signal_path) : e.target}: ${e.edge_type}, ${e.role}, ${branch}. ${e.reason}`;
}

export function Graph({ doc, sel, dispatch }: { doc: Loaded; sel: Selection; dispatch: Dispatch<Action> }) {
  const report = doc.report;
  const nodes = report?.nodes ?? [];
  const allEdges = report?.edges ?? [];
  const pruned = allEdges.filter((e) => e.branch === "pruned").length;
  const edges = useMemo(
    () => (sel.showPruned ? allEdges : allEdges.filter((e) => e.branch !== "pruned")),
    [allEdges, sel.showPruned],
  );
  const oversized = nodes.length > GRAPH_DRAW_LIMIT;
  const geometry = useMemo(() => (oversized ? null : layout(nodes)), [nodes, oversized]);
  const suspectRank = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of report?.suspects ?? []) if (!m.has(s.node_id) || (m.get(s.node_id) ?? 0) > s.rank) m.set(s.node_id, s.rank);
    return m;
  }, [report]);

  return (
    <Panel
      title="Dependency graph"
      id="graph"
      actions={
        <label className="toggle">
          <input type="checkbox" checked={sel.showPruned} onChange={() => dispatch({ type: "togglePruned" })} />
          Show pruned branches ({pruned})
        </label>
      }
    >
      <Limits doc={doc} />
      {nodes.length === 0 ? (
        <p className="empty" data-testid="graph-empty">No dependency graph in this report.</p>
      ) : (
        <>
          <p className="legend">
            Cause → effect, left to right by time. <span className="lg-solid">Solid</span>: combinational.{" "}
            <span className="lg-dashed">Dashed</span>: sequential (before → settled at one clock edge). Edge kind,
            role, and branch are also given in text below.
          </p>
          {oversized || !geometry ? (
            <p className="warning" data-testid="graph-oversized">
              {nodes.length} nodes exceed the drawing limit of {GRAPH_DRAW_LIMIT}; use the node list.
            </p>
          ) : (
            <Diagram
              geometry={geometry}
              edges={edges}
              sel={sel}
              dispatch={dispatch}
              anchorId={report?.anchor_node_id ?? null}
              suspectRank={suspectRank}
            />
          )}
          <NodeList nodes={nodes} edges={edges} sel={sel} dispatch={dispatch} anchorId={report?.anchor_node_id ?? null} radix={sel.radix} />
        </>
      )}
    </Panel>
  );
}

function Diagram({
  geometry, edges, sel, dispatch, anchorId, suspectRank,
}: {
  geometry: NonNullable<ReturnType<typeof layout>>;
  edges: CausalEdge[];
  sel: Selection;
  dispatch: Dispatch<Action>;
  anchorId: string | null;
  suspectRank: Map<string, number>;
}) {
  const [view, setView] = useState({ scale: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const { placed, width, height } = geometry;
  const zoom = (factor: number) => setView((v) => ({ ...v, scale: Math.min(4, Math.max(0.25, v.scale * factor)) }));
  const onWheel = (e: WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return; // plain wheel scrolls the page
    e.preventDefault();
    zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15);
  };
  const onDown = (e: PointerEvent) => {
    if ((e.target as Element).closest("[data-node]")) return;
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
  };
  const onMove = (e: PointerEvent) => {
    const d = drag.current;
    if (d) setView((v) => ({ ...v, x: d.vx + (e.clientX - d.x) / v.scale, y: d.vy + (e.clientY - d.y) / v.scale }));
  };
  const vbW = width / view.scale;
  const vbH = height / view.scale;
  return (
    <div className="diagram">
      <div className="diagram-tools" role="group" aria-label="Graph zoom">
        <button type="button" onClick={() => zoom(1.25)} aria-label="Zoom graph in">+</button>
        <button type="button" onClick={() => zoom(0.8)} aria-label="Zoom graph out">−</button>
        <button type="button" onClick={() => setView({ scale: 1, x: 0, y: 0 })}>Reset view</button>
        <span className="hint">Drag to pan · Ctrl+wheel to zoom</span>
      </div>
      <svg
        role="img"
        aria-label="Time-unrolled dependency graph; the node list below is the keyboard-accessible equivalent"
        viewBox={`${-view.x} ${-view.y} ${vbW} ${vbH}`}
        width="100%"
        height={Math.min(height, 420)}
        preserveAspectRatio="xMinYMin meet"
        onWheel={onWheel}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={() => (drag.current = null)}
        onPointerLeave={() => (drag.current = null)}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="arrowhead" />
          </marker>
          <pattern id="unknown-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" className="hatch" />
          </pattern>
        </defs>
        {edges.map((e) => {
          const s = placed.get(e.source);
          const t = placed.get(e.target);
          if (!s || !t) return null;
          const x1 = s.x + NODE_W;
          const y1 = s.y + NODE_H / 2;
          const x2 = t.x;
          const y2 = t.y + NODE_H / 2;
          const sameCol = s.x === t.x;
          const d = sameCol
            ? `M ${s.x + NODE_W} ${y1} C ${s.x + NODE_W + 40} ${y1}, ${t.x + NODE_W + 40} ${y2}, ${t.x + NODE_W} ${y2}`
            : `M ${x1} ${y1} C ${x1 + 40} ${y1}, ${x2 - 40} ${y2}, ${x2} ${y2}`;
          return (
            <path
              key={e.id}
              d={d}
              className={`edge ${e.edge_type} branch-${e.branch} role-${e.role}`}
              markerEnd="url(#arrow)"
              data-edge={e.id}
            >
              <title>{edgeLabel(e, placed)}</title>
            </path>
          );
        })}
        {[...placed.values()].map(({ node, x, y }) => {
          const selected = sel.selectedNodeId === node.id;
          const rank = suspectRank.get(node.id);
          const unknown = hasUnknown(node.value);
          return (
            <g
              key={node.id}
              data-node={node.id}
              transform={`translate(${x} ${y})`}
              className={`node${selected ? " selected" : ""}${node.id === anchorId ? " anchor" : ""}`}
              onClick={() => dispatch({ type: "selectNode", id: node.id })}
            >
              <title>{`${node.signal_path}, cycle ${node.observation.cycle} ${node.observation.phase}: ${describeValue(node.value, "bin")}`}</title>
              <rect width={NODE_W} height={NODE_H} rx="5" />
              {unknown && <rect width={NODE_W} height={NODE_H} rx="5" fill="url(#unknown-hatch)" className="unknown-fill" />}
              <text x="8" y="16" className="node-name">
                {shortName(node.signal_path)}
                {node.id === anchorId ? " ⚑" : ""}
                {rank ? ` #${rank}` : ""}
              </text>
              <text x="8" y="32" className="node-sub">
                c{node.observation.cycle} {node.observation.phase} · {node.value ? node.value.bits : "n/o"}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function NodeList({
  nodes, edges, sel, dispatch, anchorId, radix,
}: {
  nodes: CausalNode[];
  edges: CausalEdge[];
  sel: Selection;
  dispatch: Dispatch<Action>;
  anchorId: string | null;
  radix: Radix;
}) {
  const shown = nodes.slice(0, LIST_LIMIT);
  const { incoming, byId } = useMemo(() => {
    const incoming = new Map<string, CausalEdge[]>();
    for (const e of edges) incoming.set(e.target, [...(incoming.get(e.target) ?? []), e]);
    return { incoming, byId: new Map(nodes.map((n) => [n.id, n])) };
  }, [nodes, edges]);
  return (
    <details className="node-list" open={nodes.length <= 12}>
      <summary>Node list ({nodes.length}{nodes.length > LIST_LIMIT ? `, showing ${LIST_LIMIT}` : ""})</summary>
      <ul>
        {shown.map((n) => (
          <NodeRow
            key={n.id}
            node={n}
            inputs={incoming.get(n.id)}
            byId={byId}
            pressed={sel.selectedNodeId === n.id}
            isAnchor={n.id === anchorId}
            radix={radix}
            dispatch={dispatch}
          />
        ))}
      </ul>
    </details>
  );
}

/** Memoized so a selection change re-renders only the rows whose state changed. */
const NodeRow = memo(function NodeRow({
  node: n, inputs, byId, pressed, isAnchor, radix, dispatch,
}: {
  node: CausalNode;
  inputs: CausalEdge[] | undefined;
  byId: Map<string, CausalNode>;
  pressed: boolean;
  isAnchor: boolean;
  radix: Radix;
  dispatch: Dispatch<Action>;
}) {
  return (
    <li>
      <button
        type="button"
        aria-pressed={pressed}
        onClick={() => dispatch({ type: "selectNode", id: n.id })}
        data-testid={`node-${n.id}`}
      >
        <code>{n.signal_path}</code> · epoch {n.observation.epoch} cycle {n.observation.cycle} {n.observation.phase} ·{" "}
        {describeValue(n.value, radix)}
        {isAnchor ? " · first failure" : ""}
      </button>
      {inputs && inputs.length > 0 && (
        <ul className="inputs">
          {inputs.map((e) => (
            <li key={e.id}>
              depends on <code>{byId.get(e.source)?.signal_path ?? e.source}</code> ({byId.get(e.source)?.observation.phase}) — {e.edge_type},{" "}
              {e.role}, {e.branch === "unknown" ? "unknown control" : e.branch}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
});
