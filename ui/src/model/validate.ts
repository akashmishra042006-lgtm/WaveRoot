// Schema-1 validation for every document the viewer opens.
//
// Nothing is rendered from unvalidated data (ui-spec rule 5, scenario 7). The
// checks mirror src/contracts.py: unknown and missing fields are rejected,
// widths match bit strings, timestamps are exact canonical decimals kept as
// BigInt, and graph references must resolve.

import type {
  BranchStatus,
  CandidateAssertion,
  CausalEdge,
  CausalNode,
  CausalReport,
  ComparisonResult,
  Diagnostic,
  FailureAnchor,
  Interval,
  Loaded,
  LogicValue,
  Mode,
  Observation,
  Provenance,
  RunMeta,
  SignalWindow,
  SourceFile,
  SourceSpan,
  Status,
  Suspect,
  WaveformWindow,
} from "./types";

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export const SUPPORTED_SCHEMA = 1;
const RUN_RECORD = "waveroot-track-c-run";
const MAX_TIME_PS = (1n << 128n) - 1n;
const MAX_TIME_DIGITS = MAX_TIME_PS.toString().length;
const STATUSES: readonly Status[] = ["pass", "fail", "inconclusive", "unsupported", "invalid_input", "error"];
const EXIT_CODES: Record<Status, number> = {
  pass: 0, fail: 1, invalid_input: 2, error: 2, unsupported: 3, inconclusive: 3,
};

type Obj = Record<string, unknown>;

function fail(where: string, message: string): never {
  throw new ValidationError(`${where}: ${message}`);
}

function object(value: unknown, where: string, required: readonly string[], optional: readonly string[] = []): Obj {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(where, "expected an object");
  const obj = value as Obj;
  for (const key of required) if (!(key in obj)) fail(where, `missing field "${key}"`);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(obj)) if (!allowed.has(key)) fail(where, `unknown field "${key}"`);
  return obj;
}

function array(value: unknown, where: string): unknown[] {
  if (!Array.isArray(value)) fail(where, "expected a list");
  return value;
}

function str(value: unknown, where: string): string {
  if (typeof value !== "string" || value.length === 0) fail(where, "expected a non-empty string");
  return value;
}

function count(value: unknown, where: string, min = 0): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < min) {
    fail(where, `expected an integer >= ${min}`);
  }
  return value;
}

function oneOf<T extends string>(value: unknown, options: readonly T[], where: string): T {
  if (typeof value !== "string" || !(options as readonly string[]).includes(value)) {
    fail(where, `expected one of ${options.join(", ")}`);
  }
  return value as T;
}

function bool(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") fail(where, "expected true or false");
  return value;
}

export function parseTime(value: unknown, where: string): bigint {
  let t: bigint;
  if (typeof value === "string") {
    if (value.length > MAX_TIME_DIGITS) fail(where, `timestamp longer than ${MAX_TIME_DIGITS} digits`);
    if (!/^(0|[1-9][0-9]*)$/.test(value)) fail(where, "timestamp must be a canonical non-negative decimal");
    t = BigInt(value);
  } else if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    t = BigInt(value);
  } else {
    fail(where, "timestamp must be a decimal string (or a safe non-negative integer)");
  }
  if (t > MAX_TIME_PS) fail(where, "timestamp exceeds MAX_TIME_PS");
  return t;
}

function logicValue(value: unknown, where: string): LogicValue {
  const o = object(value, where, ["width", "bits"]);
  const width = count(o.width, `${where}.width`, 1);
  const bits = str(o.bits, `${where}.bits`).toLowerCase();
  if (bits.length !== width) fail(where, `bits has ${bits.length} characters but width is ${width}`);
  if (!/^[01xz]+$/.test(bits)) fail(where, "bits may contain only 0, 1, x, z");
  return { width, bits };
}

function nullable<T>(value: unknown, parse: (v: unknown) => T): T | null {
  return value === null || value === undefined ? null : parse(value);
}

function observation(value: unknown, where: string): Observation {
  const o = object(value, where, ["epoch", "cycle", "phase", "time_ps"]);
  return {
    epoch: count(o.epoch, `${where}.epoch`),
    cycle: count(o.cycle, `${where}.cycle`),
    phase: oneOf(o.phase, ["before", "settled"] as const, `${where}.phase`),
    time_ps: parseTime(o.time_ps, `${where}.time_ps`),
  };
}

