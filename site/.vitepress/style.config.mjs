import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  appearance: false,
  vite: {
    resolve: {
      alias: [{
        find: /^.*\/VPLocalNavOutlineDropdown\.vue$/,
        replacement: fileURLToPath(new URL('./theme/PageOutlineMenu.vue', import.meta.url))
      }]
    }
  },
  transformPageData(page, { siteConfig }) {
    if (page.filePath) {
      return { rawMarkdown: readFileSync(resolve(siteConfig.srcDir, page.filePath), 'utf8') }
    }
  },
  themeConfig: {
    outline: { level: [2, 3], label: '页面大纲' },
    docFooter: { prev: '上一篇', next: '下一篇' },
    sidebarMenuLabel: '目录',
    returnToTopLabel: '回到顶部',
    skipToContentLabel: '跳转到正文',
    search: {
      provider: 'local',
      options: {
        miniSearch: {
          options: {
            tokenize: (text) => Array.from(new Intl.Segmenter('zh-CN', { granularity: 'word' }).segment(text), item => item.segment).filter(word => /[\p{L}\p{N}]/u.test(word))
          }
        },
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            displayDetails: '显示详细列表', resetButtonTitle: '清空搜索', backButtonTitle: '关闭搜索', noResultsText: '没有找到相关内容',
            footer: { selectText: '选择', selectKeyAriaLabel: '回车', navigateText: '切换', navigateUpKeyAriaLabel: '上箭头', navigateDownKeyAriaLabel: '下箭头', closeText: '关闭', closeKeyAriaLabel: 'Esc' }
          }
        }
      }
    }
  }
})
