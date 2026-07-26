from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger
from rotor.models.routing_decision import RoutingDecisionRecord


@dataclass(slots=True)
class UsageData:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0
    usage_source: str = "provider"


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
            feature_snapshot=features or {},
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
    ) -> None:
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
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                input_audio_tokens=usage.input_audio_tokens,
                output_audio_tokens=usage.output_audio_tokens,
                usage_source=usage.usage_source,
                status="success",
                latency_ms=latency_ms,
            )
        )
        # Keep online policy updates on the accounting boundary so every
        # supported protocol feeds the same learning signal.
        from rotor.gateway.routing import routing_engine

        routing_engine.observe_result(
            model,
            channel,
            success=True,
            latency_ms=latency_ms,
            # Pricing is not implemented yet. Unknown cost must remain
            # neutral instead of teaching the policy that every request is free.
            cost=None,
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
    ) -> None:
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
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)

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

        return UsageData(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
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
    ) -> UsageData:
        if not has_provider_usage:
            return UsageData(usage_source="missing")
        return self.extract_usage({
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        })

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
                success=success,
                error_code=error_code,
                error_message=error_message[:500] if error_message else None,
                response_body=response_body,
                latency=(latency_ms / 1000) if latency_ms is not None else None,
                ip=client_ip,
            )
        )
