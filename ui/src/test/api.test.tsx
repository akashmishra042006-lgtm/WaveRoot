import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import {
  ApiClient, ApiError, TOKEN_HELP, apiBaseFromLocation, fetchDeclaredSources, tokenFromHash,
  type JobInfo, type JobState,
} from "../api/client";
import { SAMPLES } from "../samples";

const FAIL_RECORD = SAMPLES.find((s) => s.id === "real-fifo-fail")!.document;
const CASES = [
  { case_id: "case_01_fifo_wrap", variants: ["correct", "mutant"], schedules: ["directed", "full_hold"], simulator: "verilator", modes: ["cycle"] },
  { case_id: "case_04_alu_xprop", variants: ["correct", "mutant"], schedules: ["directed"], simulator: "icarus", modes: ["cycle"] },
];

function job(state: JobState, over: Partial<JobInfo> = {}): JobInfo {
  return { id: "a".repeat(32), state, case_id: "case_01_fifo_wrap", variant: "mutant", schedule: "directed", comparison: "track-a", created_at: "t", ...over };
}

function fakeClient(over: Partial<Record<keyof ApiClient, unknown>> = {}): ApiClient {
  return {
    base: "http://127.0.0.1:8765",
    health: vi.fn().mockResolvedValue(undefined),
    cases: vi.fn().mockResolvedValue(CASES),
    createJob: vi.fn().mockResolvedValue(job("queued")),
    job: vi.fn().mockResolvedValue(job("finished")),
    cancel: vi.fn().mockResolvedValue(job("cancelling")),
    result: vi.fn().mockResolvedValue(FAIL_RECORD),
    sourceText: vi.fn().mockRejectedValue(new ApiError("no source", "refused", 404)),
    ...over,
  } as unknown as ApiClient;
}

describe("the local service is optional", () => {
  it("says it is offline, explains how to start it, and still opens saved reports", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ health: vi.fn().mockRejectedValue(new ApiError("The analysis service at http://127.0.0.1:8765 is not reachable.", "offline")) });
    render(<App client={client} />);
    const offline = await screen.findByTestId("service-offline");
    expect(offline).toHaveTextContent("not reachable");
    expect(offline).toHaveTextContent("uv run waveroot serve --dev");
    await user.selectOptions(screen.getByTestId("sample-select"), "real-fifo-fail");
    expect(await screen.findByTestId("run-title")).toBeInTheDocument();
    expect(screen.getByTestId("service-offline")).toBeInTheDocument();
  });

  it("asks for the access token when the service refuses without it", async () => {
    const cases = vi.fn().mockRejectedValue(new ApiError(TOKEN_HELP, "refused", 401, "UNAUTHORIZED"));
    render(<App client={fakeClient({ cases })} />);
    const alert = await screen.findByTestId("service-unauthorized");
    expect(alert).toHaveTextContent("#token=");
    expect(screen.queryByTestId("service-offline")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-start")).not.toBeInTheDocument();
  });

  it("retries on request", async () => {
    const user = userEvent.setup();
    const health = vi.fn().mockRejectedValueOnce(new ApiError("down", "offline")).mockResolvedValue(undefined);
    render(<App client={fakeClient({ health })} />);
    await user.click(await screen.findByTestId("service-retry"));
    expect(await screen.findByTestId("run-start")).toBeEnabled();
    expect(health).toHaveBeenCalledTimes(2);
  });
});

