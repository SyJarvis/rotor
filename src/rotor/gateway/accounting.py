from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


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
    """Write request logs, usage ledger rows, and cached counters."""

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
        self._update_cached_counters(token, channel, usage, success=True)
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
    ) -> None:
        if channel:
            channel.total_requests += 1
            channel.failed_requests += 1

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

    def extract_usage(self, response_data: dict) -> UsageData:
        usage = response_data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)

        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}

        return UsageData(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            usage_source="provider" if usage else "missing",
        )

    def _update_cached_counters(
        self,
        token: Token,
        channel: Channel,
        usage: UsageData,
        *,
        success: bool,
    ) -> None:
        token.request_count += 1
        token.token_count += usage.total_tokens
        token.used_quota += usage.total_tokens

        if token.quota is not None and token.used_quota >= token.quota:
            token.enabled = False

        channel.total_requests += 1
        if success:
            channel.success_requests += 1
        else:
            channel.failed_requests += 1

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
                latency=(latency_ms / 1000) if latency_ms is not None else None,
                ip=client_ip,
            )
        )
