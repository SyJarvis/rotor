import { defineConfig } from 'vitepress'
import { fileURLToPath } from 'node:url'
import { base, cleanUrls } from '../base.mjs'
import style from '../.vitepress/style.config.mjs'
import { sidebar } from '../sidebar.mjs'

// Only the reader-facing Markdown tree is built; the repository keeps its
// planning notes and scripts outside this directory.
const navigation = sidebar.map((group) => (
  group.text === '原理与架构'
    ? { text: group.text, collapsed: false, items: group.items }
    : group
))

export default defineConfig({
  extends: style,
  // `/` for local preview, `/<repository>/` for GitHub Pages; see base.mjs.
  base,
  lang: 'zh-CN',
  title: 'Rotor',
  description: '自托管 LLM API 网关的安装、配置与运维文档',
  srcDir: '../gitbook',
  outDir: './dist',
  cacheDir: './.cache',
  // GitHub Pages has no extensionless URL rewriting, so a deployed build keeps
  // the .html suffix; the local preview keeps the shorter URLs.
  cleanUrls,
  appearance: false,
  // SUMMARY.md is the navigation source, not a reader-facing page.
  srcExclude: ['SUMMARY.md'],
  rewrites: {
    'README.md': 'index.md'
  },
  vite: {
    // Static assets live next to the configuration, not in gitbook/, so point
    // Vite at site/public explicitly; otherwise they are dropped from the build.
    publicDir: fileURLToPath(new URL('../public', import.meta.url)),
    resolve: {
      alias: {
        // Markdown lives one level above the site, outside Vite's default
        // dependency lookup root, so Vue must be resolved explicitly.
        vue: fileURLToPath(new URL('../node_modules/vue', import.meta.url))
      }
    }
  },
  head: [
    // Head links are emitted verbatim, so the base has to be added by hand.
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}favicon.svg` }]
  ],
  themeConfig: {
    nav: [
      { text: '文档', link: '/' },
      { text: 'GitHub', link: 'https://github.com/SyJarvis/rotor' }
    ],
    sidebar: navigation,
    socialLinks: [
      { icon: 'github', link: 'https://github.com/SyJarvis/rotor' }
    ],
    editLink: {
      pattern: 'https://github.com/SyJarvis/rotor/edit/main/gitbook/:path',
      text: '在 GitHub 上编辑此页'
    }
  }
})
