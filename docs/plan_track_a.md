# Track A follow-up plan

Work orders for the execution phase, after the Track A core in `src/waveform/`.
Execute the tickets in order, one commit per ticket. Read `spec.md`,
`src/contracts.py`, `src/waveform/parser.py`, and `src/waveform/diff_engine.py`
first.

## What already exists

| File | Contents |
| --- | --- |
| `src/waveform/parser.py` | `parse_vcd(text) -> ParsedTrace`, `VcdWaveformProvider` (implements `WaveformProvider`), `InvalidTraceError`, `UnsupportedTraceError`, `SkippedVariable` |
| `src/waveform/diff_engine.py` | `compare(provider, golden) -> ComparisonResult`, `compare_files(vcd_path, golden_path)` |
| `tests/test_waveform.py` | 66 tests: differential check against `MockWaveformProvider`, VCD edge cases, property tests, cycle and handshake comparison |

Measured on one laptop with an 11 MB synthetic VCD (201 signals, 520,000 value
changes): parse 0.45 s, 100,000 point queries 0.28 s. Treat these as a rough
baseline, not a guarantee.

## Rules for every ticket

- Stay inside `src/waveform/` and `tests/test_waveform*.py`. Do not edit
  `spec.md`, `src/contracts.py`, `src/app/`, `src/causal/`, `pyproject.toml`, or
  `uv.lock`. If a ticket seems to need one of those, stop and report why.
- Do not change the behavior of `parse_vcd`, `VcdWaveformProvider`, or `compare`
  unless a test proves a bug. Report the failing case before changing them.
- Keep every existing test passing. Do not weaken or delete a test to make a
  change pass.
- No new runtime dependencies. Use the standard library.
- Verification for every ticket, run from the repository root:
  - `uv run pytest tests/test_waveform.py tests/test_contracts.py -q`
  - `uv run mypy src/`
  - plus the ticket's own command below.

## Ticket 1: FST input through `fst2vcd`

**Goal.** Accept `.fst` traces by converting them with GTKWave's `fst2vcd`, then
parsing the VCD as today.

**Files.** New `src/waveform/fst.py`; new `tests/test_waveform_fst.py`; export
from `src/waveform/__init__.py`.

**Interface.**

```python
class ConverterUnavailableError(ContractError): ...
class ConversionFailedError(ContractError): ...

def convert_fst_to_vcd_text(fst_path: Path, *, timeout_s: float = 120.0,
                            executable: str = "fst2vcd") -> str: ...
def load_trace(path: Path, *, timeout_s: float = 120.0) -> VcdWaveformProvider: ...
```

**Requirements.**

- Run the converter with `subprocess.run` and an argument list. Never pass a
  shell string. Capture stdout as the VCD text; write nothing next to the input.
- `executable` not found on `PATH` raises `ConverterUnavailableError` naming the
  tool and how to install it (GTKWave).
- A non-zero exit, a timeout, or empty output raises `ConversionFailedError`
  with the exit status and the first 2,000 characters of stderr.
- `load_trace` dispatches on the suffix: `.vcd` reads the file, `.fst`
  converts, anything else raises `UnsupportedTraceError`.
- Extend `compare_files` to use `load_trace`, mapping
  `ConverterUnavailableError` and `ConversionFailedError` to status `error`.

**Tests.** Unit tests replace the executable with a small Python script written
to `tmp_path` (success, non-zero exit, timeout, empty output, missing tool). One
test marked `@pytest.mark.integration` converts a real FST and compares the
result with the equivalent VCD; it must fail, not skip, when run explicitly
without `fst2vcd` installed. Do not commit a binary FST unless it is under
10 kB and generated from the mock FIFO trace.

**Verify.** `uv run pytest tests/test_waveform_fst.py -q -m "not integration"`

## Ticket 2: Hierarchy glob filtering

**Goal.** Let callers select signals by pattern so large traces index only what
is needed.

**Files.** New `src/waveform/select.py`; edit `src/waveform/parser.py` only to
add the optional `keep` parameter; new tests in `tests/test_waveform.py`.

**Interface.**

```python
def match_paths(paths: Sequence[str], patterns: Sequence[str]) -> tuple[str, ...]: ...
def parse_vcd(text: str, *, keep: Sequence[str] | None = None) -> ParsedTrace: ...
```

**Requirements.**

- Patterns match whole canonical paths, component by component, split on `.`.
  `*` matches any characters within one component. `**` matches zero or more
  whole components. `?` matches one character. Matching is case-sensitive.
  Examples: `tb.dut.*`, `tb.**.clk`, `tb.dut.w?tr`.
- `match_paths` returns matches in the input order, without duplicates. A
  pattern that matches nothing raises `InvalidQueryError` naming the pattern.
- With `keep`, the parser stores change history only for matching paths. Value
  changes for unselected identifier codes are still validated for syntax but
  not stored. A query for an unselected path raises `SignalNotFoundError`.
- An identifier code shared by a kept path and an unkept alias is kept.
- Filtering must not change any result for a kept signal. Coverage and dump gaps
  are unchanged. The `$dumpon` restate check applies to kept signals only.

**Tests.** Pattern semantics table; filtered versus unfiltered equality for kept
signals on the mock FIFO trace; aliases; unmatched pattern error.

