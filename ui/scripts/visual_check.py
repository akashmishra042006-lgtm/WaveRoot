"""Visual and performance inspection of the built UI in real Chrome.

    npm --prefix ui run build
    python3 ui/scripts/visual_check.py OUT_DIR

Serves ui/dist, then at 1440/1024/768/375 px: loads the mock FIFO sample,
selects suspect 1, screenshots, and checks that (a) the page itself never
scrolls horizontally (the waveform's own scroller may), and (b) no two
interactive or heading elements overlap. Then measures selection latency on a
generated large report (100 signals x 1000 transitions, 1000 graph nodes).
Prints JSON results; exits nonzero if a layout check fails.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import platform
import statistics
import sys
from typing import Any
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

UI = Path(__file__).resolve().parents[1]
DIST = UI / "dist"
MOCK = UI.parent / "tests" / "fixtures" / "mock_fifo_failure.json"
WIDTHS = [1440, 1024, 768, 375]

OVERLAP_JS = """
() => {
  const els = [...document.querySelectorAll('button, select, h1, h2, .badge, dt, dd, [role=tab]')]
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && e.offsetParent !== null && e.checkVisibility(); });
  const bad = [];
  for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
    const a = els[i], b = els[j];
    if (a.contains(b) || b.contains(a)) continue;
    const r = a.getBoundingClientRect(), s = b.getBoundingClientRect();
    const ix = Math.min(r.right, s.right) - Math.max(r.left, s.left);
    const iy = Math.min(r.bottom, s.bottom) - Math.max(r.top, s.top);
    if (ix > 1 && iy > 1) bad.push(`${a.tagName}:${(a.textContent||'').trim().slice(0,30)} <> ${b.tagName}:${(b.textContent||'').trim().slice(0,30)}`);
  }
  return bad;
}
"""


def serve() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def large_report(path: Path) -> None:
    mock = json.loads(MOCK.read_text())
    report = mock["report"]
    start, step, n = 20000, 1000, 1000
    signals = []
    for i in range(100):
        name = "tb.dut.full" if i == 0 else f"tb.dut.s{i}"
        samples = [{"time_ps": str(start + step * k), "value": {"width": 1, "bits": str((k + i) % 2)}} for k in range(1, n + 1)]
        signals.append({"signal_path": name, "width": 1, "initial": {"width": 1, "bits": "0"}, "samples": samples})
    report["waveform"] = {"start_ps": str(start), "end_ps": str(start + step * (n + 1)), "signals": signals, "unavailable": []}
    obs = report["failure"]["observation"]
    anchor = dict(report["nodes"][0])
    nodes = [anchor] + [
        {"id": f"n{i}", "signal_path": f"tb.dut.s{i % 99 + 1}", "bit_range": None, "observation": obs,
         "value": {"width": 1, "bits": "1"}, "location": None}
        for i in range(1, 1000)
    ]
    edges = [{"id": f"e{i}", "source": f"n{i}", "target": anchor["id"] if i == 1 else f"n{i - 1}",
              "edge_type": "combinational", "role": "data", "branch": "retained", "reason": "generated"}
             for i in range(1, 1000)]
    report.update(nodes=nodes, edges=edges, suspects=[
        {"id": f"s{i}", "rank": i, "node_id": f"n{i * 100}", "location": None, "reason": "generated", "evidence_node_ids": []}
        for i in range(1, 6)
    ])
    report["provenance"]["note"] = "Generated performance fixture."
    path.write_text(json.dumps(mock))


def cpu_name() -> str:
    """Best-effort CPU model for the report; /proc exists only on Linux."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def overlaps_at_rest(page: Any) -> list[str]:
    """Overlapping controls, measured with the page scrolled to the top.

    The header is sticky, so once the page has scrolled (selecting a suspect
    scrolls it) the header necessarily covers whatever is beneath it; that is
    scrolling, not a layout defect. At scroll 0 nothing may overlap."""
    page.evaluate("window.scrollTo(0, 0)")
    return list(page.evaluate(OVERLAP_JS))


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    server = serve()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    results: dict[str, object] = {"layout": {}, "machine": {}}
    failed = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get("WAVEROOT_BROWSER_CHANNEL", "chrome"), headless=True)
        results["machine"] = {
            "browser": browser.version,
            "platform": platform.platform(),
            "cpu": cpu_name(),
        }
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.get_by_test_id("sample-select").select_option("mock-fifo")
            if width <= 700:
                page.get_by_role("tab", name="Suspects").click()
            if width > 700:
                # Exercise the actual React Flow node, not its list equivalent.
                page.locator('.react-flow__node[data-id="wptr.c2.settled"]').click()
                assert page.get_by_test_id("cursor-time").inner_text() == "45000 ps"
                assert "35-40" in page.get_by_test_id("source-location").inner_text()
                assert "selected" in page.locator('canvas[data-signal="tb.dut.wptr"]').get_attribute("class")
                assert page.get_by_test_id("failure-marker-time").inner_text() == "55000 ps"
            page.get_by_test_id("suspect-s1").click()
            page.wait_for_timeout(150)
            if width > 700:
                assert page.locator(".react-flow__edge-path").count() > 0
                assert page.locator('canvas[data-signal="tb.dut.wptr"]').evaluate(
                    "(c) => c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((v,i) => i%4===3 && v>0)")
                assert page.locator(".code li").evaluate_all(
                    "(lines) => lines.filter(l => !l.textContent.trim()).every(l => l.textContent === ' ' && l.getBoundingClientRect().height > 10)")
            page.screenshot(path=str(out / f"fifo-{width}.png"), full_page=True)
            page_scroll = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            overlaps = overlaps_at_rest(page)
            wave_confined = None
            if width <= 700:
                page.get_by_role("tab", name="Waveforms").click()
                page.wait_for_timeout(100)
                page.screenshot(path=str(out / f"fifo-{width}-waveforms.png"), full_page=True)
                page_scroll = max(page_scroll, page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))
                overlaps += overlaps_at_rest(page)
            wave_confined = page.evaluate(
                "(() => { const w = document.querySelector('.wave-scroll'); return w ? w.scrollWidth >= w.clientWidth : null; })()")
            ok = page_scroll <= 0 and not overlaps and not errors
            failed |= not ok
            results["layout"][str(width)] = {  # type: ignore[index]
                "page_horizontal_overflow_px": page_scroll, "overlaps": overlaps[:10],
                "waveform_scrolls_inside_panel": wave_confined, "page_errors": errors, "ok": ok,
            }
            page.close()

        # Other states at desktop width, for the record.
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url)
        for sample in ("real-alu-x", "real-fifo-pass", "state-inconclusive", "real-fifo-fail"):
            page.get_by_test_id("sample-select").select_option(sample)
            page.wait_for_timeout(100)
            page.screenshot(path=str(out / f"state-{sample}.png"), full_page=True)
        page.close()

        # Performance on a bounded large viewport.
        big = out / "large_report.json"
        large_report(big)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url)
        load_ms = page.evaluate("performance.now()")
        page.get_by_test_id("file-input").set_input_files(str(big))
        page.get_by_test_id("failure-strip").wait_for()
        load_ms = page.evaluate("performance.now()") - load_ms
        measure = """async (sel) => {
          const times = [];
          for (let i = 0; i < 20; i++) {
            const el = document.querySelector(sel(i));
            const t0 = performance.now();
            el.click();
            await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
            times.push(performance.now() - t0);
          }
          return times;
        }"""
        suspects = page.evaluate(measure.replace("sel(i)", "`[data-testid=suspect-s${i % 5 + 1}]`"))
        ticks = page.evaluate(measure.replace("sel(i)", "`button[aria-label='Move cursor forward one tick']`"))
        page.screenshot(path=str(out / "large-1440.png"))
        results["performance"] = {
            "dataset": "100 signals x 1000 transitions (100,000), 1000 graph nodes, 999 edges",
            "graph": "not drawn above 400 nodes by design; node list windowed to 500",
            "initial_load_ms": round(load_ms, 1),
            "suspect_selection_ms": {"median": round(statistics.median(suspects), 1), "max": round(max(suspects), 1)},
            "cursor_step_ms": {"median": round(statistics.median(ticks), 1), "max": round(max(ticks), 1)},
            "target_ms": 100,
        }
        browser.close()
    server.shutdown()
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/waveroot-inspect")))
