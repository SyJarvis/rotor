"""Phase-two benchmark comparison, resource, and fault-injection contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from benchmarks.clients import BenchmarkClient
import benchmarks.compare as compare_module
from benchmarks.compare import compare_rounds, compare_scenarios, load_round, main as compare_main, render_markdown
from benchmarks.metrics import RequestResult, safe_terminal_event, summarize_results
from benchmarks.mock_provider import MockProviderConfig, create_app
from benchmarks.resource_sampler import ProcessSample, ProcessSampler, normalize_sample, render_markdown as render_resource_markdown
from benchmarks.report import write_artifacts


def _round(path: Path, *, latency: tuple[float, float, float], ttft: tuple[float, float, float],
           rps: float, success_rate: float, model: str = "m") -> None:
    path.mkdir()
    environment = {
        "target": {"base_url": "https://example.invalid/v1", "api_key": "do-not-copy"},
        "parameters": {
            "protocol": "chat",
            "stream": True,
            "model": model,
            "concurrency": 2,
            "requests": 4,
            "warmup": 1,
            "timeout": 10,
            "rate": None,
            "max_tokens": 16,
            "prompt_chars": 3,
            "prompt_sha256": "a" * 64,
            "conversation_mode": "fixed",
        },
    }
    summary = {
        "total_requests": 4,
        "successful_requests": int(round(success_rate * 4)),
        "failed_requests": 4 - int(round(success_rate * 4)),
        "success_rate": success_rate,
        "error_rate": 1 - success_rate,
        "rps": rps,
        "successful_rps": rps * success_rate,
        "latency_ms": {"p50": latency[0], "p95": latency[1], "p99": latency[2]},
        "ttft_ms": {"p50": ttft[0], "p95": ttft[1], "p99": ttft[2]},
        "usage": {"input_tokens": 32, "output_tokens": 8, "total_tokens": 40, "requests_with_usage": 4},
    }
    (path / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
    (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_compare_rounds_reports_gateway_minus_direct_and_redacts(tmp_path: Path) -> None:
    direct, gateway = tmp_path / "direct", tmp_path / "gateway"
    _round(direct, latency=(10, 20, 30), ttft=(4, 8, 9), rps=5, success_rate=1)
    _round(gateway, latency=(13, 25, 35), ttft=(5, 10, 12), rps=4, success_rate=.75)
    result = compare_rounds(direct, gateway)
    assert result["comparable"] is True
    assert result["metrics"]["latency_ms"]["p50_ms"]["delta"] == 3
    assert result["metrics"]["ttft_ms"]["p99_ms"]["delta"] == 3
    assert result["metrics"]["throughput"]["successful_rps"]["delta"] == 4 * .75 - 5
    assert result["metrics"]["rates"]["error_rate"]["delta"] == .25
    rendered = json.dumps(result)
    assert "do-not-copy" not in rendered
    assert "example.invalid" in rendered
    assert "prompt" not in rendered.lower() or "prompt_sha256" in rendered
    assert "gateway - direct" in render_markdown(result)


def test_compare_rounds_blocks_conflicting_scenario(tmp_path: Path) -> None:
    direct, gateway = tmp_path / "direct", tmp_path / "gateway"
    _round(direct, latency=(1, 2, 3), ttft=(1, 2, 3), rps=1, success_rate=1)
    _round(gateway, latency=(1, 2, 3), ttft=(1, 2, 3), rps=1, success_rate=1, model="other")
    result = compare_rounds(direct, gateway)
    assert result["comparable"] is False
    assert any(item["parameter"] == "model" for item in result["scenario"]["mismatches"])
    assert compare_main([str(direct), str(gateway)]) == 3
    assert compare_main([str(direct), str(gateway), "--allow-incomparable"]) == 0


def test_compare_strict_requires_minimal_metadata_but_default_reports_unverified() -> None:
    sparse = {"environment": {"parameters": {"protocol": "chat"}}, "summary": {"rps": 1}}
    default = compare_scenarios(sparse, sparse)
    assert default["comparable"] is True
    assert default["verified"] is False
    strict = compare_scenarios(sparse, sparse, strict=True)
    assert strict["comparable"] is False
    assert strict["verified"] is False
    assert any(item["parameter"] == "model" for item in strict["mismatches"])


def test_standalone_duration_summary_uses_target_duration_alias() -> None:
    direct = {"summary": {"mode": "duration", "duration_target_seconds": 5.0, "rps": 1}}
    gateway = {"summary": {"mode": "duration", "duration_target_seconds": 6.0, "rps": 1}}
    result = compare_scenarios(direct, gateway)
    assert result["comparable"] is False
    assert any(item["parameter"] == "duration_seconds_requested" for item in result["mismatches"])


def test_compare_rejects_invalid_scenario_values_even_when_equal() -> None:
    invalid = {
        "environment": {"parameters": {
            "protocol": "bogus", "stream": "maybe", "model": " ",
            "concurrency": 0, "requests": 0, "warmup": -1,
        }},
        "summary": {},
    }
    result = compare_scenarios(invalid, invalid)
    assert result["comparable"] is False
    assert result["verified"] is False
    assert {item["parameter"] for item in result["mismatches"]} >= {
        "protocol", "stream", "model", "concurrency", "requests", "warmup",
    }


def test_compare_scenario_and_summary_output_redacts_nested_untrusted_values() -> None:
    params = {
        "protocol": "chat",
        "stream": True,
        "model": "model\n`injected`",
        "concurrency": 1,
        "requests": 1,
    }
    summary = {
        "rps": 1,
        "latency_ms": {"p50": 1, "prompt": "DO_NOT_LEAK"},
        "usage": {"total_tokens": 2, "secret": "DO_NOT_LEAK"},
    }
    result = compare_rounds(
        {"environment": {"parameters": params}, "summary": summary},
        {"environment": {"parameters": params}, "summary": summary},
    )
    encoded = json.dumps(result)
    assert "DO_NOT_LEAK" not in encoded
    assert "injected" in encoded and "\n" not in result["scenario"]["direct"]["model"]
    assert result["runs"]["direct"]["source"] == "direct"
    assert result["runs"]["gateway"]["source"] == "gateway"


def test_compare_extreme_numbers_stays_json_safe() -> None:
    left = {"summary": {"rps": -1e308, "latency_ms": {"p50": -1e308}}}
    right = {"summary": {"rps": 1e308, "latency_ms": {"p50": 1e308}}}
    result = compare_rounds(left, right)
    assert result["metrics"]["rps"]["delta"] is None
    json.dumps(result, allow_nan=False)


def test_conversation_id_is_fingerprinted_and_mapping_artifacts_are_safe(tmp_path: Path) -> None:
    result = RequestResult(
        request_id="r", conversation_id="tenant-secret-conversation",
        protocol="chat", stream=False, success=True, duration_ms=1,
    )
    encoded = result.to_dict()
    assert encoded["conversation_id"].startswith("sha256:")
    assert "tenant-secret-conversation" not in json.dumps(encoded)
    # Fingerprints are idempotent when an artifact is loaded and written again.
    assert RequestResult.from_mapping(encoded).to_dict()["conversation_id"] == encoded["conversation_id"]
    output = write_artifacts(
        tmp_path / "mapping", results=[{
            "request_id": "r", "conversation_id": "tenant-secret-conversation",
            "protocol": "chat", "stream": False, "success": True, "duration_ms": 1,
        }], environment={},
    )
    raw = (output / "raw-results.jsonl").read_text()
    assert "tenant-secret-conversation" not in raw
    assert "sha256:" in raw


def test_terminal_event_serialization_is_bounded() -> None:
    result = RequestResult(
        request_id="r", conversation_id=None, protocol="chat", stream=True,
        success=False, duration_ms=1, terminal_event="SECRET_EVENT_api_key=LEAK",
    )
    assert safe_terminal_event(result.terminal_event) == "terminal"
    encoded = result.to_dict()
    assert encoded["terminal_event"] == "terminal"
    assert "SECRET_EVENT" not in json.dumps(encoded)


def test_malicious_sse_event_name_is_not_written() -> None:
    app = FastAPI()

    @app.post("/v1/responses")
    async def responses() -> StreamingResponse:
        async def body():
            # The SSE label is untrusted; JSON still carries a recognized
            # terminal type and the serialized marker must be generic.
            yield 'event: SECRET_EVENT_api_key=LEAK\ndata: {"type":"response.completed"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    async def exercise() -> RequestResult:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            client = BenchmarkClient("http://mock", "k", "m", "responses", http_client=wire)
            return await client.request(stream=True)

    result = asyncio.run(exercise())
    assert result.success
    assert result.to_dict()["terminal_event"] == "terminal"
    assert "SECRET_EVENT" not in json.dumps(result.to_dict())


def test_malformed_intervals_are_ignored() -> None:
    summary = summarize_results([{
        "request_id": "r", "protocol": "chat", "stream": True,
        "success": True, "duration_ms": 1,
        "chunk_intervals_ms": None,
    }])
    assert summary["chunk_interval_ms"]["count"] == 0


def test_load_round_does_not_read_huge_raw_when_summary_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "large"
    root.mkdir()
    (root / "environment.json").write_text(json.dumps({"parameters": {"protocol": "chat"}}))
    (root / "summary.json").write_text(json.dumps({
        "total_requests": 1, "successful_requests": 1, "failed_requests": 0,
        "success_rate": 1, "error_rate": 0, "rps": 1,
        "latency_ms": {"p50": 1}, "ttft_ms": {"p50": 1},
    }))
    (root / "raw-results.jsonl").write_text("not-json\n")
    monkeypatch.setattr(compare_module, "_read_rows", lambda path: (_ for _ in ()).throw(AssertionError("raw rows should be skipped")))
    loaded = load_round(root)
    assert loaded.rows == ()


def test_sampler_aggregates_mocked_samples_without_waiting() -> None:
    values = iter([
        ProcessSample(cpu_percent=10, rss_bytes=100),
        {"cpu_percent": 30, "rss_bytes": 300},
        (20, 200),
    ])
    sampler = ProcessSampler(123, interval=0, source=lambda _pid: next(values))
    stats = sampler.collect(samples=3)
    assert stats.status == "ok"
    assert stats.sample_count == 3
    assert stats.cpu_percent_avg == 20
    assert stats.cpu_percent_max == 30
    assert stats.rss_bytes_avg == 200
    assert stats.rss_bytes_peak == 300
    assert stats.to_dict()["time_unit"] == "seconds"


def test_sampler_unavailable_pid_is_explicit() -> None:
    sampler = ProcessSampler(123, interval=0, source=lambda _pid: (_ for _ in ()).throw(ProcessLookupError("gone")))
    stats = sampler.collect()
    assert stats.status == "unavailable"
    assert stats.sample_count == 0
    assert stats.cpu_percent_avg is None
    assert stats.rss_bytes_peak is None
    assert stats.reason and "unavailable" in stats.reason


def test_sampler_rejects_hot_loop_and_zero_samples_are_explicit() -> None:
    sampler = ProcessSampler(123, interval=0, source=lambda _pid: (1, 2))
    zero = sampler.collect(samples=0)
    assert zero.status == "unavailable"
    assert zero.sample_count == 0
    assert zero.reason == "no samples requested"
    try:
        sampler.start()
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("zero interval must not start a background hot loop")


def test_sampler_windows_reset_between_collect_calls() -> None:
    values = iter([(1, 10), (3, 30)])
    sampler = ProcessSampler(123, interval=0, source=lambda _pid: next(values))
    first = sampler.collect(samples=1)
    second = sampler.collect(samples=1)
    assert first.sample_count == 1 and first.cpu_percent_avg == 1
    assert second.sample_count == 1 and second.cpu_percent_avg == 3


def test_sampler_filters_invalid_samples_and_clock_errors_without_leaking() -> None:
    assert normalize_sample({}) is None
    assert normalize_sample({"cpu_percent": "bad", "rss_bytes": "bad"}) is None
    partial = normalize_sample({"cpu_percent": 2, "rss_bytes": "bad", "timestamp": float("nan")})
    assert partial is not None and partial.cpu_percent == 2 and partial.timestamp is None
    sampler = ProcessSampler(
        123, interval=1,
        source=lambda _pid: (_ for _ in ()).throw(PermissionError("secret-key")),
    )
    stats = sampler.collect(samples=1)
    assert "secret-key" not in (stats.reason or "")
    assert stats.reason == "process unavailable (PermissionError)"
    clock_sampler = ProcessSampler(
        123, interval=1, source=lambda _pid: (1, 2),
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock-secret")),
    )
    clock_stats = clock_sampler.collect(samples=1)
    assert clock_stats.status == "error"
    assert "clock-secret" not in (clock_stats.reason or "")


def test_resource_markdown_sanitizes_custom_reason() -> None:
    stats = ProcessSampler(123, interval=1, source=lambda _pid: None).collect()
    stats.reason = "line\n`unsafe`"  # type: ignore[misc]
    rendered = render_resource_markdown(stats)
    assert "\n`unsafe`" not in rendered


def test_mock_provider_sequence_failure_and_recovery() -> None:
    config = MockProviderConfig(fail_after=2, recover_after=4)
    assert [config.validated().failure_for_sequence(i) is not None for i in range(1, 7)] == [False, False, True, True, False, False]

    async def exercise() -> list[tuple[int | None, bool, str | None]]:
        app = create_app(config)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            client = BenchmarkClient("http://mock", "key", "m", "chat", http_client=wire)
            values = []
            for _ in range(6):
                result = await client.request()
                values.append((result.status_code, result.success, result.error_category))
            return values

    values = asyncio.run(exercise())
    assert [item[1] for item in values] == [True, True, False, False, True, True]
    assert values[2][0] == values[3][0] == 503
    assert values[2][2] == "http_5xx"


def test_mock_provider_fixed_status_remains_fixed() -> None:
    config = MockProviderConfig(status_code=429, fail_after=1, recover_after=2).validated()
    # Fixed ``status_code`` takes precedence in the HTTP handler, while the
    # optional sequence policy remains inspectable and valid on its own.
    assert config.failure_for_sequence(2) == (503, "injected_sequence_failure")

    async def exercise() -> list[int | None]:
        app = create_app(config)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            client = BenchmarkClient("http://mock", "key", "m", "chat", http_client=wire)
            return [(await client.request()).status_code for _ in range(3)]

    assert asyncio.run(exercise()) == [429, 429, 429]


def test_mock_provider_validates_fault_schedule_and_alias() -> None:
    with pytest.raises(ValueError, match="recover_after"):
        MockProviderConfig(fail_after=2, recover_after=2).validated()
    with pytest.raises(ValueError, match="non-2xx"):
        MockProviderConfig(failure_status_code=200).validated()
    config = MockProviderConfig(fail_after=1, recover_after=2, fail_status_code=429)
    assert config.failure_for_sequence(2) == (429, "injected_sequence_failure")
