import { memo, useCallback, useMemo, useRef, type Dispatch, type KeyboardEvent, type MouseEvent } from "react";
import type { Loaded, SignalWindow, WaveformWindow } from "../model/types";
import { formatTime, formatValue, shortName, type Radix } from "../model/values";
import { eventTimes, segments, timeToX, valueAt, xToTime, type TimeWindow } from "../model/waveform";
import type { Action, Selection } from "../state/selection";
import { Panel, useWidth, Value } from "./common";

const ROW_H = 30;
const MIN_TRACK = 560; // narrower panels scroll horizontally inside the panel
const SNAP_PX = 6;

export function Waveform({ doc, sel, dispatch }: { doc: Loaded; sel: Selection; dispatch: Dispatch<Action> }) {
  const wave = doc.report?.waveform ?? null;
  const box = useRef<HTMLDivElement>(null);
  const measured = useWidth(box, 800);
  const width = Math.max(measured, MIN_TRACK);
  const events = useMemo(() => (wave ? eventTimes(wave) : []), [wave]);
  const win = sel.window;
  const onPick = useCallback(
    (signalPath: string, e: MouseEvent<SVGSVGElement>) => {
      if (!win) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (width / (rect.width || width));
      let t = xToTime(x, win, width);
      // Snap to the nearest recorded change within a few pixels.
      let bestPx = SNAP_PX + 1;
      const px = timeToX(t, win, width);
      for (const ev of events) {
        if (ev < win.start || ev >= win.end) continue;
        const d = Math.abs(timeToX(ev, win, width) - px);
        if (d < bestPx) {
          bestPx = d;
          t = ev;
        }
      }
      dispatch({ type: "setCursor", time: t, signal: signalPath });
    },
    [win, width, events, dispatch],
  );

  if (!wave || !win) {
    return (
      <Panel title="Waveforms" id="waveform">
        <p className="empty" data-testid="waveform-none">
          This report has no waveform window.
          {doc.comparisonKind === "benchmark_self_check" &&
            " The comparison used the benchmark self-check, which reads testbench samples rather than the trace."}
        </p>
      </Panel>
    );
  }

  const failureTime = doc.failure?.observation.time_ps ?? null;
  const missingSignal = sel.selectedSignalId && !wave.signals.some((s) => s.signal_path === sel.selectedSignalId)
    ? sel.selectedSignalId
    : null;

  const onKey = (e: KeyboardEvent) => {
    const t = sel.cursorTime ?? win.start;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      if (e.shiftKey) {
        dispatch({ type: "stepCursor", ticks: e.key === "ArrowRight" ? 1n : -1n });
        return;
      }
      const next = e.key === "ArrowRight" ? events.find((x) => x > t) : [...events].reverse().find((x) => x < t);
      if (next !== undefined) dispatch({ type: "setCursor", time: next });
    } else if (e.key === "+" || e.key === "=") {
      dispatch({ type: "zoom", factor: 0.5 });
    } else if (e.key === "-") {
      dispatch({ type: "zoom", factor: 2 });
    }
  };

  const minTicks = (win.end - win.start) / BigInt(Math.max(1, Math.floor(width)));

  return (
    <Panel
      title="Waveforms"
      id="waveform"
      actions={
        <div className="wave-tools" role="group" aria-label="Waveform controls">
          <button type="button" onClick={() => dispatch({ type: "zoom", factor: 0.5 })}>Zoom in</button>
          <button type="button" onClick={() => dispatch({ type: "zoom", factor: 2 })}>Zoom out</button>
          <button type="button" onClick={() => dispatch({ type: "setWindow", window: { start: wave.start_ps, end: wave.end_ps } })}>Fit</button>
          <button type="button" onClick={() => dispatch({ type: "stepCursor", ticks: -1n })} aria-label="Move cursor back one tick">−1 tick</button>
          <button type="button" onClick={() => dispatch({ type: "stepCursor", ticks: 1n })} aria-label="Move cursor forward one tick">+1 tick</button>
        </div>
      }
    >
      <p className="readout" aria-live="off">
        Cursor: <strong data-testid="cursor-time">{sel.cursorTime === null ? "none" : formatTime(sel.cursorTime)}</strong>
        {failureTime !== null && <> · First failure: <span data-testid="failure-marker-time">{formatTime(failureTime)}</span></>}
        {" "}· Window {formatTime(win.start)} – {formatTime(win.end)}
      </p>
      {missingSignal && (
        <p className="warning" data-testid="signal-missing">
          <code>{missingSignal}</code> is not in this waveform window; no value is shown for it.
        </p>
      )}
      {wave.unavailable.length > 0 && (
        <p className="hint">Hatched spans are unavailable data (dumping was off), not held values.</p>
      )}
      <div
        className="wave"
        tabIndex={0}
        role="group"
        aria-label="Waveform tracks. Left and right arrows move the cursor between changes; Shift moves one tick; plus and minus zoom."
        onKeyDown={onKey}
        data-testid="wave-tracks"
      >
        <div className="wave-names">
          {wave.signals.map((s) => {
            const v = sel.cursorTime === null ? null : valueAt(wave, s, sel.cursorTime);
            return (
              <div
                key={s.signal_path}
                className={`wave-name${sel.selectedSignalId === s.signal_path ? " selected" : ""}`}
                style={{ height: ROW_H }}
                title={s.signal_path}
              >
                <span className="sig">{shortName(s.signal_path)}</span>
                <span className="at-cursor" data-testid={`value-${shortName(s.signal_path)}`}>
                  {v === null ? "" : v.kind === "value" ? <Value value={v.value} radix={sel.radix} /> : v.kind === "unavailable" ? "unavailable" : "outside window"}
                </span>
              </div>
            );
          })}
        </div>
        <div className="wave-scroll" ref={box}>
          <div className="wave-stack" style={{ width, height: wave.signals.length * ROW_H }}>
            {wave.signals.map((s) => (
              <Track
                key={s.signal_path}
                wave={wave}
                signal={s}
                win={win}
                width={width}
                minTicks={minTicks}
                radix={sel.radix}
                selected={sel.selectedSignalId === s.signal_path}
                onPick={onPick}
              />
            ))}
            <Cursors win={win} width={width} height={wave.signals.length * ROW_H} cursor={sel.cursorTime} failure={failureTime} />
          </div>
        </div>
      </div>
    </Panel>
  );
}