describe("running a benchmark", () => {
  it("prevents duplicate jobs while creation is pending and recovers after refusal", async () => {
    const user = userEvent.setup();
    let reject!: (error: Error) => void;
    const pending = new Promise<JobInfo>((_, no) => { reject = no; });
    const client = fakeClient({createJob: vi.fn().mockReturnValueOnce(pending).mockResolvedValue(job("queued"))});
    render(<App client={client} />);
    const start = await screen.findByTestId("run-start");
    await user.dblClick(start);
    expect(client.createJob).toHaveBeenCalledTimes(1);
    expect(start).toBeDisabled();
    expect(screen.getByTestId("run-case")).toBeDisabled();
    expect(screen.getByTestId("run-cancel")).toBeDisabled();
    await act(async () => reject(new ApiError("try again", "refused", 503)));
    expect(await screen.findByTestId("run-error")).toHaveTextContent("try again");
    expect(start).toBeEnabled();
    await user.click(start);
    expect(client.createJob).toHaveBeenCalledTimes(2);
  });

  it("creates a job, follows it, and opens the run record", async () => {
    const user = userEvent.setup();
    const client = fakeClient({
      createJob: vi.fn().mockResolvedValue(job("queued")),
      job: vi.fn().mockResolvedValueOnce(job("running")).mockResolvedValue(job("finished")),
    });
    render(<App client={client} />);
    await user.selectOptions(await screen.findByTestId("run-comparison"), "self-check");
    await user.selectOptions(screen.getByTestId("run-schedule"), "full_hold");
    await user.click(screen.getByTestId("run-start"));
    expect(client.createJob).toHaveBeenCalledWith({
      case_id: "case_01_fifo_wrap", variant: "mutant", schedule: "full_hold", comparison: "self-check",
    });
    await waitFor(() => expect(screen.getByTestId("run-title")).toBeInTheDocument(), { timeout: 3000 });
    expect(client.result).toHaveBeenCalledWith("a".repeat(32));
    expect(screen.getByTestId("failure-signal")).toHaveTextContent("full");
    expect(screen.getByTestId("run-start")).toBeEnabled();
  });

  it("switching benchmark offers that benchmark's schedules", async () => {
    const user = userEvent.setup();
    render(<App client={fakeClient()} />);
    await user.selectOptions(await screen.findByTestId("run-case"), "case_04_alu_xprop");
    const options = [...screen.getByTestId("run-schedule").querySelectorAll("option")].map((o) => o.value);
    expect(options).toEqual(["directed"]);
  });

  it("cancels a running job, keeps following it, and opens the cancelled record", async () => {
    const user = userEvent.setup();
    let cancelled = false;
    const client = fakeClient({
      createJob: vi.fn().mockResolvedValue(job("running")),
      // Like the server: after a cancel the job reads "cancelling" until it stops.
      job: vi.fn().mockImplementation(async () => job(cancelled ? "cancelled" : "running")),
      cancel: vi.fn().mockImplementation(async () => { cancelled = true; return job("cancelling"); }),
    });
    render(<App client={client} />);
    await user.click(await screen.findByTestId("run-start"));
    expect(screen.getByTestId("run-cancel")).toBeEnabled();
    expect(screen.getByTestId("run-start")).toBeDisabled();
    await user.click(screen.getByTestId("run-cancel"));
    expect(client.cancel).toHaveBeenCalledWith("a".repeat(32));
    expect(screen.getByTestId("run-state")).toHaveTextContent("cancelling");
    expect(screen.getByTestId("run-cancel")).toBeDisabled();
    await waitFor(() => expect(client.result).toHaveBeenCalled(), { timeout: 3000 });
    await waitFor(() => expect(screen.getByTestId("run-start")).toBeEnabled());
    expect(screen.getByTestId("run-state")).toHaveTextContent("cancelled");
  });

  it("shows a refusal without discarding the report already open", async () => {
    const user = userEvent.setup();
    const client = fakeClient({
      createJob: vi.fn().mockRejectedValue(new ApiError("this origin may not use the API", "refused", 403, "FORBIDDEN_ORIGIN")),
    });
    render(<App client={client} />);
    await user.selectOptions(screen.getByTestId("sample-select"), "real-fifo-fail");
    await screen.findByTestId("run-title");
    await user.click(await screen.findByTestId("run-start"));
    expect(await screen.findByTestId("run-error")).toHaveTextContent("this origin may not use the API");
    expect(screen.getByTestId("run-title")).toBeInTheDocument();
  });

  it("a job that failed internally shows the server's message", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ job: vi.fn().mockResolvedValue(job("failed", { message: "the job failed internally" })) });
    render(<App client={client} />);
    await user.click(await screen.findByTestId("run-start"));
    expect(await screen.findByTestId("run-error", {}, { timeout: 3000 })).toHaveTextContent("failed internally");
    expect(client.result).not.toHaveBeenCalled();
  });

  it("losing the service mid-job returns to the offline state", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ job: vi.fn().mockRejectedValue(new ApiError("not reachable", "offline")) });
    render(<App client={client} />);
    await user.click(await screen.findByTestId("run-start"));
    expect(await screen.findByTestId("service-offline", {}, { timeout: 3000 })).toBeInTheDocument();
  });
});

describe("cancelling a queued job", () => {
  it("is not reported as an error when there is no record", async () => {
    const user = userEvent.setup();
    let cancelled = false;
    const client = fakeClient({
      createJob: vi.fn().mockResolvedValue(job("queued")),
      job: vi.fn().mockImplementation(async () => job(cancelled ? "cancelled" : "queued")),
      cancel: vi.fn().mockImplementation(async () => { cancelled = true; return job("cancelled"); }),
      result: vi.fn().mockRejectedValue(new ApiError("the job is cancelled; there is no result", "refused", 409, "NOT_READY")),
    });
    render(<App client={client} />);
    await user.click(await screen.findByTestId("run-start"));
    await user.click(screen.getByTestId("run-cancel"));
    await waitFor(() => expect(screen.getByTestId("run-start")).toBeEnabled(), { timeout: 3000 });
    expect(screen.getByTestId("run-state")).toHaveTextContent("cancelled");
    expect(screen.queryByTestId("run-error")).toBeNull();
  });
});

