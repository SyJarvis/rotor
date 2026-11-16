<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useData } from 'vitepress'

const { page } = useData()
const markdown = computed(() => page.value.rawMarkdown)
const state = ref('idle')
const label = computed(() => ({ idle: '复制页面', copied: '已复制', error: '复制失败，重试' })[state.value])
let timer

function reset() {
  clearTimeout(timer)
  state.value = 'idle'
}

watch(markdown, reset)
onUnmounted(() => clearTimeout(timer))

async function copyPage() {
  reset()
  try {
    await navigator.clipboard.writeText(markdown.value)
    state.value = 'copied'
    timer = setTimeout(reset, 2000)
  } catch {
    state.value = 'error'
  }
}
</script>

<template>
  <button v-if="markdown" type="button" class="copy-page" @click="copyPage">
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <path v-if="state === 'copied'" d="m5 12 4 4L19 6" />
      <template v-else>
        <rect x="8" y="8" width="12" height="12" rx="2" />
        <path d="M16 8V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4" transform="translate(1 1)" />
      </template>
    </svg>
    <span aria-live="polite">{{ label }}</span>
  </button>
</template>
