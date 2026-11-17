import { describe, expect, it } from "vitest";
import mockFifo from "../../../tests/fixtures/mock_fifo_failure.json";
import { loadDocument, parseTime, ValidationError } from "../model/validate";
import { formatValue, toHex } from "../model/values";
import { segments, timeToX, valueAt, xToTime } from "../model/waveform";
import { initialSelection, reduce } from "../state/selection";

describe("values", () => {
  it("never turns unknown bits into numbers", () => {
    expect(toHex("xxxxxxxx")).toBe("xx");
    expect(toHex("0101xxxx")).toBe("5x");
    expect(toHex("01x1")).toBe("X");
    expect(toHex("zzzz0001")).toBe("z1");
    expect(formatValue({ width: 4, bits: "01x1" }, "udec")).toBeNull();
    expect(formatValue({ width: 4, bits: "01x1" }, "sdec")).toBeNull();
  });

  it("formats signed values as a display transform", () => {
    expect(formatValue({ width: 16, bits: "1000000000000000" }, "sdec")).toBe("-32768");
    expect(formatValue({ width: 16, bits: "0100010010000000" }, "sdec")).toBe("17536");
  });

  it("keeps wide vectors exact", () => {
    const bits = "1".repeat(100);
    expect(formatValue({ width: 100, bits }, "udec")).toBe(((1n << 100n) - 1n).toString());
  });
});

describe("timestamps", () => {
  it("accepts canonical decimals exactly, including above 2^53", () => {
    expect(parseTime("9007199254740993", "t")).toBe(9007199254740993n);
    expect(parseTime(55000, "t")).toBe(55000n);
  });

  it.each([["007"], ["-1"], ["1e3"], ["1.0"], [1.5], [2 ** 60], [true], ["9".repeat(40)]])("rejects %s", (v) => {
    expect(() => parseTime(v, "t")).toThrow(ValidationError);
  });
});

describe("validation", () => {
  it("rejects unknown fields", () => {
    const bad = structuredClone(mockFifo) as Record<string, unknown>;
    (bad.report as Record<string, unknown>).confidence = 0.9;
    expect(() => loadDocument(bad)).toThrow(/unknown field "confidence"/);
  });

  it("rejects suspect ranks that are not 1..N", () => {
    const bad = structuredClone(mockFifo);
    bad.report.suspects[1]!.rank = 3;
    expect(() => loadDocument(bad)).toThrow(/ranks must be exactly 1..N/);
  });

  it("rejects an anchor node whose value differs from the failure's actual", () => {
    const bad = structuredClone(mockFifo);
    bad.report.nodes[0]!.value = { width: 1, bits: "1" };
    expect(() => loadDocument(bad)).toThrow(/anchor node value differs/);
  });

  it("rejects a sequential edge that is not before -> settled at one edge", () => {
    const bad = structuredClone(mockFifo);
    const e03 = bad.report.edges.find((e) => e.id === "e03")!;
    e03.source = "wptr.c2.settled";
    expect(() => loadDocument(bad)).toThrow(/sequential edge runs from before to settled/);
  });

  it("rejects a combinational edge across observations that is not a carry", () => {
    const bad = structuredClone(mockFifo);
    const e01 = bad.report.edges.find((e) => e.id === "e01")!;
    e01.source = "wptr.c2.settled";
    expect(() => loadDocument(bad)).toThrow(/combinational edge stays in one observation/);
  });

  it("rejects an additional mismatch at a different observation", () => {
    const bad = structuredClone(mockFifo);
    const other = bad.comparison.additional_mismatches[0]!;
    other.observation = { ...other.observation, cycle: 4, time_ps: "65000" };
    expect(() => loadDocument(bad)).toThrow(/share the failing observation/);
  });

  it("rejects an anchor node that does not match the failure", () => {
    const bad = structuredClone(mockFifo);
    bad.report.anchor_node_id = "wptr.c3.settled";
    expect(() => loadDocument(bad)).toThrow(/anchor node does not match/);
  });
});

describe("waveform math", () => {
  const doc = loadDocument(mockFifo);
  const wave = doc.report!.waveform!;
  const full = wave.signals.find((s) => s.signal_path === "tb.dut.full")!;

  it("reads settled values with exact edge semantics", () => {
    const at = (t: bigint) => {
      const v = valueAt(wave, full, t);
      return v.kind === "value" ? v.value.bits : v.kind;
    };
    expect(at(wave.start_ps)).toBe(full.initial?.bits);
    expect(at(wave.end_ps)).toBe("outside");
  });

  it("maps pixels to times without float drift on large absolute times", () => {
    const big = 1n << 60n;
    const win = { start: big, end: big + 1000n };
    expect(xToTime(0, win, 1000)).toBe(big);
    expect(xToTime(500, win, 1000)).toBe(big + 500n);
    expect(timeToX(big + 250n, win, 1000)).toBe(250);
  });

  it("covers the whole window with segments", () => {
    const win = { start: wave.start_ps, end: wave.end_ps };
    const segs = segments(wave, full, win, 1n);
    expect(segs[0]?.start).toBe(win.start);
    expect(segs.at(-1)?.end).toBe(win.end);
  });
});

describe("selection rules", () => {
  const doc = loadDocument(mockFifo);

  it("starts focused on the failure and its anchor node", () => {
    const sel = initialSelection(doc);
    expect(sel.selectedNodeId).toBe("full.c3.settled");
    expect(sel.cursorTime).toBe(55000n);
  });

  it("rule 3: a cursor time with no exact node clears node and suspect but keeps the signal", () => {
    let sel = reduce(doc, initialSelection(doc), { type: "selectSuspect", id: "s1" });
    sel = reduce(doc, sel, { type: "setCursor", time: 55001n });
    expect(sel.selectedNodeId).toBeNull();
    expect(sel.selectedSuspectId).toBeNull();
    expect(sel.selectedSignalId).toBe("tb.dut.wptr");
  });

  it("rule 3: an exact node at the cursor is selected with its suspect", () => {
    let sel = reduce(doc, initialSelection(doc), { type: "selectNode", id: "push.c3.before" });
    sel = reduce(doc, sel, { type: "setCursor", time: 55000n, signal: "tb.dut.wptr" });
    // Phase "before" is kept from the previous selection: wptr.c3.before, not settled.
    expect(sel.selectedNodeId).toBe("wptr.c3.before");
    expect(sel.selectedSuspectId).toBeNull();
  });

  it("rule 4: jump-to-failure never picks a nearby node when the anchor is absent", () => {
    const noAnchor = { ...doc, report: { ...doc.report!, anchor_node_id: null } };
    const sel = reduce(noAnchor, initialSelection(noAnchor), { type: "jumpToFailure" });
    expect(sel.selectedNodeId).toBeNull();
    expect(sel.cursorTime).toBe(55000n);
    expect(sel.selectedSignalId).toBe("tb.dut.full");
  });
});
