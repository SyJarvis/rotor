from __future__ import annotations

import hashlib
import json
from typing import Any

from mindagent.core import (
    ActionRequest,
    ActionType,
    AgentContext,
    AgentError,
    ErrorCode,
    ErrorPhase,
    Observation,
)

from .base import ModelCapabilityRequest, ProviderResponse
from .router import ProviderRouter


class ModelActionExecutor:
    GENERATE = "model_generate"
    INSPECT_IMAGE = "model_inspect_image"
    VERIFY = "model_verify"

    def __init__(self, router: ProviderRouter):
        self.router = router

    async def close(self) -> None:
        await self.router.close()

    @classmethod
    def action_types(cls) -> dict[str, ActionType]:
        return {
            cls.GENERATE: ActionType.MODEL,
            cls.INSPECT_IMAGE: ActionType.MODEL,
            cls.VERIFY: ActionType.MODEL,
        }

    @classmethod
    def tool_schemas(cls) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": cls.GENERATE,
                    "description": (
                        "Delegate a focused text or structured-output task "
                        "to a separately routed language model."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "messages": {"type": "array"},
                            "json_mode": {"type": "boolean"},
                            "provider_name": {"type": "string"},
                            "model": {"type": "string"},
                            "min_context_tokens": {
                                "type": "integer",
                                "minimum": 0,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": cls.INSPECT_IMAGE,
                    "description": (
                        "Inspect an image and return structured visual "
                        "findings. Repeat with a region or another provider "
                        "for local review and cross-validation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_url": {"type": "string"},
                            "prompt": {"type": "string"},
                            "detail": {
                                "type": "string",
                                "enum": ["low", "high", "auto"],
                            },
                            "region": {"type": "object"},
                            "structured": {"type": "boolean"},
                            "provider_name": {"type": "string"},
                            "model": {"type": "string"},
                            "api": {
                                "type": "string",
                                "enum": [
                                    "auto",
                                    "chat_completions",
                                    "responses",
                                ],
                            },
                        },
                        "required": ["image_url", "prompt"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": cls.VERIFY,
                    "description": (
                        "Ask an independent model to verify a claim against "
                        "supplied evidence."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "evidence": {},
                            "provider_name": {"type": "string"},
                            "model": {"type": "string"},
                            "min_context_tokens": {
                                "type": "integer",
                                "minimum": 0,
                            },
                        },
                        "required": ["subject", "evidence"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    async def execute(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation:
        if action.action_type != ActionType.MODEL:
            return self._failure(
                action,
                f"ModelActionExecutor 不处理 {action.action_type.value}",
            )
        try:
            if action.name == self.GENERATE:
                result = await self._generate(action.arguments)
            elif action.name == self.INSPECT_IMAGE:
                result = await self._inspect_image(action.arguments)
            elif action.name == self.VERIFY:
                result = await self._verify(action.arguments)
            else:
                raise ValueError(f"未知模型 Action: {action.name}")
            return Observation(action=action, ok=True, result=result)
        except Exception as exc:
            return self._failure(action, str(exc), exc)

    async def _generate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        messages = arguments.get("messages")
        prompt = arguments.get("prompt")
        if messages is None:
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("model_generate 需要 prompt 或 messages")
            messages = [{"role": "user", "content": prompt}]
        if not isinstance(messages, list) or not messages:
            raise ValueError("model_generate messages 必须是非空 list")

        json_mode = bool(arguments.get("json_mode"))
        provider = self._select(arguments, json_mode=json_mode)
        request: dict[str, Any] = {}
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        if arguments.get("model") is not None:
            request["model"] = arguments["model"]
        response = await provider.chat(messages, **request)
        return self._response_result(
            provider.name,
            response,
            parse_json=json_mode,
        )

    async def _inspect_image(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        image_url = arguments.get("image_url")
        prompt = arguments.get("prompt")
        if not isinstance(image_url, str) or not image_url:
            raise ValueError("model_inspect_image 需要 image_url")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("model_inspect_image 需要 prompt")

        region = arguments.get("region")
        if region is not None:
            if not isinstance(region, dict):
                raise ValueError("region 必须是 object")
            prompt = (
                f"{prompt}\n只重点检查归一化区域 "
                f"{json.dumps(region, ensure_ascii=False)}。"
            )
        structured = arguments.get("structured", True)
        if structured:
            prompt = (
                f"{prompt}\n返回 JSON object，字段为 summary 和 findings；"
                "findings 是对象数组，每项可包含 label、description、"
                "confidence、region。不要输出 JSON 之外的文本。"
            )

        provider = self._select(arguments, vision=True)
        response = await provider.analyze_image(
            prompt=prompt,
            image_url=image_url,
            detail=arguments.get("detail"),
            api=arguments.get("api", "auto"),
            model=arguments.get("model"),
        )
        result = self._response_result(
            provider.name,
            response,
            parse_json=bool(structured),
        )
        result["image_url"] = image_url
        result["region"] = region
        result["artifact_refs"] = [
            {
                "artifact_id": (
                    "image:"
                    + hashlib.sha256(
                        image_url.encode("utf-8")
                    ).hexdigest()
                ),
                "uri": image_url,
                "media_type": "image/*",
                "metadata": {"region": region} if region else {},
            }
        ]
        if structured:
            findings = result["structured"].get("findings")
            if not isinstance(findings, list):
                raise ValueError("视觉结果 findings 必须是 list")
        return result

    async def _verify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject = arguments.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("model_verify 需要 subject")
        prompt = (
            "独立验证以下主张。仅返回 JSON object："
            '{"accepted": boolean, "reasons": string[], '
            '"confidence": number}。\n'
            f"主张：{subject}\n证据："
            f"{json.dumps(arguments.get('evidence'), ensure_ascii=False, default=str)}"
        )
        provider = self._select(arguments, json_mode=True)
        request = {"response_format": {"type": "json_object"}}
        if arguments.get("model") is not None:
            request["model"] = arguments["model"]
        response = await provider.chat(
            [{"role": "user", "content": prompt}],
            **request,
        )
        result = self._response_result(
            provider.name,
            response,
            parse_json=True,
        )
        parsed = result["structured"]
        if not isinstance(parsed.get("accepted"), bool):
            raise ValueError("验证结果 accepted 必须是 boolean")
        reasons = parsed.get("reasons")
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) for reason in reasons
        ):
            raise ValueError("验证结果 reasons 必须是 string list")
        result["verified"] = parsed["accepted"]
        return result

    def _select(
        self,
        arguments: dict[str, Any],
        *,
        vision: bool = False,
        json_mode: bool = False,
    ):
        min_context_tokens = arguments.get("min_context_tokens", 0)
        if not isinstance(min_context_tokens, int):
            raise ValueError("min_context_tokens 必须是整数")
        return self.router.select(
            provider_name=arguments.get("provider_name"),
            capabilities=ModelCapabilityRequest(
                vision=vision,
                json_mode=json_mode,
                min_context_tokens=min_context_tokens,
            ),
        )

    @staticmethod
    def _response_result(
        provider_name: str,
        response: ProviderResponse,
        *,
        parse_json: bool,
    ) -> dict[str, Any]:
        content = response.content or ""
        if not content.strip():
            raise ValueError("模型未返回内容")
        result: dict[str, Any] = {
            "content": content,
            "provider": provider_name,
            "model": response.model,
            "usage": response.usage,
        }
        if parse_json:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError("模型未返回有效 JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("结构化模型输出必须是 JSON object")
            result["structured"] = parsed
        return result

    @staticmethod
    def _failure(
        action: ActionRequest,
        message: str,
        exc: Exception | None = None,
    ) -> Observation:
        error = AgentError(
            code=ErrorCode.ACTION_EXECUTION_FAILED,
            message=message,
            phase=ErrorPhase.ACTION_EXECUTION,
            recoverable=True,
            details={
                "action_name": action.name,
                "exception_type": type(exc).__name__ if exc else None,
            },
        )
        return Observation(
            action=action,
            ok=False,
            error=message,
            error_info=error,
        )
