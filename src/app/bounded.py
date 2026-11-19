"""Run a causal adapter in a child process that can be stopped.

Track B's engine is synchronous Python: a thread cannot interrupt it, so the
only hard bound is a separate process the service can kill. The child is a
fresh interpreter (``subprocess``, never ``fork``: the API runs worker threads,
and a forked child could inherit a held lock; and not ``multiprocessing``'s
``spawn``, which re-imports the caller's ``__main__``). It receives the
parent's ``sys.path`` and a pickle of the adapter and its inputs on stdin, and
writes the report as contract JSON, which the parent re-validates. A timeout or
a cancel kills the child; nothing it computed is used.
"""

from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from src.app.adapters import AdapterError, CausalAdapter, CausalContext
from src.app.runner import RunRecord
from src.contracts import CausalReport, FailureAnchor

POLL_S = 0.05
STDERR_TAIL = 2000

# Reads one JSON line (the parent's sys.path) so that the adapter's module is
# importable before anything is unpickled; then hands over to _child_main.
_BOOTSTRAP = (
    "import json, sys\n"
    "sys.path[:] = json.loads(sys.stdin.buffer.readline())\n"
    "from src.app.bounded import _child_main\n"
    "_child_main()\n"
)


class CausalTimeout(Exception):
    """The causal step exceeded its wall-clock limit and was stopped."""


class CausalCancelled(Exception):
    """The caller cancelled while the causal step was running; it was stopped."""


def _child_main() -> None:
    # Ctrl-C reaches the whole foreground process group; the parent decides
    # what a cancel means, so the child must not turn it into an engine error.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # The result goes to the original stdout; anything the engine prints goes
    # to stderr so it cannot corrupt the reply.
    reply: BinaryIO = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    try:
        adapter, failure, run, sources, context = pickle.load(sys.stdin.buffer)
        report = adapter.analyze(failure, run, sources, context)
        result = {"kind": "ok", "payload": report.model_dump_json()}
    except AdapterError as exc:
        result = {"kind": "adapter_error", "payload": str(exc)}
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, never swallowed
        result = {"kind": "error", "payload": f"{type(exc).__name__}: {exc}"}
    reply.write(json.dumps(result).encode())
    reply.close()


def analyze_bounded(
    adapter: CausalAdapter,
    failure: FailureAnchor,
    run: RunRecord,
    sources: Sequence[Path],
    context: CausalContext,
    *,
    timeout_s: float,
    cancel: threading.Event | None = None,
) -> CausalReport:
    """``adapter.analyze`` in a child process, stopped on timeout or cancel.

    Raises ``CausalTimeout``, ``CausalCancelled``, or ``AdapterError`` (the
    adapter refused, the engine raised, or the child died without a result).
    The limit includes the child's interpreter start-up.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    request = json.dumps(sys.path).encode() + b"\n" + pickle.dumps(
        (adapter, failure, run, list(sources), context)
    )
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOTSTRAP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
        )
        try:
            pending: bytes | None = request
            while True:
                # A cancel wins over a result that arrives at the same time.
                if cancel is not None and cancel.is_set():
                    raise CausalCancelled("cancelled during causal analysis")
                try:
                    out, _ = proc.communicate(pending, timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pending = None  # sent; a retry must not send it again
                if time.monotonic() >= deadline:
                    raise CausalTimeout(f"causal analysis exceeded its {timeout_s:g} s limit and was stopped")
            if cancel is not None and cancel.is_set():
                raise CausalCancelled("cancelled during causal analysis")
        finally:
            # The child starts no processes of its own, so killing it is enough.
            # Popen.kill is a no-op once the child has been reaped.
            proc.kill()
            proc.wait()
        try:
            result = json.loads(out)
            kind, payload = result["kind"], result["payload"]
        except (ValueError, KeyError, TypeError):
            err.seek(0, os.SEEK_END)
            err.seek(max(0, err.tell() - STDERR_TAIL))
            tail = err.read().decode(errors="replace").strip()
            raise AdapterError(
                f"the causal analysis process exited without a result (exit code {proc.returncode})"
                + (f": {tail}" if tail else "")
            ) from None
    if kind == "ok":
        return CausalReport.model_validate_json(payload)
    if kind == "adapter_error":
        raise AdapterError(payload)
    raise AdapterError(f"causal analysis failed unexpectedly ({payload}); the failure verdict is unaffected")