function diagnostics(value: unknown, where: string): Diagnostic[] {
  if (value === undefined) return [];
  return array(value, where).map((d, i) => {
    const o = object(d, `${where}[${i}]`, ["code", "message"]);
    const code = str(o.code, `${where}[${i}].code`);
    if (!/^[A-Z][A-Z0-9_]*$/.test(code)) fail(`${where}[${i}].code`, "must be UPPER_SNAKE_CASE");
    return { code, message: str(o.message, `${where}[${i}].message`) };
  });
}

function failureAnchor(value: unknown, where: string): FailureAnchor {
  const o = object(value, where,
    ["kind", "mode", "signal_path", "clock_path", "observation", "expected", "actual", "detail"],
    ["transaction_index"]);
  const kind = oneOf(o.kind, ["value_mismatch", "unknown_value", "missing_transaction", "extra_transaction"] as const, `${where}.kind`);
  const anchor: FailureAnchor = {
    kind,
    mode: oneOf(o.mode, ["cycle", "handshake"] as const, `${where}.mode`),
    signal_path: str(o.signal_path, `${where}.signal_path`),
    clock_path: str(o.clock_path, `${where}.clock_path`),
    observation: observation(o.observation, `${where}.observation`),
    transaction_index: nullable(o.transaction_index, (v) => count(v, `${where}.transaction_index`)),
    expected: nullable(o.expected, (v) => logicValue(v, `${where}.expected`)),
    actual: nullable(o.actual, (v) => logicValue(v, `${where}.actual`)),
    detail: str(o.detail, `${where}.detail`),
  };
  const { expected, actual } = anchor;
  if (expected && /[xz]/.test(expected.bits)) fail(where, "expected values must be fully known");
  if (kind === "missing_transaction" && (!expected || actual)) fail(where, "missing_transaction needs expected and no actual");
  if (kind === "extra_transaction" && (expected || !actual)) fail(where, "extra_transaction needs actual and no expected");
  if (kind === "value_mismatch" || kind === "unknown_value") {
    if (!expected || !actual) fail(where, `${kind} needs expected and actual`);
    if (expected.width !== actual.width) fail(where, "expected and actual widths differ");
    const unknown = /[xz]/.test(actual.bits);
    if (kind === "unknown_value" && !unknown) fail(where, "unknown_value requires x/z in the actual value");
    if (kind === "value_mismatch" && unknown) fail(where, "an actual value with x/z is unknown_value");
  }
  return anchor;
}

function comparison(value: unknown, where: string): ComparisonResult {
  const o = object(value, where, ["schema_version", "status", "mode", "failure", "compared_values"],
    ["additional_mismatches", "diagnostics"]);
  schemaVersion(o.schema_version, where);
  const status = oneOf(o.status, STATUSES, `${where}.status`);
  const failure = nullable(o.failure, (v) => failureAnchor(v, `${where}.failure`));
  const result: ComparisonResult = {
    status,
    mode: oneOf(o.mode, ["cycle", "handshake"] as const, `${where}.mode`),
    failure,
    additional_mismatches: o.additional_mismatches === undefined ? [] :
      array(o.additional_mismatches, `${where}.additional_mismatches`).map((f, i) => failureAnchor(f, `${where}.additional_mismatches[${i}]`)),
    compared_values: count(o.compared_values, `${where}.compared_values`),
    diagnostics: diagnostics(o.diagnostics, `${where}.diagnostics`),
  };
  if ((status === "fail") !== (failure !== null)) fail(where, "a failure is present exactly when status is fail");
  if (status !== "pass" && status !== "fail" && result.diagnostics.length === 0) {
    fail(where, `a ${status} result must carry a diagnostic`);
  }
  return result;
}

function schemaVersion(value: unknown, where: string): void {
  if (value !== SUPPORTED_SCHEMA) {
    throw new ValidationError(
      `${where}: unsupported schema_version ${JSON.stringify(value)}. ` +
        `This viewer reads schema ${SUPPORTED_SCHEMA}; open the report with a matching WaveRoot version.`,
    );
  }
}

