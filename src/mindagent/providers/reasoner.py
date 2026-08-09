from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from mindagent.core import (
    ActionBatch,
    ActionRequest,
    ActionRisk,
    ActionType,
    AgentContext,
    AgentDecision,
    CompletionClaim,
    DecisionKind,
    UserInputRequest,
)

from .base import ProviderResponse, ProviderToolCall
from .router import ProviderRouter


TextDeltaHandler = Callable[[AgentContext, str], Awaitable[None]]


class ProviderReasoner:
    ASK_USER_TOOL = "mindagent_ask_user"
    COMPLETE_TOOL = "mindagent_complete"

    def __init__(
        self,
        router: ProviderRouter,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        action_types: Mapping[str, ActionType] | None = None,
        action_risks: Mapping[str, ActionRisk] | None = None,
        action_risk_resolver: Callable[
            [str, dict[str, Any], AgentContext],
            ActionRisk,
        ]
        | None = None,
        approval_required: set[str] | None = None,
        stream: bool = False,
        parallel_tool_calls: bool = True,
        enable_control_decisions: bool = True,
    ):
        self.router = router
        self.tools = list(tools or ())
        self.enable_control_decisions = enable_control_decisions
        if enable_control_decisions:
            reserved = {self.ASK_USER_TOOL, self.COMPLETE_TOOL}
            tool_names = {
                tool.get("function", {}).get("name")
                for tool in self.tools
            }
            collisions = reserved & tool_names
            if collisions:
                raise ValueError(
                    "工具名与 MindAgent 控制决策冲突: "
                    + ", ".join(sorted(collisions))
                )
            self.tools.extend(self._control_tool_schemas())
        self.action_risks = dict(action_risks or {})
        self.action_types = dict(action_types or {})
        if any(
            not isinstance(action_type, ActionType)
            for action_type in self.action_types.values()
        ):
            raise ValueError("action_types 的值必须是 ActionType")
        self.action_risk_resolver = action_risk_resolver
        self.approval_required = set(approval_required or ())
        self.stream = stream
        self.parallel_tool_calls = parallel_tool_calls
        self._text_delta_handlers: list[TextDeltaHandler] = []

    def add_text_delta_handler(self, handler: TextDeltaHandler) -> None:
        if handler not in self._text_delta_handlers:
            self._text_delta_handlers.append(handler)

    async def close(self) -> None:
        await self.router.close()

    async def decide(self, context: AgentContext) -> AgentDecision:
        provider = self.router.select(
            provider_name=context.metadata.get("provider"),
            vision=bool(context.artifacts.get("images")),
            tool_calling=bool(self.tools),
        )
        request = {
            "tools": self.tools or None,
            "parallel_tool_calls": (
                self.parallel_tool_calls if self.tools else None
            ),
        }
        if self.stream and provider.capabilities.streaming:
            response = await self._stream_response(
                provider,
                context,
                request,
            )
        else:
            response = await provider.chat(context.messages, **request)
        return self._to_decision(context, response)

    async def _stream_response(
        self,
        provider: Any,
        context: AgentContext,
        request: dict[str, Any],
    ) -> ProviderResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        model = None
        finish_reason = None
        usage: dict[str, int] = {}

        async for chunk in provider.stream_chat(
            context.messages,
            **request,
        ):
            if chunk.content_delta:
                content_parts.append(chunk.content_delta)
                for handler in self._text_delta_handlers:
                    await handler(context, chunk.content_delta)
            if chunk.reasoning_delta:
                reasoning_parts.append(chunk.reasoning_delta)
            for delta in chunk.tool_call_deltas:
                call = calls.setdefault(
                    delta.index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if delta.id:
                    call["id"] = delta.id
                if delta.name:
                    call["name"] = delta.name
                call["arguments"] += delta.arguments_delta
            model = chunk.model or model
            finish_reason = chunk.finish_reason or finish_reason
            usage = chunk.usage or usage

        tool_calls = [
            ProviderToolCall(
                id=call["id"] or f"call-{index}",
                name=call["name"],
                arguments=self._parse_stream_arguments(call["arguments"]),
            )
            for index, call in sorted(calls.items())
        ]
        return ProviderResponse(
            content="".join(content_parts) or None,
            reasoning_summary="".join(reasoning_parts),
            tool_calls=tool_calls,
            model=model,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_stream_arguments(arguments: str) -> dict[str, Any]:
        try:
            value = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Provider 返回了无效的流式 tool arguments") from exc
        if not isinstance(value, dict):
            raise ValueError("流式 tool arguments 必须是 JSON object")
        return value

    def _to_decision(
        self,
        context: AgentContext,
        response: ProviderResponse,
    ) -> AgentDecision:
        if response.tool_calls:
            control_calls = [
                call
                for call in response.tool_calls
                if call.name
                in {self.ASK_USER_TOOL, self.COMPLETE_TOOL}
            ]
            if control_calls:
                if len(response.tool_calls) != 1:
                    raise ValueError(
                        "控制决策不能与其他 tool call 混合"
                    )
                return self._to_control_decision(
                    response,
                    control_calls[0],
                )
            return AgentDecision(
                decision_kind=DecisionKind.ACTIONS,
                reasoning_summary=response.reasoning_summary,
                action_batch=ActionBatch(
                    actions=[
                        ActionRequest(
                            action_type=self.action_types.get(
                                tool_call.name,
                                ActionType.TOOL,
                            ),
                            name=tool_call.name,
                            arguments=tool_call.arguments,
                            action_id=tool_call.id,
                            risk=(
                                self.action_risk_resolver(
                                    tool_call.name,
                                    tool_call.arguments,
                                    context,
                                )
                                if self.action_risk_resolver
                                else self.action_risks.get(
                                    tool_call.name,
                                    ActionRisk.READ_ONLY,
                                )
                            ),
                            require_human_approval=(
                                tool_call.name in self.approval_required
                            ),
                        )
                        for tool_call in response.tool_calls
                    ]
                ),
                metadata={
                    "model": response.model,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                },
            )

        answer = response.content or ""
        return AgentDecision(
            decision_kind=DecisionKind.COMPLETE,
            reasoning_summary=response.reasoning_summary,
            final_answer=answer,
            metadata={
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
            },
        )

    def _to_control_decision(
        self,
        response: ProviderResponse,
        tool_call: ProviderToolCall,
    ) -> AgentDecision:
        metadata = {
            "model": response.model,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "provider_tool_call_id": tool_call.id,
        }
        if tool_call.name == self.ASK_USER_TOOL:
            question = tool_call.arguments.get("question")
            if not isinstance(question, str):
                raise ValueError("ASK_USER question 必须是字符串")
            return AgentDecision(
                decision_kind=DecisionKind.ASK_USER,
                reasoning_summary=response.reasoning_summary,
                user_input_request=UserInputRequest(
                    question=question,
                ),
                metadata=metadata,
            )

        summary = tool_call.arguments.get("summary")
        if not isinstance(summary, str):
            raise ValueError("COMPLETE summary 必须是字符串")
        final_answer = tool_call.arguments.get("final_answer", summary)
        if not isinstance(final_answer, str):
            raise ValueError("COMPLETE final_answer 必须是字符串")
        criterion_evidence = tool_call.arguments.get(
            "criterion_evidence",
            {},
        )
        if not isinstance(criterion_evidence, dict):
            raise ValueError(
                "COMPLETE criterion_evidence 必须是 object"
            )
        return AgentDecision(
            decision_kind=DecisionKind.COMPLETE,
            reasoning_summary=response.reasoning_summary,
            final_answer=final_answer,
            completion_claim=CompletionClaim(
                summary=summary,
                criterion_evidence=criterion_evidence,
            ),
            metadata=metadata,
        )

    @classmethod
    def _control_tool_schemas(cls) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": cls.ASK_USER_TOOL,
                    "description": (
                        "Ask the user for information required to "
                        "continue the current task."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": cls.COMPLETE_TOOL,
                    "description": (
                        "Propose task completion with evidence for "
                        "each acceptance criterion."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "final_answer": {"type": "string"},
                            "criterion_evidence": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                        "required": [
                            "summary",
                            "criterion_evidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        ]
