import { memo, useEffect, useCallback, useMemo, useRef, type Dispatch, type KeyboardEvent, type MouseEvent } from "react";
import type { Loaded, SignalWindow, WaveformWindow } from "../model/types";
import { formatTime, formatValue, shortName, type Radix } from "../model/values";
import { eventTimes, tenCycleWindow, segments, timeToX, valueAt, xToTime, type TimeWindow } from "../model/waveform";
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
    (signalPath: string, e: MouseEvent<HTMLCanvasElement>) => {
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
            " The comparison used the benchmark self-check; waveform data arrives with Track A's provider."}
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
          <button type="button" onClick={() => dispatch({type:"setWindow",window:tenCycleWindow(wave,doc.failure?.clock_path ?? "",sel.cursorTime ?? wave.start_ps)})}>10 cycles</button>
          <button type="button" onClick={() => dispatch({ type: "zoom", factor: 0.5 })}>Zoom in</button>
          <button type="button" onClick={() => dispatch({ type: "zoom", factor: 2 })}>Zoom out</button>
          <button type="button" onClick={() => dispatch({ type: "setWindow", window: { start: wave.start_ps, end: wave.end_ps } })}>Fit</button>
          <button type="button" onClick={() => dispatch({ type: "stepCursor", ticks: -1n })} aria-label="Move cursor back one tick">−1 tick</button>
          <button type="button" onClick={() => dispatch({ type: "stepCursor", ticks: 1n })} aria-label="Move cursor forward one tick">+1 tick</button>
        </div>
      }
    >
      <p className="hint">10-cycle fit uses recorded rising clock edges; shorter traces show available coverage.</p>
      <p className="readout" aria-live="off">
        Cursor ({sel.selectedObservation?.phase ?? "settled"}): <strong data-testid="cursor-time">{sel.cursorTime === null ? "none" : formatTime(sel.cursorTime)}</strong>
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
          <div className="wave-axis-name" style={{height:ROW_H}}>SIGNAL / VALUE</div>
          {wave.signals.map((s) => {
            const v = sel.cursorTime === null ? null : valueAt(wave, s, sel.selectedObservation?.phase === "before" ? sel.cursorTime - 1n : sel.cursorTime);
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
          <div className="wave-axis" style={{width,height:ROW_H}} aria-label="Time axis in picoseconds">
            {Array.from({length:5},(_,i)=>{const time=win.start+(win.end-win.start)*BigInt(i)/4n;return <span key={i} style={{left:`${i*25}%`,transform:i===4?"translateX(-100%)":i===0?undefined:"translateX(-50%)"}}>{formatTime(time)}</span>;})}
          </div>
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
  onPick: (signalPath: string, e: MouseEvent<HTMLCanvasElement>) => void;
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

  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    el.width = Math.round(width * dpr); el.height = ROW_H * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, ROW_H);
    const draw = (path: string, stroke: string, fill?: string) => {
      if (!path) return;
      const shape = new Path2D(path);
      ctx.strokeStyle = stroke; ctx.lineWidth = 1.4;
      if (fill) {ctx.fillStyle = fill; ctx.fill(shape);}
      ctx.stroke(shape);
    };
    draw(shapes.unavailable, "#63738a", "#202b3b");
    if (shapes.unavailable) {
      ctx.save(); ctx.clip(new Path2D(shapes.unavailable)); ctx.strokeStyle = "#63738a";
      ctx.beginPath();
      for (let x = -ROW_H; x < width; x += 8) {ctx.moveTo(x, ROW_H); ctx.lineTo(x + ROW_H, 0);}
      ctx.stroke(); ctx.restore();
    }
    draw(shapes.dense, "#52d7ed", "#174b5d");
    draw(shapes.level, "#52d7ed");
    draw(shapes.bus, "#52d7ed", "#103441");
    draw(shapes.unknown, "#d5a0ff", "#352345");
    ctx.font = "11px ui-monospace, monospace"; ctx.fillStyle = "#c8e9ee";
    for (const label of shapes.labels) ctx.fillText(label.text, label.x, ROW_H/2+4);
  }, [shapes, width]);
  return <canvas ref={canvas} style={{width, height:ROW_H}} className={`track${selected ? " selected" : ""}`}
    onClick={e=>onPick(signal.signal_path,e)} data-signal={signal.signal_path}
    aria-label={`${signal.signal_path} waveform; values available in the signal list`} />;
});
