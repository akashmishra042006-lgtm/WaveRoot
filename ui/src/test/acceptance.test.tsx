// Behavior tests for the ui-spec acceptance scenarios (numbers match the spec).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import mockFifo from "../../../tests/fixtures/mock_fifo_failure.json";
import invalidWidth from "../../fixtures/invalid_width.json";
import danglingNode from "../../fixtures/dangling_node.json";
import unknownSchema from "../../fixtures/unknown_schema.json";

const LARGE_FAILURE = (55000n + (1n << 60n)).toString();

function setNarrow(narrow: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: narrow && query.includes("max-width: 700px"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
}

async function loadSample(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  const select = screen.getByTestId("sample-select");
  const option = within(select).getByRole("option", { name: label }) as HTMLOptionElement;
  await user.selectOptions(select, option.value);
}

async function uploadJson(user: ReturnType<typeof userEvent.setup>, data: unknown, name = "report.json") {
  const file = new File([JSON.stringify(data)], name, { type: "application/json" });
  await user.upload(screen.getByTestId("file-input"), file);
  await waitFor(() => expect(screen.queryByTestId("loading")).not.toBeInTheDocument());
}

// userEvent.setup() installs a clipboard stub; tests read it back with readText().
beforeEach(() => setNarrow(false));

afterEach(() => vi.restoreAllMocks());

describe("empty and sample labeling", () => {
  it("starts empty, and opening never implies a simulation", () => {
    render(<App />);
    expect(screen.getByTestId("empty-state")).toHaveTextContent("never runs a simulation");
  });

  it("labels synthetic data persistently with its fixture id", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    expect(screen.getByTestId("sample-badge")).toHaveTextContent("Sample data · mock_fifo_wrap");
    await user.click(screen.getByTestId("suspect-s2"));
    expect(screen.getByTestId("sample-badge")).toBeInTheDocument();
  });

  it("labels a real run record as real data", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /FIFO mutant, real Verilator run/);
    expect(screen.getByTestId("real-badge")).toHaveTextContent("Real run data");
    expect(screen.queryByTestId("sample-badge")).toBeNull();
  });

  it("labels a run record that carries a synthetic causal report as sample data", async () => {
    // In-test record: the mock fixture's comparison and causal report wrapped
    // as a fixture-mode run record (the shape the CLI writes with --fixture-causal).
    const user = userEvent.setup();
    render(<App />);
    await uploadJson(user, fixtureRunRecord());
    expect(screen.getByTestId("sample-badge")).toBeInTheDocument();
    expect(screen.getByTestId("fixture-note")).toHaveTextContent("Not produced by a causal engine");
    expect(screen.getByTestId("suspect-s1")).toBeInTheDocument();
  });

  it("rejects a record whose causal report describes a different failure", async () => {
    const user = userEvent.setup();
    render(<App />);
    const record = fixtureRunRecord();
    (record.comparisons.cycle.failure as { signal_path: string }).signal_path = "TOP.tb.dut.full";
    await uploadJson(user, record);
    expect(screen.getByTestId("load-error")).toHaveTextContent(/disagree about the failure|anchor/);
  });
});

function fixtureRunRecord() {
  const mock = structuredClone(mockFifo);
  return {
    record: "waveroot-track-c-run", record_version: 1, status: "fail", exit_code: 1,
    case_id: "mock_fifo_wrap", variant: "mutant", schedule: "directed", fixture_mode: true,
    comparison_adapter: { name: "fixture", kind: "synthetic_fixture" },
    causal_adapter: { name: "fixture:mock_fifo_failure.json", kind: "synthetic_fixture" },
    primary_mode: "cycle", comparisons: { cycle: mock.comparison }, causal: mock.report,
    diagnostics: [], run: null,
  };
}

