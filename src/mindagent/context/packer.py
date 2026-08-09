from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from .models import (
    ContextBundle,
    ContextPackResult,
    ContextPressure,
    ContextPressureLevel,
)


TokenCounter = Callable[[list[dict[str, Any]]], int]


class ContextOverflowError(RuntimeError):
    pass


class CacheAwarePacker:
    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        max_tokens: int,
        response_reserve: int,
        safety_margin: int,
        warning_ratio: float,
        compact_trigger_ratio: float,
        compact_target_ratio: float,
    ):
        self.token_counter = token_counter
        self.max_tokens = max_tokens
        self.response_reserve = response_reserve
        self.safety_margin = safety_margin
        self.warning_ratio = warning_ratio
        self.compact_trigger_ratio = compact_trigger_ratio
        self.compact_target_ratio = compact_target_ratio

    def pack(
        self,
        bundles: Sequence[ContextBundle],
        *,
        tools: Sequence[dict[str, Any]] = (),
        epoch: int = 0,
        allow_compaction: bool = True,
    ) -> ContextPackResult:
        available = (
            self.max_tokens
            - self.response_reserve
            - self.safety_margin
        )
        if available < 1:
            raise ValueError("Context 可用输入预算必须大于 0")

        total = sum(self._bundle_tokens(bundle) for bundle in bundles)
        ratio = total / available
        level = self._pressure_level(ratio)
        pressure = ContextPressure(level, total, available, ratio)
        prefix_hash = self._prefix_hash(bundles, tools)

        if ratio < self.compact_trigger_ratio or not allow_compaction:
            selected = list(bundles)
            if total > available:
                selected = self._select(bundles, available)
        else:
            target = max(
                self._required_tokens(bundles),
                int(available * self.compact_target_ratio),
            )
            selected = self._select(bundles, min(target, available))

        messages = [
            record.message
            for bundle in selected
            for record in bundle.records
        ]
        input_tokens = self.token_counter(messages)
        if input_tokens > available:
            raise ContextOverflowError(
                "required context 超过模型可用输入预算"
            )

        selected_ids = {
            record.record_id
            for bundle in selected
            for record in bundle.records
        }
        selected_record_ids = [
            record.record_id
            for bundle in bundles
            for record in bundle.records
            if record.record_id in selected_ids
        ]
        dropped_ids = [
            record.record_id
            for bundle in bundles
            for record in bundle.records
            if record.record_id not in selected_ids
        ]
        decisions = [
            {
                "bundle_id": bundle.bundle_id,
                "action": (
                    "kept"
                    if all(
                        record.record_id in selected_ids
                        for record in bundle.records
                    )
                    else "dropped"
                ),
                "reason": (
                    "required_or_selected"
                    if any(
                        record.record_id in selected_ids
                        for record in bundle.records
                    )
                    else "context_pressure"
                ),
            }
            for bundle in bundles
        ]
        return ContextPackResult(
            messages=messages,
            input_tokens=input_tokens,
            available_tokens=available,
            pressure=pressure,
            prefix_hash=prefix_hash,
            epoch=epoch,
            selected_record_ids=selected_record_ids,
            dropped_record_ids=dropped_ids,
            decisions=decisions,
        )

    def _select(
        self,
        bundles: Sequence[ContextBundle],
        budget: int,
    ) -> list[ContextBundle]:
        selected_ids: set[str] = set()
        used = 0

        for bundle in bundles:
            if not (bundle.stable_prefix or bundle.required):
                continue
            cost = self._bundle_tokens(bundle)
            selected_ids.add(bundle.bundle_id)
            used += cost

        optional = [
            (index, bundle)
            for index, bundle in enumerate(bundles)
            if bundle.bundle_id not in selected_ids
        ]
        optional.sort(
            key=lambda item: (
                -item[1].priority,
                -item[1].salience,
                -item[0],
            )
        )
        for _, bundle in optional:
            cost = self._bundle_tokens(bundle)
            if used + cost <= budget:
                selected_ids.add(bundle.bundle_id)
                used += cost

        return [
            bundle
            for bundle in bundles
            if bundle.bundle_id in selected_ids
        ]

    def _bundle_tokens(self, bundle: ContextBundle) -> int:
        return self.token_counter(
            [record.message for record in bundle.records]
        )

    def _required_tokens(
        self,
        bundles: Sequence[ContextBundle],
    ) -> int:
        return sum(
            self._bundle_tokens(bundle)
            for bundle in bundles
            if bundle.stable_prefix or bundle.required
        )

    def _pressure_level(self, ratio: float) -> ContextPressureLevel:
        if ratio >= self.compact_trigger_ratio:
            return ContextPressureLevel.CRITICAL
        if ratio >= self.warning_ratio:
            return ContextPressureLevel.WARNING
        return ContextPressureLevel.NORMAL

    @staticmethod
    def _prefix_hash(
        bundles: Sequence[ContextBundle],
        tools: Sequence[dict[str, Any]],
    ) -> str:
        prefix = [
            record.message
            for bundle in bundles
            if bundle.stable_prefix
            for record in bundle.records
        ]
        payload = json.dumps(
            {"messages": prefix, "tools": list(tools)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
