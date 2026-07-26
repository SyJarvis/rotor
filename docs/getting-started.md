# 安装与运行

## 依赖

- Python ≥ 3.11
- 推荐 conda 环境(本项目使用 `toolchains` env)

## 安装

```bash
conda activate toolchains
pip install -e .
```

## 启动服务

```bash
rotor serve --host 0.0.0.0 --port 8000
```

打开管理页:

```text
http://localhost:8000
```

## 数据存放位置

默认在 `~/.cache/rotor`:

```text
~/.cache/rotor/
  rotor.db             # 渠道 / Token / 日志 / 用量
  conversations/       # 会话内容
```

## 环境变量

```env
# 可选。默认 sqlite+aiosqlite:///~/.cache/rotor/rotor.db
DATABASE_URL=

# 可选。默认 ~/.cache/rotor/conversations
CONVERSATION_STORE_DIR=
```

---

下一步 → [渠道配置](./channels.md)
