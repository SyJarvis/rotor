from __future__ import annotations

import copy

from .models import ContextBundle, ContextRecord


class ContextStore:
    def __init__(self):
        self._records: list[ContextRecord] = []

    @property
    def records(self) -> tuple[ContextRecord, ...]:
        return tuple(self._records)

    def append(self, record: ContextRecord) -> None:
        self._records.append(
            ContextRecord(
                kind=record.kind,
                message=copy.deepcopy(record.message),
                scope=record.scope,
                required=record.required,
                priority=record.priority,
                salience=record.salience,
                group_id=record.group_id,
                stable_prefix=record.stable_prefix,
                record_id=record.record_id,
            )
        )

    def bundles(self) -> list[ContextBundle]:
        grouped: dict[str, list[ContextRecord]] = {}
        order: list[str] = []
        for record in self._records:
            bundle_id = record.group_id or record.record_id
            if bundle_id not in grouped:
                grouped[bundle_id] = []
                order.append(bundle_id)
            grouped[bundle_id].append(record)

        return [
            ContextBundle(
                bundle_id=bundle_id,
                records=tuple(grouped[bundle_id]),
                required=any(item.required for item in grouped[bundle_id]),
                priority=max(item.priority for item in grouped[bundle_id]),
                salience=max(item.salience for item in grouped[bundle_id]),
                stable_prefix=all(
                    item.stable_prefix for item in grouped[bundle_id]
                ),
            )
            for bundle_id in order
        ]