**Verify.** `uv run pytest tests/test_waveform.py -q -k "select or glob or keep"`

## Ticket 3: Parsed-trace cache

**Goal.** Avoid re-parsing an unchanged trace.

**Files.** New `src/waveform/cache.py`; new `tests/test_waveform_cache.py`.

**Interface.**

```python
CACHE_FORMAT_VERSION: Final = 1

def cache_key(trace_bytes: bytes, *, keep: Sequence[str] | None) -> str: ...
def load_cached(path: Path, *, cache_dir: Path,
                keep: Sequence[str] | None = None) -> VcdWaveformProvider: ...
```

**Requirements.**

- The key is the SHA-256 of: the trace file's bytes, `CACHE_FORMAT_VERSION`, the
  sorted `keep` patterns, and for FST input the converter's `--version` output.
  Never key on path or modification time alone.
- Store the normalized trace as JSON (not pickle) under
  `cache_dir/<key>.json`. Timestamps are decimal strings, as in `spec.md`
  section 11.
- Write to a temporary file in the same directory and rename it into place.
- A missing, unreadable, truncated, or wrong-version cache entry is ignored and
  rebuilt. It never raises and never returns stale data.
- A provider loaded from cache must give results identical to a fresh parse,
  including dump gaps, skipped variables, and same-timestamp clock ambiguity.
- The default cache directory is `.waveroot-cache/`, already ignored by git. The
  cache is opt-in: `VcdWaveformProvider.from_path` does not use it.

**Tests.** Hit, miss after content change, miss after `keep` change, corrupted
entry rebuilt, equality with a fresh parse over the mock FIFO and the dump-gap
trace, and a property test that round-trips random traces through the cache.

**Verify.** `uv run pytest tests/test_waveform_cache.py -q`

## Ticket 4: Waveform CLI option definitions

**Goal.** Define the Track A options for Track C to wire into `src/app/cli.py`.
Track A owns the option names and behavior; Track C owns the command.

**Files.** New `src/waveform/options.py`; new `tests/test_waveform_options.py`.
Do not create or edit anything in `src/app/`.

**Interface.**

```python
@dataclass(frozen=True, slots=True)
class WaveformOptions:
    trace: Path
    golden: Path
    keep: tuple[str, ...] = ()
    cache_dir: Path | None = None
    fst_timeout_s: float = 120.0

def add_waveform_arguments(parser: argparse.ArgumentParser) -> None: ...
def options_from_namespace(namespace: argparse.Namespace) -> WaveformOptions: ...
def run_comparison(options: WaveformOptions) -> ComparisonResult: ...
def exit_code(result: ComparisonResult) -> int: ...
```

**Requirements.**

- Arguments: `--trace PATH`, `--golden PATH`, `--keep PATTERN` (repeatable),
  `--cache-dir PATH`, `--fst-timeout SECONDS`.
- `run_comparison` composes tickets 1 to 3 with `compare` and never raises for
  input problems; it returns a `ComparisonResult`.
- `exit_code` follows `spec.md` section 8.4: `pass` 0, `fail` 1,
  `invalid_input` and `error` 2, `unsupported` and `inconclusive` 3.
- No `print`, no `sys.exit`, and no console entry point here.

**Tests.** Argument parsing, defaults, repeatable `--keep`, every status mapped
to its exit code, and `run_comparison` on the mock FIFO trace.

**Verify.** `uv run pytest tests/test_waveform_options.py -q`

## Notes for the contracts coordinator

These came up while building the core. Both are now resolved by the contract
change in PR #8 (`spec.md` section 15.1, decisions 8 to 11), and Track A
implements the rulings:

- **Reset and pending expectations (decision 10).** Cancelled is not waived:
  in both modes, an epoch ended by reset with expectations outstanding is
  `inconclusive`, and a comparison with nothing compared is `inconclusive`
  (`NOTHING_COMPARED`).
- **First record of all-x.** `MockTrace` now rejects an all-x first sample
  except at the coverage start or where dumping resumes.
- **Also decided:** timestamps are bounded by `MAX_TIME_PS` (decision 8), and an
  x/z clock transition after reset makes the comparison `inconclusive`
  (decision 9). Decision 11 (the VCD end-of-trace rule) was proposed pending a
  check of real simulator output; Track A does not implement it yet.

The original notes follow for the record.

1. **Reset and pending expectations.** `spec.md` section 7 says a reset cancels
   an epoch's pending expectations; section 8.2 says a cycle-mode comparison is
   complete only when every listed cycle was observed. The engine follows 8.2 in
   cycle mode (an epoch cut short is `inconclusive`, code `EPOCH_ENDED_EARLY`)
   and section 7 in handshake mode (cancelled, with an
   `EXPECTATIONS_CANCELLED_BY_RESET` diagnostic on the result). Cancelling in
   handshake mode can hide a word dropped just before a reset; the spec should
   say which is intended.
2. **First record of all-x.** A signal is all-x before its first record, so the
   real provider does not report an explicit all-x first record as a change.
   `MockTrace` permits such a first sample, and a mock window starting before it
   would then fail `WaveformWindow` validation. The mock fixture is unaffected
   because its x samples sit at the trace start.
