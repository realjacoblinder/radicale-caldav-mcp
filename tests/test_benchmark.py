"""Focused tests for the local-only benchmark's aggregation contract."""

import sys
from importlib.util import module_from_spec, spec_from_file_location

import pytest


@pytest.fixture
def benchmark(project_root):
    path = project_root / "benchmarks" / "baseline.py"
    spec = spec_from_file_location("baseline_benchmark", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_and_distribution(benchmark):
    assert benchmark.percentile([1, 2, 3, 4], 50) == 2.5
    assert benchmark.percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert benchmark.distribution([1, 2, 3, 4]) == {"median": 2.5, "p95": 3.85}
    with pytest.raises(ValueError):
        benchmark.percentile([], 50)


def test_summarize_preserves_phase_and_transfer_metrics(benchmark):
    samples = [
        {
            "wall_ms": 10.0,
            "http_ms": 7.0,
            "local_service_ms": 3.0,
            "categories": {"discovery": 2.0, "resource_transfer": 5.0},
            "methods": {"PROPFIND": 2, "REPORT": 1},
            "response_bytes": 1000,
            "response_resources": 8,
            "result_items": 5,
        },
        {
            "wall_ms": 12.0,
            "http_ms": 8.0,
            "local_service_ms": 4.0,
            "categories": {"discovery": 3.0, "resource_transfer": 5.0},
            "methods": {"PROPFIND": 2, "REPORT": 1},
            "response_bytes": 1200,
            "response_resources": 8,
            "result_items": 5,
        },
    ]
    result = benchmark.summarize(samples)
    assert result["wall_ms"]["median"] == 11.0
    assert result["network_category_ms"]["discovery"]["median"] == 2.5
    assert result["requests_per_sample"]["PROPFIND"]["median"] == 2.0
    assert result["response_bytes_per_sample"]["median"] == 1100.0
    assert result["response_resources_per_sample"]["median"] == 8.0


def test_request_recorder_retains_only_aggregate_metadata(benchmark):
    class Response:
        content = b'<D:multistatus><D:response href="private"/></D:multistatus>'
        status_code = 207

    class Session:
        def request(self, method, url, *args, **kwargs):
            return Response()

    class DAV:
        session = Session()

    class Store:
        dav = DAV()

    recorder = benchmark.RequestRecorder(Store())
    recorder.install()
    try:
        Store.dav.session.request(
            "REPORT", "http://calendar.invalid/private", headers={"Authorization": "secret"}
        )
    finally:
        recorder.close()
    assert len(recorder.metrics) == 1
    metric = recorder.metrics[0]
    assert metric.method == "REPORT"
    assert metric.category == "resource_transfer"
    assert metric.response_resources == 1
    assert metric.response_bytes == len(Response.content)
    assert not hasattr(metric, "url") and not hasattr(metric, "headers")
