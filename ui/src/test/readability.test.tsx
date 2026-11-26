import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { Value } from "../components/common";
import mockFifo from "../../../tests/fixtures/mock_fifo_failure.json";

async function loadSample(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  const select = screen.getByTestId("sample-select");
  const option = within(select).getByRole("option", { name: label }) as HTMLOptionElement;
  await user.selectOptions(select, option.value);
}

describe("suspect cards", () => {
  it("show the verified code line and no empty confidence line", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /FIFO mutant, real Verilator run/);
    expect(await screen.findByTestId("suspect-line-s1")).toHaveTextContent("wptr <= {wptr[2], 2'b00};");
    expect(screen.queryByText(/Confidence: not supplied/)).not.toBeInTheDocument();
    // The full reason stays in the card for screen readers; only CSS shortens it.
    expect(screen.getByTestId("suspect-s1")).toHaveTextContent("changed TOP.tb.dut.wptr from 3'b011 to 3'b000");
  });

  it("show no code line when the source is not verified", async () => {
    const user = userEvent.setup();
    render(<App />);
    // The mock report, but claiming a different revision of its source file.
    const doc = structuredClone(mockFifo) as { report: { sources: { sha256: string }[] } };
    doc.report.sources[0]!.sha256 = "0".repeat(64);
    const file = new File([JSON.stringify(doc)], "report.json", { type: "application/json" });
    await user.upload(screen.getByTestId("file-input"), file);
    expect(await screen.findByTestId("suspect-s1")).toBeInTheDocument();
    expect(screen.queryByTestId(/^suspect-line-/)).not.toBeInTheDocument();
  });
});

describe("auto radix (the default)", () => {
  it("is selected by default", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /FIFO mutant, real Verilator run/);
    expect(screen.getByTestId("radix")).toHaveValue("auto");
  });

  it("shows buses in hex beside the exact bits, single bits as bits only", () => {
    const { rerender } = render(<Value value={{ width: 8, bits: "10101010" }} radix="auto" testId="v" />);
    expect(screen.getByTestId("v")).toHaveTextContent("10101010 = 0xaa");
    rerender(<Value value={{ width: 1, bits: "1" }} radix="auto" testId="v" />);
    expect(screen.getByTestId("v").textContent).toBe("1");
  });

  it("never turns unknown bits into a number", () => {
    render(<Value value={{ width: 8, bits: "1010xxxx" }} radix="auto" testId="v" />);
    expect(screen.getByTestId("v")).toHaveTextContent("1010xxxx");
    expect(screen.getByTestId("v")).toHaveTextContent("= ax");
    expect(screen.getByTestId("v")).toHaveTextContent("Unknown bits");
  });
});

describe("hosted welcome", () => {
  it("is one block with both entry points, and shrinks once a report is open", async () => {
    const user = userEvent.setup();
    render(<App hosted />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("find where a chip simulation first goes wrong");
    expect(screen.getByRole("button", { name: /Open your own report/ })).toBeInTheDocument();
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("hosted-example"));
    expect(await screen.findByTestId("run-title")).toBeInTheDocument();
    expect(screen.getByTestId("hosted-note")).toHaveTextContent("Read-only viewer");
    expect(screen.queryByTestId("hosted-example")).not.toBeInTheDocument();
  });
});