describe("scenario 1: suspect selection synchronizes every panel", () => {
  it("selects node, signal, observation, source span, and time; the anchor stays tb.dut.full", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await user.click(screen.getByTestId("suspect-s1"));

    expect(screen.getByTestId("suspect-s1")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("node-wptr.c3.settled")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("node-wptr.c3.settled")).toHaveTextContent("tb.dut.wptr · epoch 0 cycle 3 settled");
    expect(screen.getByTestId("source-location")).toHaveTextContent("tests/fixtures/mock_fifo_wrap.sv:37-38");
    expect(screen.getByTestId("cursor-time")).toHaveTextContent("55000 ps");
    expect(screen.getByTestId("announcer")).toHaveTextContent("Suspect 1: wptr, epoch 0, cycle 3, settled");

    // The source text is shown only after its SHA-256 matched the report.
    expect(await screen.findByTestId("source-verified")).toBeInTheDocument();
    const hits = document.querySelectorAll(".code li.hit");
    expect([...hits].map((li) => li.getAttribute("data-line"))).toEqual(["37", "38"]);

    // The failure anchor is report data and does not move.
    expect(screen.getByTestId("failure-signal")).toHaveTextContent("tb.dut.full");
    expect(screen.getByTestId("failure-time")).toHaveTextContent("55000 ps");
  });

  it("selecting a node without a suspect clears the stale suspect highlight (rule 2)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await user.click(screen.getByTestId("suspect-s1"));
    await user.click(screen.getByTestId("node-push.c3.before"));
    expect(screen.getByTestId("suspect-s1")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("suspect-s2")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("node-push.c3.before")).toHaveAttribute("aria-pressed", "true");
    // push is in the waveform window, so its row is the selected one.
    expect(screen.queryByTestId("signal-missing")).toBeNull();
    expect(document.querySelector('canvas.track[data-signal="tb.dut.push"]')).toHaveClass("selected");
    await user.click(screen.getByTestId("node-wptr.c3.before"));
    expect(screen.getByTestId("value-wptr")).toHaveTextContent("011");
    expect(screen.getByTestId("cursor-time")).toHaveTextContent("55000 ps");
    await user.click(screen.getByTestId("node-wptr.c3.settled"));
    expect(screen.getByTestId("value-wptr")).toHaveTextContent("000");
  });
});

describe("scenario 2: the cursor never moves the failure", () => {
  it("keeps the failure marker fixed and jump-to-failure restores it", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await user.click(screen.getByTestId("suspect-s1"));

    const tracks = screen.getByTestId("wave-tracks");
    tracks.focus();
    await user.keyboard("{ArrowRight}");
    const moved = screen.getByTestId("cursor-time").textContent;
    expect(moved).not.toBe("55000 ps");
    expect(screen.getByTestId("failure-marker-time")).toHaveTextContent("55000 ps");
    expect(screen.getByTestId("failure-signal")).toHaveTextContent("tb.dut.full");

    // Rule 3: no exact node at the new time, so node and suspect highlights clear.
    expect(screen.getByTestId("suspect-s1")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("node-wptr.c3.settled")).toHaveAttribute("aria-pressed", "false");

    await user.click(screen.getByRole("button", { name: "Jump to first failure" }));
    expect(screen.getByTestId("cursor-time")).toHaveTextContent("55000 ps");
    expect(screen.getByTestId("node-full.c3.settled")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("announcer")).toHaveTextContent("First failure: full");
  });

  it("clicking a track moves only the selection cursor", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    const track = document.querySelector('canvas.track[data-signal="tb.dut.clk"]');
    expect(track).not.toBeNull();
    await act(async () => {
      track!.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 50 }));
    });
    expect(screen.getByTestId("cursor-time")).not.toHaveTextContent("55000 ps");
    expect(screen.getByTestId("cursor-time").parentElement).not.toHaveTextContent("Cursor (settled)");
    expect(screen.getByTestId("failure-marker-time")).toHaveTextContent("55000 ps");
  });
});

describe("scenario 3: unknown values are never shown as numbers", () => {
  it("shows the real Icarus run's xxxxxxxx with an Unknown value label", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /ALU unknown value, real Icarus run/);
    expect(screen.getByTestId("failure-kind")).toHaveTextContent("Unknown value");
    expect(screen.getByTestId("failure-actual")).toHaveTextContent("xxxxxxxx");
    expect(screen.getByTestId("failure-actual")).toHaveTextContent("Unknown bits");

    await user.selectOptions(screen.getByTestId("radix"), "udec");
    expect(screen.getByTestId("failure-actual")).toHaveTextContent("xxxxxxxx");
    expect(screen.getByTestId("failure-actual")).toHaveTextContent("n/a");
    expect(screen.getByTestId("failure-expected")).toHaveTextContent("= 0");

    await user.selectOptions(screen.getByTestId("radix"), "hex");
    expect(screen.getByTestId("failure-actual")).toHaveTextContent("= xx");
    expect(screen.getByTestId("failure-actual")).not.toHaveTextContent("0x");
    await user.selectOptions(screen.getByTestId("radix"), "bin");
    expect(screen.getByTestId("failure-actual")).not.toHaveTextContent("=");
    expect(screen.getByTestId("failure-expected")).not.toHaveTextContent("=");
  });

  it("explains that this real failure is not localized", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /ALU unknown value/);
    expect(screen.getByTestId("unlocalized")).toHaveTextContent("CAUSAL_UNAVAILABLE");
  });

  it("labels the benchmark self-check, and only the self-check", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /benchmark self-check/);
    expect(screen.getByTestId("comparison-counts")).toHaveTextContent("benchmark self-check");

    // A record compared by Track A's waveform engine (kind "analysis") carries no such note.
    await loadSample(user, /ALU unknown value, real Icarus run/);
    expect(screen.getByTestId("comparison-counts")).not.toHaveTextContent("self-check");
    expect(screen.getByTestId("comparison-counts")).not.toHaveTextContent("not waveform analysis");
  });
});

