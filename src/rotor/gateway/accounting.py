from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.resource_scopes import ResourceScopes, resolve_resource_scopes
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.gateway.pricing import UsageCost, calculate_usage_cost
from rotor.services.session_leases import (
    record_session_lease_success,
    resolve_idle_ttl_seconds,
)


@dataclass(slots=True)
class UsageData:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    reasoning_tokens: int = 0
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0
    usage_source: str = "provider"
    usage_schema_version: str = "2"


@dataclass(slots=True)
class StreamingUsageAccumulator:
    """Merge provider usage snapshots without mixing input breakdowns.

    Providers may first report the whole prompt as uncached and then send a
    detailed cache snapshot for the same prompt. Input counts must therefore
    be replaced together, while output counts can grow independently.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    has_provider_usage: bool = False

    def observe(self, usage: UsageData) -> None:
        self.has_provider_usage = True
        has_input_snapshot = bool(
            usage.prompt_tokens
            or usage.uncached_input_tokens
            or usage.cached_tokens
            or usage.cache_write_tokens
        )
        if has_input_snapshot:
            self.prompt_tokens = usage.prompt_tokens
            self.uncached_input_tokens = usage.uncached_input_tokens
            self.cached_tokens = usage.cached_tokens
            self.cache_write_tokens = usage.cache_write_tokens
            self.cache_write_5m_tokens = usage.cache_write_5m_tokens
            self.cache_write_1h_tokens = usage.cache_write_1h_tokens
        self.completion_tokens = max(
            self.completion_tokens,
            usage.completion_tokens,
        )

    def to_usage_data(self, service: "AccountingService") -> UsageData:
        return service.streaming_usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            has_provider_usage=self.has_provider_usage,
            cached_tokens=self.cached_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
            uncached_input_tokens=self.uncached_input_tokens,
        )


class AccountingService:
    """Write request logs and usage ledgers, and update token quota counters."""

    def record_routing_decision(
        self,
        db: AsyncSession,
        *,
        request_id: str,
        token: Token | None,
        model: str,
        request_protocol: str,
        decision,
        required_capabilities: set[str] | None = None,
        affinity_used: bool = False,
        features: dict[str, Any] | None = None,
    ) -> None:
        candidates = list(decision.candidates)
        db.add(RoutingDecisionRecord(
            request_id=request_id,
            token_id=token.id if token else None,
            model=model,
            request_protocol=request_protocol,
            strategy=decision.strategy,
            policy_version=f"{decision.strategy}-v1",
            candidate_channel_ids=[channel.id for channel in candidates],
            selected_channel_id=candidates[0].id if candidates else None,
            required_capabilities=sorted(required_capabilities or set()),
            affinity_used=affinity_used,
            score_snapshot=(
                {str(key): value for key, value in decision.scores.items()}
                if decision.scores
                else None
            ),
            feature_snapshot={
                **(features or {}),
                "session_lease": {
                    "preferred_channel_id": getattr(
                        decision, "lease_channel_id", None
                    ),
                    "used": getattr(decision, "lease_used", False),
                },
                "candidate_resource_scopes": {
                    str(channel.id): resolve_resource_scopes(channel).as_dict()
                    for channel in candidates
                },
            },
        ))

    async def record_success(
        self,
        db: AsyncSession,
        *,
        request_id: str,
        conversation_id: str | None,
        request_protocol: str,
        token: Token,
        channel: Channel,
        model: str,
        provider_model: str | None,
        usage: UsageData,
        latency_ms: int,
        client_ip: str,
        capacity_snapshot: dict[str, str] | None = None,
        tariff_at: datetime | float | None = None,
        lease_session_id: str | None = None,
        lease_migration_reason: str = "request_success",
    ) -> None:
        resource_scopes = resolve_resource_scopes(channel)
        cost = calculate_usage_cost(
            channel,
            logical_model=model,
            provider_model=provider_model,
            usage=usage,
            at=tariff_at,
        )
        self._update_token_counters(token, usage)
        self._add_request_log(
            db,
            token_id=token.id,
            channel_id=channel.id,
            model=model,
            usage=usage,
            success=True,
            latency_ms=latency_ms,
            client_ip=client_ip,
            capacity_snapshot=capacity_snapshot,
            cost=cost,
            resource_scopes=resource_scopes,
        )
        db.add(
            UsageLedger(
                request_id=request_id,
                conversation_id=conversation_id,
                user_id=token.user_id,
                token_id=token.id,
                channel_id=channel.id,
                provider=channel.type,
                model=model,
                provider_model=provider_model,
                request_protocol=request_protocol,
                provider_protocol=channel.protocol,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                uncached_input_tokens=usage.uncached_input_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_write_5m_tokens=usage.cache_write_5m_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                input_audio_tokens=usage.input_audio_tokens,
                output_audio_tokens=usage.output_audio_tokens,
                usage_source=usage.usage_source,
                usage_schema_version=usage.usage_schema_version,
                capacity_snapshot=capacity_snapshot,
                cache_scope=resource_scopes.cache_scope,
                capacity_scope=resource_scopes.capacity_scope,
                billing_scope=resource_scopes.billing_scope,
                input_cost=cost.input_cost,
                output_cost=cost.output_cost,
                total_cost=cost.total_cost,
                currency=cost.currency or "USD",
                cost_status=cost.status,
                tariff_version=cost.tariff_version,
                tariff_period=cost.tariff_period,
                tariff_snapshot=cost.snapshot,
                status="success",
                latency_ms=latency_ms,
            )
        )
        await self.record_lease_success(
            db,
            request_id=request_id,
            token=token,
            channel=channel,
            model=model,
            lease_session_id=lease_session_id,
            lease_migration_reason=lease_migration_reason,
        )
        # Keep online policy updates on the accounting boundary so every
        # supported protocol feeds the same learning signal.
        from rotor.gateway.routing import routing_engine

        routing_engine.observe_result(
            model,
            channel,
            success=True,
            latency_ms=latency_ms,
            # Phase 2 records pricing facts only. Cost-aware routing remains
            # gated until observed data proves it improves Session Lease.
            cost=None,
        )

    async def record_lease_success(
        self,
        db: AsyncSession,
        *,
        request_id: str,
        token: Token,
        channel: Channel,
        model: str,
        lease_session_id: str | None,
        lease_migration_reason: str = "request_success",
    ) -> None:
        if lease_session_id is None:
            return
        from rotor.application_settings import application_settings

        routing_settings = application_settings.get().routing
        if not (
            routing_settings.affinity_enabled
            and routing_settings.session_lease_enabled
        ):
            return
        await record_session_lease_success(
            db,
            request_id=request_id,
            token_id=token.id,
            session_id=lease_session_id,
            logical_model=model,
            channel_id=channel.id,
            idle_ttl_seconds=resolve_idle_ttl_seconds(
                channel,
                model,
                routing_settings.session_lease_idle_ttl_seconds,
            ),
            migration_reason=lease_migration_reason,
        )

    async def record_failure(
        self,
        db: AsyncSession,
        *,
        request_id: str,
        conversation_id: str | None,
        request_protocol: str,
        token: Optional[Token],
        channel: Optional[Channel],
        model: str,
        error_code: str,
        error_message: str,
        latency_ms: Optional[int],
        client_ip: str,
        provider_response: dict[str, Any] | None = None,
        capacity_snapshot: dict[str, str] | None = None,
    ) -> None:
        resource_scopes = (
            resolve_resource_scopes(channel)
            if channel is not None
            else None
        )
        self._add_request_log(
            db,
            token_id=token.id if token else None,
            channel_id=channel.id if channel else None,
            model=model,
            usage=UsageData(usage_source="missing"),
            success=False,
            latency_ms=latency_ms,
            client_ip=client_ip,
            error_code=error_code,
            error_message=error_message,
            response_body=provider_response,
            capacity_snapshot=capacity_snapshot,
            cost=None,
            resource_scopes=resource_scopes,
        )
        db.add(
            UsageLedger(
                request_id=request_id,
                conversation_id=conversation_id,
                user_id=token.user_id if token else None,
                token_id=token.id if token else None,
                channel_id=channel.id if channel else None,
                provider=channel.type if channel else None,
                model=model,
                request_protocol=request_protocol,
                provider_protocol=channel.protocol if channel else None,
                usage_source="missing",
                usage_schema_version="2",
                capacity_snapshot=capacity_snapshot,
                cache_scope=(
                    resource_scopes.cache_scope if resource_scopes else None
                ),
                capacity_scope=(
                    resource_scopes.capacity_scope if resource_scopes else None
                ),
                billing_scope=(
                    resource_scopes.billing_scope if resource_scopes else None
                ),
                cost_status="not_applicable",
                status="failed",
                error_code=error_code,
                error_message=error_message[:500],
                latency_ms=latency_ms,
            )
        )
        if channel is not None:
            from rotor.gateway.routing import routing_engine

            routing_engine.observe_result(
                model,
                channel,
                success=False,
                latency_ms=latency_ms,
            )

    def extract_usage(self, response_data: dict) -> UsageData:
        usage = response_data.get("usage") or {}
        reported_input_tokens = self._token_count(
            usage.get("prompt_tokens")
            if usage.get("prompt_tokens") is not None
            else usage.get("input_tokens")
        )
        completion_tokens = self._token_count(
            usage.get("completion_tokens")
            if usage.get("completion_tokens") is not None
            else usage.get("output_tokens")
        )

        details = (
            usage.get("completion_tokens_details")
            or usage.get("output_tokens_details")
            or {}
        )
        prompt_details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or {}
        )

        if "cached_tokens" in prompt_details:
            cached_tokens = self._token_count(prompt_details.get("cached_tokens"))
        elif "prompt_cache_hit_tokens" in usage:
            cached_tokens = self._token_count(
                usage.get("prompt_cache_hit_tokens")
            )
        else:
            cached_tokens = self._token_count(
                usage.get("cache_read_input_tokens")
            )

        cache_creation = usage.get("cache_creation") or {}
        cache_write_5m_tokens = self._token_count(
            prompt_details.get("cache_write_5m_tokens")
            or cache_creation.get("ephemeral_5m_input_tokens")
        )
        cache_write_1h_tokens = self._token_count(
            prompt_details.get("cache_write_1h_tokens")
            or cache_creation.get("ephemeral_1h_input_tokens")
        )
        if "cache_write_tokens" in prompt_details:
            cache_write_tokens = self._token_count(
                prompt_details.get("cache_write_tokens")
            )
        else:
            cache_write_tokens = self._token_count(
                usage.get("cache_creation_input_tokens")
            )
        cache_write_tokens = max(
            cache_write_tokens,
            cache_write_5m_tokens + cache_write_1h_tokens,
        )

        anthropic_breakdown = (
            "input_tokens" in usage
            and "prompt_tokens" not in usage
            and any(
                key in usage
                for key in (
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation",
                )
            )
        )
        if anthropic_breakdown:
            uncached_input_tokens = reported_input_tokens
            prompt_tokens = (
                uncached_input_tokens + cached_tokens + cache_write_tokens
            )
        else:
            prompt_tokens = reported_input_tokens
            if "prompt_cache_miss_tokens" in usage:
                uncached_input_tokens = self._token_count(
                    usage.get("prompt_cache_miss_tokens")
                )
                if prompt_tokens == 0:
                    prompt_tokens = uncached_input_tokens + cached_tokens
            elif "uncached_tokens" in prompt_details:
                uncached_input_tokens = self._token_count(
                    prompt_details.get("uncached_tokens")
                )
            else:
                uncached_input_tokens = max(
                    prompt_tokens - cached_tokens - cache_write_tokens,
                    0,
                )

        computed_total = prompt_tokens + completion_tokens
        total_tokens = max(
            self._token_count(usage.get("total_tokens")),
            computed_total,
        )

        return UsageData(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            uncached_input_tokens=uncached_input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            input_audio_tokens=int(prompt_details.get("audio_tokens") or 0),
            output_audio_tokens=int(details.get("audio_tokens") or 0),
            usage_source="provider" if usage else "missing",
        )

    def streaming_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        has_provider_usage: bool,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        uncached_input_tokens: int | None = None,
    ) -> UsageData:
        if not has_provider_usage:
            return UsageData(usage_source="missing")
        return self.extract_usage({
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "cache_write_5m_tokens": cache_write_5m_tokens,
                    "cache_write_1h_tokens": cache_write_1h_tokens,
                    **(
                        {"uncached_tokens": uncached_input_tokens}
                        if uncached_input_tokens is not None
                        else {}
                    ),
                },
            }
        })

    @staticmethod
    def _token_count(value: Any) -> int:
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _update_token_counters(
        self,
        token: Token,
        usage: UsageData,
    ) -> None:
        token.request_count += 1
        token.token_count += usage.total_tokens
        token.used_quota += usage.total_tokens
        token.last_used_at = datetime.now(timezone.utc)

        if token.quota is not None and token.used_quota >= token.quota:
            token.enabled = False

    def _add_request_log(
        self,
        db: AsyncSession,
        *,
        token_id: Optional[int],
        channel_id: Optional[int],
        model: str,
        usage: UsageData,
        success: bool,
        latency_ms: Optional[int],
        client_ip: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        response_body: dict[str, Any] | None = None,
        capacity_snapshot: dict[str, str] | None = None,
        cost: UsageCost | None = None,
        resource_scopes: ResourceScopes | None = None,
    ) -> None:
        db.add(
            RequestLog(
                token_id=token_id,
                channel_id=channel_id,
                model=model,
                request_model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                uncached_input_tokens=usage.uncached_input_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_write_5m_tokens=usage.cache_write_5m_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_tokens,
                usage_schema_version=usage.usage_schema_version,
                capacity_snapshot=capacity_snapshot,
                cache_scope=(
                    resource_scopes.cache_scope if resource_scopes else None
                ),
                capacity_scope=(
                    resource_scopes.capacity_scope if resource_scopes else None
                ),
                billing_scope=(
                    resource_scopes.billing_scope if resource_scopes else None
                ),
                cost=(
                    cost.total_cost
                    if cost is not None and cost.status == "calculated"
                    else None
                ),
                currency=(cost.currency or "USD") if cost else "USD",
                cost_status=cost.status if cost else "not_applicable",
                tariff_version=cost.tariff_version if cost else None,
                tariff_period=cost.tariff_period if cost else None,
                tariff_snapshot=cost.snapshot if cost else None,
                success=success,
                error_code=error_code,
                error_message=error_message[:500] if error_message else None,
                response_body=response_body,
                latency=(latency_ms / 1000) if latency_ms is not None else None,
                ip=client_ip,
            )
        )
