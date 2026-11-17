"""Opt-in on-disk cache of parsed traces.

An entry is the normalized ``ParsedTrace`` stored as JSON (never pickle) under
``cache_dir/<key>.json``. The key is a SHA-256 over the trace file's bytes,
``CACHE_FORMAT_VERSION``, the sorted ``keep`` patterns, and for FST input the
converter's ``--version`` output, so an entry can only be reused for identical
input. Timestamps are canonical decimal strings, as in ``spec.md`` section 11.

Any entry that is missing, unreadable, truncated, malformed, from another
format version, or stored under the wrong key is ignored and rebuilt. Writing
is best-effort: a cache directory that cannot be written never fails a load.
``VcdWaveformProvider.from_path`` and ``load_trace`` never use this cache.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from src.contracts import SignalDecl, TimeInterval, TimeUnit, TraceCoverage
from src.waveform.fst import convert_fst_to_vcd_text, converter_identity, trace_kind
from src.waveform.parser import ParsedTrace, SkippedVariable, VcdWaveformProvider, _Series, parse_vcd
from src.waveform.select import check_patterns

CACHE_FORMAT_VERSION: Final = 1
DEFAULT_CACHE_DIR: Final = Path(".waveroot-cache")

_DECIMAL_RE: Final = re.compile(r"0|[1-9][0-9]*")
_BITS_RE: Final = re.compile(r"[01xz]+")
_MAGNITUDES: Final = frozenset({1, 10, 100})
_BODY_KEYS: Final = frozenset(
    {"cache_format_version", "key", "timescale", "coverage", "unavailable", "decls", "skipped", "series", "series_index"}
)
_TOP_KEYS: Final = _BODY_KEYS | {"digest"}


class _CorruptEntry(ValueError):
    """A cache entry that cannot be trusted."""


def cache_key(
    trace_bytes: bytes, *, keep: Sequence[str] | None, converter_identity: str | None = None
) -> str:
    """SHA-256 hex digest identifying a parse of ``trace_bytes``.

    ``converter_identity`` comes from ``fst.converter_identity`` (a digest of the
    converter executable plus its ``--version`` output) and is ``None`` for VCD
    input. The order and repetition of ``keep`` patterns do not matter;
    ``keep=None`` (everything) is distinct from any pattern list.
    """
    patterns = None if keep is None else sorted(set(check_patterns(keep)))
    header = json.dumps(
        {
            "cache_format_version": CACHE_FORMAT_VERSION,
            "keep": patterns,
            "converter_identity": converter_identity,
        },
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    digest.update(trace_bytes)
    return digest.hexdigest()


def load_cached(
    path: Path,
    *,
    cache_dir: Path,
    keep: Sequence[str] | None = None,
    timeout_s: float = 120.0,
) -> VcdWaveformProvider:
    """Load a ``.vcd`` or ``.fst`` trace, reusing a cached parse when one is valid.

    Raises exactly what ``load_trace`` raises for the trace itself; problems
    with the cache are never raised.
    """
    kind = trace_kind(path)
    keep_patterns = None if keep is None else check_patterns(keep)
    data = path.read_bytes()
    identity = converter_identity(timeout_s=timeout_s) if kind == "fst" else None
    key = cache_key(data, keep=keep_patterns, converter_identity=identity)
    entry = cache_dir / f"{key}.json"

    cached = _read_entry(entry, key)
    if cached is not None:
        return VcdWaveformProvider(cached)

    if kind == "fst":
        text = convert_fst_to_vcd_text(path, timeout_s=timeout_s)
        # The converter reads the file itself; only cache if it is still the hashed content.
        unchanged = hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest()
    else:
        text = data.decode("utf-8")
        unchanged = True
    trace = parse_vcd(text, keep=keep_patterns)
    if unchanged:
        _write_entry(cache_dir, entry, _encode(trace, key))
    return VcdWaveformProvider(trace)


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def _encode(trace: ParsedTrace, key: str) -> dict[str, Any]:
    series: list[_Series] = []
    index_of: dict[int, int] = {}
    series_index: list[int] = []
    for path in trace.decls:
        entry = trace._series_by_path[path]
        position = index_of.get(id(entry))
        if position is None:  # aliases share one series object
            position = index_of[id(entry)] = len(series)
            series.append(entry)
        series_index.append(position)
    body: dict[str, Any] = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "key": key,
        "timescale": {"magnitude": trace.timescale_magnitude, "unit": trace.timescale_unit.value},
        "coverage": trace.coverage.model_dump(mode="json"),
        "unavailable": [interval.model_dump(mode="json") for interval in trace.unavailable],
        "decls": [decl.model_dump(mode="json") for decl in trace.decls.values()],
        "skipped": [
            {"scope": item.scope, "reference": item.reference, "reason": item.reason} for item in trace.skipped
        ],
        "series": [
            {
                "width": entry.width,
                "times": [str(time) for time in entry.times],
                "values": list(entry.values),
                "multi_change_times": [str(time) for time in sorted(entry.multi_change_times)],
            }
            for entry in series
        ],
        "series_index": series_index,
    }
    body["digest"] = _digest(body)
    return body


def _digest(body: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of an entry without its digest field.

    It detects any edit to a stored entry, including well-formed ones that the
    field checks alone would accept (for example a changed value).
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _write_entry(cache_dir: Path, entry: Path, payload: dict[str, Any]) -> None:
    """Write atomically (temporary file, then rename); give up silently on OS errors."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=cache_dir, prefix=f".{entry.stem}.", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, entry)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


