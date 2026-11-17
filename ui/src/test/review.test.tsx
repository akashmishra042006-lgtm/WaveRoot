import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Value } from "../components/common";
import { valueBefore } from "../model/waveform";
import type { WaveformWindow } from "../model/types";

describe("review regressions", () => {
  it.each(["xxxxxxxx", "zzzzzzzz", "0101xxxx", "z0100011"])("does not prefix unknown hex %s as a number", bits => {
    render(<Value value={{ width: 8, bits }} radix="hex" testId="value" />);
    expect(screen.getByTestId("value")).not.toHaveTextContent("0x");
    expect(screen.getByTestId("value")).toHaveTextContent(bits);
    expect(screen.getByTestId("value")).toHaveTextContent("Unknown bits");
  });
  it("prefixes known hex and leaves binary as raw bits", () => {
    const { rerender } = render(<Value value={{ width: 8, bits: "10101010" }} radix="hex" testId="value" />);
    expect(screen.getByTestId("value")).toHaveTextContent("= 0xaa");
    rerender(<Value value={{ width: 8, bits: "10101010" }} radix="bin" testId="value" />);
    expect(screen.getByTestId("value")).toHaveTextContent(/^10101010$/);
  });
  it("reads strictly before an exact large timestamp and respects window/gap boundaries", () => {
    const start = 9007199254740993n;
    const wave: WaveformWindow = { start_ps: start, end_ps: start + 10n, unavailable: [{ start_ps: start+4n, end_ps: start+6n }], signals: [{ signal_path: "tb.q", width: 1, initial: { width: 1, bits: "0" }, samples: [{ time_ps: start+2n, value: {width:1,bits:"1"} }, { time_ps: start+6n, value: {width:1,bits:"0"} }] }] };
    const signal = wave.signals[0]!;
    expect(valueBefore(wave, signal, start)).toEqual({kind:"outside"});
    expect(valueBefore(wave, signal, start+2n)).toEqual({kind:"value",value:{width:1,bits:"0"}});
    expect(valueBefore(wave, signal, start+3n)).toEqual({kind:"value",value:{width:1,bits:"1"}});
    expect(valueBefore(wave, signal, start+6n)).toEqual({kind:"unavailable"});
    expect(valueBefore(wave, signal, start+7n)).toEqual({kind:"value",value:{width:1,bits:"0"}});
  });
});