function span(value: unknown, where: string, sources: Set<string>): SourceSpan {
  const o = object(value, where, ["source_id", "line", "end_line"]);
  const s: SourceSpan = {
    source_id: str(o.source_id, `${where}.source_id`),
    line: count(o.line, `${where}.line`, 1),
    end_line: count(o.end_line, `${where}.end_line`, 1),
  };
  if (s.end_line < s.line) fail(where, "end_line precedes line");
  if (!sources.has(s.source_id)) fail(where, `unknown source "${s.source_id}"`);
  return s;
}

function unique(ids: string[], what: string): void {
  const seen = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) fail("report", `duplicate ${what} id "${id}"`);
    seen.add(id);
  }
}

function waveform(value: unknown, where: string): WaveformWindow {
  const o = object(value, where, ["start_ps", "end_ps", "signals"], ["unavailable"]);
  const start = parseTime(o.start_ps, `${where}.start_ps`);
  const end = parseTime(o.end_ps, `${where}.end_ps`);
  if (end <= start) fail(where, "empty waveform window");
  const unavailable: Interval[] = o.unavailable === undefined ? [] :
    array(o.unavailable, `${where}.unavailable`).map((u, i) => {
      const io = object(u, `${where}.unavailable[${i}]`, ["start_ps", "end_ps"]);
      return { start_ps: parseTime(io.start_ps, `${where}.unavailable[${i}]`), end_ps: parseTime(io.end_ps, `${where}.unavailable[${i}]`) };
    });
  const signals: SignalWindow[] = array(o.signals, `${where}.signals`).map((s, i) => {
    const w = `${where}.signals[${i}]`;
    const so = object(s, w, ["signal_path", "width", "initial"], ["samples"]);
    const width = count(so.width, `${w}.width`, 1);
    const initial = nullable(so.initial, (v) => logicValue(v, `${w}.initial`));
    if (initial && initial.width !== width) fail(w, "initial width differs from the signal width");
    let last = start;
    const samples = (so.samples === undefined ? [] : array(so.samples, `${w}.samples`)).map((x, j) => {
      const xo = object(x, `${w}.samples[${j}]`, ["time_ps", "value"]);
      const t = parseTime(xo.time_ps, `${w}.samples[${j}].time_ps`);
      const v = logicValue(xo.value, `${w}.samples[${j}].value`);
      if (v.width !== width) fail(`${w}.samples[${j}]`, "sample width differs from the signal width");
      if (t <= last || t >= end) fail(`${w}.samples[${j}]`, "samples must ascend strictly inside the window");
      last = t;
      return { time_ps: t, value: v };
    });
    return { signal_path: str(so.signal_path, `${w}.signal_path`), width, initial, samples };
  });
  return { start_ps: start, end_ps: end, signals, unavailable };
}

