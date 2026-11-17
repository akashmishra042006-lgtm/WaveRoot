// Exact value and time formatting. Bit strings stay strings; numeric radix
// views are display transforms that never turn x/z into a number.

import type { FailureKind, LogicValue, Observation, Phase } from "./types";

export type Radix = "bin" | "hex" | "udec" | "sdec";

export const RADIX_LABELS: Record<Radix, string> = {
  bin: "Binary",
  hex: "Hex",
  udec: "Unsigned decimal",
  sdec: "Signed decimal",
};

/** Hex view, one digit per nibble from the right. A nibble that is entirely
 * x (or z) shows `x` (`z`); a partly unknown nibble shows `X` (`Z` if only z). */
export function toHex(bits: string): string {
  const padded = bits.padStart(Math.ceil(bits.length / 4) * 4, bits[0] === "x" || bits[0] === "z" ? bits[0] : "0");
  let out = "";
  for (let i = 0; i < padded.length; i += 4) {
    const nibble = padded.slice(i, i + 4);
    if (/^[01]{4}$/.test(nibble)) out += parseInt(nibble, 2).toString(16);
    else if (/^x{4}$/.test(nibble)) out += "x";
    else if (/^z{4}$/.test(nibble)) out += "z";
    else if (/x/.test(nibble)) out += "X";
    else out += "Z";
  }
  return out;
}

export function isKnown(bits: string): boolean {
  return /^[01]+$/.test(bits);
}

/** Formatted value, or null when the radix cannot represent unknown bits. */
export function formatValue(value: LogicValue, radix: Radix): string | null {
  const { bits, width } = value;
  switch (radix) {
    case "bin":
      return bits;
    case "hex":
      return toHex(bits);
    case "udec":
      return isKnown(bits) ? BigInt(`0b${bits}`).toString() : null;
    case "sdec": {
      if (!isKnown(bits)) return null;
      const unsigned = BigInt(`0b${bits}`);
      const signed = bits[0] === "1" ? unsigned - (1n << BigInt(width)) : unsigned;
      return signed.toString();
    }
  }
}

/** Value text for display: the radix view plus the raw bits whenever they
 * carry information the view cannot (unknowns, or a non-binary radix). */
export function describeValue(value: LogicValue | null, radix: Radix): string {
  if (value === null) return "not observed";
  const view = formatValue(value, radix);
  if (radix === "bin") return value.bits;
  if (view === null) return `${value.bits} (contains unknown bits)`;
  return `${view} (${value.bits})`;
}

export function hasUnknown(value: LogicValue | null): boolean {
  return value !== null && /[xz]/.test(value.bits);
}

export const KIND_LABELS: Record<FailureKind, string> = {
  value_mismatch: "Value mismatch",
  unknown_value: "Unknown value",
  missing_transaction: "Missing transaction",
  extra_transaction: "Extra transaction",
};

export const PHASE_LABELS: Record<Phase, string> = {
  before: "before the edge",
  settled: "settled after the edge",
};

export function formatTime(t: bigint): string {
  return `${t.toString()} ps`;
}

export function formatObservation(o: Observation): string {
  return `epoch ${o.epoch}, cycle ${o.cycle}, ${PHASE_LABELS[o.phase]}, ${formatTime(o.time_ps)}`;
}

export function shortName(path: string): string {
  return path.split(".").pop() ?? path;
}
