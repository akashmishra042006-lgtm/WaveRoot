"""FST input for Track A through GTKWave's ``fst2vcd``.

An FST trace is converted to VCD text in memory and then parsed exactly like a
``.vcd`` file. The converter runs with an argument list (never a shell string),
its standard output is the VCD text, and nothing is written next to the input.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from src.contracts import ContractError
from src.waveform.parser import UnsupportedTraceError, VcdWaveformProvider, parse_vcd

DEFAULT_CONVERTER: Final = "fst2vcd"
STDERR_LIMIT: Final = 2000
_INSTALL_HINT: Final = (
    "install GTKWave, which provides fst2vcd (for example `brew install gtkwave` "
    "or `apt install gtkwave`), and make sure it is on PATH"
)


class ConverterUnavailableError(ContractError):
    """The FST converter executable cannot be found or started."""


class ConversionFailedError(ContractError):
    """The FST converter ran but did not produce a VCD trace."""

    def __init__(self, message: str, *, returncode: int | None, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _check_timeout(timeout_s: float) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError("timeout_s must be a number of seconds")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be a positive, finite number of seconds")
    return float(timeout_s)


def _resolve(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved is None:
        raise ConverterUnavailableError(f"FST converter {executable!r} was not found; {_INSTALL_HINT}")
    return resolved


def _stderr_text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return text[:STDERR_LIMIT]


def _run(argv: list[str], timeout_s: float, what: str) -> bytes:
    """Run ``argv`` and return its stdout, mapping every failure to a contract error."""
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as error:
        stderr = _stderr_text(error.stderr)
        raise ConversionFailedError(
            f"{what} timed out after {timeout_s:g} s (exit status: none, killed); stderr: {stderr!r}",
            returncode=None,
            stderr=stderr,
        ) from error
    except OSError as error:
        raise ConverterUnavailableError(f"{what} could not be started: {error}; {_INSTALL_HINT}") from error
    stderr = _stderr_text(completed.stderr)
    if completed.returncode != 0:
        raise ConversionFailedError(
            f"{what} failed with exit status {completed.returncode}; stderr: {stderr!r}",
            returncode=completed.returncode,
            stderr=stderr,
        )
    return completed.stdout


def convert_fst_to_vcd_text(
    fst_path: Path, *, timeout_s: float = 120.0, executable: str = DEFAULT_CONVERTER
) -> str:
    """Convert ``fst_path`` with ``executable`` and return the VCD text.

    Raises ``OSError`` if the input cannot be opened, ``ConverterUnavailableError``
    if the converter is missing, and ``ConversionFailedError`` for a non-zero
    exit, a timeout, or empty or non-UTF-8 output.
    """
    timeout_s = _check_timeout(timeout_s)
    # Report an unreadable input as such rather than as a converter failure.
    with fst_path.open("rb"):
        pass
    resolved = _resolve(executable)
    what = f"{executable} on {str(fst_path)!r}"
    stdout = _run([resolved, str(fst_path)], timeout_s, what)
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConversionFailedError(
            f"{what} produced output that is not UTF-8 text (exit status 0)", returncode=0, stderr=""
        ) from error
    if not text.strip():
        raise ConversionFailedError(f"{what} produced no output (exit status 0)", returncode=0, stderr="")
    return text


def trace_kind(path: Path) -> str:
    """``"vcd"`` or ``"fst"`` from the suffix (case-insensitive); otherwise unsupported."""
    suffix = path.suffix.lower()
    if suffix == ".vcd":
        return "vcd"
    if suffix == ".fst":
        return "fst"
    raise UnsupportedTraceError(
        f"{str(path)!r} has suffix {path.suffix!r}; only .vcd and .fst traces are supported"
    )


def load_trace(
    path: Path, *, timeout_s: float = 120.0, keep: Sequence[str] | None = None
) -> VcdWaveformProvider:
    """Load a ``.vcd`` or ``.fst`` trace into a provider.

    ``keep`` selects signals by hierarchy pattern, as in ``parse_vcd``.
    """
    if trace_kind(path) == "fst":
        text = convert_fst_to_vcd_text(path, timeout_s=timeout_s)
    else:
        text = path.read_text(encoding="utf-8")
    return VcdWaveformProvider(parse_vcd(text, keep=keep))