export function causalReport(value: unknown, where = "report"): CausalReport {
  const o = object(value, where, ["schema_version", "provenance", "status", "failure"],
    ["anchor_node_id", "nodes", "edges", "suspects", "sources", "assertions", "waveform", "truncated", "diagnostics"]);
  schemaVersion(o.schema_version, where);
  const po = object(o.provenance, `${where}.provenance`, ["kind", "note"], ["case_id"]);
  const provenance: Provenance = {
    kind: oneOf(po.kind, ["analysis", "synthetic_fixture"] as const, `${where}.provenance.kind`),
    case_id: nullable(po.case_id, (v) => str(v, `${where}.provenance.case_id`)),
    note: str(po.note, `${where}.provenance.note`),
  };
  const sources: SourceFile[] = (o.sources === undefined ? [] : array(o.sources, `${where}.sources`)).map((s, i) => {
    const so = object(s, `${where}.sources[${i}]`, ["id", "path", "sha256"]);
    const path = str(so.path, `${where}.sources[${i}].path`);
    if (path.startsWith("/") || path.split("/").includes("..")) fail(`${where}.sources[${i}].path`, "must be a normalized relative path");
    const sha256 = str(so.sha256, `${where}.sources[${i}].sha256`);
    if (!/^[a-f0-9]{64}$/.test(sha256)) fail(`${where}.sources[${i}].sha256`, "must be a lowercase SHA-256");
    return { id: str(so.id, `${where}.sources[${i}].id`), path, sha256 };
  });
  unique(sources.map((s) => s.id), "source");
  const sourceIds = new Set(sources.map((s) => s.id));

  const nodes: CausalNode[] = (o.nodes === undefined ? [] : array(o.nodes, `${where}.nodes`)).map((n, i) => {
    const w = `${where}.nodes[${i}]`;
    const no = object(n, w, ["id", "signal_path", "observation", "value", "location"], ["bit_range"]);
    const node: CausalNode = {
      id: str(no.id, `${w}.id`),
      signal_path: str(no.signal_path, `${w}.signal_path`),
      bit_range: nullable(no.bit_range, (v) => {
        const bo = object(v, `${w}.bit_range`, ["left", "right"]);
        return { left: count(bo.left, `${w}.bit_range.left`), right: count(bo.right, `${w}.bit_range.right`) };
      }),
      observation: observation(no.observation, `${w}.observation`),
      value: nullable(no.value, (v) => logicValue(v, `${w}.value`)),
      location: nullable(no.location, (v) => span(v, `${w}.location`, sourceIds)),
    };
    if (node.bit_range && node.value) {
      const width = Math.abs(node.bit_range.left - node.bit_range.right) + 1;
      if (width !== node.value.width) fail(w, "value width differs from bit_range");
    }
    return node;
  });
  unique(nodes.map((n) => n.id), "node");
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const edges: CausalEdge[] = (o.edges === undefined ? [] : array(o.edges, `${where}.edges`)).map((e, i) => {
    const w = `${where}.edges[${i}]`;
    const eo = object(e, w, ["id", "source", "target", "edge_type", "role", "branch", "reason"]);
    const edge: CausalEdge = {
      id: str(eo.id, `${w}.id`),
      source: str(eo.source, `${w}.source`),
      target: str(eo.target, `${w}.target`),
      edge_type: oneOf(eo.edge_type, ["sequential", "combinational"] as const, `${w}.edge_type`),
      role: oneOf(eo.role, ["data", "control"] as const, `${w}.role`),
      branch: oneOf(eo.branch, ["retained", "pruned", "unknown"] as readonly BranchStatus[], `${w}.branch`),
      reason: str(eo.reason, `${w}.reason`),
    };
    if (!byId.has(edge.source) || !byId.has(edge.target)) {
      fail(w, `dangling node reference (${edge.source} -> ${edge.target})`);
    }
    return edge;
  });
  unique(edges.map((e) => e.id), "edge");

  const suspects: Suspect[] = (o.suspects === undefined ? [] : array(o.suspects, `${where}.suspects`)).map((s, i) => {
    const w = `${where}.suspects[${i}]`;
    const so = object(s, w, ["id", "rank", "node_id", "location", "reason"], ["evidence_node_ids"]);
    const suspect: Suspect = {
      id: str(so.id, `${w}.id`),
      rank: count(so.rank, `${w}.rank`, 1),
      node_id: str(so.node_id, `${w}.node_id`),
      location: nullable(so.location, (v) => span(v, `${w}.location`, sourceIds)),
      reason: str(so.reason, `${w}.reason`),
      evidence_node_ids: (so.evidence_node_ids === undefined ? [] : array(so.evidence_node_ids, `${w}.evidence_node_ids`))
        .map((id, j) => str(id, `${w}.evidence_node_ids[${j}]`)),
    };
    for (const id of [suspect.node_id, ...suspect.evidence_node_ids]) {
      if (!byId.has(id)) fail(w, `dangling node reference "${id}"`);
    }
    return suspect;
  });
  unique(suspects.map((s) => s.id), "suspect");
  const ranks = suspects.map((s) => s.rank).sort((a, b) => a - b);
  if (ranks.some((r, i) => r !== i + 1)) fail(where, "suspect ranks must be exactly 1..N without ties");

  const assertions: CandidateAssertion[] = (o.assertions === undefined ? [] : array(o.assertions, `${where}.assertions`)).map((a, i) => {
    const w = `${where}.assertions[${i}]`;
    const ao = object(a, w, ["id", "text", "validation"], ["tool"]);
    const assertion: CandidateAssertion = {
      id: str(ao.id, `${w}.id`),
      text: str(ao.text, `${w}.text`),
      validation: oneOf(ao.validation, ["candidate", "validated", "failed", "unsupported"] as const, `${w}.validation`),
      tool: nullable(ao.tool, (v) => str(v, `${w}.tool`)),
    };
    if ((assertion.validation === "validated" || assertion.validation === "failed") && !assertion.tool) {
      fail(w, "a validated or failed assertion must name its tool");
    }
    return assertion;
  });
  unique(assertions.map((a) => a.id), "assertion");

  const status = oneOf(o.status, STATUSES, `${where}.status`);
  const failure = nullable(o.failure, (v) => failureAnchor(v, `${where}.failure`));
  const anchorId = nullable(o.anchor_node_id, (v) => str(v, `${where}.anchor_node_id`));
  const report: CausalReport = {
    provenance, status, failure, anchor_node_id: anchorId, nodes, edges, suspects, sources, assertions,
    waveform: nullable(o.waveform, (v) => waveform(v, `${where}.waveform`)),
    truncated: o.truncated === undefined ? false : bool(o.truncated, `${where}.truncated`),
    diagnostics: diagnostics(o.diagnostics, `${where}.diagnostics`),
  };

  if (status === "fail") {
    if (!failure) fail(where, "a fail report requires a failure");
    if (nodes.length === 0 && report.diagnostics.length === 0) fail(where, "an unlocalized failure must be explained in diagnostics");
  } else if (failure || nodes.length || edges.length || suspects.length || assertions.length || anchorId) {
    fail(where, `a ${status} report carries no failure or graph`);
  } else if (status !== "pass" && report.diagnostics.length === 0) {
    fail(where, `a ${status} report must explain itself in diagnostics`);
  }
  if (nodes.length > 0) {
    const anchor = byId.get(anchorId ?? "");
    if (!anchor || !failure) fail(where, "a graph requires anchor_node_id naming an existing node");
    if (anchor.signal_path !== failure.signal_path || !sameObservation(anchor.observation, failure.observation)) {
      fail(where, "anchor node does not match the failure signal and observation");
    }
  }
  return report;
}

