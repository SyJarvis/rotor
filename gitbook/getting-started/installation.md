# 安装与启动

本页使用项目提供的 `rotor` 命令，在本机启动一个默认使用 SQLite 的 Rotor 实例。

## 前置条件

- Python 3.11 或更高版本；
- 一个可访问的上游模型服务及其 API Key。

## 安装

在仓库根目录执行：

```bash
python -m pip install -e .
```

如果要运行 Python SDK 示例，同时安装示例依赖：

```bash
python -m pip install -e ".[examples]"
```

## 启动

```bash
rotor serve --host 127.0.0.1 --port 8000
```

确认服务正常：

```bash
curl http://127.0.0.1:8000/health
```

正常响应的 `status` 为 `healthy`。浏览器管理页位于：

```text
http://127.0.0.1:8000/
```

首次启动的管理员账号为 `admin`，默认密码为 `123456`。第一次登录会进入强制改密
页面，完成修改前不能访问渠道、Token、日志或设置接口。请在把服务绑定到非本机地址
之前完成此步骤。

## 默认数据位置

首次通过 CLI 启动时，Rotor 会创建：

```text
~/.cache/rotor/
├── rotor.db
├── conversations/
└── logs/
    └── YYYY-MM/
        └── YYYY-MM-DD.log
```

运行时路由设置另存为 `~/.rotor/settings.json`。可以通过环境变量更改数据库、
会话和日志目录，详见[运行时配置](../operations/configuration.md)。

下一步：[完成第一次请求](quickstart.md)。
