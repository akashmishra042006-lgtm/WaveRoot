"""Local HTTP API over the Track C analysis service (C06).

Standard library only. One process, bound to loopback, no authentication: the
guards below stop *other web pages and other hosts* from driving it, not other
programs on the same machine.

* Binding: only a loopback IPv4 address is accepted.
* ``Host`` must be this server's own loopback name and port (DNS-rebinding
  guard). ``Origin``, when sent, must be this server or an allowed origin;
  creating or cancelling a job *requires* an allowed ``Origin``. Programs that
  send no ``Origin`` may read, never write.
* Clients name a benchmark by id; they never supply paths. Cases are
  discovered under one configured root, and anything resolving outside it is
  ignored. Fixture mode and causal fixtures are CLI-only.
* Request bodies are small JSON objects with exactly the documented fields.
* Source text is served only for ids a finished job's report declares, only
  from inside that case's directory, and only when its SHA-256 equals the
  report's.
* Responses are never cached, and errors never carry tracebacks or server
  paths.

Routes::

    GET  /api/health
    GET  /api/cases
    POST /api/jobs                       {case_id, variant, schedule, comparison?}
    GET  /api/jobs/{id}                  state of one job
    POST /api/jobs/{id}/cancel
    GET  /api/jobs/{id}/result           the run record (once finished or cancelled)
    GET  /api/jobs/{id}/sources/{source} verified source text
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Protocol, cast

from src.app.adapters import SelfCheckComparison, WaveformComparison
from src.app.runner import Limits, PathEscapeError, Runner, resolve_inside
from src.app.service import AnalysisOutcome, AnalysisService, ManifestError, load_manifest

MAX_BODY_BYTES: Final = 4096
MAX_SOURCE_BYTES: Final = 2_000_000
DEFAULT_DEV_ORIGINS: Final = (
    "http://localhost:5173", "http://127.0.0.1:5173",  # vite dev
    "http://localhost:4173", "http://127.0.0.1:4173",  # vite preview
)

_NAME: Final = re.compile(r"^[a-z0-9_]{1,64}$")
_JOB_ROUTE: Final = re.compile(
    r"^/api/jobs/(?P<id>[0-9a-f]{32})(?:/(?P<action>cancel|result)|/sources/(?P<source>[A-Za-z0-9_.:\-]{1,64}))?$"
)
_ACTIVE: Final = frozenset({"queued", "running", "cancelling"})


class _Service(Protocol):
    def analyze(
        self, manifest_path: Path, *, variant: str, schedule: str,
        simulator: str | None = None, cancel: threading.Event | None = None,
    ) -> AnalysisOutcome: ...


ServiceFactory = Callable[[str], _Service]


@dataclass(frozen=True)
class ApiConfig:
    root: Path
    artifacts: Path
    host: str = "127.0.0.1"
    port: int = 0
    extra_origins: tuple[str, ...] = DEFAULT_DEV_ORIGINS
    timeout_s: float = 180.0
    max_concurrent: int = 1
    max_jobs: int = 64


@dataclass
class _Job:
    id: str
    case_id: str
    case_dir: Path
    manifest: Path
    variant: str
    schedule: str
    comparison: str
    created_at: str
    state: str = "queued"
    record: dict[str, Any] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "state": self.state, "case_id": self.case_id, "variant": self.variant,
            "schedule": self.schedule, "comparison": self.comparison, "created_at": self.created_at,
        }
        if self.state == "failed":
            out["message"] = "the job failed internally; see the server log"
        return out


class _Refusal(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _check_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None or address.version != 4 or not address.is_loopback:
        raise ValueError(f"the API binds only to a loopback IPv4 address such as 127.0.0.1, not {host!r}")


class ApiServer:
    def __init__(self, config: ApiConfig, service_factory: ServiceFactory | None = None) -> None:
        _check_loopback(config.host)
        self.config = config
        self._root = config.root.resolve(strict=True)
        self._factory: ServiceFactory = service_factory or self._default_factory
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=config.max_concurrent, thread_name_prefix="waveroot-job")
        api = self

        class Handler(_Handler):
            server_api = api

        self._http = ThreadingHTTPServer((config.host, config.port), Handler)
        self._http.daemon_threads = True
        self._thread: threading.Thread | None = None
        self.address: tuple[str, int] = (str(self._http.server_address[0]), int(self._http.server_address[1]))

    # -- lifecycle ------------------------------------------------------

    @property
    def port(self) -> int:
        return self.address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True, name="waveroot-api"
        )
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._http.shutdown()
        self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def serve_forever(self) -> None:
        """Serve on the calling thread until interrupted, then cancel running jobs."""
        try:
            self._http.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            with self._lock:
                for job in self._jobs.values():
                    job.cancel.set()
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._http.server_close()

    # -- request guards -------------------------------------------------

    def allowed_origins(self) -> frozenset[str]:
        own = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        return frozenset(own | set(self.config.extra_origins))

    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    # -- cases ----------------------------------------------------------

    def discover_cases(self) -> dict[str, tuple[Path, Any]]:
        """case id -> (manifest path, parsed manifest); only real directories
        that resolve inside the configured root."""
        found: dict[str, tuple[Path, Any]] = {}
        for entry in sorted(self._root.iterdir()):
            try:
                directory = entry.resolve(strict=True)
            except OSError:
                continue
            if not directory.is_dir() or not directory.is_relative_to(self._root):
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = load_manifest(manifest_path)
            except ManifestError:
                continue
            found[manifest.case_id] = (manifest_path, manifest)
        return found

    def list_cases(self) -> list[dict[str, Any]]:
        return [
            {"case_id": cid, "variants": sorted(m.rtl), "schedules": sorted(m.schedules),
             "simulator": m.simulator, "modes": [mode.value for mode in m.modes]}
            for cid, (_p, m) in self.discover_cases().items()
        ]

    # -- jobs -----------------------------------------------------------

    def create_job(self, body: object) -> _Job:
        if not isinstance(body, dict):
            raise _Refusal(400, "BAD_REQUEST", "the body must be a JSON object")
        allowed = {"case_id", "variant", "schedule", "comparison"}
        unknown = set(body) - allowed
        if unknown:
            raise _Refusal(400, "BAD_REQUEST", f"unknown field(s): {', '.join(sorted(map(str, unknown)))}")
        for required in ("case_id", "variant", "schedule"):
            if not isinstance(body.get(required), str):
                raise _Refusal(400, "BAD_REQUEST", f"{required} must be a string")
        case_id, variant, schedule = str(body["case_id"]), str(body["variant"]), str(body["schedule"])
        comparison = body.get("comparison", "track-a")
        if comparison not in ("track-a", "self-check"):
            raise _Refusal(400, "BAD_REQUEST", "comparison must be track-a or self-check")
        if not _NAME.match(case_id):
            raise _Refusal(400, "BAD_REQUEST", "case_id is not a valid benchmark id")
        if variant not in ("correct", "mutant"):
            raise _Refusal(400, "BAD_REQUEST", "variant must be correct or mutant")
        if not _NAME.match(schedule):
            raise _Refusal(400, "BAD_REQUEST", "schedule is not a valid schedule name")
        cases = self.discover_cases()
        if case_id not in cases:
            raise _Refusal(404, "UNKNOWN_CASE", f"no benchmark {case_id!r}")
        manifest_path, manifest = cases[case_id]
        if schedule not in manifest.schedules:
            raise _Refusal(400, "BAD_REQUEST", f"{case_id} has no schedule {schedule!r}")
        job = _Job(
            id=uuid.uuid4().hex, case_id=case_id, case_dir=manifest_path.parent, manifest=manifest_path,
            variant=variant, schedule=schedule, comparison=str(comparison),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._evict_locked()
            if sum(1 for j in self._jobs.values() if j.state in _ACTIVE) >= self.config.max_jobs:
                raise _Refusal(503, "BUSY", "too many active jobs")
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def _evict_locked(self) -> None:
        while len(self._jobs) >= self.config.max_jobs:
            done = [j for j in self._jobs.values() if j.state not in _ACTIVE]
            if not done:
                return
            del self._jobs[done[0].id]  # dicts keep insertion order: oldest first

    def get_job(self, job_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise _Refusal(404, "UNKNOWN_JOB", "no such job")
        return job

    def cancel_job(self, job_id: str) -> _Job:
        job = self.get_job(job_id)
        with self._lock:
            if job.state == "queued":
                job.state = "cancelled"
                job.cancel.set()
            elif job.state in ("running", "cancelling"):
                job.state = "cancelling"
                job.cancel.set()
            else:
                raise _Refusal(409, "NOT_CANCELLABLE", f"the job is already {job.state}")
        return job

    def _run(self, job: _Job) -> None:
        with self._lock:
            if job.cancel.is_set():
                return
            job.state = "running"
        try:
            outcome = self._factory(job.comparison).analyze(
                job.manifest, variant=job.variant, schedule=job.schedule, cancel=job.cancel
            )
            record = self._scrub(outcome.to_json())
            cancelled = job.cancel.is_set() and any(d.code == "CANCELLED" for d in outcome.diagnostics)
            with self._lock:
                job.record = record
                job.state = "cancelled" if cancelled else "finished"
        except Exception:  # noqa: BLE001 - reported as a generic failure, never leaked
            with self._lock:
                job.state = "failed"

    def _scrub(self, record: dict[str, Any]) -> dict[str, Any]:
        """Remove server filesystem paths from a run record."""
        run = record.get("run")
        if isinstance(run, dict) and run.get("run_dir"):
            run["run_dir"] = Path(str(run["run_dir"])).name
        prefixes = [str(self.config.artifacts.resolve()), str(self._root), str(self.config.artifacts)]

        def clean(value: Any) -> Any:
            if isinstance(value, str):
                for prefix in prefixes:
                    value = value.replace(prefix + "/", "")
                return value
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value

        return cast(dict[str, Any], clean(record))

    def source_text(self, job: _Job, source_id: str) -> bytes:
        record = job.record
        causal = record.get("causal") if record else None
        declared = causal.get("sources") if isinstance(causal, dict) else None
        entry = next(
            (s for s in declared or [] if isinstance(s, dict) and s.get("id") == source_id), None
        )
        if entry is None:
            raise _Refusal(404, "UNKNOWN_SOURCE", "the report declares no such source")
        try:
            path = resolve_inside(job.case_dir, Path(str(entry["path"])))
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise PathEscapeError("too large")
            data = path.read_bytes()
        except (PathEscapeError, OSError):
            raise _Refusal(404, "SOURCE_UNAVAILABLE", "the source file is not available") from None
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise _Refusal(409, "SOURCE_HASH_MISMATCH", "the file no longer matches the report's SHA-256")
        return data

    def _default_factory(self, comparison: str) -> _Service:
        adapter = WaveformComparison() if comparison == "track-a" else SelfCheckComparison()
        runner = Runner(self.config.artifacts, Limits(wall_clock_s=self.config.timeout_s))
        return AnalysisService(runner, adapter)


class _Handler(BaseHTTPRequestHandler):
    server_api: ApiServer
    server_version = "WaveRootAPI"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by default
        return

    # -- plumbing -------------------------------------------------------

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _send(self, status: int, body: bytes, content_type: str, origin: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: object, origin: str | None) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json", origin)

    def _read_json(self) -> object:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            raise _Refusal(400, "BAD_REQUEST", "invalid Content-Length") from None
        if length < 0:
            raise _Refusal(400, "BAD_REQUEST", "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise _Refusal(413, "BODY_TOO_LARGE", f"the body is limited to {MAX_BODY_BYTES} bytes")
        if length == 0:
            return {}
        media = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media != "application/json":
            raise _Refusal(415, "UNSUPPORTED_MEDIA_TYPE", "send application/json")
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise _Refusal(400, "BAD_REQUEST", "the body is not valid JSON") from None

    def _guard(self, method: str) -> str | None:
        """Return the origin to echo in CORS headers, or raise a refusal."""
        api = self.server_api
        host = self.headers.get("Host", "")
        if host not in api.allowed_hosts():
            raise _Refusal(403, "FORBIDDEN_HOST", "unexpected Host header")
        origin = self._origin()
        if origin is not None and origin not in api.allowed_origins():
            raise _Refusal(403, "FORBIDDEN_ORIGIN", "this origin may not use the API")
        if method in ("POST", "OPTIONS") and origin is None:
            raise _Refusal(403, "FORBIDDEN_ORIGIN", "an allowed Origin header is required")
        return origin

    def _handle(self, method: str) -> None:
        origin: str | None = None
        try:
            origin = self._guard(method)
            if method == "OPTIONS":
                self._preflight(origin)
                return
            self._route(method, self.path.split("?", 1)[0], origin)
        except _Refusal as refusal:
            self._json(refusal.status, {"error": {"code": refusal.code, "message": refusal.message}},
                       origin if refusal.status != 403 else None)
        except Exception:  # noqa: BLE001 - never leak internals
            self._json(500, {"error": {"code": "INTERNAL", "message": "internal error"}}, origin)

    def _preflight(self, origin: str | None) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", str(origin))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self, method: str, path: str, origin: str | None) -> None:
        api = self.server_api
        if method == "GET" and path == "/api/health":
            self._json(200, {"ok": True}, origin)
        elif method == "GET" and path == "/api/cases":
            self._json(200, {"cases": api.list_cases()}, origin)
        elif method == "POST" and path == "/api/jobs":
            job = api.create_job(self._read_json())
            self._json(202, job.public(), origin)
        else:
            match = _JOB_ROUTE.match(path)
            if match is None:
                raise _Refusal(404, "NOT_FOUND", "no such route")
            job_id, action, source = match.group("id"), match.group("action"), match.group("source")
            if method == "GET" and action is None and source is None:
                self._json(200, api.get_job(job_id).public(), origin)
            elif method == "POST" and action == "cancel":
                self._read_json()
                self._json(202, api.cancel_job(job_id).public(), origin)
            elif method == "GET" and action == "result":
                job = api.get_job(job_id)
                if job.record is None:
                    raise _Refusal(409, "NOT_READY", f"the job is {job.state}; there is no result")
                self._json(200, job.record, origin)
            elif method == "GET" and source is not None:
                data = api.source_text(api.get_job(job_id), source)
                self._send(200, data, "text/plain; charset=utf-8", origin)
            else:
                raise _Refusal(405, "METHOD_NOT_ALLOWED", "method not allowed")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")
