import { h } from 'vue'
import DefaultTheme from 'vitepress/theme'
import CopyPage from './CopyPage.vue'
import './style.css'

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout, null, {
    'aside-outline-after': () => h(CopyPage)
  })
}
