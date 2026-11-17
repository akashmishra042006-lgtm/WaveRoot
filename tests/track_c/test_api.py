"""C06: the local API (src/app/api.py).

Security and lifecycle tests drive the real HTTP server with a fake analysis
service, so they need no simulator. Integration tests run a real job and
cancel a real simulator build.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from src.app.api import ApiConfig, ApiServer
from src.app.service import AnalysisOutcome
from src.contracts import AnalysisStatus, Diagnostic, load_mock_fixture

REPO = Path(__file__).resolve().parent.parent.parent
FIFO_DIR = REPO / "benchmarks" / "case_01_fifo_wrap"
MOCK_FIXTURE = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"
MOCK_SOURCE = REPO / "tests" / "fixtures" / "mock_fifo_wrap.sv"
ALLOWED = "http://localhost:5173"


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


class FakeService:
    """Stands in for AnalysisService: records calls and can block until
    cancelled, so lifecycle behavior is tested without a simulator."""

    def __init__(self, *, block: bool = False, causal: bool = False) -> None:
        self.block = block
        self.causal = causal
        self.started = threading.Event()
        self.calls: list[tuple[str, str]] = []

    def analyze(
        self, manifest_path: Path, *, variant: str, schedule: str,
        simulator: str | None = None, cancel: threading.Event | None = None,
    ) -> AnalysisOutcome:
        self.calls.append((variant, schedule))
        self.started.set()
        cancelled = False
        if self.block and cancel is not None:
            cancelled = cancel.wait(timeout=20)
        outcome = AnalysisOutcome(
            status=AnalysisStatus.ERROR if cancelled else AnalysisStatus.PASS,
            case_id="case_01_fifo_wrap", variant=variant, schedule=schedule, fixture_mode=False,
            comparison_adapter=("fake", "analysis"), causal_adapter=None,
        )
        if cancelled:
            outcome.diagnostics.append(Diagnostic(code="CANCELLED", message="cancelled"))
        if self.causal:
            outcome.causal = load_mock_fixture(MOCK_FIXTURE).report
        return outcome


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "benchmarks"
    shutil.copytree(FIFO_DIR, base / "case_01_fifo_wrap")
    return base


def make_server(root: Path, tmp_path: Path, service: FakeService | None, **kw: Any) -> ApiServer:
    config = ApiConfig(root=root, artifacts=tmp_path / "artifacts", extra_origins=(ALLOWED,), **kw)
    factory: Callable[[str], Any] | None = (lambda _comparison: service) if service is not None else None
    return ApiServer(config, service_factory=factory)


@pytest.fixture
def fake() -> FakeService:
    return FakeService()


@pytest.fixture
def server(root: Path, tmp_path: Path, fake: FakeService) -> Iterator[ApiServer]:
    srv = make_server(root, tmp_path, fake)
    srv.start()
    yield srv
    srv.stop()


def call(
    srv: ApiServer, method: str, path: str, body: object | bytes | None = None, *,
    origin: str | None = ALLOWED, host: str | None = None, content_type: str | None = "application/json",
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers: dict[str, str] = {}
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    payload: bytes | None = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        if content_type is not None:
            headers["Content-Type"] = content_type
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
    conn.close()
    return out


def jcall(srv: ApiServer, method: str, path: str, body: object | None = None, **kw: Any) -> tuple[int, Any]:
    status, _h, data = call(srv, method, path, body, **kw)
    return status, json.loads(data) if data else None


JOB = {"case_id": "case_01_fifo_wrap", "variant": "mutant", "schedule": "directed"}


def create(srv: ApiServer, **override: object) -> str:
    status, body = jcall(srv, "POST", "/api/jobs", {**JOB, **override})
    assert status == 202, body
    return str(body["id"])


def wait_state(srv: ApiServer, job: str, states: set[str], timeout: float = 20) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status, body = jcall(srv, "GET", f"/api/jobs/{job}")
        assert status == 200
        if body["state"] in states:
            return dict(body)
        time.sleep(0.02)
    raise AssertionError(f"job {job} never reached {states}; last {body}")


# ----------------------------------------------------------------------
# Binding and request guards
# ----------------------------------------------------------------------


def test_refuses_to_bind_a_non_loopback_address(root: Path, tmp_path: Path) -> None:
    for host in ("0.0.0.0", "::", "192.168.1.5", "example.com"):
        with pytest.raises(ValueError, match="loopback"):
            ApiServer(ApiConfig(root=root, artifacts=tmp_path / "a", host=host))


def test_server_listens_on_loopback_only(server: ApiServer) -> None:
    assert server.address[0] == "127.0.0.1"


def test_health_and_case_list(server: ApiServer) -> None:
    assert jcall(server, "GET", "/api/health")[1] == {"ok": True}
    status, body = jcall(server, "GET", "/api/cases")
    assert status == 200
    (case,) = body["cases"]
    assert case["case_id"] == "case_01_fifo_wrap"
    assert set(case["variants"]) == {"correct", "mutant"}
    assert "directed" in case["schedules"]
    assert "path" not in json.dumps(body).lower()  # no server paths leak


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:9999", "null", "http://127.0.0.1.evil.example"])
def test_foreign_origin_is_refused_even_for_reads(server: ApiServer, origin: str) -> None:
    status, headers, _ = call(server, "GET", "/api/cases", origin=origin)
    assert status == 403
    assert "access-control-allow-origin" not in headers


def test_creating_a_job_requires_an_allowed_origin(server: ApiServer, fake: FakeService) -> None:
    assert call(server, "POST", "/api/jobs", JOB, origin=None)[0] == 403
    assert call(server, "POST", "/api/jobs", JOB, origin="https://evil.example")[0] == 403
    assert fake.calls == []


def test_the_servers_own_origin_is_allowed(server: ApiServer) -> None:
    assert call(server, "GET", "/api/cases", origin=server.origin)[0] == 200


def test_reads_without_an_origin_are_allowed(server: ApiServer) -> None:
    assert call(server, "GET", "/api/health", origin=None)[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1:1", "localhost:1"])
def test_unexpected_host_header_is_refused(server: ApiServer, host: str) -> None:
    """DNS-rebinding guard: a name that merely resolves to 127.0.0.1 is refused."""
    assert call(server, "GET", "/api/health", origin=None, host=host)[0] == 403


def test_preflight_answers_only_allowed_origins(server: ApiServer) -> None:
    ok = http.client.HTTPConnection("127.0.0.1", server.port)
    ok.request("OPTIONS", "/api/jobs", headers={
        "Origin": ALLOWED, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    resp = ok.getresponse()
    resp.read()
    assert resp.status == 204
    assert resp.getheader("Access-Control-Allow-Origin") == ALLOWED
    assert "POST" in (resp.getheader("Access-Control-Allow-Methods") or "")
    bad = http.client.HTTPConnection("127.0.0.1", server.port)
    bad.request("OPTIONS", "/api/jobs", headers={"Origin": "https://evil.example",
                                                 "Access-Control-Request-Method": "POST"})
    resp = bad.getresponse()
    resp.read()
    assert resp.status == 403
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_responses_are_not_cacheable_or_sniffable(server: ApiServer) -> None:
    _s, headers, _ = call(server, "GET", "/api/cases")
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-type"].startswith("application/json")


# ----------------------------------------------------------------------
# Job creation validation
# ----------------------------------------------------------------------


def test_job_body_must_be_json_of_the_right_shape(server: ApiServer, fake: FakeService) -> None:
    assert call(server, "POST", "/api/jobs", JOB, content_type="text/plain")[0] == 415
    assert call(server, "POST", "/api/jobs", JOB, content_type=None)[0] == 415
    assert call(server, "POST", "/api/jobs", b"{not json")[0] == 400
    assert call(server, "POST", "/api/jobs", b"[]")[0] == 400
    assert call(server, "POST", "/api/jobs", b"x" * 5000)[0] == 413
    assert fake.calls == []


@pytest.mark.parametrize("bad", [
    {"variant": "mutant", "schedule": "directed"},                                  # missing case
    {**JOB, "extra": 1},                                                              # unknown field
    {**JOB, "variant": "other"},
    {**JOB, "schedule": "../../etc/passwd"},
    {**JOB, "schedule": "A" * 100},
    {**JOB, "comparison": "magic"},
    {**JOB, "case_id": "../../../etc"},
    {**JOB, "case_id": "/abs/path/manifest.json"},
    {**JOB, "case_id": "case_01_fifo_wrap/../case_01_fifo_wrap"},
    {**JOB, "manifest": "/etc/passwd"},                                               # the API takes ids, not paths
    {**JOB, "fixture_causal": "x.json"},                                              # fixture mode is CLI-only
    {**JOB, "case_id": 5},
])
def test_invalid_job_requests_are_rejected_without_running(server: ApiServer, fake: FakeService, bad: object) -> None:
    status, body = jcall(server, "POST", "/api/jobs", bad)
    assert status in (400, 404), body
    assert "error" in body
    assert fake.calls == []


def test_unknown_case_is_404_and_unknown_schedule_is_400(server: ApiServer) -> None:
    assert jcall(server, "POST", "/api/jobs", {**JOB, "case_id": "case_99_nothing"})[0] == 404
    assert jcall(server, "POST", "/api/jobs", {**JOB, "schedule": "nonexistent"})[0] == 400


def test_a_manifest_outside_the_root_is_never_listed_or_run(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    shutil.copytree(FIFO_DIR, outside / "case_01_fifo_wrap")
    (root / "linked").symlink_to(outside / "case_01_fifo_wrap")
    srv = make_server(root, tmp_path, FakeService())
    srv.start()
    try:
        ids = [c["case_id"] for c in jcall(srv, "GET", "/api/cases")[1]["cases"]]
        assert ids == ["case_01_fifo_wrap"]
        assert jcall(srv, "POST", "/api/jobs", {**JOB, "case_id": "linked"})[0] in (400, 404)
    finally:
        srv.stop()


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_job_runs_and_result_is_the_run_record(server: ApiServer, fake: FakeService) -> None:
    job = create(server)
    state = wait_state(server, job, {"finished"})
    assert state["case_id"] == "case_01_fifo_wrap" and state["variant"] == "mutant"
    status, record = jcall(server, "GET", f"/api/jobs/{job}/result")
    assert status == 200
    assert record["record"] == "waveroot-track-c-run" and record["status"] == "pass"
    assert fake.calls == [("mutant", "directed")]


def test_result_before_completion_is_409_and_unknown_jobs_are_404(root: Path, tmp_path: Path) -> None:
    fake = FakeService(block=True)
    srv = make_server(root, tmp_path, fake)
    srv.start()
    try:
        job = create(srv)
        assert fake.started.wait(10)
        assert jcall(srv, "GET", f"/api/jobs/{job}/result")[0] == 409
        assert jcall(srv, "GET", "/api/jobs/" + "0" * 32)[0] == 404
        assert jcall(srv, "GET", "/api/jobs/not-an-id")[0] == 404
        assert jcall(srv, "POST", "/api/jobs/" + "0" * 32 + "/cancel", {})[0] == 404
        jcall(srv, "POST", f"/api/jobs/{job}/cancel", {})
        wait_state(srv, job, {"cancelled", "finished"})
    finally:
        srv.stop()


def test_cancel_sets_the_services_cancel_event(root: Path, tmp_path: Path) -> None:
    fake = FakeService(block=True)
    srv = make_server(root, tmp_path, fake)
    srv.start()
    try:
        job = create(srv)
        assert fake.started.wait(10)
        status, body = jcall(srv, "POST", f"/api/jobs/{job}/cancel", {})
        assert status == 202 and body["state"] in {"cancelling", "cancelled"}
        final = wait_state(srv, job, {"cancelled"})
        assert final["state"] == "cancelled"
        status, record = jcall(srv, "GET", f"/api/jobs/{job}/result")
        assert status == 200 and record["status"] == "error"
        assert [d["code"] for d in record["diagnostics"]] == ["CANCELLED"]
        assert jcall(srv, "POST", f"/api/jobs/{job}/cancel", {})[0] == 409  # already over
    finally:
        srv.stop()


def test_a_queued_job_can_be_cancelled_before_it_starts(root: Path, tmp_path: Path) -> None:
    fake = FakeService(block=True)
    srv = make_server(root, tmp_path, fake, max_concurrent=1)
    srv.start()
    try:
        first = create(srv)
        assert fake.started.wait(10)
        second = create(srv)
        assert jcall(srv, "GET", f"/api/jobs/{second}")[1]["state"] == "queued"
        assert jcall(srv, "POST", f"/api/jobs/{second}/cancel", {})[0] == 202
        wait_state(srv, second, {"cancelled"})
        jcall(srv, "POST", f"/api/jobs/{first}/cancel", {})
        wait_state(srv, first, {"cancelled"})
        assert len(fake.calls) == 1  # the queued job never reached the service
        assert jcall(srv, "GET", f"/api/jobs/{second}/result")[0] == 409
    finally:
        srv.stop()


def test_an_internal_failure_is_reported_without_leaking_details(root: Path, tmp_path: Path) -> None:
    class Boom(FakeService):
        def analyze(self, *a: Any, **k: Any) -> AnalysisOutcome:
            raise RuntimeError(f"secret path {root}")

    srv = make_server(root, tmp_path, Boom())
    srv.start()
    try:
        job = create(srv)
        state = wait_state(srv, job, {"failed"})
        assert state["state"] == "failed"
        assert str(root) not in json.dumps(state)
        assert jcall(srv, "GET", f"/api/jobs/{job}/result")[0] == 409
    finally:
        srv.stop()


def test_old_finished_jobs_are_evicted(root: Path, tmp_path: Path) -> None:
    srv = make_server(root, tmp_path, FakeService(), max_jobs=3)
    srv.start()
    try:
        ids = []
        for _ in range(5):
            job = create(srv)
            wait_state(srv, job, {"finished"})
            ids.append(job)
        assert jcall(srv, "GET", f"/api/jobs/{ids[0]}")[0] == 404
        assert jcall(srv, "GET", f"/api/jobs/{ids[-1]}")[0] == 200
    finally:
        srv.stop()


def test_stopping_the_server_cancels_running_jobs(root: Path, tmp_path: Path) -> None:
    fake = FakeService(block=True)
    srv = make_server(root, tmp_path, fake)
    srv.start()
    create(srv)
    assert fake.started.wait(10)
    started = time.monotonic()
    srv.stop()
    assert time.monotonic() - started < 10


# ----------------------------------------------------------------------
# Source retrieval
# ----------------------------------------------------------------------


@pytest.fixture
def source_server(root: Path, tmp_path: Path) -> Iterator[tuple[ApiServer, Path]]:
    """A job whose report declares one source; the file sits under the case dir."""
    target = root / "case_01_fifo_wrap" / "tests" / "fixtures" / "mock_fifo_wrap.sv"
    target.parent.mkdir(parents=True)
    shutil.copy(MOCK_SOURCE, target)
    srv = make_server(root, tmp_path, FakeService(causal=True))
    srv.start()
    yield srv, target
    srv.stop()


def finished_job(srv: ApiServer) -> str:
    job = create(srv)
    wait_state(srv, job, {"finished"})
    return job


def test_source_is_served_by_id_when_its_hash_matches(source_server: tuple[ApiServer, Path]) -> None:
    srv, _ = source_server
    job = finished_job(srv)
    status, headers, data = call(srv, "GET", f"/api/jobs/{job}/sources/fifo-src")
    assert status == 200
    assert hashlib.sha256(data).hexdigest() == "76726bf96aaae9b4a04de3777d660e2f31993a2807a098e8863d6b894b9bbed0"
    assert headers["content-type"].startswith("text/plain")


def test_source_with_a_different_hash_is_refused(source_server: tuple[ApiServer, Path]) -> None:
    srv, target = source_server
    job = finished_job(srv)
    target.write_text(target.read_text() + "// edited after the report\n")
    status, body = jcall(srv, "GET", f"/api/jobs/{job}/sources/fifo-src")
    assert status == 409
    assert body["error"]["code"] == "SOURCE_HASH_MISMATCH"


def test_only_sources_declared_by_the_report_are_served(source_server: tuple[ApiServer, Path]) -> None:
    srv, _ = source_server
    job = finished_job(srv)
    for source_id in ("other", "..", "%2e%2e", "fifo-src/..", "rtl%2fcorrect%2ffifo.sv"):
        assert call(srv, "GET", f"/api/jobs/{job}/sources/{source_id}")[0] == 404, source_id


def test_a_source_symlinked_outside_the_case_directory_is_refused(
    source_server: tuple[ApiServer, Path], tmp_path: Path
) -> None:
    srv, target = source_server
    job = finished_job(srv)
    outside = tmp_path / "outside.sv"
    shutil.copy(MOCK_SOURCE, outside)  # same bytes, so only the location is wrong
    target.unlink()
    target.symlink_to(outside)
    assert call(srv, "GET", f"/api/jobs/{job}/sources/fifo-src")[0] == 404


def test_a_job_without_a_causal_report_serves_no_sources(server: ApiServer) -> None:
    job = finished_job(server)
    assert call(server, "GET", f"/api/jobs/{job}/sources/fifo-src")[0] == 404


# ----------------------------------------------------------------------
# Integration: real simulator
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestRealJobs:
    @pytest.fixture(autouse=True)
    def _require_verilator(self) -> None:
        if shutil.which("verilator") is None:
            pytest.fail("the FIFO benchmark requires Verilator (hard gate, not a skip)")

    def test_a_real_job_produces_the_documented_failure(self, tmp_path: Path) -> None:
        srv = ApiServer(ApiConfig(root=REPO / "benchmarks", artifacts=tmp_path, extra_origins=(ALLOWED,)))
        srv.start()
        try:
            job = create(srv)
            wait_state(srv, job, {"finished"}, timeout=240)
            status, record = jcall(srv, "GET", f"/api/jobs/{job}/result")
            assert status == 200 and record["status"] == "fail"
            assert record["comparison_adapter"]["kind"] == "analysis"
            failure = record["comparisons"]["cycle"]["failure"]
            assert failure["signal_path"] == "TOP.tb.dut.full"
            assert failure["observation"]["time_ps"] == "55000"
            assert not Path(record["run"]["run_dir"]).is_absolute()
        finally:
            srv.stop()

    def test_cancelling_kills_the_simulator_process_group(self, tmp_path: Path) -> None:
        from tests.track_c.test_runner import processes_mentioning

        srv = ApiServer(ApiConfig(root=REPO / "benchmarks", artifacts=tmp_path, extra_origins=(ALLOWED,)))
        srv.start()
        try:
            job = create(srv)
            end = time.monotonic() + 60
            while time.monotonic() < end and not processes_mentioning(str(tmp_path)):
                time.sleep(0.05)
            assert processes_mentioning(str(tmp_path)), "the build never started"
            assert jcall(srv, "POST", f"/api/jobs/{job}/cancel", {})[0] == 202
            wait_state(srv, job, {"cancelled"}, timeout=60)
            time.sleep(0.5)
            assert processes_mentioning(str(tmp_path)) == []
        finally:
            srv.stop()
