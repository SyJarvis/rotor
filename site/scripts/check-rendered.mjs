// Rendered checks for the documentation site using a headless Chrome DevTools
// session. Reports layout numbers and interaction results as text so they can
// be reviewed without an image viewer.
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { pageUrl } from './site-url.mjs'

const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const PORT = 9333
const url = process.argv[2] || pageUrl('/reference/control-api')
const width = Number(process.argv[3] || 1440)

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  '--disable-gpu',
  '--no-first-run',
  `--window-size=${width},1000`,
  '--user-data-dir=/tmp/rotor-chrome-profile',
  'about:blank'
], { stdio: 'ignore' })

async function endpoint() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (response.ok) return (await response.json()).webSocketDebuggerUrl
    } catch {}
    await sleep(200)
  }
  throw new Error('Chrome did not start')
}

const socketUrl = await endpoint()
const socket = new WebSocket(socketUrl)
await new Promise((resolve) => socket.addEventListener('open', resolve))

let id = 0
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message)
    pending.delete(message.id)
  }
})

function send(method, params = {}, sessionId) {
  id += 1
  const messageId = id
  return new Promise((resolve) => {
    pending.set(messageId, resolve)
    socket.send(JSON.stringify({ id: messageId, method, params, sessionId }))
  })
}

const { result: target } = await send('Target.createTarget', { url: 'about:blank' })
const { result: attached } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true })
const session = attached.sessionId

await send('Page.enable', {}, session)
await send('Runtime.enable', {}, session)
await send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: width < 768 }, session)
await send('Page.navigate', { url }, session)
await sleep(2500)

async function evaluate(expression) {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, session)
  if (response.result?.exceptionDetails) throw new Error(response.result.exceptionDetails.text)
  return response.result?.result?.value
}

const report = await evaluate(`(() => {
  const style = (selector) => {
    const node = document.querySelector(selector)
    if (!node) return null
    const computed = getComputedStyle(node)
    const box = node.getBoundingClientRect()
    return {
      fontSize: computed.fontSize,
      fontWeight: computed.fontWeight,
      color: computed.color,
      background: computed.backgroundColor,
      paddingLeft: computed.paddingLeft,
      width: Math.round(box.width),
      visible: box.width > 0 && box.height > 0
    }
  }
  const sidebar = document.querySelector('.VPSidebar')
  const navItems = [...document.querySelectorAll('.VPSidebarItem.level-0')].map((node) => ({
    text: node.querySelector('.text')?.textContent?.trim(),
    collapsible: node.classList.contains('collapsible'),
    expanded: !node.querySelector('.items')?.classList?.contains('collapsed'),
    subItems: node.querySelectorAll('.VPSidebarItem.level-1').length
  }))
  const outline = [...document.querySelectorAll('.VPDocAsideOutline .outline-link')].map((node) => node.textContent.trim())
  // The narrow-window panel renders its own hidden button, so pick the visible one.
  const copy = [...document.querySelectorAll('.copy-page')].find((node) => node.getBoundingClientRect().height > 0)
  const outlineNodes = [...document.querySelectorAll('.VPDocAsideOutline .outline-link')]
  const lastOutline = outlineNodes.at(-1)
  const copyAfterOutline = copy && lastOutline
    ? copy.getBoundingClientRect().top >= lastOutline.getBoundingClientRect().bottom
    : null
  return {
    lang: document.documentElement.lang,
    title: document.title,
    h1: style('.vp-doc h1'),
    h2: style('.vp-doc h2'),
    body: style('.vp-doc p'),
    sidebar: style('.VPSidebar'),
    sidebarScrollbar: sidebar ? { overflowY: getComputedStyle(sidebar).overflowY, scrollHeight: sidebar.scrollHeight, clientHeight: sidebar.clientHeight } : null,
    activeNav: style('.VPSidebarItem.is-active > .item'),
    navItems,
    outlineCount: outline.length,
    outlineSample: outline.slice(0, 6),
    outlineIndentLevel2: (() => {
      const nested = document.querySelector('.VPDocAsideOutline .VPDocOutlineItem.nested')
      return nested ? getComputedStyle(nested).paddingLeft : null
    })(),
    outlineTitleHidden: (() => {
      const title = document.querySelector('.VPDocAsideOutline .outline-title')
      if (!title) return null
      const box = title.getBoundingClientRect()
      return { width: Math.round(box.width), text: title.textContent.trim() }
    })(),
    copyButton: copy ? { text: copy.textContent.trim(), top: Math.round(copy.getBoundingClientRect().top), afterOutline: copyAfterOutline } : null,
    contentWidth: Math.round(document.querySelector('.VPDoc .content-container')?.getBoundingClientRect().width || 0),
    hasRawMarkdown: Boolean(window.__VP_RAW__),
    tables: document.querySelectorAll('.vp-doc table').length,
    codeBlocks: document.querySelectorAll('.vp-doc div[class*="language-"]').length
  }
})()`)

console.log(JSON.stringify(report, null, 2))

if (process.argv.includes('--copy')) {
  const copied = await evaluate(`(async () => {
    const button = [...document.querySelectorAll('.copy-page')].find((node) => node.getBoundingClientRect().height > 0)
    button.click()
    await new Promise((resolve) => setTimeout(resolve, 300))
    return { label: button.textContent.trim() }
  })()`)
  console.log('copy label after click:', JSON.stringify(copied))
}

await send('Target.closeTarget', { targetId: target.targetId })
socket.close()
chrome.kill()
