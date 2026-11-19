"""Causal adapters for tests/track_c/test_causal_bound.py.

They live in their own module because ``src/app/bounded.py`` runs the adapter
in a ``spawn``ed child, which must import the adapter's class by module name.
None of them is analysis; they exercise the process boundary only.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

from src.app.adapters import AdapterError, AdapterKind, CausalContext
from src.app.runner import RunRecord
from src.contracts import CausalReport, FailureAnchor, load_mock_fixture

MOCK_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mock_fifo_failure.json"


class _Stub:
    kind: AdapterKind = "analysis"

    def __init__(self, pid_file: Path | None = None) -> None:
        self.name = type(self).__name__
        self.pid_file = pid_file

    def _mark(self) -> None:
        if self.pid_file is not None:
            self.pid_file.write_text(str(os.getpid()))


class HangingCausal(_Stub):
    """Never returns on its own: stands in for an engine on a huge trace."""

    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path], context: CausalContext
    ) -> CausalReport:
        self._mark()
        while True:
            time.sleep(0.01)


class RefusingCausal(_Stub):
    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path], context: CausalContext
    ) -> CausalReport:
        raise AdapterError("refused for the test")


class CrashingCausal(_Stub):
    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path], context: CausalContext
    ) -> CausalReport:
        raise ZeroDivisionError("engine bug")


class DyingCausal(_Stub):
    """The child process ends without sending anything (a hard crash)."""

    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path], context: CausalContext
    ) -> CausalReport:
        os._exit(7)


class ChattyCausal(_Stub):
    """Prints to stdout, as engine debugging might, then returns a report."""

    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path], context: CausalContext
    ) -> CausalReport:
        print("debug output on stdout")
        return load_mock_fixture(MOCK_FIXTURE).report