describe("the client", () => {
  const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

  it("maps a network or CORS failure to an offline error that names both causes", async () => {
    // A real browser rejects fetch the same way whether the service is down or
    // refused this origin (403 without CORS headers).
    const client = new ApiClient("http://127.0.0.1:1", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    const err = await client.health().catch((e: unknown) => e);
    expect(err).toMatchObject({ kind: "offline" });
    expect((err as Error).message).toContain("does not allow this page's origin");
    expect((err as Error).message).toContain("--allow-origin");
  });

  it("surfaces the server's error code and message", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "SOURCE_HASH_MISMATCH", message: "changed" } }), { status: 409 }));
    await expect(new ApiClient("http://127.0.0.1:1", fetchImpl).result("x")).rejects.toMatchObject({
      kind: "refused", status: 409, code: "SOURCE_HASH_MISMATCH", message: "changed",
    });
  });

  it("posts JSON ids only, never paths", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(ok(job("queued")));
    await new ApiClient("http://127.0.0.1:8765", fetchImpl).createJob({
      case_id: "case_01_fifo_wrap", variant: "mutant", schedule: "directed", comparison: "track-a",
    });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe("http://127.0.0.1:8765/api/jobs");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(init.cache).toBe("no-store");
  });

  it("sends the access token as a bearer header, and nothing when there is none", async () => {
    const token = "t".repeat(43);
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(ok({ cases: [] })));
    await new ApiClient("http://127.0.0.1:8765", fetchImpl, token).cases();
    expect(fetchImpl.mock.calls[0]![1].headers).toEqual({ Authorization: `Bearer ${token}` });
    await new ApiClient("http://127.0.0.1:8765", fetchImpl).cases();
    expect(fetchImpl.mock.calls[1]![1].headers).toBeUndefined();
  });

  it("explains a missing or stale token instead of echoing the server", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "UNAUTHORIZED", message: "x" } }), { status: 401 }));
    await expect(new ApiClient("http://127.0.0.1:1", fetchImpl).cases()).rejects.toMatchObject({
      kind: "refused", status: 401, code: "UNAUTHORIZED", message: TOKEN_HELP,
    });
  });

  it("reads the token only from the fragment, and only a plausible one", () => {
    const token = "A1_-".repeat(10);
    expect(tokenFromHash(`#token=${token}`)).toBe(token);
    expect(tokenFromHash(`token=${token}`)).toBe(token);
    for (const bad of ["", "#", "#token=", "#token=short", `#token=${"a".repeat(40)}%20x`, `#tok=${token}`]) {
      expect(tokenFromHash(bad)).toBeUndefined();
    }
  });

  it("only ever talks to a loopback http service", () => {
    expect(apiBaseFromLocation("")).toBe("http://127.0.0.1:8765");
    expect(apiBaseFromLocation("?api=http://localhost:9000")).toBe("http://localhost:9000");
    for (const bad of ["https://evil.example", "http://evil.example:8765", "http://127.0.0.1.evil.example", "http://user:pw@127.0.0.1:1", "file:///etc", "nonsense"]) {
      expect(apiBaseFromLocation(`?api=${encodeURIComponent(bad)}`)).toBe("http://127.0.0.1:8765");
    }
  });
});

describe("declared sources", () => {
  const record = { causal: { sources: [{ id: "fifo-src", path: "rtl/fifo.sv" }, { id: "gone", path: "rtl/gone.sv" }] } };

  it("fetches each declared source and reports the ones it cannot get", async () => {
    const sourceText = vi.fn().mockImplementation(async (_job: string, id: string) => {
      if (id === "fifo-src") return "module fifo; endmodule\n";
      throw new ApiError("the file no longer matches the report's SHA-256", "refused", 409, "SOURCE_HASH_MISMATCH");
    });
    const { texts, problems } = await fetchDeclaredSources({ sourceText }, "j", record);
    expect(texts.map((t) => t.name)).toEqual(["rtl/fifo.sv"]);
    expect(texts[0]!.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(problems).toEqual(["rtl/gone.sv: the file no longer matches the report's SHA-256"]);
  });

  it("records without a causal report have none", async () => {
    const sourceText = vi.fn();
    expect(await fetchDeclaredSources({ sourceText }, "j", { causal: null })).toEqual({ texts: [], problems: [] });
    expect(sourceText).not.toHaveBeenCalled();
  });
});
