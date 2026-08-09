from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .context import AgentContext
from .orchestrator import AgentOrchestrator, AgentTaskResult


@dataclass(frozen=True)
class PipelineStep:
    step_id: str
    agent_id: str
    task: str
    depends_on: tuple[str, ...] = ()
    input_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("pipeline step_id 不能为空")
        if not self.agent_id.strip():
            raise ValueError("pipeline agent_id 不能为空")
        if not self.task.strip():
            raise ValueError("pipeline task 不能为空")


@dataclass(frozen=True)
class PipelineResult:
    results: dict[str, AgentTaskResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            step_id: result.to_dict()
            for step_id, result in self.results.items()
        }


class AgentPipeline:
    def __init__(self, steps: list[PipelineStep] | None = None) -> None:
        self._steps: dict[str, PipelineStep] = {}
        for step in steps or []:
            self.add_step(step)

    def add(
        self,
        step_id: str,
        *,
        agent_id: str,
        task: str,
        depends_on: tuple[str, ...] | list[str] = (),
        input_context: dict[str, Any] | None = None,
    ) -> None:
        self.add_step(
            PipelineStep(
                step_id=step_id,
                agent_id=agent_id,
                task=task,
                depends_on=tuple(depends_on),
                input_context=input_context or {},
            )
        )

    def add_step(self, step: PipelineStep) -> None:
        if step.step_id in self._steps:
            raise ValueError(f"pipeline step 已存在: {step.step_id}")
        self._steps[step.step_id] = step

    async def run(
        self,
        orchestrator: AgentOrchestrator,
        *,
        parent_context: AgentContext | None = None,
        parent_task_id: str | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> PipelineResult:
        results: dict[str, AgentTaskResult] = {}
        step_task_ids: dict[str, str] = {}

        for layer in self._topological_layers():
            layer_results = await asyncio.gather(
                *[
                    orchestrator.run_agent(
                        step.agent_id,
                        step.task,
                        input_context={
                            **(input_context or {}),
                            **step.input_context,
                            "pipeline_step_id": step.step_id,
                            "dependencies": {
                                dependency: results[
                                    dependency
                                ].to_dict()
                                for dependency in step.depends_on
                            },
                        },
                        parent_context=parent_context,
                        parent_task_id=parent_task_id,
                        metadata={
                            "pipeline_step_id": step.step_id,
                            "depends_on_step_ids": list(step.depends_on),
                            "depends_on_task_ids": [
                                step_task_ids[dependency]
                                for dependency in step.depends_on
                            ],
                        },
                    )
                    for step in layer
                ]
            )
            for step, result in zip(layer, layer_results):
                results[step.step_id] = result
                step_task_ids[step.step_id] = result.task_id

        return PipelineResult(results=results)

    def _topological_layers(self) -> list[list[PipelineStep]]:
        self._validate_dependencies()
        pending = set(self._steps)
        completed: set[str] = set()
        layers: list[list[PipelineStep]] = []

        while pending:
            ready = sorted(
                step_id
                for step_id in pending
                if set(self._steps[step_id].depends_on) <= completed
            )
            if not ready:
                raise ValueError("pipeline 依赖存在环")
            layers.append([self._steps[step_id] for step_id in ready])
            pending.difference_update(ready)
            completed.update(ready)

        return layers

    def _validate_dependencies(self) -> None:
        missing: dict[str, list[str]] = {}
        for step in self._steps.values():
            absent = [
                dependency
                for dependency in step.depends_on
                if dependency not in self._steps
            ]
            if absent:
                missing[step.step_id] = absent
        if missing:
            raise ValueError(f"pipeline 包含未知依赖: {missing}")