# --------------------------------------------------------------------------
# Decoding: every field is checked; anything unexpected is a miss
# --------------------------------------------------------------------------


def _read_entry(entry: Path, key: str) -> ParsedTrace | None:
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        return _decode(payload, key)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise _CorruptEntry(what)


def _int(value: object, what: str, *, minimum: int = 0) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, what)
    assert isinstance(value, int)
    return value


def _time(value: object) -> int:
    _require(isinstance(value, str) and _DECIMAL_RE.fullmatch(value) is not None, "timestamp")
    assert isinstance(value, str)
    return int(value)


def _list(value: object, what: str) -> list[Any]:
    _require(isinstance(value, list), what)
    assert isinstance(value, list)
    return value


def _decode(payload: object, key: str) -> ParsedTrace:
    _require(isinstance(payload, dict) and set(payload) == _TOP_KEYS, "top-level fields")
    assert isinstance(payload, dict)
    _require(payload["cache_format_version"] == CACHE_FORMAT_VERSION, "format version")
    _require(payload["key"] == key, "key")
    body = {name: value for name, value in payload.items() if name != "digest"}
    _require(payload["digest"] == _digest(body), "digest")

    timescale = payload["timescale"]
    _require(isinstance(timescale, dict) and set(timescale) == {"magnitude", "unit"}, "timescale")
    magnitude = _int(timescale["magnitude"], "timescale magnitude")
    _require(magnitude in _MAGNITUDES, "timescale magnitude")
    unit = TimeUnit(timescale["unit"])

    coverage = TraceCoverage.model_validate(payload["coverage"])
    unavailable = tuple(TimeInterval.model_validate(item) for item in _list(payload["unavailable"], "unavailable"))
    decls = [SignalDecl.model_validate(item) for item in _list(payload["decls"], "decls")]
    _require(len({decl.path for decl in decls}) == len(decls), "duplicate paths")

    skipped: list[SkippedVariable] = []
    for item in _list(payload["skipped"], "skipped"):
        _require(isinstance(item, dict) and set(item) == {"scope", "reference", "reason"}, "skipped")
        _require(all(isinstance(item[name], str) for name in item), "skipped")
        skipped.append(SkippedVariable(scope=item["scope"], reference=item["reference"], reason=item["reason"]))

    series = [_decode_series(item) for item in _list(payload["series"], "series")]
    series_index = _list(payload["series_index"], "series_index")
    _require(len(series_index) == len(decls), "series_index length")
    series_by_path: dict[str, _Series] = {}
    used: set[int] = set()
    for decl, raw_index in zip(decls, series_index, strict=True):
        index = _int(raw_index, "series index")
        _require(index < len(series) and series[index].width == decl.width, "series index")
        series_by_path[decl.path] = series[index]
        used.add(index)
    _require(len(used) == len(series), "unreferenced series")
    _check_invariants(series, coverage, unavailable)

    return ParsedTrace(
        timescale_magnitude=magnitude,
        timescale_unit=unit,
        coverage=coverage,
        unavailable=unavailable,
        decls={decl.path: decl for decl in decls},
        skipped=tuple(skipped),
        _series_by_path=series_by_path,
    )


def _check_invariants(
    series: list[_Series], coverage: TraceCoverage, unavailable: tuple[TimeInterval, ...]
) -> None:
    """Cross-field rules every parsed trace satisfies (see ``parser.py``)."""
    for earlier, later in zip(unavailable, unavailable[1:]):
        _require(earlier.end_ps < later.start_ps, "dump gaps overlap or are unordered")
    for entry in series:
        if entry.times:
            _require(coverage.start_ps <= entry.times[0] and entry.times[-1] <= coverage.end_ps, "time outside coverage")
        present = set(entry.times)
        for gap in unavailable:
            if gap.end_ps <= coverage.end_ps:
                # $dumpon must restate every signal where the gap ends.
                _require(gap.end_ps in present, "missing restated value at a dump gap end")
            first = bisect.bisect_left(entry.times, gap.start_ps)
            _require(first == len(entry.times) or entry.times[first] >= gap.end_ps, "value inside a dump gap")


def _decode_series(item: object) -> _Series:
    _require(
        isinstance(item, dict) and set(item) == {"width", "times", "values", "multi_change_times"}, "series fields"
    )
    assert isinstance(item, dict)
    width = _int(item["width"], "series width", minimum=1)
    times = [_time(value) for value in _list(item["times"], "times")]
    _require(all(earlier < later for earlier, later in zip(times, times[1:])), "times not increasing")
    values = _list(item["values"], "values")
    _require(len(values) == len(times), "values length")
    for value in values:
        _require(isinstance(value, str) and len(value) == width and _BITS_RE.fullmatch(value) is not None, "value")
    multi = [_time(value) for value in _list(item["multi_change_times"], "multi_change_times")]
    _require(len(set(multi)) == len(multi), "multi_change_times")
    return _Series(width=width, times=times, values=list(values), multi_change_times=set(multi))
