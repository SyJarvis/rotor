"""Admin monitoring of Coding Agent requests and stable sessions."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.application_settings import application_settings
from rotor.core.client_session import SESSION_SOURCE_TO_CLIENT
from rotor.database import get_db
from rotor.models.conversation import ConversationRecord
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLease
from rotor.models.usage import UsageLedger


router = APIRouter(prefix="/monitoring", tags=["monitoring"])

_AGENTS = (
    ("codex", "Codex"),
    ("claude_code", "Claude Code"),
    ("opencode", "OpenCode"),
    ("pi", "Pi"),
    ("grok_build", "Grok Build"),
    ("mindcode", "MindCode"),
)
_SUMMARY_LIMIT = 180
SessionKey = tuple[str, int, str]
RequestKey = tuple[int, str]


class MonitoringSession(BaseModel):
    session_id: str
    token_id: int
    summary: str | None
    turn_count: int
    total_tokens: int
    models: list[str]
    first_seen_at: datetime
    last_seen_at: datetime
    active: bool


class MonitoringAgent(BaseModel):
    id: str
    name: str
    request_count: int
    ungrouped_request_count: int
    session_count: int
    active_session_count: int
    total_tokens: int
    turn_count: int
    sessions: list[MonitoringSession]


class MonitoringSourcesResponse(BaseModel):
    date: date
    timezone: str
    generated_at: datetime
    agents: list[MonitoringAgent]


def _today_window(
    generated_at: datetime,
    timezone_name: str,
) -> tuple[date, datetime, datetime]:
    display_timezone = ZoneInfo(timezone_name)
    selected = generated_at.astimezone(display_timezone).date()

    def utc_naive(day: date) -> datetime:
        return datetime.combine(day, time.min, display_timezone).astimezone(
            timezone.utc
        ).replace(tzinfo=None)

    return selected, utc_naive(selected), utc_naive(selected + timedelta(days=1))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/sources", response_model=MonitoringSourcesResponse)
async def get_monitoring_sources(
    db: AsyncSession = Depends(get_db),
) -> MonitoringSourcesResponse:
    generated_at = _utc_now()
    timezone_name = application_settings.get().display_timezone
    selected_date, start_time, end_time = _today_window(
        generated_at,
        timezone_name,
    )

    routing_rows = (await db.execute(
        select(
            RoutingDecisionRecord.request_id,
            RoutingDecisionRecord.token_id,
            RoutingDecisionRecord.feature_snapshot,
        )
        .where(
            RoutingDecisionRecord.created_at >= start_time,
            RoutingDecisionRecord.created_at < end_time,
        )
        .order_by(RoutingDecisionRecord.id)
    )).all()
    agent_ids = {agent_id for agent_id, _ in _AGENTS}
    source_by_request: dict[RequestKey, str] = {}
    session_requests: set[RequestKey] = set()
    for row in routing_rows:
        features = row.feature_snapshot or {}
        source = features.get("session_source")
        agent_id = features.get("client_source")
        if agent_id not in agent_ids:
            agent_id = SESSION_SOURCE_TO_CLIENT.get(source)
        if agent_id is not None and row.token_id is not None:
            request_key = (row.token_id, row.request_id)
            source_by_request[request_key] = agent_id
            if source:
                session_requests.add(request_key)

    usage_rows = (await db.execute(
        select(
            UsageLedger.request_id,
            UsageLedger.conversation_id,
            UsageLedger.token_id,
            UsageLedger.model,
            UsageLedger.total_tokens,
            UsageLedger.created_at,
        ).where(
            UsageLedger.created_at >= start_time,
            UsageLedger.created_at < end_time,
            UsageLedger.token_id.is_not(None),
        )
    )).all()

    tokens_by_agent = dict.fromkeys(agent_ids, 0)
    sessions: dict[SessionKey, dict] = {}
    earliest_requests: dict[SessionKey, tuple[datetime, str]] = {}
    for row in usage_rows:
        request_key = (row.token_id, row.request_id)
        agent_id = source_by_request.get(request_key)
        if agent_id is None:
            continue
        tokens_by_agent[agent_id] += int(row.total_tokens or 0)
        if request_key not in session_requests or not row.conversation_id:
            continue
        session_key = (agent_id, row.token_id, row.conversation_id)
        activity_at = _as_utc(row.created_at)
        session = sessions.setdefault(session_key, {
            "agent_id": agent_id,
            "session_id": row.conversation_id,
            "token_id": row.token_id,
            "request_ids": set(),
            "total_tokens": 0,
            "models": set(),
            "first_seen_at": activity_at,
            "last_seen_at": activity_at,
        })
        session["request_ids"].add(row.request_id)
        session["total_tokens"] += int(row.total_tokens or 0)
        session["models"].add(row.model)
        session["first_seen_at"] = min(session["first_seen_at"], activity_at)
        session["last_seen_at"] = max(session["last_seen_at"], activity_at)
        request_candidate = (activity_at, row.request_id)
        previous_request = earliest_requests.get(session_key)
        if previous_request is None or request_candidate < previous_request:
            earliest_requests[session_key] = request_candidate

    request_targets: dict[RequestKey, list[SessionKey]] = {}
    for session_key, (_, request_id) in earliest_requests.items():
        request_key = (session_key[1], request_id)
        request_targets.setdefault(request_key, []).append(session_key)

    active_keys = {
        (row.token_id, row.session_id)
        for row in (await db.execute(
            select(SessionLease.token_id, SessionLease.session_id).where(
                SessionLease.expires_at > generated_at.replace(tzinfo=None)
            )
        )).all()
    }
    summaries = await _load_summaries(
        db,
        request_targets,
        start_time,
        end_time,
    )

    agents: list[MonitoringAgent] = []
    for agent_id, agent_name in _AGENTS:
        agent_sessions = [
            MonitoringSession(
                session_id=session["session_id"],
                token_id=session["token_id"],
                summary=summaries.get(session_key),
                turn_count=len(session["request_ids"]),
                total_tokens=session["total_tokens"],
                models=sorted(session["models"]),
                first_seen_at=session["first_seen_at"],
                last_seen_at=session["last_seen_at"],
                active=(session_key[1], session_key[2]) in active_keys,
            )
            for session_key, session in sessions.items()
            if session["agent_id"] == agent_id
        ]
        agent_sessions.sort(
            key=lambda item: (not item.active, -item.last_seen_at.timestamp())
        )
        agents.append(MonitoringAgent(
            id=agent_id,
            name=agent_name,
            request_count=sum(
                source == agent_id for source in source_by_request.values()
            ),
            ungrouped_request_count=sum(
                source == agent_id and key not in session_requests
                for key, source in source_by_request.items()
            ),
            session_count=len(agent_sessions),
            active_session_count=sum(item.active for item in agent_sessions),
            total_tokens=tokens_by_agent[agent_id],
            turn_count=sum(item.turn_count for item in agent_sessions),
            sessions=agent_sessions,
        ))

    return MonitoringSourcesResponse(
        date=selected_date,
        timezone=timezone_name,
        generated_at=generated_at,
        agents=agents,
    )


async def _load_summaries(
    db: AsyncSession,
    request_targets: dict[RequestKey, list[SessionKey]],
    start_time: datetime,
    end_time: datetime,
) -> dict[SessionKey, str]:
    if not request_targets:
        return {}
    records = (await db.execute(
        select(
            ConversationRecord.file_path,
            ConversationRecord.request_id,
            ConversationRecord.token_id,
        ).where(
            ConversationRecord.created_at >= start_time,
            ConversationRecord.created_at < end_time,
        )
    )).all()
    path_targets: dict[
        str,
        dict[str, list[SessionKey]],
    ] = {}
    for record in records:
        if record.token_id is None:
            continue
        target = request_targets.get((record.token_id, record.request_id))
        if target is None:
            continue
        path_targets.setdefault(record.file_path, {}).setdefault(
            record.request_id,
            [],
        ).extend(target)
    return await asyncio.to_thread(_read_summary_files, path_targets)


def _read_summary_files(
    path_targets: dict[
        str,
        dict[str, list[SessionKey]],
    ],
) -> dict[SessionKey, str]:
    summaries: dict[SessionKey, str] = {}
    for file_path, targets_by_request in path_targets.items():
        unresolved = set(targets_by_request)
        try:
            with Path(file_path).open(encoding="utf-8") as lines:
                for line in lines:
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    request_id = payload.get("request_id")
                    if request_id not in unresolved:
                        continue
                    summary = _first_user_text(payload.get("messages"))
                    if summary is None:
                        continue
                    for session_key in targets_by_request[request_id]:
                        summaries[session_key] = summary
                    unresolved.remove(request_id)
                    if not unresolved:
                        break
        except (OSError, UnicodeError):
            continue
    return summaries


def _first_user_text(messages) -> str | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in {
                    "text", "input_text",
                } and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if text:
            return text[:_SUMMARY_LIMIT]
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.get("/performance")
async def get_performance():
    from rotor.api.v1.chat import conversation_store
    from rotor.observability import performance_metrics

    return {**performance_metrics.snapshot(), "store": conversation_store.status()}


@router.get("/reconciliation")
async def get_reconciliation():
    from rotor.observability import reconciliation_monitor

    return await reconciliation_monitor.check()


@router.post("/reconciliation/reset")
async def reset_reconciliation():
    from rotor.observability import reconciliation_monitor

    return await reconciliation_monitor.reset()