/** Cursor lines drawn once over all tracks, so moving the cursor never
 * re-renders the tracks themselves. */
function Cursors({ win, width, height, cursor, failure }: {
  win: TimeWindow; width: number; height: number; cursor: bigint | null; failure: bigint | null;
}) {
  const inWin = (t: bigint | null): t is bigint => t !== null && t >= win.start && t < win.end;
  return (
    <svg className="wave-cursors" width={width} height={height} aria-hidden="true">
      {inWin(failure) && (
        <line x1={timeToX(failure, win, width)} x2={timeToX(failure, win, width)} y1={0} y2={height} className="cursor-failure" />
      )}
      {inWin(cursor) && (
        <line x1={timeToX(cursor, win, width)} x2={timeToX(cursor, win, width)} y1={0} y2={height} className="cursor-select" />
      )}
    </svg>
  );
}

const Track = memo(function Track({
  wave, signal, win, width, minTicks, radix, selected, onPick,
}: {
  wave: WaveformWindow;
  signal: SignalWindow;
  win: TimeWindow;
  width: number;
  minTicks: bigint;
  radix: Radix;
  selected: boolean;
  onPick: (signalPath: string, e: MouseEvent<SVGSVGElement>) => void;
}) {
  const shapes = useMemo(() => {
    // A handful of path strings per track instead of one element per change.
    const segs = segments(wave, signal, win, minTicks);
    const hi = 6;
    const lo = ROW_H - 6;
    const mid = ROW_H / 2;
    let level = "";
    let dense = "";
    let bus = "";
    let unknown = "";
    let unavailable = "";
    const labels: { x: number; text: string }[] = [];
    let prevBit: string | null = null;
    for (const seg of segs) {
      const x1 = timeToX(seg.start, win, width);
      const x2 = timeToX(seg.end, win, width);
      if (seg.value === null) {
        unavailable += `M${x1} ${hi}H${Math.max(x1 + 1, x2)}V${lo}H${x1}Z`;
        prevBit = null;
        continue;
      }
      const isUnknown = /[xz]/.test(seg.value.bits);
      if (signal.width === 1 && !isUnknown) {
        const y = seg.value.bits === "1" ? hi : lo;
        if (prevBit !== null && prevBit !== seg.value.bits) level += `M${x1} ${hi}V${lo}`;
        level += `M${x1} ${y}H${x2}`;
        if (seg.dense) dense += `M${x1} ${hi}H${Math.max(x1 + 1, x2)}V${lo}H${x1}Z`;
        prevBit = seg.value.bits;
        continue;
      }
      prevBit = null;
      const hex = `M${x1 + 2} ${mid}L${x1 + 5} ${hi}H${x2 - 5}L${x2 - 2} ${mid}L${x2 - 5} ${lo}H${x1 + 5}Z`;
      if (isUnknown) unknown += hex;
      else bus += hex;
      const text = radix === "bin" ? seg.value.bits : formatValue(seg.value, radix) ?? seg.value.bits;
      if (x2 - x1 > text.length * 7 + 10) labels.push({ x: x1 + 8, text });
    }
    return { level, dense, bus, unknown, unavailable, labels };
  }, [wave, signal, win, width, minTicks, radix]);

  return (
    <svg
      width={width}
      height={ROW_H}
      className={`track${selected ? " selected" : ""}`}
      onClick={(e) => onPick(signal.signal_path, e)}
      role="presentation"
      data-signal={signal.signal_path}
    >
      {shapes.unavailable && <path d={shapes.unavailable} className="seg-unavailable" />}
      {shapes.dense && <path d={shapes.dense} className="dense-band" />}
      {shapes.level && <path d={shapes.level} className="bit" />}
      {shapes.bus && <path d={shapes.bus} className="bus-shape" />}
      {shapes.unknown && <path d={shapes.unknown} className="bus-shape unknown" />}
      {shapes.labels.map((l, i) => (
        <text key={i} x={l.x} y={ROW_H / 2 + 4} className="bus-text">{l.text}</text>
      ))}
    </svg>
  );
});
