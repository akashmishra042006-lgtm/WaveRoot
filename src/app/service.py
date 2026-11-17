"""Track C application service (ticket C03).

One entry point, ``AnalysisService.analyze``, used by the CLI now and by the
local API later. It loads a benchmark manifest, generates goldens with the
case's independent model, runs the simulator through the hardened ``Runner``,
compares through an injected adapter, and optionally attaches a causal report
from an injected adapter. It writes one run record (``report.json``) per run.

It never substitutes sample data for a failed real step: a build error, a
timeout, a malformed trace, or a missing causal engine is reported as such.
"""

from __future__ import annotations

import importlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast

from src.app.adapters import AdapterError, CausalAdapter, ComparisonAdapter
from src.app.runner import (
    PathEscapeError,
    RunRecord,
    Runner,
    RunStatus,
    SimRequest,
    Simulator,
    resolve_inside,
)
from src.app.selfcheck import ObservedFormatError
from src.app.tracepaths import rebase_golden
from src.contracts import (
    AnalysisStatus,
    CausalReport,
    ComparisonMode,
    ComparisonResult,
    Diagnostic,
    GoldenTrace,
)

RECORD_NAME: Final = "waveroot-track-c-run"
RECORD_VERSION: Final = 1
Variant = Literal["correct", "mutant"]

# spec.md section 8.4
EXIT_CODES: Final[Mapping[AnalysisStatus, int]] = {
    AnalysisStatus.PASS: 0,
    AnalysisStatus.FAIL: 1,
    AnalysisStatus.INVALID_INPUT: 2,
    AnalysisStatus.ERROR: 2,
    AnalysisStatus.UNSUPPORTED: 3,
    AnalysisStatus.INCONCLUSIVE: 3,
}

# Precedence when combining per-mode results that did not fail.
_SEVERITY: Final[Mapping[AnalysisStatus, int]] = {
    AnalysisStatus.PASS: 0,
    AnalysisStatus.INCONCLUSIVE: 1,
    AnalysisStatus.UNSUPPORTED: 2,
    AnalysisStatus.INVALID_INPUT: 3,
    AnalysisStatus.ERROR: 4,
}

_RUN_STATUS_MAP: Final[Mapping[RunStatus, AnalysisStatus]] = {
    RunStatus.INVALID_REQUEST: AnalysisStatus.INVALID_INPUT,
    RunStatus.TOOL_MISSING: AnalysisStatus.ERROR,
    RunStatus.BUILD_FAILED: AnalysisStatus.ERROR,
    RunStatus.RUN_FAILED: AnalysisStatus.ERROR,
    RunStatus.TIMEOUT: AnalysisStatus.ERROR,
    RunStatus.ARTIFACT_LIMIT: AnalysisStatus.ERROR,
    RunStatus.CANCELLED: AnalysisStatus.ERROR,
    RunStatus.TRACE_MISSING: AnalysisStatus.ERROR,
}

_GOLDEN_MODULE: Final = re.compile(r"^benchmarks\.case_[a-z0-9_]+\.golden_model$")
_NAME: Final = re.compile(r"^[a-z0-9_]{1,64}$")


class ManifestError(ValueError):
    pass


class FixtureModeError(ValueError):
    """A synthetic-fixture adapter was supplied without explicit fixture mode."""


@dataclass(frozen=True)
class CaseManifest:
    case_id: str
    path: Path
    simulator: Simulator
    testbench: Path
    rtl: Mapping[Variant, tuple[Path, ...]]
    schedules: Mapping[str, Path]
    golden_module: str
    modes: tuple[ComparisonMode, ...]
    primary_mode: ComparisonMode

    @property
    def directory(self) -> Path:
        return self.path.parent


