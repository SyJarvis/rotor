from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.usage import UsageLedger
from rotor.schemas.control import ModelUsageSummary


@dataclass(frozen=True)
class ModelUsageResult:
    items: list[ModelUsageSummary]
    start_time: datetime
    end_time: datetime


async def list_model_usage(
    db: AsyncSession,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 50,
) -> ModelUsageResult:
    effective_end = _as_utc(end_time) if end_time else datetime.now(
        timezone.utc
    )
    effective_start = (
        _as_utc(start_time)
        if start_time
        else effective_end - timedelta(hours=1)
    )
    _validate_window(effective_start, effective_end)

    total_tokens = func.sum(UsageLedger.total_tokens)
    query = (
        select(
            UsageLedger.model,
            func.count(distinct(UsageLedger.request_id)).label(
                "request_count"
            ),
            func.count(UsageLedger.id).label("ledger_count"),
            func.sum(UsageLedger.prompt_tokens).label("prompt_tokens"),
            func.sum(UsageLedger.completion_tokens).label(
                "completion_tokens"
            ),
            total_tokens.label("total_tokens"),
            func.sum(UsageLedger.uncached_input_tokens).label(
                "uncached_input_tokens"
            ),
            func.sum(UsageLedger.cached_tokens).label("cached_tokens"),
            func.sum(UsageLedger.cache_write_tokens).label(
                "cache_write_tokens"
            ),
            func.sum(UsageLedger.cache_write_5m_tokens).label(
                "cache_write_5m_tokens"
            ),
            func.sum(UsageLedger.cache_write_1h_tokens).label(
                "cache_write_1h_tokens"
            ),
            func.sum(case(
                (UsageLedger.usage_schema_version == "2", 1),
                else_=0,
            )).label("usage_v2_ledger_count"),
            func.sum(UsageLedger.reasoning_tokens).label(
                "reasoning_tokens"
            ),
            func.sum(UsageLedger.input_audio_tokens).label(
                "input_audio_tokens"
            ),
            func.sum(UsageLedger.output_audio_tokens).label(
                "output_audio_tokens"
            ),
        )
        .where(
            UsageLedger.created_at >= effective_start,
            UsageLedger.created_at < effective_end,
        )
        .group_by(UsageLedger.model)
        .order_by(total_tokens.desc(), UsageLedger.model)
        .limit(limit)
    )
    rows = (await db.execute(query)).all()
    selected_models = [row.model for row in rows]
    cost_rows = []
    if selected_models:
        cost_rows = (await db.execute(
            select(
                UsageLedger.model,
                UsageLedger.currency,
                func.sum(UsageLedger.total_cost).label("total_cost"),
                func.count(UsageLedger.id).label("ledger_count"),
            )
            .where(
                UsageLedger.created_at >= effective_start,
                UsageLedger.created_at < effective_end,
                UsageLedger.model.in_(selected_models),
                UsageLedger.cost_status == "calculated",
            )
            .group_by(UsageLedger.model, UsageLedger.currency)
        )).all()
    cost_totals_by_model: dict[str, dict[str, float]] = {
        model: {} for model in selected_models
    }
    costed_ledgers_by_model = {model: 0 for model in selected_models}
    for cost_row in cost_rows:
        if cost_row.currency:
            cost_totals_by_model[cost_row.model][cost_row.currency] = float(
                cost_row.total_cost or 0.0
            )
        costed_ledgers_by_model[cost_row.model] += int(
            cost_row.ledger_count or 0
        )
    return ModelUsageResult(
        items=[
            ModelUsageSummary(
                model=row.model,
                request_count=row.request_count,
                ledger_count=row.ledger_count,
                prompt_tokens=row.prompt_tokens or 0,
                completion_tokens=row.completion_tokens or 0,
                total_tokens=row.total_tokens or 0,
                uncached_input_tokens=row.uncached_input_tokens or 0,
                cached_tokens=row.cached_tokens or 0,
                cache_write_tokens=row.cache_write_tokens or 0,
                cache_write_5m_tokens=row.cache_write_5m_tokens or 0,
                cache_write_1h_tokens=row.cache_write_1h_tokens or 0,
                usage_v2_ledger_count=row.usage_v2_ledger_count or 0,
                costed_ledger_count=costed_ledgers_by_model[row.model],
                cost_totals_by_currency=cost_totals_by_model[row.model],
                reasoning_tokens=row.reasoning_tokens or 0,
                input_audio_tokens=row.input_audio_tokens or 0,
                output_audio_tokens=row.output_audio_tokens or 0,
            )
            for row in rows
        ],
        start_time=effective_start,
        end_time=effective_end,
    )


def _validate_window(start_time: datetime, end_time: datetime) -> None:
    if start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")
    if end_time - start_time > timedelta(days=7):
        raise ValueError("model usage query window must not exceed 7 days")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
