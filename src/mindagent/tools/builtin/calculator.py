from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition


class CalculatorTool(BaseTool):
    definition = ToolDefinition(
        name="calculator",
        description="Evaluate a basic arithmetic expression.",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression using numbers and operators.",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    _binary_operators: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _unary_operators: dict[type[ast.unaryop], Callable[[Any], Any]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    async def execute(
        self,
        arguments: dict,
        context: ToolContext,
    ) -> int | float:
        expression = arguments["expression"]
        if len(expression) > 200:
            raise ValueError("表达式过长")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError("无效的算术表达式") from exc
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError("表达式过于复杂")
        return self._evaluate(tree.body)

    @classmethod
    def _evaluate(cls, node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(
                node.value,
                (int, float),
            ):
                raise ValueError("表达式只能包含数字")
            return node.value

        if isinstance(node, ast.UnaryOp):
            operation = cls._unary_operators.get(type(node.op))
            if operation is None:
                raise ValueError("不支持的一元运算符")
            return operation(cls._evaluate(node.operand))

        if isinstance(node, ast.BinOp):
            operation = cls._binary_operators.get(type(node.op))
            if operation is None:
                raise ValueError("不支持的二元运算符")
            left = cls._evaluate(node.left)
            right = cls._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("指数绝对值不能超过 100")
            return operation(left, right)

        raise ValueError("表达式包含不允许的语法")
