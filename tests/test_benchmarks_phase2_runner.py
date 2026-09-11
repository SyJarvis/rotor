"""Offline tests for phase-two runner controls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from benchmarks.mock_provider import MockProviderConfig, create_app
import benchmarks.runner as runner_module
from benchmarks.runner import aggregate_round_summaries, run_protocol


def _summary(
    *,
    rps: float,
    latency: tuple[float, float, float],
    ttft: tuple[float, float, float],
    total: int = 4,
    failed: int = 0,
) -> dict:
    successful = total - failed
    return {
        "total_requests": total,
        "successful_requests": successful,
        "failed_requests": failed,
        "success_rate": successful / total if total else 0.0,
        "error_rate": failed / total if total else 0.0,
        "elapsed_seconds": total / rps if rps else None,
        "rps": rps,
        "latency_ms": {"p50": latency[0], "p95": latency[1], "p99": latency[2]},
        "ttft_ms": {"p50": ttft[0], "p95": ttft[1], "p99": ttft[2]},
        "errors": {"http_5xx": failed} if failed else {},
        "status_codes": {"200": successful, "500": failed} if failed else {"200": successful},
        "usage": {"input_tokens": total, "output_tokens": total, "total_tokens": total * 2, "requests_with_usage": total},
    }


def test_aggregate_rounds_has_median_and_dispersion() -> None:
    aggregate = aggregate_round_summaries(
        [
            _summary(rps=10, latency=(10, 20, 30), ttft=(4, 8, 12)),
            _summary(rps=20, latency=(20, 40, 60), ttft=(6, 10, 14), failed=1),
            _summary(rps=30, latency=(30, 60, 90), ttft=(8, 12, 16)),
        ]
    )
    assert aggregate["round_count"] == 3
    assert aggregate["total_requests"] == 12
    assert aggregate["failed_requests"] == 1
    assert aggregate["metrics"]["rps"]["median"] == 20
    assert aggregate["metrics"]["rps"]["stdev"] > 0
    assert aggregate["metrics"]["latency_p95_ms"]["median"] == 40
    assert aggregate["metrics"]["ttft_p99_ms"]["range"] == 4
    # Only known fields are retained in per-round records.
    assert "prompt" not in json.dumps(aggregate)


def test_run_protocol_rounds_writes_artifacts_and_collision(tmp_path: Path) -> None:
    async def exercise() -> tuple[list[Path], list[Path]]:
        app = create_app(MockProviderConfig(response_text="round output"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            first = await run_protocol(
                base_url="http://mock",
                api_key="round-secret",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=2,
                requests=2,
                warmup=1,
                timeout=3,
                output_dir=tmp_path / "repeated",
                rounds=2,
                prompt="private round prompt",
                http_client=wire,
            )
            second = await run_protocol(
                base_url="http://mock",
                api_key="round-secret",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=2,
                requests=1,
                warmup=0,
                timeout=3,
                output_dir=tmp_path / "repeated",
                rounds=2,
                prompt="private round prompt",
                http_client=wire,
            )
        return first, second

    first, second = asyncio.run(exercise())
    assert isinstance(first, list) and len(first) == 2
    assert isinstance(second, list) and len(second) == 2
    assert first[0].parent != second[0].parent
    for path in first:
        assert {item.name for item in path.iterdir()} == {
            "environment.json", "raw-results.jsonl", "summary.json", "report.md"
        }
    aggregate_path = first[0].parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text())
    assert aggregate["round_count"] == 2
    assert len(aggregate["rounds"]) == 2
    assert aggregate["metrics"]["latency_p95_ms"]["median"] is not None
    rendered = "\n".join(item.read_text() for item in first[0].parent.iterdir() if item.is_file())
    assert "round-secret" not in rendered
    assert "private round prompt" not in rendered
    assert "round output" not in rendered


def test_duration_short_run_streams_raw_rows_and_validates(tmp_path: Path) -> None:
    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="duration output"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            return await run_protocol(
                base_url="http://mock",
                api_key="duration-secret",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=2,
                requests=999,
                warmup=1,
                timeout=3,
                duration=0.03,
                output_dir=tmp_path / "duration",
                prompt="duration prompt must not persist",
                http_client=wire,
            )

    output = asyncio.run(exercise())
    summary = json.loads((output / "summary.json").read_text())
    assert summary["mode"] == "duration"
    assert summary["duration_target_seconds"] == pytest.approx(0.03)
    assert summary["total_requests"] > 0
    assert summary["quantile_sample_size"] <= summary["quantile_sample_limit"]
    raw_rows = (output / "raw-results.jsonl").read_text().splitlines()
    assert len(raw_rows) == summary["total_requests"]
    assert "duration-secret" not in (output / "environment.json").read_text()
    with pytest.raises(ValueError, match="duration"):
        asyncio.run(
            run_protocol(
                base_url="http://mock",
                api_key="k",
                model="m",
                protocol="chat",
                stream=False,
                concurrency=1,
                requests=1,
                warmup=0,
                timeout=1,
                duration=0,
                output_dir=tmp_path / "invalid",
            )
        )


def test_duration_placeholders_are_not_scenario_controls(tmp_path: Path) -> None:
    """Equal duration windows remain comparable despite different requests args."""

    from benchmarks.compare import compare_scenarios

    async def exercise() -> tuple[Path, Path]:
        app = create_app(MockProviderConfig(response_text="duration"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            first = await run_protocol(
                base_url="http://mock", api_key="k", model="duration-model",
                protocol="chat", stream=False, concurrency=1, requests=1,
                warmup=0, timeout=3, duration=0.01,
                output_dir=tmp_path / "first", http_client=wire,
            )
            second = await run_protocol(
                base_url="http://mock", api_key="k", model="duration-model",
                protocol="chat", stream=False, concurrency=1, requests=999,
                warmup=0, timeout=3, duration=0.01,
                output_dir=tmp_path / "second", http_client=wire,
            )
        return first, second

    first, second = asyncio.run(exercise())
    first_env = json.loads((first / "environment.json").read_text())
    second_env = json.loads((second / "environment.json").read_text())
    assert first_env["parameters"]["model"] == "duration-model"
    assert first_env["parameters"]["requests"] is None
    assert second_env["parameters"]["requests"] is None
    scenario = compare_scenarios(first, second)
    assert scenario["comparable"] is True
    assert "requests" not in scenario["checked"]


def test_duration_allows_zero_request_placeholder(tmp_path: Path) -> None:
    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="duration"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            return await run_protocol(
                base_url="http://mock", api_key="k", model="m", protocol="chat",
                stream=False, concurrency=1, requests=0, warmup=0, timeout=3,
                duration=0.005, output_dir=tmp_path / "zero", http_client=wire,
            )

    output = asyncio.run(exercise())
    environment = json.loads((output / "environment.json").read_text())
    assert environment["parameters"]["requests"] is None


def test_finite_sampler_starts_after_warmup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []

    class FakeSampler:
        def start(self):
            events.append("sampler-start")
            return self

        def stop(self):
            events.append("sampler-stop")
            return type("Stats", (), {"to_dict": lambda self: {"status": "ok", "sample_count": 1}})()

    monkeypatch.setattr(runner_module, "_resource_sampler", lambda pid, interval: FakeSampler())

    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="warmup"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            # The mock sequence is observable without touching production code:
            # warmup request is issued before the sampler callback.
            original_request = runner_module.run_requests

            async def traced(*args, **kwargs):
                result = await original_request(*args, **kwargs)
                events.append("requests")
                return result

            monkeypatch.setattr(runner_module, "run_requests", traced)
            return await run_protocol(
                base_url="http://mock", api_key="k", model="m", protocol="chat",
                stream=False, concurrency=1, requests=1, warmup=1, timeout=3,
                output_dir=tmp_path / "sampler-order", pid=123,
                sample_interval=0.1, http_client=wire,
            )

    output = asyncio.run(exercise())
    assert events[:2] == ["requests", "sampler-start"]
    assert events.count("requests") == 2
    assert json.loads((output / "summary.json").read_text())["resource"]["sample_count"] == 1


def test_default_single_round_return_and_artifacts_remain_compatible(tmp_path: Path) -> None:
    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="legacy output"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            return await run_protocol(
                base_url="http://mock",
                api_key="legacy-secret",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=1,
                requests=1,
                warmup=0,
                timeout=3,
                output_dir=tmp_path / "legacy",
                http_client=wire,
            )

    output = asyncio.run(exercise())
    assert isinstance(output, Path)
    assert (output / "summary.json").exists()
    assert not (output / "aggregate.json").exists()


def test_optional_pid_sampler_is_written_without_body_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStats:
        def to_dict(self):
            return {"pid": 123, "status": "ok", "sample_count": 1, "cpu_percent_avg": 2.0}

    class FakeSampler:
        def __init__(self, pid, interval):
            self.pid = pid
            self.interval = interval

        def start(self):
            return self

        def stop(self):
            return FakeStats()

    monkeypatch.setattr(runner_module, "_resource_sampler", lambda pid, interval: FakeSampler(pid, interval))

    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="resource output"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as wire:
            return await run_protocol(
                base_url="http://mock",
                api_key="resource-secret",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=1,
                requests=1,
                warmup=0,
                timeout=3,
                output_dir=tmp_path / "resource",
                pid=123,
                sample_interval=0.1,
                http_client=wire,
            )

    output = asyncio.run(exercise())
    summary = json.loads((output / "summary.json").read_text())
    environment = json.loads((output / "environment.json").read_text())
    assert summary["resource"]["sample_count"] == 1
    assert environment["resource"]["pid"] == 123
    assert "resource-secret" not in json.dumps(environment)
    assert "resource output" not in (output / "raw-results.jsonl").read_text()
