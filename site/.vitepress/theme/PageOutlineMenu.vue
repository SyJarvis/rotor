<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { onContentUpdated } from 'vitepress'
import VPDocOutlineItem from 'vitepress/dist/client/theme-default/components/VPDocOutlineItem.vue'
import CopyPage from './CopyPage.vue'

defineProps({ headers: { type: Array, default: () => [] }, navHeight: Number })
const menu = ref()

function close() {
  if (menu.value) menu.value.open = false
}

function onOutsideClick(event) {
  if (!menu.value?.contains(event.target)) close()
}

function onEscape(event) {
  if (event.key === 'Escape' && menu.value?.open) {
    close()
    menu.value.querySelector('summary').focus()
  }
}

onContentUpdated(close)
onMounted(() => {
  document.addEventListener('pointerdown', onOutsideClick)
  document.addEventListener('keydown', onEscape)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', onOutsideClick)
  document.removeEventListener('keydown', onEscape)
})
</script>

<template>
  <details ref="menu" class="page-outline-menu">
    <summary aria-label="页面大纲">
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
        <path d="M4 6h16M8 12h12M8 18h12" />
      </svg>
    </summary>
    <div class="page-outline-panel">
      <nav v-if="headers.length" aria-label="页面大纲" @click="event => event.target.closest('a') && close()">
        <VPDocOutlineItem :headers="headers" root />
      </nav>
      <CopyPage />
    </div>
  </details>
</template>
