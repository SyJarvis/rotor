import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.channel import Channel
from rotor.schemas.control import (
    ChannelSecretState,
    ControlChannel,
)


@dataclass(frozen=True)
class ChannelPage:
    items: list[ControlChannel]
    next_cursor: str | None
    has_more: bool


async def list_control_channels(
    db: AsyncSession,
    *,
    enabled: bool | None = None,
    model: str | None = None,
    protocol: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> ChannelPage:
    filter_key = _filter_key(
        enabled=enabled,
        model=model,
        protocol=protocol,
    )
    offset = (
        _decode_cursor(cursor, expected_filter_key=filter_key)
        if cursor is not None
        else 0
    )
    channels = list(
        (
            await db.scalars(
                select(Channel).order_by(
                    Channel.priority.desc(),
                    Channel.id,
                )
            )
        ).all()
    )
    filtered = [
        channel
        for channel in channels
        if _matches(
            channel,
            enabled=enabled,
            model=model,
            protocol=protocol,
        )
    ]
    page_channels = filtered[offset:offset + limit + 1]
    has_more = len(page_channels) > limit
    items = [
        _control_channel(channel)
        for channel in page_channels[:limit]
    ]
    next_cursor = (
        _encode_cursor(offset=offset + limit, filter_key=filter_key)
        if has_more
        else None
    )
    return ChannelPage(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def get_control_channel(
    db: AsyncSession,
    channel_id: int,
) -> ControlChannel | None:
    channel = await db.scalar(
        select(Channel).where(Channel.id == channel_id)
    )
    return _control_channel(channel) if channel is not None else None


def _matches(
    channel: Channel,
    *,
    enabled: bool | None,
    model: str | None,
    protocol: str | None,
) -> bool:
    models = channel.models if isinstance(channel.models, list) else []
    return (
        (enabled is None or channel.enabled is enabled)
        and (model is None or model in models)
        and (protocol is None or channel.protocol == protocol)
    )


def _control_channel(channel: Channel) -> ControlChannel:
    models = channel.models if isinstance(channel.models, list) else []
    mapping = (
        channel.model_mapping
        if isinstance(channel.model_mapping, dict)
        else {}
    )
    return ControlChannel(
        id=channel.id,
        name=channel.name,
        type=channel.type,
        base_url_origin=_base_url_origin(channel.base_url),
        protocol=channel.protocol,
        models=[
            value
            for value in models
            if isinstance(value, str)
        ],
        model_mapping={
            key: value
            for key, value in mapping.items()
            if isinstance(key, str) and isinstance(value, str)
        },
        priority=channel.priority,
        weight=channel.weight,
        enabled=channel.enabled,
        test_only=channel.test_only,
        secret_state=ChannelSecretState(has_key=bool(channel.key)),
        updated_at=channel.updated_at,
    )


def _base_url_origin(base_url: str) -> str | None:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    port_suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{port_suffix}"


def _filter_key(**filters: object) -> str:
    encoded = json.dumps(
        filters,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(*, offset: int, filter_key: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "offset": offset,
            "filter_key": filter_key,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    expected_filter_key: str,
) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + padding)
        )
        if payload.get("v") != 1:
            raise ValueError
        offset = payload["offset"]
        filter_key = payload["filter_key"]
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or filter_key != expected_filter_key
        ):
            raise ValueError
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            "cursor is invalid or does not match the channel filters"
        ) from exc
    return offset