describe("scenario 4: no stale data after opening a pass", () => {
  it("clears graph and suspects from the previous failure", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await user.click(screen.getByTestId("suspect-s1"));
    await loadSample(user, /FIFO correct, real Verilator run/);

    expect(screen.getByTestId("outcome-pass")).toHaveTextContent("No failure found");
    expect(screen.getByTestId("status-badge")).toHaveTextContent("Pass");
    expect(screen.queryByTestId("failure-strip")).toBeNull();
    expect(screen.queryByTestId("suspect-s1")).toBeNull();
    expect(screen.queryByTestId("node-wptr.c3.settled")).toBeNull();
    expect(screen.getByTestId("comparison-counts")).toHaveTextContent("cycle mode: pass");
  });
});

describe("scenario 5: limits are exposed, never a misleading pass", () => {
  it("presents inconclusive with its limits and no pass styling", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Inconclusive/);
    expect(screen.getByTestId("status-badge")).toHaveTextContent("Inconclusive");
    expect(screen.getByTestId("outcome-limited")).toHaveTextContent("not a pass");
    expect(screen.getByTestId("outcome-limited")).toHaveTextContent("COVERAGE");
    expect(screen.queryByTestId("outcome-pass")).toBeNull();
  });

  it("keeps evidence when a suspect has no source location", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Source unavailable/);
    expect(screen.getByTestId("suspect-s1")).toHaveTextContent("Source unavailable");
    expect(screen.getByTestId("truncated")).toHaveTextContent("Absent nodes are not evidence of irrelevance");
    expect(screen.getByTestId("graph-limits")).toHaveTextContent("SOURCE_UNAVAILABLE");
    await user.click(screen.getByTestId("suspect-s1"));
    expect(screen.getByTestId("node-wptr.c3.settled")).toHaveAttribute("aria-pressed", "true");
  });

  it("shows source as unavailable, or as a revision mismatch, rather than guessing", async () => {
    const user = userEvent.setup();
    render(<App />);
    // Upload the mock fixture as a user file: the bundled text still matches by hash.
    await uploadJson(user, mockFifo);
    await user.click(screen.getByTestId("suspect-s1"));
    expect(await screen.findByTestId("source-verified")).toBeInTheDocument();

    // A same-named file with different bytes is a revision mismatch, never shown as analyzed code.
    const report = structuredClone(mockFifo) as typeof mockFifo;
    report.report.sources[0]!.sha256 = "0".repeat(64);
    await uploadJson(user, report);
    await user.click(screen.getByTestId("suspect-s1"));
    const edited = new File(["module mock_fifo_wrap; endmodule\n"], "mock_fifo_wrap.sv");
    await user.upload(screen.getByTestId("source-input"), edited);
    expect(await screen.findByTestId("source-mismatch")).toHaveTextContent("Revision mismatch");
    expect(document.querySelector(".code")).toBeNull();
  });
});

describe("infrastructure errors", () => {
  it("shows a real timeout as a tooling problem, not a design verdict", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /timed out/);
    expect(screen.getByTestId("status-badge")).toHaveTextContent("Infrastructure error");
    const panel = screen.getByTestId("outcome-error");
    expect(panel).toHaveTextContent("not a verdict about the RTL");
    expect(panel).toHaveTextContent("TIMEOUT");
    expect(within(panel).getByRole("table")).toHaveTextContent("build-15build.log");
    expect(screen.queryByTestId("failure-strip")).toBeNull();
  });
});

describe("scenario 6: timestamps above 2^53 stay exact", () => {
  it("distinguishes a one-tick cursor change", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /above 2\^53/);
    expect(screen.getByTestId("failure-time")).toHaveTextContent(`${LARGE_FAILURE} ps`);
    expect(screen.getByTestId("cursor-time")).toHaveTextContent(`${LARGE_FAILURE} ps`);

    await user.click(screen.getByRole("button", { name: "Move cursor forward one tick" }));
    const next = (BigInt(LARGE_FAILURE) + 1n).toString();
    expect(screen.getByTestId("cursor-time")).toHaveTextContent(`${next} ps`);
    expect(screen.getByTestId("failure-marker-time")).toHaveTextContent(`${LARGE_FAILURE} ps`);
    // As doubles these would be equal; the display must not collapse them.
    expect(Number(next)).toBe(Number(LARGE_FAILURE));
  });
});

