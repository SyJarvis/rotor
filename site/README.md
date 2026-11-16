# 文档站

`gitbook/` 是文档正文与导航的唯一来源，`site/` 只负责把它渲染成网页。修改内容时只需
编辑 `gitbook/` 下的 Markdown，不需要同步第二份副本。

## 目录

```text
gitbook/            # Markdown 正文、SUMMARY.md 导航、.gitbook.yaml
site/
├── .vitepress/
│   ├── config.mjs  # 站点配置：标题、路由、主题、内容范围
│   ├── style.config.mjs
│   └── theme/      # 黑白主题、页内目录与“复制页面”组件
├── base.mjs        # 按 DOCS_BASE 推导 base 与 cleanUrls
├── public/         # 站点静态资源，例如 favicon.svg
├── sidebar.mjs     # 从 gitbook/SUMMARY.md 生成侧栏
├── scripts/        # 构建产物校验脚本
└── package.json
```

`site/.vitepress/config.mjs` 把 `gitbook/` 设为 `srcDir`，因此正文仍留在原位置；
`README.md` 通过 `rewrites` 成为首页，`SUMMARY.md` 由 `srcExclude` 排除，只作为导航源。

## 开发预览

```bash
cd site
npm install
npm run dev        # http://localhost:5173
```

## 构建与预览

```bash
cd site
npm run build      # 产物写入 site/dist
npm run preview    # 本地预览产物，http://localhost:4173
```

构建需要 Node.js 18 或更高版本。

## 校验

```bash
cd site
node scripts/verify-raw.mjs     # 每页“复制页面”的内容与源 Markdown 是否逐字一致
node scripts/verify-links.mjs   # 站内链接与锚点是否有效
```

需要浏览器级检查时，先运行 `npm run preview`，再执行：

```bash
node scripts/check-rendered.mjs http://localhost:4173/reference/control-api 1440
node scripts/check-long-outline.mjs http://localhost:4173/concepts/protocol-formats 1440 900
node scripts/check-copy.mjs
```

这些脚本通过本地 Chrome 的 DevTools 协议读取渲染结果，输出字号、缩进、滚动范围与
剪贴板内容，不会修改文档。Chrome 不在默认位置时用 `CHROME_PATH` 指定。

## 部署

### GitHub Pages

`.github/workflows/docs.yml` 在 `main` 有 `gitbook/` 或 `site/` 改动时构建站点并发布到
GitHub Pages。仓库需在 Settings → Pages → Build and deployment 把 Source 设为
**GitHub Actions**，只需设置一次；之后推送即自动部署，也可以在 Actions 里手动触发
`docs` 工作流。

项目站点从 `https://<用户>.github.io/<仓库>/` 访问，因此工作流用
`DOCS_BASE=/${仓库名}/` 构建：

- `base` 给所有资源与站内链接加上仓库前缀；
- 非根 `base` 时关闭 `cleanUrls`，链接保留 `.html` 后缀。GitHub Pages 是纯静态托管，
  不会把 `/guides/chat` 重写成 `/guides/chat.html`。

本地构建默认 `base` 为 `/`、保留 `cleanUrls`，两者互不影响。要让本地构建完全模拟部署
产物，用同一个环境变量，并注意 `.html` 后缀：

```bash
DOCS_BASE=/rotor/ npm run build
DOCS_BASE=/rotor/ node scripts/verify-raw.mjs
DOCS_BASE=/rotor/ node scripts/verify-links.mjs
SITE_URL=http://localhost:4173 DOCS_BASE=/rotor/ node scripts/check-copy.mjs
npm run preview    # 以 /rotor/ 前缀提供产物
```

### 其它静态托管

`npm run build` 产出的 `site/dist` 是纯静态文件，可直接交给任意静态托管。部署到域名根
路径时不设 `DOCS_BASE`；部署到子路径时把 `DOCS_BASE` 设为该前缀，并确保托管方不会依赖
无扩展名重写。


## 新增页面

1. 在 `gitbook/` 下按现有分组创建 Markdown 文件。
2. 在 `gitbook/SUMMARY.md` 的对应分组添加链接；侧栏会随之更新。
3. 重新构建并运行两个校验脚本。

`SUMMARY.md` 中位于任何分组之前的第一条是“概览”入口，其余条目按 `##` 分组渲染。
“原理与架构”分组在站点配置里设为默认展开且可折叠。
