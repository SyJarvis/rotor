from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mindagent.core.context import (
    AgentContext,
    AgentDecision,
    DecisionKind,
    Observation,
    ObservationBatch,
)

from .models import ContextPackResult, ContextRecord, ContextScope
from .packer import CacheAwarePacker
from .store import ContextStore


@dataclass
class ContextConfig:
    system_prompt: str | None = None
    max_tokens: int = 120000
    response_reserve_tokens: int = 4096
    safety_margin_tokens: int = 256
    warning_ratio: float = 0.70
    compact_trigger_ratio: float = 0.85
    compact_target_ratio: float = 0.60
    model: str = "gpt-4o"
    image_token_cost: int = 1024
    use_tiktoken: bool = False

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens 必须大于 0")
        if self.response_reserve_tokens < 0:
            raise ValueError("response_reserve_tokens 不能小于 0")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens 不能小于 0")
        if self.image_token_cost < 0:
            raise ValueError("image_token_cost 不能小于 0")
        if not (
            0 < self.warning_ratio
            < self.compact_trigger_ratio
            <= 1
        ):
            raise ValueError("warning/compact trigger 比例配置无效")
        if not 0 < self.compact_target_ratio < self.compact_trigger_ratio:
            raise ValueError("compact_target_ratio 必须小于触发比例")


@dataclass
class _RunContextState:
    store: ContextStore
    epoch: int = 0
    packed_once: bool = False
    dropped_signature: tuple[str, ...] = ()


