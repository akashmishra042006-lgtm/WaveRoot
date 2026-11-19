"""End-to-end check of the UI against the real local API and a real simulator.

Run from the repository root (needs Verilator and Chrome, and `npm --prefix ui
run build` done first):

    uv run --with playwright python ui/scripts/live_check.py

Starts ``python -m src.app.cli serve`` and ``vite preview``, then in Chrome:
the service-offline state, the missing-token state, a real FIFO run through
the UI, and a cancel. Exits
non-zero on any failed expectation.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[2]
API_PORT, UI_PORT = 8766, 4173


def wait_for(url: str, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"{url} never came up")


def free(port: int) -> None:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"port {port} is already in use")


def main() -> int:
    free(API_PORT)
    free(UI_PORT)
    artifacts = tempfile.mkdtemp(prefix="waveroot-live-")
    token_file = Path(artifacts) / "api-token"
    api = subprocess.Popen(
        [sys.executable, "-m", "src.app.cli", "serve", "--port", str(API_PORT), "--artifacts", artifacts, "--dev",
         "--token-file", str(token_file)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    ui = subprocess.Popen(
        ["npm", "--prefix", "ui", "run", "preview", "--", "--port", str(UI_PORT), "--strictPort", "--host", "127.0.0.1"],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        start_new_session=True,  # npm starts vite as a child; stop the whole group
    )
    try:
        wait_for(f"http://127.0.0.1:{API_PORT}/api/health")
        wait_for(f"http://127.0.0.1:{UI_PORT}/")
        token = token_file.read_text().strip()
        channel = os.environ.get("WAVEROOT_BROWSER_CHANNEL", "chrome")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=channel, headless=True)
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            # 1. No service: an explicit offline state, and saved reports still open.
            page.goto(f"http://127.0.0.1:{UI_PORT}/?api=http://127.0.0.1:1")
            expect(page.get_by_test_id("service-offline")).to_contain_text("python -m src.app.cli serve")
            page.get_by_test_id("sample-select").select_option("real-fifo-fail")
            expect(page.get_by_test_id("run-title")).to_be_visible()

            # 2. The real service without its token: a token prompt, not "offline".
            page.goto(f"http://127.0.0.1:{UI_PORT}/?api=http://127.0.0.1:{API_PORT}")
            expect(page.get_by_test_id("service-unauthorized")).to_contain_text("#token=")
            print("live token: refused without it")

            # 3. A real FIFO run through the UI (Verilator + Track A's engine).
            # Leave the page first: a fragment-only change would not reload it.
            page.goto("about:blank")
            page.goto(f"http://127.0.0.1:{UI_PORT}/?api=http://127.0.0.1:{API_PORT}#token={token}")
            expect(page.get_by_test_id("run-start")).to_be_enabled(timeout=15000)
            if token in page.url:
                raise SystemExit("the token was left in the address bar")
            page.get_by_test_id("run-start").click()
            expect(page.get_by_test_id("run-title")).to_be_visible(timeout=240000)
            expect(page.get_by_test_id("run-state")).to_contain_text("finished")
            expect(page.get_by_test_id("failure-signal")).to_have_text("TOP.tb.dut.full")
            expect(page.get_by_test_id("failure-time")).to_contain_text("55000")
            expect(page.get_by_test_id("real-badge")).to_be_visible()
            print("live run: fail at TOP.tb.dut.full, 55000 ps")

            # Track B's real report: ranked suspects, and the source behind the
            # first one served by the API and hash-verified in the browser.
            suspects = page.locator('[data-testid^="suspect-"]')
            expect(suspects.first).to_be_visible()
            suspects.first.click()
            expect(page.get_by_test_id("source-verified")).to_be_visible(timeout=15000)
            expect(page.get_by_test_id("source-location")).to_contain_text("fifo.sv")
            print(f"live causal: {suspects.count()} suspect(s), first source verified")

            # 4. Cancel a second run from the UI.
            page.get_by_test_id("run-start").click()
            expect(page.get_by_test_id("run-cancel")).to_be_enabled(timeout=15000)
            page.get_by_test_id("run-cancel").click()
            expect(page.get_by_test_id("run-state")).to_contain_text("cancelled", timeout=60000)
            print("live cancel: cancelled")

            assert not errors, f"page errors: {errors}"
            browser.close()
        return 0
    finally:
        api.terminate()
        try:
            os.killpg(ui.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for proc in (ui, api):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            os.killpg(ui.pid, signal.SIGKILL)  # vite may outlive npm
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
