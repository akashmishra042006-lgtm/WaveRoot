import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { ApiClient } from "../api/client";

function spyClient(): ApiClient {
  const fail = vi.fn().mockRejectedValue(new Error("the hosted viewer must not call the service"));
  return { base: "http://127.0.0.1:8765", health: fail, cases: fail, createJob: fail, job: fail, cancel: fail, result: fail, sourceText: fail } as unknown as ApiClient;
}

describe("the hosted, read-only build", () => {
  it("never contacts an analysis service and says where analyses run", async () => {
    const client = spyClient();
    render(<App client={client} hosted />);
    const note = screen.getByTestId("hosted-note");
    expect(note).toHaveTextContent("Read-only viewer");
    expect(note).toHaveTextContent("uv run waveroot serve");
    expect(screen.getByRole("link", { name: "README" })).toHaveAttribute("href", expect.stringContaining("github.com"));
    expect(screen.queryByTestId("run-panel")).not.toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(client.health).not.toHaveBeenCalled();
    expect(client.cases).not.toHaveBeenCalled();
  });

  it("opens the real FIFO example with ranked suspects and verified source", async () => {
    const user = userEvent.setup();
    render(<App client={spyClient()} hosted />);
    await user.click(screen.getByTestId("hosted-example"));
    expect(await screen.findByTestId("run-title")).toBeInTheDocument();
    expect(screen.getByTestId("failure-signal")).toHaveTextContent("TOP.tb.dut.full");
    await user.click(screen.getByTestId("suspect-s1"));
    expect(await screen.findByTestId("source-verified")).toBeInTheDocument();
    expect(screen.getByTestId("source-location")).toHaveTextContent("fifo.sv");
  });

  it("the local build still shows the run panel", () => {
    render(<App client={spyClient()} hosted={false} />);
    expect(screen.queryByTestId("hosted-note")).not.toBeInTheDocument();
  });
});