export function sameObservation(a: Observation, b: Observation): boolean {
  return a.epoch === b.epoch && a.cycle === b.cycle && a.phase === b.phase && a.time_ps === b.time_ps;
}

function runMeta(value: unknown): RunMeta | null {
  if (value === null || value === undefined) return null;
  // Track C provenance: type-check the fields the viewer shows, keep the rest raw.
  if (typeof value !== "object" || Array.isArray(value)) fail("run", "expected an object");
  const o = value as Obj;
  const steps = Array.isArray(o.steps) ? o.steps : [];
  return {
    status: typeof o.status === "string" ? o.status : "unknown",
    message: typeof o.message === "string" ? o.message : "",
    simulator: typeof o.simulator === "string" ? o.simulator : null,
    tool_version: typeof o.tool_version === "string" ? o.tool_version : null,
    steps: steps.map((s, i) => {
      const so = object(s, `run.steps[${i}]`, ["name", "exit_code", "log"], ["argv", "duration_s", "log_bytes_total", "log_truncated"]);
      const exit = so.exit_code;
      // Negative exit codes are signals (for example -9 after a kill).
      if (exit !== null && !(typeof exit === "number" && Number.isInteger(exit))) {
        fail(`run.steps[${i}].exit_code`, "expected an integer or null");
      }
      return {
        name: str(so.name, `run.steps[${i}].name`),
        exit_code: exit,
        log: str(so.log, `run.steps[${i}].log`),
        log_truncated: so.log_truncated === true,
      };
    }),
  };
}

