import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, TERMINAL, fetchDeclaredSources,
  type ApiClient, type CaseInfo, type JobInfo,
} from "../api/client";
import type { SourceText } from "../model/sources";

type Service =
  | { kind: "checking" }
  | { kind: "offline"; message: string }
  | { kind: "online"; cases: CaseInfo[] };

interface Props {
  client: ApiClient;
  pollMs?: number;
  /** A finished or cancelled job's run record, plus any verified source text. */
  onResult: (record: unknown, sources: SourceText[], problems: string[]) => void;
}

const SERVE_COMMAND = "python -m src.app.cli serve --dev";

/** Runs a benchmark through the local service. The service being absent is a
 * normal state: saved reports still open without it. */
export function RunPanel({ client, pollMs = 300, onResult }: Props) {
  const [service, setService] = useState<Service>({ kind: "checking" });
  const [caseId, setCaseId] = useState("");
  const [variant, setVariant] = useState<"correct" | "mutant">("mutant");
  const [schedule, setSchedule] = useState("");
  const [comparison, setComparison] = useState<"track-a" | "self-check">("track-a");
  const [job, setJob] = useState<JobInfo | null>(null);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const live = useRef(true);

  const connect = useCallback(async () => {
    setService({ kind: "checking" });
    setError("");
    try {
      await client.health();
      const cases = await client.cases();
      if (!live.current) return;
      setService({ kind: "online", cases });
      const first = cases[0];
      if (first) {
        setCaseId(first.case_id);
        setSchedule(first.schedules[0] ?? "");
      }
    } catch (err) {
      if (!live.current) return;
      setService({
        kind: "offline",
        message: err instanceof ApiError ? err.message : `Unexpected error: ${String(err)}`,
      });
    }
  }, [client]);

  useEffect(() => {
    live.current = true;
    void connect();
    return () => {
      live.current = false;
      clearTimeout(timer.current);
    };
  }, [connect]);

  const finish = useCallback(async (done: JobInfo) => {
    setJob(done);
    if (done.state === "failed") {
      setError(done.message ?? "The job failed.");
      return;
    }
    // A job cancelled before it started has no run record; that is not an error.
    if (done.state === "cancelled") {
      try {
        const record = await client.result(done.id);
        if (live.current) onResult(record, [], []);
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 409) && live.current) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
      return;
    }
    try {
      const record = await client.result(done.id);
      const { texts, problems } = await fetchDeclaredSources(client, done.id, record);
      if (live.current) onResult(record, texts, problems);
    } catch (err) {
      if (live.current) setError(err instanceof Error ? err.message : String(err));
    }
  }, [client, onResult]);

  const poll = useCallback((id: string) => {
    timer.current = setTimeout(async () => {
      try {
        const next = await client.job(id);
        if (!live.current) return;
        if (TERMINAL.includes(next.state)) await finish(next);
        else {
          setJob(next);
          poll(id);
        }
      } catch (err) {
        if (!live.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setJob(null);
        if (err instanceof ApiError && err.kind === "offline") setService({ kind: "offline", message: err.message });
      }
    }, pollMs);
  }, [client, finish, pollMs]);

  const run = async () => {
    setError("");
    try {
      const started = await client.createJob({ case_id: caseId, variant, schedule, comparison });
      setJob(started);
      poll(started.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      if (err instanceof ApiError && err.kind === "offline") setService({ kind: "offline", message: err.message });
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      const reply = await client.cancel(job.id);
      // Never step back over a newer state the poll already saw.
      setJob((current) => (current && TERMINAL.includes(current.state) ? current : reply));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const active = job !== null && !TERMINAL.includes(job.state);
  const selected = service.kind === "online" ? service.cases.find((c) => c.case_id === caseId) : undefined;

  return (
    <section className="run-panel" aria-label="Run an analysis" data-testid="run-panel">
      {service.kind === "checking" && <p role="status" data-testid="service-checking">Checking for the local analysis service…</p>}
      {service.kind === "offline" && (
        <div role="alert" data-testid="service-offline">
          <strong>Analysis service not available.</strong> {service.message}{" "}
          <span>Start it with <code>{SERVE_COMMAND}</code>. Saved reports still open without it.</span>{" "}
          <button type="button" onClick={() => void connect()} data-testid="service-retry">Try again</button>
        </div>
      )}
      {service.kind === "online" && (
        <div className="run-controls">
          <label className="select"><span>Benchmark</span>
            <select value={caseId} disabled={active} data-testid="run-case"
              onChange={(e) => {
                setCaseId(e.target.value);
                setSchedule(service.cases.find((c) => c.case_id === e.target.value)?.schedules[0] ?? "");
              }}>
              {service.cases.map((c) => <option key={c.case_id} value={c.case_id}>{c.case_id}</option>)}
            </select>
          </label>
          <label className="select"><span>Variant</span>
            <select value={variant} disabled={active} data-testid="run-variant"
              onChange={(e) => setVariant(e.target.value as "correct" | "mutant")}>
              <option value="mutant">mutant</option><option value="correct">correct</option>
            </select>
          </label>
          <label className="select"><span>Schedule</span>
            <select value={schedule} disabled={active} data-testid="run-schedule" onChange={(e) => setSchedule(e.target.value)}>
              {(selected?.schedules ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="select"><span>Comparison</span>
            <select value={comparison} disabled={active} data-testid="run-comparison"
              onChange={(e) => setComparison(e.target.value as "track-a" | "self-check")}>
              <option value="track-a">Track A waveform engine</option>
              <option value="self-check">Benchmark self-check</option>
            </select>
          </label>
          <button type="button" className="primary" disabled={active || !caseId || !schedule} onClick={() => void run()} data-testid="run-start">
            Run analysis
          </button>
          <button type="button" disabled={!active || job?.state === "cancelling"} onClick={() => void cancel()} data-testid="run-cancel">
            Cancel
          </button>
          {job && (
            <span role="status" data-testid="run-state">
              {job.case_id} {job.variant}/{job.schedule}: {job.state}
            </span>
          )}
        </div>
      )}
      {error && <p className="run-error" role="alert" data-testid="run-error">{error}</p>}
    </section>
  );
}