describe("scenario 7: invalid documents fail visibly before rendering", () => {
  it.each([
    ["invalid width", invalidWidth, /bits has 2 characters but width is 3/],
    ["dangling node", danglingNode, /dangling node reference/],
    ["unknown schema", unknownSchema, /unsupported schema_version 2.*reads schema 1/],
  ])("rejects %s", async (_name, data, message) => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await uploadJson(user, data);
    expect(screen.getByTestId("load-error")).toHaveTextContent(message);
    expect(screen.getByTestId("load-error")).toHaveTextContent("Nothing from it has been rendered");
    // The previous report is cleared, not left on screen.
    expect(screen.queryByTestId("failure-strip")).toBeNull();
    expect(screen.queryByTestId("suspect-s1")).toBeNull();
  });

  it("rejects non-JSON text", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.upload(screen.getByTestId("file-input"), new File(["{nope"], "x.json"));
    expect(screen.getByTestId("load-error")).toHaveTextContent("not valid JSON");
  });
});

describe("scenario 8: keyboard operation and announcements", () => {
  it("selects, copies, zooms, and jumps without a mouse", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);

    screen.getByTestId("suspect-s1").focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("suspect-s1")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("announcer")).toHaveTextContent("Suspect 1");

    screen.getByRole("button", { name: "Copy location" }).focus();
    await user.keyboard("{Enter}");
    expect(await navigator.clipboard.readText()).toBe("tests/fixtures/mock_fifo_wrap.sv:37-38");

    const before = screen.getByText(/Window /).textContent;
    screen.getByRole("button", { name: "Zoom in" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByText(/Window /).textContent).not.toBe(before);
    expect(screen.getByTestId("cursor-time")).toHaveTextContent("55000 ps"); // cursor retained

    screen.getByRole("button", { name: "Jump to first failure" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("announcer")).toHaveTextContent("First failure");
  });

  it("copies assertion candidates with an explicit not-validated header", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Candidate assertions/);
    expect(screen.getByTestId("assertion-a1")).toHaveTextContent("Candidate — not validated");
    expect(screen.getByTestId("assertion-a2")).toHaveTextContent("Validated by example: Icarus 12.0");
    await user.click(within(screen.getByTestId("assertion-a1")).getByRole("button", { name: "Copy assertion" }));
    expect(await navigator.clipboard.readText()).toMatch(/^\/\/ Candidate assertion from WaveRoot — not validated\nassert property/);
  });

  it("switches tabs by keyboard at narrow width and keeps the selection", async () => {
    setNarrow(true);
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    await user.click(screen.getByTestId("suspect-s1"));

    const suspectsTab = screen.getByRole("tab", { name: "Suspects" });
    suspectsTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(screen.getByTestId("node-wptr.c3.settled")).toHaveAttribute("aria-pressed", "true");

    await user.keyboard("{ArrowRight}");
    expect(screen.getByTestId("source-location")).toHaveTextContent("mock_fifo_wrap.sv:37-38");
  });
});

describe("pruned branches and graph limits", () => {
  it("hides pruned branches until toggled", async () => {
    const user = userEvent.setup();
    render(<App />);
    await loadSample(user, /Mock FIFO failure/);
    expect(document.querySelector('[data-edge="e04"]')).toBeNull();
    await user.click(screen.getByLabelText(/Show pruned branches \(1\)/));
    expect(document.querySelector('[data-edge="e04"]')).not.toBeNull();
    expect(document.querySelector('[data-edge="e04"]')?.getAttribute('title')).toContain("pruned");
  });

  it("does not draw an oversized graph but keeps the list", async () => {
    const user = userEvent.setup();
    render(<App />);
    const big = structuredClone(mockFifo) as typeof mockFifo;
    const extra = Array.from({ length: 401 }, (_, i) => ({
      ...big.report.nodes[1]!,
      id: `extra.${i}`,
      signal_path: `tb.dut.extra_${i}`,
    }));
    big.report.nodes.push(...extra);
    await uploadJson(user, big);
    await waitFor(() => expect(screen.getByTestId("graph-oversized")).toHaveTextContent("411 nodes"));
    expect(screen.getByTestId("node-extra.400")).toBeInTheDocument();
    // A node whose signal is absent from the waveform window is reported, not drawn as a value.
    await user.click(screen.getByTestId("node-extra.0"));
    expect(screen.getByTestId("signal-missing")).toHaveTextContent("tb.dut.extra_0");
  });
});