function loadRunRecord(o: Obj): Loaded {
  object(o, "run record", ["record", "record_version", "status", "exit_code", "case_id", "variant", "schedule",
    "fixture_mode", "comparison_adapter", "causal_adapter", "primary_mode", "comparisons", "causal", "diagnostics", "run"]);
  if (o.record_version !== 1) {
    throw new ValidationError(`run record: unsupported record_version ${JSON.stringify(o.record_version)}; this viewer reads version 1.`);
  }
  const status = oneOf(o.status, STATUSES, "run record.status");
  if (o.exit_code !== EXIT_CODES[status]) fail("run record", "exit_code does not match status");
  const comparisonsRaw = object(o.comparisons, "run record.comparisons", [], ["cycle", "handshake"]);
  const comparisons: Partial<Record<Mode, ComparisonResult>> = {};
  for (const mode of ["cycle", "handshake"] as const) {
    if (comparisonsRaw[mode] !== undefined) {
      const result = comparison(comparisonsRaw[mode], `comparisons.${mode}`);
      if (result.mode !== mode) fail(`comparisons.${mode}`, "mode does not match its key");
      comparisons[mode] = result;
    }
  }
  const primaryMode = o.primary_mode === null ? null : oneOf(o.primary_mode, ["cycle", "handshake"] as const, "run record.primary_mode");
  const report = o.causal === null ? null : causalReport(o.causal, "causal");
  const fixtureMode = bool(o.fixture_mode, "run record.fixture_mode");
  if (report?.provenance.kind === "synthetic_fixture" && !fixtureMode) {
    fail("run record", "synthetic causal data in a record not marked fixture_mode");
  }
  const adapter = object(o.comparison_adapter, "run record.comparison_adapter", ["name", "kind"]);
  const failure = primaryMode ? comparisons[primaryMode]?.failure ?? null : null;
  if (report?.failure && failure && !sameFailure(report.failure, failure)) {
    fail("run record", "causal report and comparison disagree about the failure");
  }
  return {
    kind: "run_record",
    title: `${typeof o.case_id === "string" ? o.case_id : "run"} · ${String(o.variant)}/${String(o.schedule)}`,
    fixture: fixtureMode,
    fixtureId: fixtureMode ? report?.provenance.case_id ?? "fixture" : null,
    fixtureNote: fixtureMode ? report?.provenance.note ?? "Fixture data in use" : null,
    status,
    comparisons,
    primaryMode,
    failure,
    report,
    diagnostics: diagnostics(o.diagnostics, "run record.diagnostics"),
    run: runMeta(o.run),
    comparisonKind: str(adapter.kind, "comparison_adapter.kind"),
    raw: o,
  };
}

function sameFailure(a: FailureAnchor, b: FailureAnchor): boolean {
  return a.kind === b.kind && a.signal_path === b.signal_path && sameObservation(a.observation, b.observation) &&
    a.expected?.bits === b.expected?.bits && a.actual?.bits === b.actual?.bits;
}

function fromReport(report: CausalReport, kind: Loaded["kind"], raw: unknown, comparisons: Loaded["comparisons"] = {}): Loaded {
  const fixture = report.provenance.kind === "synthetic_fixture";
  return {
    kind,
    title: report.provenance.case_id ?? "causal report",
    fixture,
    fixtureId: fixture ? report.provenance.case_id ?? "fixture" : null,
    fixtureNote: fixture ? report.provenance.note : null,
    status: report.status,
    comparisons,
    primaryMode: report.failure?.mode ?? null,
    failure: report.failure,
    report,
    diagnostics: [],
    run: null,
    comparisonKind: null,
    raw,
  };
}

/** Parse and validate any supported document. Throws ValidationError. */
export function loadDocument(value: unknown): Loaded {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ValidationError("The file is not a WaveRoot report: expected a JSON object.");
  }
  const o = value as Obj;
  if (o.record === RUN_RECORD) return loadRunRecord(o);
  if ("record" in o) throw new ValidationError(`Unrecognized record type ${JSON.stringify(o.record)}.`);
  if ("trace" in o && "golden" in o && "report" in o) {
    object(o, "mock fixture", ["schema_version", "provenance", "trace", "golden", "comparison", "report"]);
    schemaVersion(o.schema_version, "mock fixture");
    const report = causalReport(o.report);
    const cmp = comparison(o.comparison, "mock fixture.comparison");
    if (report.provenance.kind !== "synthetic_fixture") fail("mock fixture", "a mock fixture must be labeled synthetic_fixture");
    return fromReport(report, "mock_fixture", value, { [cmp.mode]: cmp });
  }
  if ("provenance" in o && "status" in o) return fromReport(causalReport(o), "causal_report", value);
  throw new ValidationError("Unrecognized document: expected a run record, a mock fixture, or a causal report.");
}

/** Parse JSON text, keeping the error message user-facing. */
export function parseDocumentText(text: string): Loaded {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new ValidationError("The file is not valid JSON.");
  }
  return loadDocument(value);
}
