import { ReactFlow, Background, Controls, MarkerType, Position } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { memo, useMemo, type Dispatch } from "react";
import type { CausalEdge, CausalNode, Loaded } from "../model/types";
import { describeValue, shortName, type Radix } from "../model/values";
import type { Action, Selection } from "../state/selection";
import { Panel } from "./common";
import { Limits } from "./Evidence";

/** Above this many nodes the diagram is not drawn; the list is windowed. */
export const GRAPH_DRAW_LIMIT = 400;
const LIST_LIMIT = 500;
const COL_W = 235;
const ROW_H = 86;

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
  const active = new Set<string>();
  if (sel.selectedNodeId) {
    active.add(sel.selectedNodeId);
    // Follow retained upstream evidence; a pruned arm is never an active path.
    let changed = true;
    while (changed) {
      changed = false;
      for (const edge of edges) if (edge.branch === "retained" && active.has(edge.target) && !active.has(edge.source)) {
        active.add(edge.source); changed = true;
      }
    }
  }
  // Retained downstream propagation is highlighted only if it reaches the
  // immutable failure anchor, rather than every fan-out of the selection.
  const toAnchor = new Set<string>(anchorId ? [anchorId] : []);
  let changed = true;
  while (changed) {
    changed = false;
    for (const e of edges) if (e.branch === "retained" && toAnchor.has(e.target) && !toAnchor.has(e.source)) {
      toAnchor.add(e.source); changed = true;
    }
  }
  const downstream = new Set<string>(sel.selectedNodeId ? [sel.selectedNodeId] : []);
  changed = true;
  while (changed) {
    changed = false;
    for (const e of edges) if (e.branch === "retained" && downstream.has(e.source) && toAnchor.has(e.target) && !downstream.has(e.target)) {
      downstream.add(e.target); active.add(e.target); changed = true;
    }
  }
  const nodes = [...geometry.placed.values()].map(({node, x, y}) => ({
    id: node.id, position: {x, y}, sourcePosition: Position.Right, targetPosition: Position.Left,
    selected: node.id === sel.selectedNodeId,
    className: `signal-node ${node.id === anchorId ? "failure-node" : ""} ${active.has(node.id) ? "active-node" : "muted-node"}`,
    data: {label: <div><div className="flex items-center justify-between gap-2"><strong>{shortName(node.signal_path)}</strong><span>{node.id === anchorId ? "FAIL" : suspectRank.has(node.id) ? `#${suspectRank.get(node.id)}` : ""}</span></div><div className="node-detail">C{node.observation.cycle} · {node.observation.phase} · {node.value?.bits ?? "n/o"}</div></div>},
    ariaLabel: `${node.signal_path}, cycle ${node.observation.cycle} ${node.observation.phase}`,
  }));
  const flowEdges = edges.map(e => {
    const sequential = e.edge_type === "sequential";
    const retained = e.branch === "retained";
    const color = e.branch === "unknown" ? "#d5a0ff" : sequential ? "#f5ba63" : "#52d7ed";
    const source = geometry.placed.get(e.source)?.node;
    const target = geometry.placed.get(e.target)?.node;
    const delta = source && target ? target.observation.cycle - source.observation.cycle : 0;
    // Schema-1 sequential edges cross before → settled at the SAME edge.
    // Never falsely label these as a previous cycle.
    const label = e.branch === "unknown" ? "unknown control" : sequential ? (delta ? `−${delta} cycle` : "clock edge") : delta ? `−${delta} cycle · carry` : undefined;
    return {id:e.id, source:e.source, target:e.target, type:"smoothstep", label,
      className: `branch-${e.branch}`,
      style:{stroke:color, strokeWidth:active.has(e.source) && active.has(e.target) && retained ? 2.5 : 1.4, strokeDasharray:sequential ? "7 5" : undefined, opacity:e.branch === "pruned" ? 0.18 : 1},
      labelStyle:{fill:color,fontSize:10}, labelBgStyle:{fill:"#101a28",fillOpacity:0.95},
      markerEnd:{type:MarkerType.ArrowClosed,color},
      ariaLabel:`${e.edge_type}, ${e.branch}. ${e.reason}`,
    };
  });
  return <div className="diagram" style={{height:390}} data-testid="flow-graph">
    <ReactFlow nodes={nodes} edges={flowEdges} onNodeClick={(_,node)=>dispatch({type:"selectNode",id:node.id})}
      nodesDraggable={false} nodesConnectable={false} fitView fitViewOptions={{padding:0.18}}
      minZoom={0.2} maxZoom={2.5} preventScrolling={false}>
      <Background color="#26384a" gap={20}/><Controls showInteractive={false}/>
    </ReactFlow>
  </div>;
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
    <details className="node-list">
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
            <li key={e.id} data-edge={e.id} title={`${e.branch}: ${e.reason}`}>
              depends on <code>{byId.get(e.source)?.signal_path ?? e.source}</code> ({byId.get(e.source)?.observation.phase}) — {e.edge_type},{" "}
              {e.role}, {e.branch === "unknown" ? "unknown control" : e.branch}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
});
