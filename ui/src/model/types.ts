// TypeScript view of the shared contracts (spec.md schema 1, src/contracts.py).
// These are the *validated* shapes: timestamps are BigInt, bit strings are
// canonical lowercase. Nothing here is constructed without passing validate.ts.

export type Phase = "before" | "settled";
export type Mode = "cycle" | "handshake";
export type Status = "pass" | "fail" | "inconclusive" | "unsupported" | "invalid_input" | "error";
export type FailureKind = "value_mismatch" | "unknown_value" | "missing_transaction" | "extra_transaction";
export type EdgeType = "sequential" | "combinational";
export type EdgeRole = "data" | "control";
export type BranchStatus = "retained" | "pruned" | "unknown";
export type AssertionValidation = "candidate" | "validated" | "failed" | "unsupported";
export type ProvenanceKind = "analysis" | "synthetic_fixture";

export interface LogicValue {
  width: number;
  bits: string; // [01xz]{width}, declared left bit first
}

export interface Observation {
  epoch: number;
  cycle: number;
  phase: Phase;
  time_ps: bigint;
}

export interface Diagnostic {
  code: string;
  message: string;
}

export interface FailureAnchor {
  kind: FailureKind;
  mode: Mode;
  signal_path: string;
  clock_path: string;
  observation: Observation;
  transaction_index: number | null;
  expected: LogicValue | null;
  actual: LogicValue | null;
  detail: string;
}

export interface ComparisonResult {
  status: Status;
  mode: Mode;
  failure: FailureAnchor | null;
  additional_mismatches: FailureAnchor[];
  compared_values: number;
  diagnostics: Diagnostic[];
}

export interface SourceFile {
  id: string;
  path: string;
  sha256: string;
}

export interface SourceSpan {
  source_id: string;
  line: number;
  end_line: number;
}

export interface BitRange {
  left: number;
  right: number;
}

export interface CausalNode {
  id: string;
  signal_path: string;
  bit_range: BitRange | null;
  observation: Observation;
  value: LogicValue | null;
  location: SourceSpan | null;
}

export interface CausalEdge {
  id: string;
  source: string;
  target: string;
  edge_type: EdgeType;
  role: EdgeRole;
  branch: BranchStatus;
  reason: string;
}

export interface Suspect {
  id: string;
  rank: number;
  node_id: string;
  location: SourceSpan | null;
  reason: string;
  evidence_node_ids: string[];
}

export interface CandidateAssertion {
  id: string;
  text: string;
  validation: AssertionValidation;
  tool: string | null;
}

export interface Provenance {
  kind: ProvenanceKind;
  case_id: string | null;
  note: string;
}

export interface Sample {
  time_ps: bigint;
  value: LogicValue;
}

export interface SignalWindow {
  signal_path: string;
  width: number;
  initial: LogicValue | null;
  samples: Sample[];
}

export interface Interval {
  start_ps: bigint;
  end_ps: bigint; // exclusive
}

export interface WaveformWindow {
  start_ps: bigint;
  end_ps: bigint; // exclusive
  signals: SignalWindow[];
  unavailable: Interval[];
}

export interface CausalReport {
  provenance: Provenance;
  status: Status;
  failure: FailureAnchor | null;
  anchor_node_id: string | null;
  nodes: CausalNode[];
  edges: CausalEdge[];
  suspects: Suspect[];
  sources: SourceFile[];
  assertions: CandidateAssertion[];
  waveform: WaveformWindow | null;
  truncated: boolean;
  diagnostics: Diagnostic[];
}

export interface RunStep {
  name: string;
  exit_code: number | null;
  log: string;
  log_truncated: boolean;
}

export interface RunMeta {
  status: string;
  message: string;
  simulator: string | null;
  tool_version: string | null;
  steps: RunStep[];
}

export type DocumentKind = "run_record" | "mock_fixture" | "causal_report";

/** One loaded, validated document, normalized for display. */
export interface Loaded {
  kind: DocumentKind;
  title: string;
  /** True when any part of the displayed data is hand-authored. */
  fixture: boolean;
  fixtureId: string | null;
  fixtureNote: string | null;
  status: Status;
  comparisons: Partial<Record<Mode, ComparisonResult>>;
  primaryMode: Mode | null;
  failure: FailureAnchor | null;
  report: CausalReport | null;
  diagnostics: Diagnostic[];
  run: RunMeta | null;
  comparisonKind: string | null;
  raw: unknown;
}