def _str(obj: Mapping[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}.{key} must be a non-empty string")
    return value


def _obj(obj: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise ManifestError(f"{where}.{key} must be an object")
    return cast(Mapping[str, object], value)


def _paths(obj: Mapping[str, object], key: str, where: str) -> tuple[Path, ...]:
    value = obj.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where}.{key} must be a non-empty list of paths")
    return tuple(Path(v) for v in cast(list[str], value))


def load_manifest(path: Path) -> CaseManifest:
    """Parse and validate a benchmark manifest. Paths stay relative to the
    manifest's directory; the runner resolves them against that root."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: cannot load manifest ({exc})") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: manifest must be an object")
    data = cast(Mapping[str, object], raw)
    if data.get("schema_version") != 1:
        raise ManifestError(f"{path}: unsupported manifest schema_version {data.get('schema_version')!r}")
    case_id = _str(data, "case_id", "manifest")
    simulator = _str(data, "simulator", "manifest")
    if simulator not in ("verilator", "icarus"):
        raise ManifestError(f"manifest.simulator {simulator!r} is not supported")
    analysis = _obj(data, "analysis_inputs", "manifest")
    control = _obj(data, "control_inputs", "manifest")
    testbench = _paths(analysis, "testbench", "analysis_inputs")
    if len(testbench) != 1:
        raise ManifestError("analysis_inputs.testbench must name exactly one file")
    schedules_raw = _obj(data, "schedules", "manifest")
    schedules: dict[str, Path] = {}
    for name, entry in schedules_raw.items():
        if not _NAME.fullmatch(name) or not isinstance(entry, dict):
            raise ManifestError(f"schedule {name!r} is malformed")
        schedules[name] = Path(_str(cast(Mapping[str, object], entry), "stim", f"schedules.{name}"))
    golden = _obj(data, "golden", "manifest")
    module = _str(golden, "module", "golden")
    if not _GOLDEN_MODULE.fullmatch(module):
        raise ManifestError(f"golden.module {module!r} is not a benchmark golden model")
    modes_raw = golden.get("modes")
    if not isinstance(modes_raw, list) or not modes_raw:
        raise ManifestError("golden.modes must be a non-empty list")
    try:
        modes = tuple(ComparisonMode(m) for m in modes_raw)
        primary = ComparisonMode(_str(golden, "primary_mode", "golden"))
    except ValueError as exc:
        raise ManifestError(f"golden mode: {exc}") from exc
    if primary not in modes:
        raise ManifestError("golden.primary_mode must be one of golden.modes")
    return CaseManifest(
        case_id=case_id,
        path=path,
        simulator=cast(Simulator, simulator),
        testbench=testbench[0],
        rtl={"mutant": _paths(analysis, "rtl", "analysis_inputs"), "correct": _paths(control, "rtl", "control_inputs")},
        schedules=schedules,
        golden_module=module,
        modes=modes,
        primary_mode=primary,
    )


@dataclass
class AnalysisOutcome:
    status: AnalysisStatus
    case_id: str | None
    variant: str
    schedule: str
    fixture_mode: bool
    comparison_adapter: tuple[str, str]
    causal_adapter: tuple[str, str] | None
    primary_mode: ComparisonMode | None = None
    comparisons: dict[ComparisonMode, ComparisonResult] = field(default_factory=dict)
    causal: CausalReport | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    run: RunRecord | None = None
    report_path: Path | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def primary(self) -> ComparisonResult | None:
        if self.primary_mode is None:
            return None
        return self.comparisons.get(self.primary_mode)

    def to_json(self) -> dict[str, object]:
        return {
            "record": RECORD_NAME,
            "record_version": RECORD_VERSION,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "case_id": self.case_id,
            "variant": self.variant,
            "schedule": self.schedule,
            "fixture_mode": self.fixture_mode,
            "comparison_adapter": {"name": self.comparison_adapter[0], "kind": self.comparison_adapter[1]},
            "causal_adapter": (
                {"name": self.causal_adapter[0], "kind": self.causal_adapter[1]}
                if self.causal_adapter
                else None
            ),
            "primary_mode": self.primary_mode.value if self.primary_mode else None,
            "comparisons": {
                mode.value: json.loads(result.model_dump_json())
                for mode, result in self.comparisons.items()
            },
            "causal": json.loads(self.causal.model_dump_json()) if self.causal else None,
            "diagnostics": [json.loads(d.model_dump_json()) for d in self.diagnostics],
            "run": self.run.to_json() if self.run else None,
        }


class AnalysisService:
    def __init__(
        self,
        runner: Runner,
        comparison: ComparisonAdapter,
        causal: CausalAdapter | None = None,
        *,
        fixture_mode: bool = False,
    ) -> None:
        adapters: Sequence[ComparisonAdapter | CausalAdapter] = [comparison, *([causal] if causal else [])]
        uses_fixture = any(a.kind == "synthetic_fixture" for a in adapters)
        if uses_fixture and not fixture_mode:
            raise FixtureModeError(
                "a synthetic_fixture adapter requires explicit fixture mode; "
                "fixture data is never used as a fallback"
            )
        self.runner = runner
        self.comparison = comparison
        self.causal = causal
        self.fixture_mode = fixture_mode

    def analyze(
        self,
        manifest_path: Path,
        *,
        variant: str,
        schedule: str,
        simulator: str | None = None,
        cancel: threading.Event | None = None,
    ) -> AnalysisOutcome:
        outcome = AnalysisOutcome(
            status=AnalysisStatus.ERROR,
            case_id=None,
            variant=variant,
            schedule=schedule,
            fixture_mode=self.fixture_mode,
            comparison_adapter=(self.comparison.name, self.comparison.kind),
            causal_adapter=(self.causal.name, self.causal.kind) if self.causal else None,
        )

        def stop(status: AnalysisStatus, code: str, message: str) -> AnalysisOutcome:
            outcome.status = status
            outcome.diagnostics.append(Diagnostic(code=code, message=message))
            return self._finish(outcome)

        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            return stop(AnalysisStatus.INVALID_INPUT, "MANIFEST_INVALID", str(exc))
        outcome.case_id = manifest.case_id
        outcome.primary_mode = manifest.primary_mode
        if variant not in ("correct", "mutant"):
            return stop(AnalysisStatus.INVALID_INPUT, "VARIANT_UNKNOWN", f"variant {variant!r} is not correct or mutant")
        if schedule not in manifest.schedules:
            return stop(AnalysisStatus.INVALID_INPUT, "SCHEDULE_UNKNOWN", f"schedule {schedule!r} is not in the manifest")
        if simulator is not None and simulator != manifest.simulator:
            return stop(
                AnalysisStatus.UNSUPPORTED,
                "SIMULATOR_UNSUPPORTED",
                f"{manifest.case_id} is validated only on {manifest.simulator}; "
                f"{simulator} is not substituted (its semantics may differ)",
            )
        try:
            stim = resolve_inside(manifest.directory, manifest.schedules[schedule])
        except PathEscapeError as exc:
            return stop(AnalysisStatus.INVALID_INPUT, "PATH_ESCAPE", str(exc))

        try:
            goldens = self._goldens(manifest, stim)
        except (ValueError, ImportError, TypeError) as exc:
            return stop(AnalysisStatus.INVALID_INPUT, "GOLDEN_INVALID", f"{type(exc).__name__}: {exc}")

        request = SimRequest(
            simulator=manifest.simulator,
            allowed_root=manifest.directory,
            sources=(manifest.testbench, *manifest.rtl[cast(Variant, variant)]),
            stimulus=manifest.schedules[schedule],
        )
        run = self.runner.run(request, label=f"{manifest.case_id}-{variant}-{schedule}", cancel=cancel)
        outcome.run = run
        if run.status is not RunStatus.OK:
            return stop(_RUN_STATUS_MAP[run.status], run.status.name, run.message)
        # Map canonical golden paths onto this trace's hierarchy (spec 3.1) so
        # every anchor resolves in the trace it came from (spec 5.1).
        goldens = {mode: rebase_golden(g, run.trace_scope_prefix or "") for mode, g in goldens.items()}
        if run.run_dir is not None:
            for mode, golden in goldens.items():
                (run.run_dir / f"golden_{mode.value}.jsonl").write_text(golden.to_jsonl())

        for mode, golden in goldens.items():
            try:
                outcome.comparisons[mode] = self.comparison.compare(golden, run)
            except ObservedFormatError as exc:
                outcome.comparisons.clear()
                return stop(AnalysisStatus.INVALID_INPUT, "TRACE_MALFORMED", str(exc))
            except AdapterError as exc:
                outcome.comparisons.clear()
                return stop(AnalysisStatus.ERROR, "COMPARISON_FAILED", str(exc))

        outcome.status = self._combine(outcome)
        primary_failure = self._first_failure(outcome)
        if primary_failure is not None:
            outcome.primary_mode = primary_failure
            failure = outcome.comparisons[primary_failure].failure
            assert failure is not None
            if self.causal is None:
                outcome.diagnostics.append(
                    Diagnostic(
                        code="CAUSAL_UNAVAILABLE",
                        message="no causal-analysis adapter is configured; the failure is unlocalized",
                    )
                )
            else:
                try:
                    sources = [manifest.directory / p for p in manifest.rtl[cast(Variant, variant)]]
                    outcome.causal = self.causal.analyze(failure, run, sources)
                except AdapterError as exc:
                    outcome.diagnostics.append(Diagnostic(code="CAUSAL_FAILED", message=str(exc)))
        return self._finish(outcome)

    # -- helpers --------------------------------------------------------

    def _goldens(self, manifest: CaseManifest, stim: Path) -> dict[ComparisonMode, GoldenTrace]:
        module = importlib.import_module(manifest.golden_module)
        produce = getattr(module, "goldens_for", None)
        if not callable(produce):
            raise TypeError(f"{manifest.golden_module} has no goldens_for()")
        produced = produce(stim)
        if not isinstance(produced, tuple) or len(produced) != 2:
            raise TypeError("goldens_for() must return (cycle, handshake)")
        cycle, handshake = produced
        by_mode: dict[ComparisonMode, object] = {ComparisonMode.CYCLE: cycle, ComparisonMode.HANDSHAKE: handshake}
        out: dict[ComparisonMode, GoldenTrace] = {}
        for mode in manifest.modes:
            trace = by_mode[mode]
            if not isinstance(trace, GoldenTrace) or trace.header.mode is not mode:
                raise TypeError(f"golden model produced no {mode.value}-mode trace")
            out[mode] = trace
        return out

    @staticmethod
    def _combine(outcome: AnalysisOutcome) -> AnalysisStatus:
        statuses = [r.status for r in outcome.comparisons.values()]
        if AnalysisStatus.FAIL in statuses:
            return AnalysisStatus.FAIL
        return max(statuses, key=lambda s: _SEVERITY[s])

    @staticmethod
    def _first_failure(outcome: AnalysisOutcome) -> ComparisonMode | None:
        """Mode whose failure is earliest in trace order; ties go to the
        manifest's primary mode (cycle) so the documented anchor is stable."""
        failing = [
            (r.failure.observation.time_ps, mode is not outcome.primary_mode, mode)
            for mode, r in outcome.comparisons.items()
            if r.failure is not None
        ]
        return min(failing)[2] if failing else None

    @staticmethod
    def _finish(outcome: AnalysisOutcome) -> AnalysisOutcome:
        if outcome.run is not None and outcome.run.run_dir is not None:
            path = outcome.run.run_dir / "report.json"
            path.write_text(json.dumps(outcome.to_json(), indent=2) + "\n")
            outcome.report_path = path
        return outcome


def load_report(path: Path) -> dict[str, object]:
    """Load a saved run record and re-validate every contract object in it.

    Raises ``ValueError`` on a schema mismatch or malformed content; nothing
    is rendered from an unvalidated record.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("run record must be an object")
    data = cast(dict[str, object], raw)
    if data.get("record") != RECORD_NAME or data.get("record_version") != RECORD_VERSION:
        raise ValueError(
            f"unsupported run record {data.get('record')!r} version {data.get('record_version')!r}"
        )
    status = AnalysisStatus(str(data.get("status")))
    comparisons = data.get("comparisons")
    if not isinstance(comparisons, dict):
        raise ValueError("comparisons must be an object")
    for mode, result in comparisons.items():
        ComparisonMode(mode)
        ComparisonResult.model_validate_json(json.dumps(result))
    causal = data.get("causal")
    if causal is not None:
        CausalReport.model_validate_json(json.dumps(causal))
    if data.get("exit_code") != EXIT_CODES[status]:
        raise ValueError("exit_code does not match status")
    return data