class ContextManager:
    def __init__(self, config: ContextConfig | None = None):
        self.config = config or ContextConfig()
        self._encoder = (
            self._load_encoder(self.config.model)
            if self.config.use_tiktoken
            else None
        )
        self._runs: dict[str, _RunContextState] = {}

    async def build_context(self, context: AgentContext) -> None:
        if context.metadata.get("_context_built"):
            return

        state = self._state(context)
        self._ingest_existing_messages(state.store, context.messages)
        if self.config.system_prompt and not any(
            record.kind == "system"
            for record in state.store.records
        ):
            state.store.append(
                ContextRecord(
                    kind="system",
                    message={
                        "role": "system",
                        "content": self.config.system_prompt,
                    },
                    scope=ContextScope.GLOBAL,
                    required=True,
                    priority=100,
                    salience=1.0,
                    stable_prefix=True,
                )
            )

        task_state_message = self._build_task_state_message(context)
        if task_state_message is not None:
            state.store.append(
                ContextRecord(
                    kind="task_state",
                    message=task_state_message,
                    scope=ContextScope.TURN,
                    required=True,
                    priority=100,
                    salience=1.0,
                    group_id=f"task:{context.run_id}",
                )
            )

        checkpoint = context.metadata.get("_continuation_checkpoint")
        if checkpoint is not None:
            checkpoint_payload = (
                checkpoint.to_dict()
                if hasattr(checkpoint, "to_dict")
                else checkpoint
            )
            state.store.append(
                ContextRecord(
                    kind="continuation_checkpoint",
                    message={
                        "role": "system",
                        "content": json.dumps(
                            {
                                "type": "continuation_checkpoint",
                                "checkpoint": checkpoint_payload,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                    scope=ContextScope.TURN,
                    required=True,
                    priority=100,
                    salience=1.0,
                    group_id=f"checkpoint:{context.run_id}",
                )
            )

        state.store.append(
            ContextRecord(
                kind="user",
                message={
                    "role": "user",
                    "content": self._build_user_content(context),
                },
                scope=ContextScope.TURN,
                required=True,
                priority=100,
                salience=1.0,
                group_id=f"turn:{context.run_id}",
            )
        )
        context.metadata["_context_built"] = True
        await self.prepare_context(context)

    async def prepare_context(
        self,
        context: AgentContext,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextPackResult:
        state = self._state(context)
        result = self._packer().pack(
            state.store.bundles(),
            tools=tools or (),
            epoch=state.epoch,
            allow_compaction=not state.packed_once,
        )
        dropped_signature = tuple(sorted(result.dropped_record_ids))
        if (
            dropped_signature
            and dropped_signature != state.dropped_signature
            and not state.packed_once
        ):
            state.epoch += 1
            result.epoch = state.epoch
        state.dropped_signature = dropped_signature
        state.packed_once = True
        context.messages[:] = result.messages
        context.metadata["_context_pack"] = {
            "epoch": result.epoch,
            "prefix_hash": result.prefix_hash,
            "input_tokens": result.input_tokens,
            "available_tokens": result.available_tokens,
            "pressure": result.pressure.level.value,
            "pressure_ratio": result.pressure.ratio,
            "dropped_record_ids": result.dropped_record_ids,
        }
        return result

    async def record_decision(
        self,
        context: AgentContext,
        decision: AgentDecision,
    ) -> None:
        state = self._state(context)
        if decision.decision_kind == DecisionKind.COMPLETE:
            message = {
                "role": "assistant",
                "content": decision.final_answer or "",
            }
            group_id = f"turn:{context.run_id}"
        elif decision.decision_kind == DecisionKind.ASK_USER:
            request = decision.user_input_request
            if request is None:
                return
            message = {
                "role": "assistant",
                "content": request.question,
            }
            group_id = (
                f"user-request:{context.run_id}:{context.step_index}"
            )
        else:
            action_batch = decision.action_batch
            if action_batch is None:
                return
            call_ids = self._decision_call_ids(decision)
            group_id = self._batch_group_id(decision)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": action.name,
                            "arguments": action.arguments,
                        },
                    }
                    for action, call_id in zip(
                        action_batch.actions,
                        call_ids,
                    )
                ],
            }

        state.store.append(
            ContextRecord(
                kind="assistant",
                message=message,
                scope=ContextScope.ITERATION,
                required=False,
                priority=95,
                salience=1.0,
                group_id=group_id,
            )
        )
        context.messages.append(message)

    async def record_completion_feedback(
        self,
        context: AgentContext,
        reasons: tuple[str, ...],
    ) -> None:
        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "type": "completion_rejected",
                    "reasons": list(reasons),
                },
                ensure_ascii=False,
            ),
        }
        self._state(context).store.append(
            ContextRecord(
                kind="completion_feedback",
                message=message,
                scope=ContextScope.ITERATION,
                required=False,
                priority=100,
                salience=1.0,
                group_id=(
                    f"completion:{context.run_id}:{context.step_index}"
                ),
            )
        )
        context.messages.append(message)

    async def update_after_observation_batch(
        self,
        context: AgentContext,
        observation_batch: ObservationBatch,
    ) -> None:
        decision = context.current_decision
        if decision is not None and decision.action_batch is not None:
            if observation_batch.batch_id != decision.action_batch.batch_id:
                raise ValueError(
                    "ObservationBatch 与 ActionBatch 的 batch_id 不一致"
                )
            expected_ids = [
                action.action_id
                for action in decision.action_batch.actions
            ]
            actual_ids = [
                observation.action.action_id
                for observation in observation_batch.observations
            ]
            if actual_ids != expected_ids:
                raise ValueError(
                    "ObservationBatch 与 ActionBatch 的 action 顺序不一致"
                )
        call_ids = (
            self._decision_call_ids(decision)
            if decision is not None
            else [
                observation.action.action_id
                for observation in observation_batch.observations
            ]
        )
        group_id = (
            self._batch_group_id(decision)
            if decision is not None
            else f"tool-batch:{observation_batch.batch_id}"
        )
        for observation, call_id in zip(
            observation_batch.observations,
            call_ids,
        ):
            self._append_observation(
                context,
                observation,
                call_id=call_id,
                group_id=group_id,
            )

    async def record_user_input(
        self,
        context: AgentContext,
        user_input: str,
    ) -> None:
        message = {"role": "user", "content": user_input}
        self._state(context).store.append(
            ContextRecord(
                kind="user",
                message=message,
                scope=ContextScope.TURN,
                required=False,
                priority=100,
                salience=1.0,
                group_id=f"user-response:{context.run_id}:{context.step_index}",
            )
        )
        context.messages.append(message)

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            total += 4
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") in {"image_url", "input_image"}:
                        total += self.config.image_token_cost
                    else:
                        total += self._count_text(
                            json.dumps(item, ensure_ascii=False)
                        )
            else:
                total += self._count_text(
                    json.dumps(message, ensure_ascii=False, default=str)
                )
        return total

    def trim(self, context: AgentContext) -> None:
        store = ContextStore()
        self._ingest_existing_messages(store, context.messages)
        bundles = store.bundles()
        stable = [bundle for bundle in bundles if bundle.stable_prefix]
        optional = [bundle for bundle in bundles if not bundle.stable_prefix]
        selected = stable + (optional[-1:] if optional else [])
        context.messages[:] = [
            record.message
            for bundle in selected
            for record in bundle.records
        ]

    def get_store(self, run_id: str) -> ContextStore | None:
        state = self._runs.get(run_id)
        return state.store if state else None

    def release_context(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    def _state(self, context: AgentContext) -> _RunContextState:
        return self._runs.setdefault(
            context.run_id,
            _RunContextState(store=ContextStore()),
        )

    def _packer(self) -> CacheAwarePacker:
        reserve = min(
            self.config.response_reserve_tokens,
            max(0, self.config.max_tokens // 4),
        )
        margin = min(
            self.config.safety_margin_tokens,
            max(0, self.config.max_tokens // 10),
        )
        return CacheAwarePacker(
            self.estimate_tokens,
            max_tokens=self.config.max_tokens,
            response_reserve=reserve,
            safety_margin=margin,
            warning_ratio=self.config.warning_ratio,
            compact_trigger_ratio=self.config.compact_trigger_ratio,
            compact_target_ratio=self.config.compact_target_ratio,
        )

    def _ingest_existing_messages(
        self,
        store: ContextStore,
        messages: list[dict[str, Any]],
    ) -> None:
        turn_index = 0
        current_turn = "history:0"
        tool_call_groups: dict[str, str] = {}
        for message in messages:
            role = message.get("role", "unknown")
            if role == "system":
                group_id = None
            elif role == "user":
                turn_index += 1
                current_turn = f"history:{turn_index}"
                group_id = current_turn
            elif role == "assistant" and message.get("tool_calls"):
                calls = message["tool_calls"]
                call_ids = [
                    call.get("id")
                    for call in calls
                    if call.get("id")
                ]
                if len(call_ids) == 1:
                    group_id = f"tool:{call_ids[0]}"
                elif call_ids:
                    group_id = f"tool-batch:{call_ids[0]}"
                else:
                    group_id = f"tool:{current_turn}"
                for call_id in call_ids:
                    tool_call_groups[call_id] = group_id
            elif role == "tool":
                call_id = message.get("tool_call_id")
                group_id = tool_call_groups.get(
                    call_id,
                    f"tool:{call_id or current_turn}",
                )
            else:
                group_id = current_turn

            store.append(
                ContextRecord(
                    kind=role,
                    message=message,
                    scope=(
                        ContextScope.GLOBAL
                        if role == "system"
                        else ContextScope.SESSION
                    ),
                    required=role == "system",
                    priority=100 if role == "system" else 50,
                    salience=1.0 if role == "system" else 0.5,
                    group_id=group_id,
                    stable_prefix=role == "system",
                )
            )

    @staticmethod
    def _decision_call_ids(decision: AgentDecision) -> list[str]:
        if decision.action_batch is None:
            return []
        return [
            action.action_id
            for action in decision.action_batch.actions
        ]

    @classmethod
    def _batch_group_id(cls, decision: AgentDecision) -> str:
        if decision.action_batch is None:
            return "tool:unknown"
        call_ids = cls._decision_call_ids(decision)
        if len(call_ids) == 1:
            return f"tool:{call_ids[0]}"
        return f"tool-batch:{decision.action_batch.batch_id}"

    def _append_observation(
        self,
        context: AgentContext,
        observation: Observation,
        *,
        call_id: str,
        group_id: str,
    ) -> None:
        message: dict[str, Any] = {
            "role": "tool",
            "name": observation.action.name,
            "tool_call_id": call_id,
            "content": json.dumps(
                {
                    "ok": observation.ok,
                    "result": observation.result,
                    "evidence": (
                        observation.evidence.to_dict(
                            include_content=False
                        )
                        if observation.evidence is not None
                        else None
                    ),
                    "error": observation.error,
                    "error_info": (
                        observation.error_info.to_dict()
                        if observation.error_info
                        else None
                    ),
                },
                ensure_ascii=False,
                default=str,
            ),
        }
        self._state(context).store.append(
            ContextRecord(
                kind="tool",
                message=message,
                scope=ContextScope.ITERATION,
                required=False,
                priority=95,
                salience=1.0,
                group_id=group_id,
            )
        )
        context.messages.append(message)

    def _build_user_content(self, context: AgentContext) -> Any:
        images = context.artifacts.get("images") or []
        if not images:
            return context.user_input

        content: list[dict[str, Any]] = [
            {"type": "text", "text": context.user_input}
        ]
        for image in images:
            if isinstance(image, str):
                image_url = {"url": image}
            elif isinstance(image, dict) and "url" in image:
                image_url = {
                    key: value
                    for key, value in image.items()
                    if key in {"url", "detail"}
                }
            else:
                raise ValueError(
                    "images 仅支持 URL/data URL 字符串或包含 url 的字典"
                )
            content.append({"type": "image_url", "image_url": image_url})
        return content

    @staticmethod
    def _build_task_state_message(
        context: AgentContext,
    ) -> dict[str, Any] | None:
        task_state = context.task_state
        if task_state is None or (
            task_state.objective == context.user_input
            and not task_state.constraints
            and not task_state.acceptance_criteria
        ):
            return None
        return {
            "role": "system",
            "content": json.dumps(
                {
                    "type": "task_state",
                    "objective": task_state.objective,
                    "constraints": [
                        {
                            "id": item.constraint_id,
                            "description": item.description,
                        }
                        for item in task_state.constraints
                    ],
                    "acceptance_criteria": [
                        {
                            "id": item.criterion_id,
                            "description": item.description,
                            "required": item.required,
                        }
                        for item in task_state.acceptance_criteria
                    ],
                    "evidence_reference_format": [
                        "evidence:<batch_id>:<action_id>",
                    ],
                },
                ensure_ascii=False,
            ),
        }

    def _count_text(self, text: str) -> int:
        if self._encoder is not None:
            return len(self._encoder.encode(text))
        return max(1, len(text) // 4)

    @staticmethod
    def _load_encoder(model: str):
        try:
            import tiktoken

            try:
                return tiktoken.encoding_for_model(model)
            except KeyError:
                return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None
