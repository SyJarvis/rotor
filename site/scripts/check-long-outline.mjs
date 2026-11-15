import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { pageUrl } from './site-url.mjs'

const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const PORT = 9338
const url = process.argv[2] || pageUrl('/concepts/protocol-formats')
const width = Number(process.argv[3] || 1440)
const height = Number(process.argv[4] || 900)

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, '--disable-gpu',
  '--no-first-run', `--window-size=${width},${height}`,
  '--user-data-dir=/tmp/rotor-chrome-profile6', 'about:blank'
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

const socket = new WebSocket(await endpoint())
await new Promise((resolve) => socket.addEventListener('open', resolve))
let id = 0
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
})
const send = (method, params = {}, sessionId) => {
  id += 1
  const messageId = id
  return new Promise((resolve) => { pending.set(messageId, resolve); socket.send(JSON.stringify({ id: messageId, method, params, sessionId })) })
}

const { result: target } = await send('Target.createTarget', { url: 'about:blank' })
const { result: attached } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true })
const session = attached.sessionId
await send('Page.enable', {}, session)
await send('Runtime.enable', {}, session)
await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 768 }, session)
await send('Page.navigate', { url }, session)
await sleep(2200)

const evaluate = async (expression) => {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, session)
  if (response.result?.exceptionDetails) return { exception: response.result.exceptionDetails.text }
  return response.result?.result?.value
}

console.log(JSON.stringify(await evaluate(`(() => {
  const outline = document.querySelector('.VPDocAsideOutline .content')
  const aside = document.querySelector('.VPDocAside')
  const asideContainer = document.querySelector('.VPDoc .aside-container')
  const links = [...document.querySelectorAll('.VPDocAsideOutline .outline-link')]
  const narrowButton = document.querySelector('.page-outline-menu > summary')
  const narrowCopy = document.querySelector('.page-outline-panel .copy-page')
  return {
    viewport: { width: innerWidth, height: innerHeight },
    outlineLinks: links.length,
    outlineScroll: outline ? {
      scrollHeight: outline.scrollHeight, clientHeight: outline.clientHeight,
      overflowY: getComputedStyle(outline).overflowY,
      scrollbarWidth: getComputedStyle(outline).scrollbarWidth,
      canScroll: outline.scrollHeight > outline.clientHeight
    } : null,
    asideSticky: aside ? getComputedStyle(aside).position : null,
    asideContainer: asideContainer ? {
      position: getComputedStyle(asideContainer).position,
      overflowY: getComputedStyle(asideContainer).overflowY,
      scrollHeight: asideContainer.scrollHeight,
      clientHeight: asideContainer.clientHeight,
      canScroll: asideContainer.scrollHeight > asideContainer.clientHeight
    } : null,
    narrowOutlineButton: narrowButton ? { visible: narrowButton.getBoundingClientRect().width > 0, label: narrowButton.getAttribute('aria-label') } : null,
    narrowCopyExists: Boolean(narrowCopy),
    contentWidth: Math.round(document.querySelector('.VPDoc .content-container')?.getBoundingClientRect().width || 0)
  }
})()`), null, 2))

// Narrow layout: open the outline menu, verify links then the copy button.
if (width < 768) {
  console.log('narrow menu:', JSON.stringify(await evaluate(`(async () => {
    const details = document.querySelector('.page-outline-menu')
    details.open = true
    await new Promise((resolve) => setTimeout(resolve, 300))
    const panel = document.querySelector('.page-outline-panel')
    const links = [...panel.querySelectorAll('.outline-link')]
    const copy = panel.querySelector('.copy-page')
    const last = links.at(-1)
    return {
      open: details.open,
      links: links.length,
      hasScrollablePanel: panel.scrollHeight > panel.clientHeight,
      copyAfterLastLink: copy.getBoundingClientRect().top >= last.getBoundingClientRect().bottom,
      copyLabel: copy.textContent.trim()
    }
  })()`)))

  // Escape closes the menu and returns focus to the summary.
  console.log('escape:', JSON.stringify(await evaluate(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 200))
    return { open: document.querySelector('.page-outline-menu').open, focused: document.activeElement?.tagName }
  })()`)))
}

await send('Target.closeTarget', { targetId: target.targetId })
socket.close()
chrome.kill()
