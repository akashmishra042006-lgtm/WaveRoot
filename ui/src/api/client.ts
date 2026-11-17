// Client for the local WaveRoot API (src/app/api.py). The browser supplies the
// Origin header itself; this module never builds paths, only ids.

import type { SourceText } from "../model/sources";
import { makeSourceText } from "../model/sources";

export const DEFAULT_API = "http://127.0.0.1:8765";

export interface CaseInfo {
  case_id: string;
  variants: string[];
  schedules: string[];
  simulator: string;
  modes: string[];
}

export type JobState = "queued" | "running" | "cancelling" | "cancelled" | "finished" | "failed";
export const TERMINAL: readonly JobState[] = ["cancelled", "finished", "failed"];

export interface JobInfo {
  id: string;
  state: JobState;
  case_id: string;
  variant: string;
  schedule: string;
  comparison: string;
  created_at: string;
  message?: string;
}

export interface JobRequest {
  case_id: string;
  variant: "correct" | "mutant";
  schedule: string;
  comparison: "track-a" | "self-check";
}

/** `offline`: the service could not be reached at all. `refused`: it answered
 * with an error (403 origin, 404, 409, ...). */
export class ApiError extends Error {
  constructor(message: string, readonly kind: "offline" | "refused", readonly status?: number, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

/** Only loopback services are ever contacted, whatever the page's query says. */
export function apiBaseFromLocation(search: string, configured?: string): string {
  const requested = new URLSearchParams(search).get("api") ?? configured ?? DEFAULT_API;
  try {
    const url = new URL(requested);
    const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (url.protocol === "http:" && loopback && !url.username && !url.password) return url.origin;
  } catch {
    // fall through to the default
  }
  return DEFAULT_API;
}

export class ApiClient {
  constructor(readonly base: string = DEFAULT_API, private readonly fetchImpl: typeof fetch = (...a) => fetch(...a)) {}

  private async request(method: "GET" | "POST", path: string, body?: unknown): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetchImpl(this.base + path, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
      });
    } catch {
      throw new ApiError(`The analysis service at ${this.base} is not reachable.`, "offline");
    }
    if (!response.ok) {
      let message = `The service refused the request (HTTP ${response.status}).`;
      let code: string | undefined;
      try {
        const data = (await response.json()) as { error?: { code?: string; message?: string } };
        if (data.error?.message) message = data.error.message;
        code = data.error?.code;
      } catch {
        // keep the generic message
      }
      throw new ApiError(message, "refused", response.status, code);
    }
    return response;
  }

  async health(): Promise<void> {
    await this.request("GET", "/api/health");
  }
  async cases(): Promise<CaseInfo[]> {
    return ((await (await this.request("GET", "/api/cases")).json()) as { cases: CaseInfo[] }).cases;
  }
  async createJob(req: JobRequest): Promise<JobInfo> {
    return (await (await this.request("POST", "/api/jobs", req)).json()) as JobInfo;
  }
  async job(id: string): Promise<JobInfo> {
    return (await (await this.request("GET", `/api/jobs/${id}`)).json()) as JobInfo;
  }
  async cancel(id: string): Promise<JobInfo> {
    return (await (await this.request("POST", `/api/jobs/${id}/cancel`, {})).json()) as JobInfo;
  }
  async result(id: string): Promise<unknown> {
    return (await this.request("GET", `/api/jobs/${id}/result`)).json();
  }
  async sourceText(id: string, sourceId: string): Promise<string> {
    return (await this.request("GET", `/api/jobs/${id}/sources/${encodeURIComponent(sourceId)}`)).text();
  }
}

/** Fetch every source a record's causal report declares. The server checks the
 * SHA-256; the viewer checks it again when it matches text to the report, so a
 * wrong file is never shown. Problems are reported, not thrown. */
export async function fetchDeclaredSources(
  client: Pick<ApiClient, "sourceText">,
  jobId: string,
  record: unknown,
): Promise<{ texts: SourceText[]; problems: string[] }> {
  const causal = (record as { causal?: { sources?: { id?: unknown; path?: unknown }[] } | null } | null)?.causal;
  const declared = Array.isArray(causal?.sources) ? causal.sources : [];
  const texts: SourceText[] = [];
  const problems: string[] = [];
  for (const entry of declared) {
    if (typeof entry.id !== "string" || typeof entry.path !== "string") continue;
    try {
      texts.push(await makeSourceText(entry.path, await client.sourceText(jobId, entry.id)));
    } catch (err) {
      problems.push(`${entry.path}: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
  return { texts, problems };
}
