// Waveform window math. Absolute times stay BigInt; only bounded offsets
// relative to the visible window are converted to pixels.

import type { LogicValue, SignalWindow, WaveformWindow } from "./types";

export interface TimeWindow {
  start: bigint;
  end: bigint; // exclusive
}

export type ValueAt = { kind: "value"; value: LogicValue } | { kind: "unavailable" } | { kind: "outside" };

/** Settled value of ``signal`` at ``t`` (the last sample at or before t). */
export function valueAt(wave: WaveformWindow, signal: SignalWindow, t: bigint): ValueAt {
  if (t < wave.start_ps || t >= wave.end_ps) return { kind: "outside" };
  if (wave.unavailable.some((u) => t >= u.start_ps && t < u.end_ps)) return { kind: "unavailable" };
  const samples = signal.samples;
  let lo = 0;
  let hi = samples.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const sample = samples[mid];
    if (sample === undefined) break;
    if (sample.time_ps <= t) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const value = found >= 0 ? samples[found]?.value ?? null : signal.initial;
  return value === null ? { kind: "unavailable" } : { kind: "value", value };
}

export function timeToX(t: bigint, win: TimeWindow, width: number): number {
  const span = win.end - win.start;
  if (span <= 0n) return 0;
  // Relative offsets are bounded by the window; Number() is safe for pixels.
  return (Number(t - win.start) / Number(span)) * width;
}

export function xToTime(x: number, win: TimeWindow, width: number): bigint {
  const span = win.end - win.start;
  const fraction = Math.min(Math.max(x / width, 0), 1);
  // Scale in fixed point to keep sub-tick precision out of floats.
  const scaled = BigInt(Math.round(fraction * 1_000_000));
  const t = win.start + (span * scaled) / 1_000_000n;
  return t >= win.end ? win.end - 1n : t;
}

export interface Segment {
  start: bigint;
  end: bigint;
  value: LogicValue | null; // null: unavailable
  dense: boolean; // several changes collapsed into one pixel column
}

/** Value segments of ``signal`` inside ``win``, collapsing changes that fall
 * within ``minTicks`` of each other so huge traces stay drawable. */
export function segments(wave: WaveformWindow, signal: SignalWindow, win: TimeWindow, minTicks: bigint): Segment[] {
  const out: Segment[] = [];
  const startValue = valueAt(wave, signal, win.start);
  let current: Segment = {
    start: win.start,
    end: win.end,
    value: startValue.kind === "value" ? startValue.value : null,
    dense: false,
  };
  for (const sample of signal.samples) {
    if (sample.time_ps <= win.start) continue;
    if (sample.time_ps >= win.end) break;
    if (sample.time_ps - current.start < minTicks) {
      current.value = sample.value;
      current.dense = true;
      continue;
    }
    current.end = sample.time_ps;
    out.push(current);
    current = { start: sample.time_ps, end: win.end, value: sample.value, dense: false };
  }
  out.push(current);
  return out;
}

/** Every distinct sample time in the window, ascending: cursor stops. */
export function eventTimes(wave: WaveformWindow): bigint[] {
  const set = new Set<bigint>();
  for (const s of wave.signals) for (const sample of s.samples) set.add(sample.time_ps);
  return [...set].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function clampWindow(win: TimeWindow, bounds: TimeWindow): TimeWindow {
  let { start, end } = win;
  const span = end - start;
  if (span >= bounds.end - bounds.start) return { ...bounds };
  if (start < bounds.start) {
    start = bounds.start;
    end = start + span;
  }
  if (end > bounds.end) {
    end = bounds.end;
    start = end - span;
  }
  return { start, end };
}

/** Zoom around ``center`` by ``factor`` (<1 zooms in). Minimum span 2 ticks. */
export function zoomWindow(win: TimeWindow, center: bigint, factor: number, bounds: TimeWindow): TimeWindow {
  const span = win.end - win.start;
  let next = BigInt(Math.round(Number(span) * factor));
  if (next < 2n) next = 2n;
  const left = (next * (center - win.start)) / (span || 1n);
  return clampWindow({ start: center - left, end: center - left + next }, bounds);
}

/** Recenter so ``t`` is visible; keep the window unchanged if it already is. */
export function ensureVisible(win: TimeWindow, t: bigint, bounds: TimeWindow): TimeWindow {
  if (t >= win.start && t < win.end) return win;
  const span = win.end - win.start;
  return clampWindow({ start: t - span / 2n, end: t - span / 2n + span }, bounds);
}
