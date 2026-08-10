# 动态引用常见模式

清理死代码时需要警惕的动态引用模式。这些模式下 grep 显示无引用，
但运行时实际会被使用。

## 工厂注册

```python
# providers/factory.py
_REGISTRY: dict[str, type[Provider]] = {}

def register(name: str, cls: type[Provider]) -> None:
    _REGISTRY[name] = cls

# providers/openai/provider.py
factory.register("openai", OpenAIProvider)  # ← grep "OpenAIProvider" 只找到这里和定义
```

如果只 grep `OpenAIProvider` 看起来只有一个引用，但它通过工厂被动态调用。

## 字符串到函数映射

```python
_TOOL_BUILDERS = {
    "file_tools": create_file_tools,
    "shell_tools": create_shell_tools,
}
```

`create_file_tools` 看起来只出现在定义和这个字典里，但通过
`_TOOL_BUILDERS[name]()` 被动态调用。

## getattr / setattr

```python
method = getattr(obj, f"handle_{event_type}")
method()
```

`handle_xxx` 不会出现在 grep 结果中作为"调用"。

## 配置驱动的类加载

```python
# config 中只有字符串 "openai"
provider_type = config.provider_type  # "openai"
# 运行时通过字符串查找类
cls = _REGISTRY[provider_type]
```

## Pydantic / Dataclass 序列化

```python
class MyModel(BaseModel):
    field_a: str
    field_b: int  # grep 无引用，但 JSON 反序列化需要
```

字段可能在 JSON 数据中被使用，不在 Python 代码中直接引用。

## 识别方法

遇到以下模式时提高警惕：

1. 类出现在 dict/list 映射中 → 可能是注册模式
2. 类名出现在 `register()` / `factory` / `_REGISTRY` 附近
3. 函数出现在回调参数位置（如 `on_event=func`）
4. 装饰器注册（`@router.get`, `@plugin.register`）
5. `__all__` 中的显式导出
