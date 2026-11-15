# 使用网页对话

登录 Rotor 后，顶部切换到 “Chat”。Chat 是用当前账户直接调用网关的
对话框，不需要创建或填写 API Key；请求仍经过渠道路由、用量记账和请求日志。

## 开始一段对话

1. 打开 `http://127.0.0.1:8000/` 并登录。
2. 切换到“Chat”。
3. 在右侧面板选择模型；模型列表来自当前账户有权限访问的启用渠道。
4. 输入内容并发送。

每段对话独立保存右侧面板中的模型、API 格式、System Prompt、temperature 和
max_tokens。temperature 与 max_tokens 留空时不向上游发送对应参数，使用模型默认值。
System Prompt 最多 100 000 字符。

模型调用默认使用 Chat Completions；把 API 格式切换为 Responses 后，请求改走
`/v1/responses`。两种格式都沿用同一套渠道路由和记账。

## 连接测试

“连接测试”按当前面板参数发起一次简短的真实文本生成请求，因此可能产生上游费用。
它验证的是所选模型能否正常生成文本，不验证图片或文档理解能力。

## 附件

对话支持以下内容：

| 类型 | 支持格式 | 限制 |
| --- | --- | --- |
| 图片 | PNG、JPEG、WebP、GIF | 单文件 5 MB |
| 文本文档 | UTF-8 文本与常见代码/配置文件 | 单文件 5 MB，最多 500 000 字节 |
| PDF | 文本型 PDF | 最多 100 页，提取文本最多 500 000 字符 |
| DOCX | 文本型 DOCX | 提取文本最多 500 000 字符 |

每条消息最多 6 个附件、合计不超过 10 MB，整段对话的附件总量不超过 20 MB。

PDF 与 DOCX 由后端提取文字，不保留图片与表格布局，也不执行 OCR。扫描版 PDF 需要
先转成图片再上传。图片能否被理解取决于所选模型是否具备视觉能力。

## 保存、导出与删除

对话、设置和草稿保存在服务器 `CHAT_STORE_DIR` 下按用户 ID 隔离的目录中：

```text
$CHAT_STORE_DIR/<user_id>/index.json
$CHAT_STORE_DIR/<user_id>/<conversation_id>/conversation.json
$CHAT_STORE_DIR/<user_id>/<conversation_id>/attachments/
```

同一账户在其他浏览器登录后可以读取已保存的内容。历史列表每项的“…”菜单可以导出
当前对话的 JSON（含可读取的附件内容），或删除会话；删除会同时移除该会话的附件。

单段对话最多 100 条消息。达到上限后需要新建对话。

## 与 API Key、OAuth 的关系

网页对话、账户名下的 API Key 和客户端 OAuth 请求共同累计账户已用额度。API Key
请求另外受该 Key 自身的权限和额度限制；创建、切换或停用 Key 不会影响网页对话。

旧版本的浏览器本地对话不会迁移，也不会从 `CONVERSATION_STORE_DIR` 中的网关请求
记录恢复聊天历史。这两套服务器存储相互独立。

网页对话接口不是稳定的外部 API，自动化调用请使用[客户端 API](../reference/client-api.md)。

