"""Track A tests: the opt-in parsed-trace cache."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.contracts import IndeterminateClockError, MockFixture, load_mock_fixture
from src.waveform import (
    CACHE_FORMAT_VERSION,
    InvalidTraceError,
    VcdWaveformProvider,
    cache_key,
    load_cached,
    load_trace,
    parse_vcd,
)
from src.waveform import cache as cache_module
from tests.test_waveform import GAP_TEXT, HEADER, vcd_from_mock
from tests.test_waveform_fst import fake_converter

FIXTURES = Path(__file__).parent / "fixtures"

SKIPPED_AND_GLITCH = """$timescale 1ns $end
$scope module top $end
$var real 64 r voltage $end
$var wire 1 e \\weird.name $end
$var wire 1 ! clk $end
$var wire 4 # bus [3:0] $end
$scope module u $end
$var wire 4 # port [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
r1.5 r
1e
0!
b0 #
#5
1!
0!
1!
b1x #
#10
0!
#15
"""


@pytest.fixture(scope="module")
def fixture() -> MockFixture:
    return load_mock_fixture(FIXTURES / "mock_fifo_failure.json")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def entries(cache_dir: Path) -> list[str]:
    return sorted(item.name for item in cache_dir.iterdir()) if cache_dir.exists() else []


# --------------------------------------------------------------------------
# Hits and misses
# --------------------------------------------------------------------------


def test_second_load_is_a_hit_without_reparsing(
    tmp_path: Path, fixture: MockFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = vcd_from_mock(fixture.trace)
    trace = write(tmp_path / "sim.vcd", text)
    cache_dir = tmp_path / "cache"
    first = load_cached(trace, cache_dir=cache_dir)
    key = cache_key(text.encode(), keep=None)
    assert entries(cache_dir) == [f"{key}.json"]  # no temporary files left behind

    monkeypatch.setattr(cache_module, "parse_vcd", lambda *a, **k: pytest.fail("trace was re-parsed"))
    second = load_cached(trace, cache_dir=cache_dir)
    assert second.trace == first.trace == parse_vcd(text)

    # The key is content, not location or modification time.
    copy = write(tmp_path / "elsewhere" / "copy.vcd", text)
    os.utime(copy, (0, 0))
    assert load_cached(copy, cache_dir=cache_dir).trace == first.trace
    assert entries(cache_dir) == [f"{key}.json"]


def test_content_change_is_a_miss(tmp_path: Path, fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    trace = write(tmp_path / "sim.vcd", text)
    cache_dir = tmp_path / "cache"
    before = load_cached(trace, cache_dir=cache_dir)
    stat = trace.stat()

    changed = text + "#100001\n"
    trace.write_text(changed, encoding="utf-8")
    os.utime(trace, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same mtime, new content
    after = load_cached(trace, cache_dir=cache_dir)
    assert after.trace == parse_vcd(changed)
    assert after.get_coverage().end_ps == 100001 != before.get_coverage().end_ps
    assert len(entries(cache_dir)) == 2


def test_keep_change_is_a_miss_and_keep_order_is_irrelevant(tmp_path: Path, fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    trace = write(tmp_path / "sim.vcd", text)
    cache_dir = tmp_path / "cache"
    everything = load_cached(trace, cache_dir=cache_dir)
    clocks = load_cached(trace, cache_dir=cache_dir, keep=["tb.dut.clk", "tb.dut.rst_n"])
    assert clocks.trace.signal_paths == ("tb.dut.clk", "tb.dut.rst_n")
    assert clocks.trace == parse_vcd(text, keep=["tb.dut.clk", "tb.dut.rst_n"])
    assert len(everything.trace.signal_paths) == 14
    assert len(entries(cache_dir)) == 2

    data = text.encode()
    assert cache_key(data, keep=["b.*", "a.**"]) == cache_key(data, keep=["a.**", "b.*", "a.**"])
    assert len({cache_key(data, keep=None), cache_key(data, keep=["**"]), cache_key(data, keep=["*.**"])}) == 3
    assert cache_key(data, keep=None) != cache_key(data, keep=None, converter_version="fst2vcd 3.3")


def test_cache_is_opt_in(tmp_path: Path, fixture: MockFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    trace = write(tmp_path / "sim.vcd", vcd_from_mock(fixture.trace))
    VcdWaveformProvider.from_path(trace)
    load_trace(trace)
    assert not (tmp_path / ".waveroot-cache").exists()


# --------------------------------------------------------------------------
# Damaged entries
# --------------------------------------------------------------------------


def _tamper(payload: dict[str, Any], change: str) -> dict[str, Any]:
    if change == "version":
        payload["cache_format_version"] = CACHE_FORMAT_VERSION + 1
    elif change == "key":
        payload["key"] = "0" * 64
    elif change == "extra_field":
        payload["note"] = 1
    elif change == "width":
        payload["series"][0]["values"][0] += "1"
    elif change == "order":
        payload["series"][0]["times"].reverse()
    elif change == "integer_time":
        payload["series"][0]["times"][0] = int(payload["series"][0]["times"][0])
    elif change == "leading_zero":
        payload["series"][0]["times"][-1] = "0" + payload["series"][0]["times"][-1]
    elif change == "bad_bits":
        payload["series"][0]["values"][-1] = "2"
    elif change == "index":
        payload["series_index"][0] = len(payload["series"])
    elif change == "decl":
        payload["decls"][0]["width"] = "one"
    return payload


@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "empty",
        "binary",
        "not_an_object",
        "directory",
        "version",
        "key",
        "extra_field",
        "width",
        "order",
        "integer_time",
        "leading_zero",
        "bad_bits",
        "index",
        "decl",
    ],
)
def test_damaged_entry_is_ignored_and_rebuilt(tmp_path: Path, fixture: MockFixture, damage: str) -> None:
    text = vcd_from_mock(fixture.trace)
    trace = write(tmp_path / "sim.vcd", text)
    cache_dir = tmp_path / "cache"
    load_cached(trace, cache_dir=cache_dir)
    entry = cache_dir / f"{cache_key(text.encode(), keep=None)}.json"
    good = entry.read_text(encoding="utf-8")

    if damage == "truncated":
        entry.write_text(good[: len(good) // 2], encoding="utf-8")
    elif damage == "empty":
        entry.write_text("", encoding="utf-8")
    elif damage == "binary":
        entry.write_bytes(b"\xff\xfe\x00garbage")
    elif damage == "not_an_object":
        entry.write_text("[1, 2, 3]", encoding="utf-8")
    elif damage == "directory":
        entry.unlink()
        entry.mkdir()
    else:
        entry.write_text(json.dumps(_tamper(json.loads(good), damage)), encoding="utf-8")

    rebuilt = load_cached(trace, cache_dir=cache_dir)
    assert rebuilt.trace == parse_vcd(text)
    if damage != "directory":  # a directory in the way cannot be replaced; the load still succeeds
        assert entry.read_text(encoding="utf-8") == good


def test_entry_for_other_content_is_never_returned(tmp_path: Path, fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    other = text + "#100001\n"
    trace = write(tmp_path / "sim.vcd", text)
    cache_dir = tmp_path / "cache"
    load_cached(write(tmp_path / "other.vcd", other), cache_dir=cache_dir)
    stale = cache_dir / f"{cache_key(other.encode(), keep=None)}.json"
    # Plant the other trace's entry under this trace's key.
    stale.rename(cache_dir / f"{cache_key(text.encode(), keep=None)}.json")
    assert load_cached(trace, cache_dir=cache_dir).trace == parse_vcd(text)


def test_unwritable_cache_directory_never_fails_a_load(tmp_path: Path, fixture: MockFixture) -> None:
    text = vcd_from_mock(fixture.trace)
    trace = write(tmp_path / "sim.vcd", text)
    blocker = write(tmp_path / "not_a_dir", "a file where the cache directory should be")
    assert load_cached(trace, cache_dir=blocker).trace == parse_vcd(text)
    assert load_cached(trace, cache_dir=blocker / "nested").trace == parse_vcd(text)


def test_trace_errors_propagate_and_are_not_cached(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    bad = write(tmp_path / "bad.vcd", HEADER + "#0\n0?\n")
    with pytest.raises(InvalidTraceError):
        load_cached(bad, cache_dir=cache_dir)
    with pytest.raises(FileNotFoundError):
        load_cached(tmp_path / "absent.vcd", cache_dir=cache_dir)
    assert entries(cache_dir) == []


# --------------------------------------------------------------------------
# Fidelity: a cached provider equals a fresh parse
# --------------------------------------------------------------------------


def _assert_hit_equals_fresh(tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> VcdWaveformProvider:
    trace = write(tmp_path / "trace.vcd", text)
    cache_dir = tmp_path / "cache"
    load_cached(trace, cache_dir=cache_dir, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(cache_module, "parse_vcd", lambda *a, **k: pytest.fail("trace was re-parsed"))
        cached = load_cached(trace, cache_dir=cache_dir, **kwargs)
    fresh = VcdWaveformProvider(parse_vcd(text, **kwargs))
    assert cached.trace == fresh.trace
    return cached


def test_cached_fifo_matches_a_fresh_parse(tmp_path: Path, fixture: MockFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    text = vcd_from_mock(fixture.trace)
    cached = _assert_hit_equals_fresh(tmp_path, text, monkeypatch)
    fresh = VcdWaveformProvider.from_text(text)
    paths = list(fresh.trace.signal_paths)
    assert cached.get_window_slice(paths, 0, 100001) == fresh.get_window_slice(paths, 0, 100001)
    assert cached.get_clock_edges("tb.dut.clk", 0, 100001) == fresh.get_clock_edges("tb.dut.clk", 0, 100001)

    # Timestamps are stored as canonical decimal strings.
    entry = next((tmp_path / "cache").iterdir())
    payload = json.loads(entry.read_text(encoding="utf-8"))
    assert payload["coverage"] == {"start_ps": "0", "end_ps": "100000"}
    assert all(isinstance(time, str) for series in payload["series"] for time in series["times"])


def test_cached_dump_gap_trace_matches_a_fresh_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = _assert_hit_equals_fresh(tmp_path, GAP_TEXT, monkeypatch)
    assert cached.trace.unavailable == parse_vcd(GAP_TEXT).unavailable
    assert len(cached.trace.unavailable) == 1


def test_cached_skipped_variables_aliases_and_clock_ambiguity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = _assert_hit_equals_fresh(tmp_path, SKIPPED_AND_GLITCH, monkeypatch)
    assert {item.reference for item in cached.trace.skipped} == {"voltage", "\\weird.name"}
    series = cached.trace._series_by_path
    assert series["top.bus"] is series["top.u.port"]
    assert series["top.clk"].multi_change_times == {5000}
    with pytest.raises(IndeterminateClockError):
        cached.get_clock_edges("top.clk", 0, 15001)


def test_cached_keep_selection_matches_a_fresh_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = _assert_hit_equals_fresh(tmp_path, SKIPPED_AND_GLITCH, monkeypatch, keep=["top.u.*"])
    assert cached.trace.signal_paths == ("top.u.port",)


# --------------------------------------------------------------------------
# FST input
# --------------------------------------------------------------------------


def test_fst_key_includes_the_converter_version(
    tmp_path: Path, fixture: MockFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = vcd_from_mock(fixture.trace)
    bin_dir = tmp_path / "bin"
    version_file = write(tmp_path / "version.txt", "fst2vcd 3.3.100")
    counter = tmp_path / "conversions.txt"
    fake_converter(
        bin_dir,
        f"""
        if sys.argv[1:] == ["--version"]:
            print(open({str(version_file)!r}).read())
            sys.exit(0)
        with open({str(counter)!r}, "a") as handle:
            handle.write("x")
        sys.stdout.write({text!r})
        """,
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    fst = tmp_path / "sim.fst"
    fst.write_bytes(b"opaque FST bytes")
    cache_dir = tmp_path / "cache"

    def conversions() -> int:
        return len(counter.read_text()) if counter.exists() else 0

    assert load_cached(fst, cache_dir=cache_dir).trace == parse_vcd(text)
    assert load_cached(fst, cache_dir=cache_dir).trace == parse_vcd(text)
    assert conversions() == 1
    expected = cache_key(b"opaque FST bytes", keep=None, converter_version="fst2vcd 3.3.100")
    assert entries(cache_dir) == [f"{expected}.json"]

    version_file.write_text("fst2vcd 3.3.200")
    load_cached(fst, cache_dir=cache_dir)
    assert conversions() == 2
    assert len(entries(cache_dir)) == 2


# --------------------------------------------------------------------------
# Property test
# --------------------------------------------------------------------------

changes = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=30),
        st.sampled_from(["!", "#", "%"]),
        st.text(alphabet="01xzXZ", min_size=1, max_size=3),
    ),
    min_size=1,
    max_size=25,
)


@settings(max_examples=60, deadline=None)
@given(changes, st.one_of(st.none(), st.tuples(st.integers(0, 30), st.integers(1, 10))), st.booleans())
def test_random_traces_round_trip_through_the_cache(
    records: list[tuple[int, str, str]], gap: tuple[int, int] | None, filtered: bool
) -> None:
    widths = {"!": 1, "#": 3, "%": 3}
    lines = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 3 # bus [2:0] $end",
        "$var wire 3 % alias [2:0] $end",
        "$scope module u $end",
        "$var wire 3 # port [2:0] $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    timeline: list[tuple[int, str]] = []
    for time, code, bits in records:
        bits = bits[: widths[code]]
        timeline.append((time, f"{bits}{code}" if widths[code] == 1 else f"b{bits} {code}"))
    if gap is not None:
        start, length = gap
        timeline.append((start, "$dumpoff $end"))
        timeline.append((start + length, "$dumpon 0! b0 # b1 % $end"))
    current: int | None = None
    for time, line in sorted(timeline, key=lambda item: item[0]):
        if time != current:
            lines.append(f"#{time}")
            current = time
        lines.append(line)
    text = "\n".join(lines) + "\n"
    keep = ["top.u.*", "top.clk"] if filtered else None
    try:
        fresh = parse_vcd(text, keep=keep)
    except InvalidTraceError:
        assume(False)
        raise
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        trace = write(root / "t.vcd", text)
        load_cached(trace, cache_dir=root / "cache", keep=keep)
        with mock.patch.object(cache_module, "parse_vcd", side_effect=AssertionError("re-parsed")):
            cached = load_cached(trace, cache_dir=root / "cache", keep=keep)
    assert cached.trace == fresh
    assert cached.get_coverage() == fresh.coverage
